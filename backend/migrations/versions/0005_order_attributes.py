"""Persist the attributes work units are classified from, and order segment."""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add classification inputs and the cohort recovery levers target.

    All nullable: existing orders were ingested before these existed, and a
    producer that has not adopted them yet still ingests cleanly.
    """
    op.execute(
        """
        ALTER TABLE orders
            ADD COLUMN line_count INTEGER CHECK (line_count > 0),
            ADD COLUMN unit_count INTEGER CHECK (unit_count > 0),
            ADD COLUMN special_handling BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN segment VARCHAR(64)
        """
    )
    # Queue levers filter by cohort, so this is read on every plan projection.
    op.execute("CREATE INDEX idx_orders_facility_segment ON orders(facility_id, segment)")


def downgrade() -> None:
    """Drop the classification inputs and segment."""
    op.execute("DROP INDEX idx_orders_facility_segment")
    op.execute(
        """
        ALTER TABLE orders
            DROP COLUMN segment,
            DROP COLUMN special_handling,
            DROP COLUMN unit_count,
            DROP COLUMN line_count
        """
    )
