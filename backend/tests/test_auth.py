"""Integration tests for POST /api/auth/signup.

These hit a real PostgreSQL database (via the app's normal DB engine), the
same idiom as test_events_integration.py. Skipped automatically when no
database is reachable -- unless GSD_REQUIRE_DATABASE=1, in which case an
unreachable database is a hard failure (see conftest.py's require_database).
"""

import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.helpers import mark_verified

pytestmark = pytest.mark.usefixtures("require_database")


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts from a clean slate on the users/sessions tables.

    Deletes from `sessions` before `users` -- the FK cascades on its own, but
    deleting children first keeps the intent obvious to a future reader. Uses
    its own throwaway asyncpg connection/event loop (via asyncio.run),
    deliberately not the app's shared async engine -- see conftest.py's
    docstring for why mixing loops on that shared pool breaks things. None of
    the other `clean_tables` fixtures in this suite touch these two tables.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM sessions")
            await conn.execute("DELETE FROM users")
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared async engine's pool after each test.

    Each `with TestClient(app) as client:` block below runs on its own fresh
    event loop. The app's shared async engine (app.db.engine) pools
    connections tied to whichever loop last used it, so without disposing it
    between tests, test N+1's loop would try to reuse a pooled connection
    that belongs to test N's already-closed loop and blow up.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


async def _read_password_hash(email: str) -> str | None:
    """Read the stored password hash directly, bypassing the app entirely."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            "SELECT password_hash FROM users WHERE lower(email) = lower($1)", email
        )
    finally:
        await conn.close()


def test_signup_with_fresh_email_returns_201_with_id_and_email():
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "fresh@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["email"] == "fresh@example.com"
        assert body.get("id")
        # No password field of any kind must be present in the response.
        assert not any("password" in key.lower() for key in body)


def test_signup_with_profile_fields_stores_and_returns_them():
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={
                "email": "profile@example.com",
                "password": "Correct-Horse-Battery9",
                "full_name": "Alex Morgan",
                "company": "Acme Logistics",
                "role": "Operations leader",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["full_name"] == "Alex Morgan"
        assert body["company"] == "Acme Logistics"
        assert body["role"] == "Operations leader"


def test_signup_without_profile_fields_defaults_to_empty_strings():
    """Backward compatible: the pre-existing email+password shape still works."""
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "noprofile@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["full_name"] == ""
        assert body["company"] == ""
        assert body["role"] == ""


def test_signup_twice_same_email_returns_409_naming_duplicate():
    with TestClient(app) as client:
        payload = {"email": "dupe@example.com", "password": "Correct-Horse-Battery9"}
        first = client.post("/api/auth/signup", json=payload)
        second = client.post("/api/auth/signup", json=payload)

        assert first.status_code == 201
        assert second.status_code == 409
        assert "already" in second.json()["detail"].lower()
        assert "registered" in second.json()["detail"].lower() or (
            "exists" in second.json()["detail"].lower()
        )


def test_signup_case_variant_email_is_also_rejected():
    with TestClient(app) as client:
        first = client.post(
            "/api/auth/signup",
            json={"email": "Ada@Example.com", "password": "Correct-Horse-Battery9"},
        )
        second = client.post(
            "/api/auth/signup",
            json={"email": "ada@example.com", "password": "Correct-Horse-Battery9"},
        )

        assert first.status_code == 201
        assert second.status_code == 409


def test_signup_with_short_password_returns_400_naming_length_rule():
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "shortpw@example.com", "password": "abc"},
        )
        assert r.status_code == 400
        assert "8 characters" in r.json()["detail"]


def test_signup_stores_argon2id_hash_not_plaintext():
    plaintext = "Correct-Horse-Battery9"
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "hashed@example.com", "password": plaintext},
        )
        assert r.status_code == 201

    stored = asyncio.run(_read_password_hash("hashed@example.com"))
    assert stored is not None
    assert stored.startswith("$argon2id$")
    assert stored != plaintext


# ---------------------------------------------------------------------------
# Plan 02: login, logout, /me, session lifetime, enumeration resistance
# (AUTH-04 through AUTH-09).
# ---------------------------------------------------------------------------


def _signup(client: TestClient, email: str, password: str = "Correct-Horse-Battery9") -> dict:
    """Create a real account through the public endpoint and return the credentials.

    Every plan-02 test starts from a genuine signed-up user rather than an
    inserted row, so the tests exercise the same path a real person takes.
    """
    r = client.post("/api/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    # Login is gated on verification, so a bare signup cannot sign in.
    # These tests need a usable account as a precondition, not a
    # verification round trip -- that path is covered in
    # test_email_verification.py.
    mark_verified(email)
    return {"email": email, "password": password}


async def _read_token_hashes() -> list[str]:
    """Read every stored `token_hash`, bypassing the app entirely (D-04 check)."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch("SELECT token_hash FROM sessions")
        return [row["token_hash"] for row in rows]
    finally:
        await conn.close()


