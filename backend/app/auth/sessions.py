"""Opaque session token lifecycle: create, look up, and revoke.

Sessions are random tokens, not JWTs (D-01): the server can forget one on
demand by deleting its row, with no refresh-token rotation to reason about.
SQLAlchemy Core only (D-14) -- `text()` with named binds on the caller's
`AsyncConnection`, matching `app/repository.py`'s idiom. No declarative ORM
base, no ORM-style Session, anywhere in this module.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import settings

# 256 bits of entropy (32 bytes -> 44 URL-safe base64 characters). OWASP's
# Session Management Cheat Sheet sets a 64-bit floor and names 128 bits as a
# solid reference point; 256 bits clears both with no practical downside --
# cookie size is trivial at this length.
SESSION_TOKEN_BYTES = 32


def _hash_token(raw: str) -> str:
    """Return the deterministic digest of `raw` used as the storage/lookup key.

    Why SHA-256 and not Argon2 (the same hasher used for passwords): the
    session table must support O(1) lookup by presented token -- the request
    handler receives the raw cookie value and needs to find the matching row
    without hashing every stored row. That requires a DETERMINISTIC hash:
    `sha256(token)` always produces the same digest for the same token, so
    `WHERE token_hash = :hash` can hit a plain index. A salted KDF (Argon2,
    bcrypt) is deliberately non-deterministic per call -- a fresh salt is
    embedded every time -- so it could not serve as a lookup key at all
    without also storing a second, separate deterministic identifier, which
    would defeat the point of hashing at rest. This is not a speed
    optimisation either: the token is a 256-bit CSPRNG value with no
    guessable structure for a slow hash to protect. A cheap, deterministic
    digest costs nothing here and buys the index; a KDF would cost real
    latency on every authenticated request and buy nothing extra.
    """
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


async def create_session(
    conn: AsyncConnection, user_id: str, ttl_seconds: int | None = None
) -> str:
    """Issue a new session for `user_id` and return the RAW token for the cookie.

    `secrets.token_urlsafe` is the CSPRNG-backed stdlib generator -- `random`
    is explicitly not cryptographically secure, and `uuid4()`'s randomness
    source is not guaranteed CSPRNG on every platform, so neither is an
    acceptable substitute for a value that authenticates a user.

    Only `_hash_token(raw)` is ever written to `sessions.token_hash` (D-04):
    a stolen database dump yields digests that cannot be replayed as cookies.
    The raw token returned here is never stored anywhere and exists only long
    enough to be handed to the caller for `Response.set_cookie`.

    Args:
        conn: Request-scoped transaction.
        user_id: The authenticated user this session belongs to.
        ttl_seconds: How long the session should live. Defaults to
            `settings.session_lifetime_seconds` (the fixed 12h session) when
            omitted; `login`'s "remember me" path passes a longer explicit
            value instead of changing the module-level default.

    Returns:
        The raw, unhashed session token.
    """
    raw_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    lifetime = ttl_seconds if ttl_seconds is not None else settings.session_lifetime_seconds
    expires_at = datetime.now(UTC) + timedelta(seconds=lifetime)
    await conn.execute(
        text(
            """
            INSERT INTO sessions (token_hash, user_id, expires_at)
            VALUES (:token_hash, :user_id, :expires_at)
            """
        ),
        {"token_hash": _hash_token(raw_token), "user_id": user_id, "expires_at": expires_at},
    )
    return raw_token


async def lookup_session(conn: AsyncConnection, raw_token: str) -> str | None:
    """Return the session's `user_id` if the token is valid and unexpired.

    Expiry is enforced in the WHERE clause (D-05/AUTH-08), not by the caller:
    the cookie can outlive the session -- the client is never trusted to
    discard an expired cookie on its own -- so this query is the actual
    enforcement point. `Response.set_cookie(max_age=...)` at login is only a
    client-side convenience; this predicate is what makes expiry real.

    The comparison below is a plain SQL equality lookup, not
    `secrets.compare_digest`: `token_hash` IS the primary key being probed by
    an index, not a locally-held secret being compared against
    attacker-controlled input inside this process. There is nothing here for
    a timing side-channel to extract -- Postgres's index lookup either finds
    the row or it does not. (`compare_digest` earns its keep in plan 03,
    where a configured service-token secret is compared byte-for-byte inside
    this process; that is a genuinely different situation, not an
    inconsistency with this one.)

    Args:
        conn: Request-scoped transaction.
        raw_token: The token presented in the request's session cookie.

    Returns:
        The owning user's id, or `None` if the token is unknown, revoked, or
        expired.
    """
    result = await conn.execute(
        text(
            """
            SELECT user_id FROM sessions
            WHERE token_hash = :token_hash AND expires_at > NOW()
            """
        ),
        {"token_hash": _hash_token(raw_token)},
    )
    row = result.first()
    return row[0] if row is not None else None


async def revoke_session(conn: AsyncConnection, raw_token: str) -> None:
    """Delete the session row so a replayed cookie can never validate again.

    AUTH-06's real assertion lives here: logging out must delete the row, not
    merely ask the browser to forget the cookie, so a captured cookie value
    replayed on a different client after logout is rejected by
    `lookup_session` finding no matching row.

    Args:
        conn: Request-scoped transaction.
        raw_token: The token to revoke.
    """
    await conn.execute(
        text("DELETE FROM sessions WHERE token_hash = :token_hash"),
        {"token_hash": _hash_token(raw_token)},
    )
