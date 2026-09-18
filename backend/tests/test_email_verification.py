"""Integration tests for the verification email sent on signup.

Same idiom as test_auth.py: real PostgreSQL via the app's normal DB engine,
skipped automatically when unreachable (see conftest.py's require_database),
using `with TestClient(app) as client:` -- Starlette's TestClient runs
BackgroundTasks to completion before returning, so no sleeps or polling are
needed anywhere in this file.
"""

import asyncio
import hashlib
import logging

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.helpers import read_verified

pytestmark = pytest.mark.usefixtures("require_database")


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts from a clean slate on email_tokens/sessions/users.

    `email_tokens` is deleted first for explicitness, even though the FK's
    `ON DELETE CASCADE` from `users` would already cover it (see migration
    0011) -- matching test_auth.py's `clean_tables`, which orders deletes
    the same deliberate way.
    """
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


async def _fetch_email_tokens(email: str) -> list[dict]:
    """Read email_tokens rows for the account with `email`, bypassing the app."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT et.token_hash, et.purpose, et.consumed_at
            FROM email_tokens et
            JOIN users u ON u.id = et.user_id
            WHERE lower(u.email) = lower($1)
            """,
            email,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def test_signup_queues_exactly_one_verification_email(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "app.auth.router.deliver",
        lambda message: calls.append(message),
    )

    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "verify-me@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert r.status_code == 201

    assert len(calls) == 1
    message = calls[0]
    assert message["To"] == "verify-me@example.com"


def test_signup_email_links_to_the_frontend_verify_page_when_configured(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))
    monkeypatch.setattr(settings, "frontend_base_url", "http://localhost:5173")

    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "frontend-link@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert r.status_code == 201

    body = calls[0].get_content()
    assert "http://localhost:5173/verify-email?token=" in body
    assert "/api/auth/verify" not in body


def test_signup_email_body_has_link_and_a_hashed_row_exists(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "app.auth.router.deliver",
        lambda message: calls.append(message),
    )

    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "link-check@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert r.status_code == 201

    body = calls[0].get_content()
    assert "token=" in body
    raw_token = body.split("token=", 1)[1].split()[0].strip()

    rows = asyncio.run(_fetch_email_tokens("link-check@example.com"))
    assert len(rows) == 1
    assert rows[0]["purpose"] == "verify"
    assert rows[0]["consumed_at"] is None
    # The raw token from the link must be absent from the database (T-02-01):
    # only its SHA-256 digest is stored.
    assert rows[0]["token_hash"] != raw_token
    assert raw_token not in rows[0]["token_hash"]


def test_signup_returns_201_even_when_the_transport_raises(monkeypatch, caplog):
    """EMAIL-07/D-16: a broken mailer must not turn a successful signup into an error."""
    from app.auth import mailer

    monkeypatch.setattr(mailer.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(mailer.settings, "smtp_username", None)

    class _ExplodingSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", _ExplodingSMTP)

    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "bounces@example.com", "password": "Correct-Horse-Battery9"},
        )
    assert r.status_code == 201
    assert r.json()["email"] == "bounces@example.com"
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_default_configuration_attempts_no_smtp_connection(monkeypatch):
    """D-05: an unconfigured app must never open a real SMTP connection."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("smtplib.SMTP must not be constructed when smtp_host is None")

    monkeypatch.setattr("smtplib.SMTP", _fail_if_called)
    # settings.smtp_host defaults to None -- do not set it in this test.

    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "console-only@example.com", "password": "Correct-Horse-Battery9"},
        )
    assert r.status_code == 201


def test_signup_rejects_control_characters_in_email():
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={
                "email": "x@y.com\r\nBcc: evil@example.com",
                "password": "Correct-Horse-Battery9",
            },
        )
    assert r.status_code == 422


# --- GET /api/auth/verify -----------------------------------------------
#
# T-02-12 requires expired, already-used, unknown, and wrong-purpose tokens
# to be indistinguishable. Rather than trust each test's own literal, every
# failure test below asserts the exact same body against this one constant
# -- if the route ever drifted into two different failure strings, one of
# these assertions would catch it immediately.
INVALID_OR_EXPIRED_TOKEN_MESSAGE = (
    "This link is invalid, expired, or has already been used. Request a new one and try again."
)


