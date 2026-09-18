"""Shared test helpers that reach past the app to the database directly.

Kept out of conftest.py because pytest does not put conftest on sys.path,
so it cannot be imported by name from a test module.
"""

import asyncio

import asyncpg
from fastapi.testclient import TestClient

from app.config import settings

# A fixed password used only to give an otherwise-unrelated test a session --
# it never needs to satisfy any property beyond passing `password_policy_error`.
_SIGN_IN_PASSWORD = "Correct-Horse-Battery9"


def mark_verified(email: str) -> None:
    """Mark an account verified directly in the database.

    Login is gated on verification, so any test whose subject is NOT
    verification -- sessions, logout, password reset, the service token --
    needs a verified account as a *precondition* before it can sign in at
    all. Driving the real emailed link in each of those would couple a dozen
    unrelated tests to the mailer for no added coverage; the genuine
    signup -> email -> click -> verified path is exercised end to end in
    tests/test_email_verification.py, which is where it belongs.

    Deliberately bypasses the app, like the other helpers here, so it cannot
    accidentally pass because an endpoint under test misbehaved.
    """

    async def _run() -> None:
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "UPDATE users SET verified = TRUE WHERE lower(email) = lower($1)", email
            )
        finally:
            await conn.close()

    asyncio.run(_run())


def read_verified(email: str) -> bool:
    """Read an account's `verified` flag straight from the database.

    Lets a test observe verification without signing in. Going through login
    would couple an assertion about one column to the entire session path --
    and since login is now gated on this very flag, it cannot observe the
    False case at all.
    """

    async def _run() -> bool:
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(
                "SELECT verified FROM users WHERE lower(email) = lower($1)", email
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def read_is_admin(email: str) -> bool:
    """Read an account's administrator flag directly from the database."""

    async def _run() -> bool:
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(
                "SELECT is_admin FROM users WHERE lower(email) = lower($1)", email
            )
        finally:
            await conn.close()

    return asyncio.run(_run())


def sign_in(client: TestClient, email: str, password: str = _SIGN_IN_PASSWORD) -> None:
    """Give `client` a real, live session cookie as a test precondition.

    Phase 3 gates the dashboard reads, simulations, recovery plans, and demo
    controls behind `require_session` (ACCESS-01). Most of the suite's
    existing tests are about scheduling, reads, and demo control -- not
    about auth -- so rather than weakening the gate, they call this first to
    obtain a session the same way a browser would: signup, then
    `mark_verified` (login is gated on verification, and driving the real
    emailed link here would couple unrelated tests to the mailer for no
    added coverage -- see `mark_verified`'s own docstring), then login.

    Idempotent per email: signup's `409` (already registered) is swallowed,
    since many call sites reuse the same fixed email across several tests
    against a users table that persists between them within one test file.
    A truly unexpected signup failure (anything other than `201` or `409`)
    still raises, so a real regression is not silently absorbed here.

    Args:
        client: The `TestClient` to attach a session cookie to. Mutates its
            cookie jar; every subsequent request on this client carries it.
        email: Account email. Reuse one fixed value per test file/module
            unless a test specifically needs an isolated account.
        password: Must satisfy `password_policy_error`; the default does.

    Raises:
        AssertionError: If signup fails for a reason other than "already
            registered", or if login does not return 200 afterward.
    """
    credentials = {"email": email, "password": password}
    signup = client.post("/api/auth/signup", json=credentials)
    assert signup.status_code in (201, 409), (
        f"sign_in: unexpected signup status {signup.status_code}: {signup.text}"
    )
    mark_verified(email)
    login = client.post("/api/auth/login", json=credentials)
    assert login.status_code == 200, f"sign_in: login failed: {login.status_code} {login.text}"
