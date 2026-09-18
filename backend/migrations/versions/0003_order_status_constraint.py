"""Constrain orders.status to the canonical MVP lifecycle."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reject stored statuses the MVP lifecycle cannot produce.

    RECEIVED, CANCELLED, and DELAYED stay out of the constraint: they exist in
    the enum but have no defined transition semantics.
    """
    op.execute(
        """
        ALTER TABLE orders
        ADD CONSTRAINT orders_status_lifecycle
        CHECK (status IN ('PENDING', 'PICKING', 'PACKED', 'READY', 'DISPATCHED'))
        """
    )


def downgrade() -> None:
    """Allow any status value again."""
    op.execute("ALTER TABLE orders DROP CONSTRAINT orders_status_lifecycle")
