"""Create email_tokens for verification and password-reset links (phase 2)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `email_tokens`, one table for both verification and reset links.

    One table with a `purpose` column, not two tables: both kinds of token
    share an identical shape (hash, owner, expiry, single-use consumption),
    and this codebase's existing convention (see migration 0010's `sessions`
    table) is one table per distinct *shape* of thing, not one table per
    *use case* of an identical shape. A CHECK constraint keeps `purpose`
    from drifting to an unexpected value, but the CHECK only constrains the
    stored value -- application code is still what decides a verify token
    may never complete a reset (EMAIL-02/EMAIL-04); `app/auth/tokens.py`'s
    `consume_token` matches `purpose` in its WHERE clause for exactly this
    reason.

    `token_hash` is the primary key, identical reasoning to
    `sessions.token_hash` in migration 0010: an O(1) lookup by presented
    token needs a deterministic digest, not a salted KDF, and the raw token
    is never stored anywhere (D-10).

    `idx_email_tokens_user_purpose` supports invalidating a user's prior
    unconsumed tokens of the same purpose when a fresh one is issued, so a
    screenshotted older link stops working the moment a newer one is minted.

    No `app` imports appear anywhere in this file, matching migration 0010's
    reasoning: a migration runs with DDL privilege and no application
    context.
    """
    op.execute(
        """
        CREATE TABLE email_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL CHECK (purpose IN ('verify', 'reset')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX idx_email_tokens_user_purpose ON email_tokens(user_id, purpose)")


def downgrade() -> None:
    """Drop email_tokens."""
    op.execute("DROP TABLE email_tokens")
