"""Integration tests for forgotten-password recovery (EMAIL-03, -04, -05).

Same idiom as test_email_verification.py: real PostgreSQL via the app's
normal DB engine, skipped automatically when unreachable (see conftest.py's
require_database), using `with TestClient(app) as client:` -- Starlette's
TestClient runs BackgroundTasks to completion before returning, so no sleeps
or polling are needed anywhere in this file.
"""

import asyncio
import hashlib

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.helpers import mark_verified

pytestmark = pytest.mark.usefixtures("require_database")

# Mirrors app.auth.router.INVALID_OR_EXPIRED_TOKEN_MESSAGE byte-for-byte, so
# every failure assertion below pins the exact shared literal rather than a
# loose substring -- the same discipline test_email_verification.py applies
# to T-02-12.
INVALID_OR_EXPIRED_TOKEN_MESSAGE = (
    "This link is invalid, expired, or has already been used. Request a new one and try again."
)


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts from a clean slate on email_tokens/sessions/users."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM email_tokens")
            await conn.execute("DELETE FROM sessions")
            await conn.execute("DELETE FROM users")
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared async engine's pool after each test.

    See test_auth.py's identical fixture for why: each TestClient block runs
    on its own fresh event loop, and the pooled connections must not be
    reused across loops.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _signup(client: TestClient, email: str, password: str = "Correct-Horse-Battery9") -> dict:
    r = client.post("/api/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    # Login is gated on verification, so a bare signup cannot sign in.
    # These tests need a usable account as a precondition, not a
    # verification round trip -- that path is covered in
    # test_email_verification.py.
    mark_verified(email)
    return {"email": email, "password": password}


def _extract_token(message) -> str:
    """Pull the raw reset token out of a queued EmailMessage's body.

    Reads the message a real recipient would receive, not the database.
    """
    body = message.get_content()
    assert "token=" in body
    return body.split("token=", 1)[1].split()[0].strip()


async def _insert_email_token(
    user_id: str, raw_token: str, purpose: str, expires_at_sql: str
) -> None:
    """Insert an email_tokens row directly, bypassing issue_token.

    Builds fixtures issue_token cannot produce on demand: an already-expired
    token, or a token of a purpose the test controls end-to-end (the
    verify/reset crossover). `expires_at_sql` is a raw SQL expression, not a
    bound parameter -- the value is test-authored, never attacker-controlled.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        await conn.execute(
            f"""
            INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at)
            VALUES ($1, $2, $3, {expires_at_sql})
            """,
            token_hash,
            user_id,
            purpose,
        )
    finally:
        await conn.close()


async def _read_password_hash(email: str) -> str | None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            "SELECT password_hash FROM users WHERE lower(email) = lower($1)", email
        )
    finally:
        await conn.close()


# --- POST /api/auth/reset/request: enumeration parity (EMAIL-05) ----------


def test_reset_email_links_to_the_frontend_page_when_configured(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))
    monkeypatch.setattr(settings, "frontend_base_url", "http://localhost:5173")

    with TestClient(app) as client:
        _signup(client, "frontend-reset@example.com")
        calls.clear()
        r = client.post("/api/auth/reset/request", json={"email": "frontend-reset@example.com"})
        assert r.status_code == 200

    body = calls[0].get_content()
    assert "http://localhost:5173/set-new-password?token=" in body
    assert "/api/auth/reset/confirm" not in body


def test_reset_request_is_identical_for_registered_and_unregistered_addresses(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        _signup(client, "has-account@example.com")
        # Signup itself queues a verification email through the SAME mocked
        # `deliver` -- clear it so `calls` below reflects only the reset
        # flow, not a stale verification message from setup.
        calls.clear()

        registered = client.post(
            "/api/auth/reset/request", json={"email": "has-account@example.com"}
        )
        unregistered = client.post(
            "/api/auth/reset/request", json={"email": "no-account@example.com"}
        )

    assert registered.status_code == unregistered.status_code
    assert registered.json() == unregistered.json()

    # Parity is in the RESPONSE, not in the side effect: exactly one email
    # queued for the registered address, none for the unregistered one.
    assert len(calls) == 1
    assert calls[0]["To"] == "has-account@example.com"


# --- Happy path: request, non-consuming check, confirm (EMAIL-03) ---------


def test_full_reset_flow_changes_the_password_and_old_password_stops_working(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        _signup(client, "reset-flow@example.com", password="Old-Password-9x")
        # Signup itself queues a verification email through the same mocked
        # `deliver` -- clear it so `calls[0]` below is genuinely the reset
        # email, not the leftover verification message from setup.
        calls.clear()

        requested = client.post(
            "/api/auth/reset/request", json={"email": "reset-flow@example.com"}
        )
        assert requested.status_code == 200
        raw_token = _extract_token(calls[0])

        # GET check must NOT consume: assert valid, then assert it is STILL
        # usable by checking it again before ever POSTing.
        first_check = client.get(f"/api/auth/reset/confirm?token={raw_token}")
        assert first_check.status_code == 200
        second_check = client.get(f"/api/auth/reset/confirm?token={raw_token}")
        assert second_check.status_code == 200

        confirm = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "New-Password-9x"},
        )
        assert confirm.status_code == 200

        old_login = client.post(
            "/api/auth/login",
            json={"email": "reset-flow@example.com", "password": "Old-Password-9x"},
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/auth/login",
            json={"email": "reset-flow@example.com", "password": "New-Password-9x"},
        )
        assert new_login.status_code == 200

    stored = asyncio.run(_read_password_hash("reset-flow@example.com"))
    assert stored is not None
    assert stored.startswith("$argon2id$")


# --- Session revocation (EMAIL-04) -----------------------------------------


def test_completing_a_reset_revokes_a_session_captured_before_it(monkeypatch):
    """Behaviour, not row counts: a cookie from before the reset must 401 after."""
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        _signup(client, "revoke-me@example.com", password="Old-Password-9x")
        login = client.post(
            "/api/auth/login",
            json={"email": "revoke-me@example.com", "password": "Old-Password-9x"},
        )
        assert login.status_code == 200
        captured_cookie = client.cookies[settings.session_cookie_name]
        # Signup queued a verification email through the same mocked
        # `deliver` -- clear it so `calls[0]` below is the reset email.
        calls.clear()

        client.post("/api/auth/reset/request", json={"email": "revoke-me@example.com"})
        raw_token = _extract_token(calls[0])
        confirm = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "New-Password-9x"},
        )
        assert confirm.status_code == 200

    from app.db import engine

    asyncio.run(engine.dispose())

    with TestClient(app) as fresh_client:
        fresh_client.cookies.set(settings.session_cookie_name, captured_cookie)
        r = fresh_client.get("/api/me")
        assert r.status_code == 401


# --- Abuse cases -------------------------------------------------------


def test_replaying_a_reset_token_fails(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        _signup(client, "replay-me@example.com")
        # Signup queued a verification email through the same mocked
        # `deliver` -- clear it so `calls[0]` below is the reset email.
        calls.clear()
        client.post("/api/auth/reset/request", json={"email": "replay-me@example.com"})
        raw_token = _extract_token(calls[0])

        first = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "New-Password-9x"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "Another-Password-9x"},
        )
        assert second.status_code == 400
        assert second.json()["detail"] == INVALID_OR_EXPIRED_TOKEN_MESSAGE


def test_an_expired_reset_token_fails_with_the_shared_message():
    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "expired-reset@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert signup.status_code == 201
        user_id = signup.json()["id"]

        raw_token = "expired-reset-test-token"
        asyncio.run(
            _insert_email_token(user_id, raw_token, "reset", "NOW() - INTERVAL '1 hour'")
        )

        check = client.get(f"/api/auth/reset/confirm?token={raw_token}")
        assert check.status_code == 400
        assert check.text == INVALID_OR_EXPIRED_TOKEN_MESSAGE

        confirm = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "New-Password-9x"},
        )
        assert confirm.status_code == 400
        assert confirm.json()["detail"] == INVALID_OR_EXPIRED_TOKEN_MESSAGE


def test_a_verify_purpose_token_is_refused_by_reset_confirm():
    """T-02-20 crossover: a verify token must never complete a reset."""
    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "verify-crossover@example.com", "password": "Old-Password-9x"},
        )
        assert signup.status_code == 201
        user_id = signup.json()["id"]
        # Login is gated on verification; this test is about token purpose,
        # not about email.
        mark_verified("verify-crossover@example.com")

        raw_token = "verify-purpose-test-token"
        asyncio.run(
            _insert_email_token(user_id, raw_token, "verify", "NOW() + INTERVAL '1 hour'")
        )

        check = client.get(f"/api/auth/reset/confirm?token={raw_token}")
        assert check.status_code == 400
        assert check.text == INVALID_OR_EXPIRED_TOKEN_MESSAGE

        confirm = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "New-Password-9x"},
        )
        assert confirm.status_code == 400
        assert confirm.json()["detail"] == INVALID_OR_EXPIRED_TOKEN_MESSAGE

        # The old password must still work -- a wrong-purpose token must not
        # have changed anything.
        login = client.post(
            "/api/auth/login",
            json={"email": "verify-crossover@example.com", "password": "Old-Password-9x"},
        )
        assert login.status_code == 200


def test_a_policy_failing_new_password_is_rejected_and_leaves_the_old_password_working(
    monkeypatch,
):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        _signup(client, "policy-fail@example.com", password="Old-Password-9x")
        # Signup queued a verification email through the same mocked
        # `deliver` -- clear it so `calls[0]` below is the reset email.
        calls.clear()
        client.post("/api/auth/reset/request", json={"email": "policy-fail@example.com"})
        raw_token = _extract_token(calls[0])

        confirm = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "short"},
        )
        assert confirm.status_code == 400
        assert "8 characters" in confirm.json()["detail"]

        # The rejected attempt must not have touched the credential at all --
        # checked BEFORE the retry below, so this proves something about the
        # rejected attempt rather than about the (not yet performed) retry.
        still_old = client.post(
            "/api/auth/login",
            json={"email": "policy-fail@example.com", "password": "Old-Password-9x"},
        )
        assert still_old.status_code == 200

        # A rejected password must not burn the token -- a real retry with a
        # good password must still succeed.
        retry = client.post(
            "/api/auth/reset/confirm",
            json={"token": raw_token, "password": "New-Password-9x"},
        )
        assert retry.status_code == 200

        old_login = client.post(
            "/api/auth/login",
            json={"email": "policy-fail@example.com", "password": "Old-Password-9x"},
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/auth/login",
            json={"email": "policy-fail@example.com", "password": "New-Password-9x"},
        )
        assert new_login.status_code == 200
