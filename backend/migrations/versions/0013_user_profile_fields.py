"""Add full_name, company, and role to users for the redesigned signup form."""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add three optional profile columns to `users`.

    `NOT NULL DEFAULT ''` rather than a nullable column, matching how
    `users.verified` was handled in migration 0010: every existing account
    (including the seeded demo user) gets a real, non-null value with no
    separate backfill step, and the application never has to distinguish
    "never provided" from "empty" for these fields.
    """
    op.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE users ADD COLUMN company TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN role")
    op.execute("ALTER TABLE users DROP COLUMN company")
    op.execute("ALTER TABLE users DROP COLUMN full_name")
