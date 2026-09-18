"""Index the columns the polled read paths filter on."""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Support the per-facility demand window and pending-queue lookups.

    The frontend polls every 2-3 seconds, so both of these run constantly.
    """
    op.execute(
        "CREATE INDEX idx_orders_facility_created ON orders(facility_id, created_at)"
    )
    op.execute(
        "CREATE INDEX idx_orders_facility_status ON orders(facility_id, status)"
    )
    # Superseded by the composite indexes above.
    op.execute("DROP INDEX idx_orders_facility_id")
    op.execute("DROP INDEX idx_orders_status")


def downgrade() -> None:
    """Restore the original single-column indexes."""
    op.execute("CREATE INDEX idx_orders_facility_id ON orders(facility_id)")
    op.execute("CREATE INDEX idx_orders_status ON orders(status)")
    op.execute("DROP INDEX idx_orders_facility_status")
    op.execute("DROP INDEX idx_orders_facility_created")
