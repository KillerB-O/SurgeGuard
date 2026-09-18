"""Create the users and sessions tables backing real accounts (phase 1)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `users` and `sessions`, and record the decisions behind their shape.

    This migration makes four decisions that are easy to get wrong later, so
    they are spelled out here rather than left for a reader to reconstruct
    from the DDL alone.

    1. Case-insensitive email uniqueness is enforced with a functional unique
       index -- `UNIQUE INDEX idx_users_email_lower ON users (lower(email))`
       -- rather than the Postgres `citext` extension. No sibling migration
       (0001 through 0009) installs any extension, and reaching for one here
       just to save a `lower()` call would be new infrastructure for a single
       column. The tradeoff is that EVERY application-side comparison must
       remember to use `lower(:email)` (or compare against a value already
       lowercased) to actually hit this index; a comparison against the raw
       `email` column would silently full-scan and, worse, would not catch a
       duplicate that only differs by case. `app/auth/router.py` is written
       with this in mind.

    2. `sessions.token_hash` stores a SHA-256 digest of the session token, and
       the primary key on it IS the lookup key a request presents its cookie
       against. This is deliberately NOT the same kind of hash as
       `users.password_hash`. A password hash (Argon2id, salted) is built to
       be slow and non-deterministic -- verifying means recomputing and
       comparing, never looking up by value. A session token instead needs an
       O(1) lookup on every authenticated request, which only a deterministic
       digest supports; a salted hash would need a second plaintext-adjacent
       column just to find the row, which defeats the entire point of hashing
       the token at rest (D-04: only the cookie value is a secret, and a
       database read must not hand over a live session).

    3. `idx_sessions_user_id` is added now, in this migration, even though
       nothing in phase 1 queries sessions by `user_id` yet. Phase 2's
       EMAIL-04 ("a password reset revokes every session for a user") needs
       exactly this index, and adding it retroactively would mean a second
       migration touching a table this one already owns. Cheap now, and it
       removes a follow-up migration from a later phase's checklist.

    4. `users.verified` ships in this phase, defaulting `FALSE`, because
       `/me` (AUTH-07, plan 02) reports it in its response shape from day
       one. Nothing in phase 1 ever sets it `TRUE` and nothing gates on it
       being `TRUE` -- email verification is Phase 2 (EMAIL-01/02). Adding
       the column now avoids an ALTER TABLE later purely to satisfy a
       response model that already needs the field.

    `id` and `token_hash` are TEXT, matching this repo's existing convention
    for generated identifiers (`facilities.facility_id`, `orders.order_id`,
    and `action_id = f"action-{uuid4().hex}"` in `simulation.py`). A native
    Postgres `UUID` column and `gen_random_uuid()` appear nowhere in this
    codebase, and this migration does not introduce them.

    No `app` imports appear anywhere in this file (D-16): a migration runs
    with DDL privilege and no application context, so a password hash cannot
    be computed here. That is also why phase 3's seeded demo user is created
    at application startup from environment variables rather than baked into
    a migration as a precomputed hash literal, which would be a credential
    committed to git.
    """
    op.execute(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            verified BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX idx_users_email_lower ON users (lower(email))")

    op.execute(
        """
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX idx_sessions_user_id ON sessions(user_id)")


def downgrade() -> None:
    """Drop `sessions` before `users` -- the FK requires this order."""
    op.execute("DROP TABLE sessions")
    op.execute("DROP TABLE users")
