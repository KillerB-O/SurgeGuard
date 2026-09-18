"""Record every status transition, so throughput can be measured rather than
asked for.

`FulfillmentSnapshotEvent.work_units_completed_last_hour` required a producer
to compute and send its own throughput figure. No real WMS emits that metric
-- real WMSs emit discrete pick/pack/ship confirmations per order, which this
backend already receives as OrderStatusUpdatedEvent and, until now, threw
away everything except the order's current status.

Two details make this table honest rather than a second source of drift:

- `origin` distinguishes a live event from a future backfill. Without it, a
  facility's entire history dumped in on day one would report a fictitious
  throughput spike at t=0.
- `work_units` is snapshotted per row rather than joined to `orders`. If a
  future lever ever applies `work_unit_multiplier` for real (today it is
  projection-only), a join would retroactively rewrite measured history.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE order_status_transitions (
            id BIGSERIAL PRIMARY KEY,
            order_id VARCHAR(64) NOT NULL,
            facility_id VARCHAR(64) NOT NULL,
            from_status VARCHAR(32),
            to_status VARCHAR(32) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_id VARCHAR(128) NOT NULL,
            work_units DOUBLE PRECISION NOT NULL,
            origin VARCHAR(16) NOT NULL DEFAULT 'event'
                CHECK (origin IN ('event', 'backfill'))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_order_status_transitions_throughput "
        "ON order_status_transitions(facility_id, occurred_at DESC) "
        "WHERE origin = 'event'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE order_status_transitions")