def _extract_token(message) -> str:
    """Pull the raw verification token out of a queued EmailMessage's body.

    Reads the message a real recipient would receive, not the database --
    this is what makes these tests exercise what a user would actually
    click (see this file's plan, Task 1).
    """
    body = message.get_content()
    assert "token=" in body
    return body.split("token=", 1)[1].split()[0].strip()


async def _insert_email_token(
    user_id: str, raw_token: str, purpose: str, expires_at_sql: str
) -> None:
    """Insert an email_tokens row directly, bypassing issue_token.

    Builds fixtures `issue_token` cannot produce on demand: an
    already-expired token, or a token of a purpose the test controls
    end-to-end (the reset/verify crossover). `expires_at_sql` is a raw SQL
    expression (`NOW() - INTERVAL '1 hour'`), not a bound parameter --
    asyncpg has no interval-literal shortcut worth reaching for here, and
    the value is test-authored, never attacker-controlled.
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


def test_visiting_a_valid_link_verifies_the_account_and_me_reflects_it(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "verify-flow@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert signup.status_code == 201
        raw_token = _extract_token(calls[0])

        verify = client.get(f"/api/auth/verify?token={raw_token}")
        assert verify.status_code == 200

        login = client.post(
            "/api/auth/login",
            json={"email": "verify-flow@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert login.status_code == 200

        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["verified"] is True


def test_reusing_a_verification_link_fails_and_does_not_unverify(monkeypatch):
    calls: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: calls.append(message))

    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "reuse-me@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert signup.status_code == 201
        raw_token = _extract_token(calls[0])

        first = client.get(f"/api/auth/verify?token={raw_token}")
        assert first.status_code == 200

        # Second visit: same message as every other failure cause, and the
        # earlier successful verification must not be undone or re-error.
        second = client.get(f"/api/auth/verify?token={raw_token}")
        assert second.status_code == 400
        assert second.text == INVALID_OR_EXPIRED_TOKEN_MESSAGE

        login = client.post(
            "/api/auth/login",
            json={"email": "reuse-me@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert login.status_code == 200
        me = client.get("/api/me")
        assert me.json()["verified"] is True


def test_an_expired_token_fails_with_the_same_message():
    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "expired-link@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert signup.status_code == 201
        user_id = signup.json()["id"]

        raw_token = "expired-test-token-value"
        asyncio.run(
            _insert_email_token(user_id, raw_token, "verify", "NOW() - INTERVAL '1 hour'")
        )

        resp = client.get(f"/api/auth/verify?token={raw_token}")
        assert resp.status_code == 400
        assert resp.text == INVALID_OR_EXPIRED_TOKEN_MESSAGE


def test_an_unknown_token_fails_the_same_way():
    with TestClient(app) as client:
        resp = client.get("/api/auth/verify?token=totally-unknown-token-value")
        assert resp.status_code == 400
        assert resp.text == INVALID_OR_EXPIRED_TOKEN_MESSAGE


def test_a_reset_purpose_token_is_rejected_by_the_verify_route():
    """T-02-07/T-02-13 crossover: a reset token must never complete a verify."""
    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "crossover@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert signup.status_code == 201
        user_id = signup.json()["id"]

        raw_token = "reset-purpose-test-token"
        asyncio.run(
            _insert_email_token(user_id, raw_token, "reset", "NOW() + INTERVAL '1 hour'")
        )

        resp = client.get(f"/api/auth/verify?token={raw_token}")
        assert resp.status_code == 400
        assert resp.text == INVALID_OR_EXPIRED_TOKEN_MESSAGE

        # Read the column directly rather than through /me. Login is gated on
        # this very flag, so a signed-in observation could never see the False
        # case this test exists to prove.
        assert read_verified("crossover@example.com") is False


def test_an_unverified_account_cannot_log_in():
    """Verification gates login: correct credentials, unconfirmed address -> 403.

    Replaces an earlier test asserting the opposite. Verification was
    informational until the product decision to gate on it; the reversal is
    recorded here rather than by quietly deleting the old assertion.
    """
    with TestClient(app) as client:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "never-clicked@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert signup.status_code == 201

        login = client.post(
            "/api/auth/login",
            json={"email": "never-clicked@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert login.status_code == 403
        # The message must tell the person what to do, not just refuse.
        assert "verify" in login.json()["detail"].lower()


def test_a_wrong_password_on_an_unverified_account_is_401_not_403():
    """The gate must not become an account-enumeration oracle.

    403 is reachable ONLY after the password check passes. If an unverified
    account answered 403 to any password, the status code would tell a
    stranger "this address is registered" for free -- exactly what AUTH-09
    exists to prevent. This is the test that pins the ordering inside the
    handler; a refactor that hoists the verified check above the credential
    check fails here and nowhere else.
    """
    with TestClient(app) as client:
        client.post(
            "/api/auth/signup",
            json={"email": "unverified-wrongpw@example.com", "password": "Correct-Horse-Battery9"},
        )

        wrong = client.post(
            "/api/auth/login",
            json={"email": "unverified-wrongpw@example.com", "password": "Nope-Nope-Nope1!"},
        )
        unknown = client.post(
            "/api/auth/login",
            json={"email": "no-such-account@example.com", "password": "Nope-Nope-Nope1!"},
        )

        assert wrong.status_code == 401
        # Identical to an address that does not exist at all.
        assert wrong.json() == unknown.json()


def test_verifying_then_logging_in_succeeds(monkeypatch):
    """The whole point: clicking the link makes the account usable."""
    captured: list = []
    monkeypatch.setattr("app.auth.router.deliver", lambda message: captured.append(message))

    with TestClient(app) as client:
        client.post(
            "/api/auth/signup",
            json={"email": "verify-then-login@example.com", "password": "Correct-Horse-Battery9"},
        )
        token = _extract_token(captured[0])

        assert client.get(f"/api/auth/verify?token={token}").status_code == 200
        login = client.post(
            "/api/auth/login",
            json={"email": "verify-then-login@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert login.status_code == 200


def test_the_account_is_committed_before_the_email_is_sent():
    """Signing up must durably create the account BEFORE any mail is attempted.

    The sender runs as a FastAPI background task, and background tasks execute
    before a `yield` dependency's teardown -- which is where `get_connection`
    commits. So an unmodified handler holds the signup transaction open across
    the entire SMTP round trip, and the account does not exist for anyone else
    until the relay answers.

    Observed against a live Brevo relay: signup returned 201 in 159ms while the
    row only became visible ~1.27s later, and 8 out of 8 signup-then-immediately
    -login sequences failed with 401. The same sequence is exactly what the
    sign-up screen does, so this was a guaranteed failure in the UI, not a rare
    race. It also parks a database transaction on a third-party network call,
    which is its own problem under load.

    This asserts the invariant directly rather than by timing: the sender opens
    its OWN connection and looks for the account. If the handler has not
    committed by then, it cannot see it.
    """
    seen: list[bool] = []

    def recording_deliver(message) -> None:
        # A separate connection, so this can only see COMMITTED data -- the
        # whole point. Reusing the request's connection would see its own
        # uncommitted work and prove nothing. asyncpg + asyncio.run matches
        # conftest.py's own throwaway-connection idiom; this runs in a
        # threadpool (deliver is a plain def), so it has no running loop.
        async def _probe() -> bool:
            dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
            other = await asyncpg.connect(dsn)
            try:
                found = await other.fetchval(
                    "SELECT 1 FROM users WHERE lower(email) = lower($1)", message["To"]
                )
                return found is not None
            finally:
                await other.close()

        seen.append(asyncio.run(_probe()))

    with TestClient(app) as client:
        import app.auth.router as auth_router

        original = auth_router.deliver
        auth_router.deliver = recording_deliver
        try:
            r = client.post(
                "/api/auth/signup",
                json={"email": "commit-order@example.com", "password": "Correct-Horse-Battery9"},
            )
        finally:
            auth_router.deliver = original

    assert r.status_code == 201, r.text
    assert seen, "the sender never ran, so this proves nothing"
    assert seen[0], (
        "the account was NOT committed before the email was sent -- the signup "
        "transaction is being held open across the SMTP call"
    )
