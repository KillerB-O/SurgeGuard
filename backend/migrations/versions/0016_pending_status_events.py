"""Park status events for orders that have not arrived yet, instead of
dropping them.

Real webhooks are not ordered: a WMS "picking started" event can reach the
backend before the commerce feed's "order created" event does, even though
both describe the same order. `ingest_order_status_updated` used to 404 and
drop such an event outright -- the principle that a status event must not
create an order is correct, but discarding the event loses a fact that
becomes true the moment the order it describes arrives.

Exactly-once delivery is already handled by `processed_events` (event_id is
claimed there before the park decision is made), so this table does not need
its own uniqueness on event_id -- a retry of an already-parked event is
recognised as a duplicate the ordinary way.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pending_status_events (
            id BIGSERIAL PRIMARY KEY,
            order_id VARCHAR(64) NOT NULL,
            facility_id VARCHAR(64) NOT NULL,
            event_id VARCHAR(128) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(32) NOT NULL,
            source VARCHAR(64) NOT NULL,
            parked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_pending_status_events_order "
        "ON pending_status_events(facility_id, order_id, occurred_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE pending_status_events")
