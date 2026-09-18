"""Mint, hash, and atomically consume single-use email tokens.

Verification and reset links are the same shape as session tokens (D-10):
high-entropy random values, hashed at rest with SHA-256, looked up by an O(1)
deterministic digest. `app/auth/sessions.py` already made and documented
these choices; this module copies the approach rather than inventing a
second one, deliberately re-deriving `_hash_token` locally instead of
importing that module's private name.

SQLAlchemy Core only (D-18) -- `text()` with named binds on the caller's
`AsyncConnection`. No declarative ORM base, no ORM-style Session, anywhere
in this module.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# 256 bits of entropy, matching SESSION_TOKEN_BYTES in sessions.py -- the
# same reasoning applies: a value that authenticates an action (here,
# proving control of an email inbox) needs CSPRNG-grade unguessability, not
# merely "hard to guess by hand".
TOKEN_BYTES = 32


class TokenPurpose(StrEnum):
    """Matches the `email_tokens.purpose` CHECK constraint exactly (migration 0011)."""

    VERIFY = "verify"
    RESET = "reset"


def _hash_token(raw: str) -> str:
    """Return the deterministic digest used as `email_tokens.token_hash`.

    Same reasoning as `sessions._hash_token`: the table is looked up by a
    presented token, which needs a deterministic digest to support an index
    lookup. A salted KDF (Argon2, bcrypt) cannot serve as a lookup key at
    all. See `app/auth/sessions.py`'s docstring for the full argument -- not
    duplicated here to avoid the two copies drifting apart in wording while
    disagreeing on substance.
    """
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


async def issue_token(
    conn: AsyncConnection, user_id: str, purpose: TokenPurpose, lifetime_seconds: int
) -> str:
    """Mint a new single-use token for `user_id` and return the RAW value.

    Invalidates this user's prior unconsumed tokens of the same `purpose`
    first, in one UPDATE, so a screenshotted older link stops working the
    moment a fresh one is issued -- only the newest link of a given purpose
    is ever valid. Only `_hash_token(raw)` is ever written to
    `email_tokens.token_hash` (D-10); the raw token returned here is never
    stored and exists only long enough to be embedded in the outgoing link.

    Args:
        conn: Request-scoped transaction.
        user_id: The account this token grants access to act on.
        purpose: Which action this token may complete (verify or reset).
        lifetime_seconds: How long the token remains valid from now.

    Returns:
        The raw, unhashed token for the link.
    """
    await conn.execute(
        text(
            """
            UPDATE email_tokens
            SET consumed_at = NOW()
            WHERE user_id = :user_id AND purpose = :purpose AND consumed_at IS NULL
            """
        ),
        {"user_id": user_id, "purpose": purpose.value},
    )

    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = datetime.now(UTC) + timedelta(seconds=lifetime_seconds)
    await conn.execute(
        text(
            """
            INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at)
            VALUES (:token_hash, :user_id, :purpose, :expires_at)
            """
        ),
        {
            "token_hash": _hash_token(raw_token),
            "user_id": user_id,
            "purpose": purpose.value,
            "expires_at": expires_at,
        },
    )
    return raw_token


async def consume_token(conn: AsyncConnection, raw: str, purpose: TokenPurpose) -> str | None:
    """Atomically validate and consume a token, returning its owner's user_id.

    A single `UPDATE ... RETURNING` is both the check and the claim (see
    02-RESEARCH.md "Pattern 2"): under PostgreSQL's READ COMMITTED
    isolation, two concurrent consumption attempts on the same row serialize
    on the row lock, and the second transaction re-evaluates
    `consumed_at IS NULL` against the post-commit row -- so it updates zero
    rows once the first attempt has already consumed it. Never read-then-
    write: that would let two simultaneous clicks both succeed.

    Args:
        conn: Request-scoped transaction.
        raw: The token presented in the link.
        purpose: The purpose this consumption attempt must match -- a verify
            token can never complete a reset, or vice versa (T-02-07).

    Returns:
        The owning user's id if the token was valid, unexpired, and unused;
        `None` otherwise.
    """
    result = await conn.execute(
        text(
            """
            UPDATE email_tokens
            SET consumed_at = NOW()
            WHERE token_hash = :token_hash
              AND purpose = :purpose
              AND consumed_at IS NULL
              AND expires_at > NOW()
            RETURNING user_id
            """
        ),
        {"token_hash": _hash_token(raw), "purpose": purpose.value},
    )
    row = result.first()
    return row[0] if row is not None else None


async def peek_token(conn: AsyncConnection, raw: str, purpose: TokenPurpose) -> bool:
    """Return whether a token is currently valid, WITHOUT consuming it.

    Needed by plan 03's reset pre-check: a GET on the reset link should be
    able to tell the user "this link works" before they type a new
    password, without spending the token's one use on a page load (see
    02-RESEARCH.md "Pitfall 5" -- the reset flow's GET must stay a read).

    Args:
        conn: Request-scoped transaction.
        raw: The token presented in the link.
        purpose: The purpose to check against.

    Returns:
        True if the token exists, matches `purpose`, is unconsumed, and
        unexpired; False otherwise.
    """
    result = await conn.execute(
        text(
            """
            SELECT 1 FROM email_tokens
            WHERE token_hash = :token_hash
              AND purpose = :purpose
              AND consumed_at IS NULL
              AND expires_at > NOW()
            """
        ),
        {"token_hash": _hash_token(raw), "purpose": purpose.value},
    )
    return result.first() is not None
