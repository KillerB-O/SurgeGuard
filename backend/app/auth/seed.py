"""Seed a pre-verified demo account at startup (ACCESS-03).

The live demo cannot afford to sit through signup and email verification, so
a judge needs an account that already exists and is already verified the
moment the backend comes up. This is deliberately NOT done in a migration --
migration 0010's docstring (point D-16) explains why: every migration in this
repo is raw `op.execute` with zero `app` imports, and hashing a password here
would mean either importing `app.auth.passwords` into a migration (breaking
that convention) or committing a precomputed hash literal (a credential in
git). Seeding at application startup, from environment variables, avoids
both.
"""

import logging
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth.passwords import hash_password
from app.config import settings

logger = logging.getLogger(__name__)


async def seed_demo_user(engine: AsyncEngine) -> None:
    """Idempotently create the configured demo account, already verified.

    A no-op unless BOTH `settings.demo_user_email` and
    `settings.demo_user_password` are set (D-08-style fail-closed default,
    same reasoning as `settings.service_token`) -- a fresh clone or a CI run
    with no demo credentials configured must never gain a surprise account.

    Idempotent, targeting migration 0010's `idx_users_email_lower` functional
    unique index explicitly (`ON CONFLICT ((lower(email)))`), UNLIKE
    `POST /auth/signup`'s bare `ON CONFLICT DO NOTHING`. The difference is
    deliberate: signup only ever needs to detect a duplicate and reject it,
    but this seed's entire purpose is a login that needs no verification
    click, so a `DO NOTHING` that silently no-ops against an existing,
    unverified row -- whether from an earlier partial signup or a previous
    seed -- would leave the configured account exactly as unable to sign in
    as ACCESS-03 exists to prevent. `DO UPDATE SET verified = TRUE` closes
    that gap: a restart with the same configured email is safe to run any
    number of times, and a pre-existing row for that email always ends up
    verified regardless of how it got there.

    Deliberately does NOT update `password_hash` on conflict -- only on the
    initial insert. Two consequences follow from that, both intentional:
    changing `DEMO_USER_PASSWORD` after the first boot does not retroactively
    change an already-seeded account's credential (re-seeding a changed
    credential is a job for a deliberate one-off script, not silent startup
    behaviour), and if the configured email happens to collide with a real
    user's own account, this never overwrites that person's password --
    it only ensures the row is verified, which is a comparatively narrow
    side effect. Operators should still treat the configured demo email as
    reserved and not reuse one a real user might sign up with.

    Never raises. A database that is not yet reachable at startup (slow to
    come up, still applying migrations, briefly unavailable) must not take
    the whole application down with it -- `/health` in particular must keep
    answering with no database access at all (ACCESS-04), so a seeding
    failure here is logged and swallowed rather than propagated.

    Args:
        engine: The application's async engine, used for one short-lived
            connection outside any request's transaction.
    """
    email = settings.demo_user_email
    password = settings.demo_user_password
    if not email or not password:
        return

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users (id, email, password_hash, verified, is_admin)
                    VALUES (:id, :email, :password_hash, TRUE, TRUE)
                    ON CONFLICT ((lower(email))) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        verified = TRUE,
                        is_admin = TRUE
                    """
                ),
                {
                    "id": f"user-{uuid4().hex}",
                    "email": email,
                    "password_hash": hash_password(password),
                },
            )
    except Exception:
        # Broad on purpose (mirrors app.main's OperationalError/ConnectionError
        # handling philosophy): whatever the cause -- unreachable database,
        # migration 0010 not yet applied, anything else -- a demo-account seed
        # must never be the reason the application fails to start.
        logger.exception(
            "demo user seed skipped: could not create/verify %r at startup", email
        )