async def _read_session_expiry() -> datetime:
    """Read the most recently created session's `expires_at`, bypassing the app."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            "SELECT expires_at FROM sessions ORDER BY created_at DESC LIMIT 1"
        )
    finally:
        await conn.close()


async def _session_count() -> int:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval("SELECT count(*) FROM sessions")
    finally:
        await conn.close()


async def _expire_all_sessions() -> None:
    """Force every session row into the past, simulating AUTH-08's expiry case.

    Uses a throwaway asyncpg connection (not the app's shared engine) per
    this file's `clean_tables` docstring on mixing event loops.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE sessions SET expires_at = NOW() - INTERVAL '1 hour'")
    finally:
        await conn.close()


def _cookie_value(client: TestClient) -> str:
    """Return the raw session cookie value the client is currently holding."""
    return client.cookies[settings.session_cookie_name]


def test_login_sets_httponly_samesite_lax_cookie_without_secure():
    """AUTH-04: correct credentials return 200 and the right cookie attributes."""
    with TestClient(app) as client:
        creds = _signup(client, "login-cookie@example.com")
        r = client.post("/api/auth/login", json=creds)
        assert r.status_code == 200

        raw_header = r.headers.get("set-cookie", "")
        assert settings.session_cookie_name in raw_header
        assert "HttpOnly" in raw_header
        assert "SameSite=lax" in raw_header or "SameSite=Lax" in raw_header
        # session_cookie_secure defaults False for plain http (D-03/config.py).
        assert "Secure" not in raw_header

        # D-02: the token travels ONLY in the cookie, never in the body.
        assert r.json() == {"status": "ok"}


def test_login_cookie_value_is_not_the_stored_hash():
    """AUTH-04/D-04: a database read must not hand over live sessions."""
    with TestClient(app) as client:
        creds = _signup(client, "login-hash-mismatch@example.com")
        r = client.post("/api/auth/login", json=creds)
        assert r.status_code == 200
        cookie_value = _cookie_value(client)

    hashes = asyncio.run(_read_token_hashes())
    assert len(hashes) == 1
    stored_hash = hashes[0]
    assert stored_hash != cookie_value
    assert len(stored_hash) == 64
    assert stored_hash == stored_hash.lower()
    assert all(c in "0123456789abcdef" for c in stored_hash)


def test_login_without_remember_me_uses_the_default_session_lifetime():
    with TestClient(app) as client:
        creds = _signup(client, "no-remember@example.com")
        before = datetime.now(UTC)
        r = client.post("/api/auth/login", json=creds)
        assert r.status_code == 200
        assert f"Max-Age={settings.session_lifetime_seconds}" in r.headers.get("set-cookie", "")

    expires_at = asyncio.run(_read_session_expiry())
    delta_seconds = (expires_at - before).total_seconds()
    assert abs(delta_seconds - settings.session_lifetime_seconds) < 5


def test_login_with_remember_me_uses_the_extended_session_lifetime():
    with TestClient(app) as client:
        creds = _signup(client, "remember@example.com")
        before = datetime.now(UTC)
        r = client.post("/api/auth/login", json={**creds, "remember_me": True})
        assert r.status_code == 200
        assert (
            f"Max-Age={settings.remembered_session_lifetime_seconds}"
            in r.headers.get("set-cookie", "")
        )

    expires_at = asyncio.run(_read_session_expiry())
    delta_seconds = (expires_at - before).total_seconds()
    assert abs(delta_seconds - settings.remembered_session_lifetime_seconds) < 5


def test_session_survives_a_refresh():
    """AUTH-05: a second request on the same client reaches /api/me."""
    with TestClient(app) as client:
        creds = _signup(client, "refresh@example.com")
        login = client.post("/api/auth/login", json=creds)
        assert login.status_code == 200

        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["email"] == "refresh@example.com"


def test_me_returns_identity_with_no_password_field():
    """AUTH-07: /me returns email and verified, never password material."""
    with TestClient(app) as client:
        creds = _signup(client, "identity@example.com")
        client.post("/api/auth/login", json=creds)

        me = client.get("/api/me")
        assert me.status_code == 200
        body = me.json()
        assert body["email"] == "identity@example.com"
        assert body["verified"] is True  # _signup verifies; login is gated on it
        assert not any("password" in key.lower() for key in body)
        assert not any("hash" in key.lower() for key in body)


def test_me_reflects_stored_profile_fields():
    with TestClient(app) as client:
        client.post(
            "/api/auth/signup",
            json={
                "email": "me-profile@example.com",
                "password": "Correct-Horse-Battery9",
                "full_name": "Priya Raghavan",
                "company": "Acme Logistics",
                "role": "Floor lead",
            },
        )
        mark_verified("me-profile@example.com")
        client.post(
            "/api/auth/login",
            json={"email": "me-profile@example.com", "password": "Correct-Horse-Battery9"},
        )

        me = client.get("/api/me")
        assert me.status_code == 200
        body = me.json()
        assert body["full_name"] == "Priya Raghavan"
        assert body["company"] == "Acme Logistics"
        assert body["role"] == "Floor lead"


def test_me_without_a_cookie_is_401():
    """AUTH-07: an unauthenticated request to /me is rejected."""
    with TestClient(app) as client:
        r = client.get("/api/me")
        assert r.status_code == 401


def test_logout_revokes_the_session_server_side():
    """AUTH-06 (the test that matters): a replayed cookie fails after logout.

    Captures the raw cookie value and replays it on a NEW client, so this
    proves the server-side row is gone -- not merely that the original
    browser cooperated and dropped its cookie.

    Two sequential `with TestClient(app)` blocks in one test function means
    two sequential portals/event loops. The app's shared async engine
    (`app.db.engine`) pools connections bound to whichever loop last used it,
    so it must be disposed between the two blocks here -- the same reason
    `dispose_shared_engine_between_tests` disposes it between separate test
    functions. Without this, the second block's loop tries to close/reuse a
    connection created on the first block's already-closed loop and raises
    `RuntimeError: Event loop is closed` during teardown.
    """
    with TestClient(app) as client:
        creds = _signup(client, "logout-revoke@example.com")
        client.post("/api/auth/login", json=creds)
        captured_cookie = _cookie_value(client)

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200

    from app.db import engine

    asyncio.run(engine.dispose())

    with TestClient(app) as fresh_client:
        fresh_client.cookies.set(settings.session_cookie_name, captured_cookie)
        r = fresh_client.get("/api/me")
        assert r.status_code == 401


def test_logout_clears_the_cookie_in_the_response():
    """AUTH-06 (lower stakes, still worth pinning): logout's cookie is EXPIRED, not re-set live.

    Checking only that `settings.session_cookie_name` appears in the header
    would also pass if logout accidentally re-issued a live cookie -- the
    property that actually matters is that the emitted cookie is a clearing
    cookie (`Max-Age=0`, an already-past `expires`), matching what
    `Response.delete_cookie` produces.
    """
    with TestClient(app) as client:
        creds = _signup(client, "logout-clear@example.com")
        client.post("/api/auth/login", json=creds)

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        raw_header = logout.headers.get("set-cookie", "")
        assert settings.session_cookie_name in raw_header
        assert "Max-Age=0" in raw_header
        assert "HttpOnly" in raw_header
        assert "SameSite=lax" in raw_header or "SameSite=Lax" in raw_header


def test_logout_deletes_the_session_row():
    """AUTH-06: the row backing the session is actually deleted, not just marked."""
    with TestClient(app) as client:
        creds = _signup(client, "logout-row@example.com")
        client.post("/api/auth/login", json=creds)
        assert asyncio.run(_session_count()) == 1

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200

    assert asyncio.run(_session_count()) == 0


def test_expired_session_is_rejected_even_with_a_valid_looking_cookie():
    """AUTH-08: an expired row is rejected even though the cookie is untouched."""
    with TestClient(app) as client:
        creds = _signup(client, "expired@example.com")
        client.post("/api/auth/login", json=creds)

        asyncio.run(_expire_all_sessions())

        r = client.get("/api/me")
        assert r.status_code == 401


def test_login_failure_is_identical_for_unknown_email_and_wrong_password():
    """AUTH-09: an attacker cannot distinguish "no such user" from "wrong password"."""
    with TestClient(app) as client:
        _signup(client, "enumeration-target@example.com", password="The-Real-Password1")

        unknown_email = client.post(
            "/api/auth/login",
            json={"email": "no-such-user@example.com", "password": "whatever"},
        )
        wrong_password = client.post(
            "/api/auth/login",
            json={"email": "enumeration-target@example.com", "password": "wrong-password"},
        )

        assert unknown_email.status_code == wrong_password.status_code
        assert unknown_email.json() == wrong_password.json()


def test_two_logins_issue_independent_sessions():
    """Logging in twice (two tabs) must not revoke the first session.

    Guards against a "one session per user" implementation that would break
    the demo the moment a judge opens a second tab. Uses a SINGLE
    `TestClient`/event loop and mutates the client's own cookie jar (via
    `client.cookies.set`) to swap between the two captured cookies, rather
    than opening two `TestClient` instances -- that would run two concurrent
    portals/event loops against the app's shared async engine, the exact
    loop-mixing failure `dispose_shared_engine_between_tests` exists to
    prevent, just within one test instead of between two.
    """
    with TestClient(app) as client:
        creds = _signup(client, "two-sessions@example.com")

        first_login = client.post("/api/auth/login", json=creds)
        second_login = client.post("/api/auth/login", json=creds)
        cookie_1 = first_login.cookies[settings.session_cookie_name]
        cookie_2 = second_login.cookies[settings.session_cookie_name]
        assert cookie_1 != cookie_2

        assert asyncio.run(_session_count()) == 2

        client.cookies.set(settings.session_cookie_name, cookie_1)
        logout_1 = client.post("/api/auth/logout")
        assert logout_1.status_code == 200

        client.cookies.set(settings.session_cookie_name, cookie_2)
        me_2 = client.get("/api/me")
        assert me_2.status_code == 200


# --- GET /api/auth/password-policy -----------------------------------------


def test_password_policy_is_readable_without_a_session():
    """The signup screen needs it before any account exists."""
    with TestClient(app) as client:
        r = client.get("/api/auth/password-policy")
        assert r.status_code == 200
        body = r.json()
        assert body["min_length"] == settings.password_min_length
        assert body["max_length"] == settings.password_max_length
        assert [rule["id"] for rule in body["rules"]] == [
            "length",
            "uppercase",
            "lowercase",
            "digit",
            "symbol",
        ]
        assert all(rule["label"] for rule in body["rules"])


def test_every_advertised_rule_is_actually_enforced_by_signup():
    """The published policy and the enforced policy cannot drift apart.

    This is the whole reason the endpoint exists. The signup form renders a
    live checklist from this response; if the server advertised a rule it did
    not enforce (or enforced one it did not advertise), the form would show a
    fully-ticked checklist and signup would still answer 400 -- which is worse
    than shipping no checklist at all.

    One password per rule, each violating exactly that rule, driven from the
    endpoint's own output rather than a hardcoded list here.
    """
    violations = {
        "length": "Sh0rt!",
        "uppercase": "correct-horse-9",
        "lowercase": "CORRECT-HORSE-9",
        "digit": "Correct-Horse-Xy",
        "symbol": "CorrectHorse9xy",
    }
    with TestClient(app) as client:
        advertised = [rule["id"] for rule in client.get("/api/auth/password-policy").json()["rules"]]
        assert set(advertised) == set(violations), (
            "a rule was added or renamed without a matching signup test"
        )
        for index, rule_id in enumerate(advertised):
            r = client.post(
                "/api/auth/signup",
                json={"email": f"drift-{index}@example.com", "password": violations[rule_id]},
            )
            assert r.status_code == 400, f"rule {rule_id!r} is advertised but not enforced"


def test_a_password_satisfying_every_advertised_rule_is_accepted():
    """The converse: nothing is enforced that the policy does not advertise."""
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/signup",
            json={"email": "satisfies-policy@example.com", "password": "Correct-Horse-Battery9"},
        )
        assert r.status_code == 201, r.text
