"""Create the initial SurgeGuard database schema.

SQL statements stay separate because asyncpg rejects multi-statement queries.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create core tables and seed the demo facility."""
    op.execute(
        """
        CREATE TABLE facilities (
            facility_id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            capacity_per_hour DOUBLE PRECISION NOT NULL
                CHECK (capacity_per_hour > 0),
            dispatch_promise_hours DOUBLE PRECISION NOT NULL
                CHECK (dispatch_promise_hours > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE orders (
            order_id VARCHAR(64) PRIMARY KEY,
            facility_id VARCHAR(64) NOT NULL
                REFERENCES facilities(facility_id),

            created_at TIMESTAMPTZ NOT NULL,
            promised_dispatch_at TIMESTAMPTZ NOT NULL,

            item_count INTEGER NOT NULL CHECK (item_count > 0),
            work_units DOUBLE PRECISION NOT NULL CHECK (work_units > 0),
            order_value NUMERIC(12, 2) NOT NULL CHECK (order_value >= 0),

            status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CHECK (promised_dispatch_at > created_at)
        )
        """
    )

    op.execute("CREATE INDEX idx_orders_facility_id ON orders(facility_id)")
    op.execute("CREATE INDEX idx_orders_status ON orders(status)")

    op.execute(
        """
        CREATE TABLE fulfillment_snapshots (
            id BIGSERIAL PRIMARY KEY,
            facility_id VARCHAR(64) NOT NULL
                REFERENCES facilities(facility_id),
            occurred_at TIMESTAMPTZ NOT NULL,
            open_orders INTEGER NOT NULL CHECK (open_orders >= 0),
            work_units_completed_last_hour DOUBLE PRECISION NOT NULL
                CHECK (work_units_completed_last_hour >= 0)
        )
        """
    )

    op.execute(
        "CREATE INDEX idx_fulfillment_snapshots_facility_time "
        "ON fulfillment_snapshots(facility_id, occurred_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE processed_events (
            event_id VARCHAR(128) PRIMARY KEY,
            source VARCHAR(64) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        INSERT INTO facilities (
            facility_id, name, capacity_per_hour, dispatch_promise_hours
        )
        VALUES ('WH-01', 'Demo Fulfillment Center', 52, 24)
        """
    )


def downgrade() -> None:
    """Drop core tables in foreign-key-safe order."""
    op.execute("DROP TABLE processed_events")
    op.execute("DROP TABLE fulfillment_snapshots")
    op.execute("DROP TABLE orders")
    op.execute("DROP TABLE facilities")
