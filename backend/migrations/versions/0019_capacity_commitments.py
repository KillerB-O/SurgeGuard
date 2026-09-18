"""Verify a capacity claim against telemetry, instead of trusting it forever.

Before this, a SUCCESS or ENACTED action's claimed capacity was written
straight to `facilities.capacity_per_hour` and treated as fact from that
moment on -- an operator could approve overtime, have it fail to happen for
any reason, and the dashboard would keep scheduling against 72 wu/hr while
the floor quietly stayed at 51.

`capacity_commitments` records what a capacity_delta effect claimed, and a
verifier (the alert-worker tick, `app/verification.py`) later compares it
against `order_status_transitions`-derived throughput -- the same measurement
`get_throughput_signal` already prefers over configured capacity, so nothing
here changes what the scheduler reads; it only changes whether a stale claim
stays silently believed.

`lead_time_minutes` is snapshotted from the lever at commit time, not looked
up again at verification time, for the same reason 0017 snapshots
`work_units` on a transition row: a later catalog retune must not rewrite an
already-running verification's deadline.

`verification_status` uses `UNVERIFIED` for "not yet resolved", deliberately
not `PENDING` -- this table is read straight alongside `recovery_actions`,
whose own `status` already has a `PENDING` with a different meaning, and a
naive join or comparison against the wrong column is exactly the kind of
mistake two same-named-but-different-meaning values invites.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE capacity_commitments (
            id BIGSERIAL PRIMARY KEY,
            facility_id VARCHAR(64) NOT NULL REFERENCES facilities(facility_id),
            action_id VARCHAR(128) NOT NULL REFERENCES recovery_actions(action_id)
                ON DELETE CASCADE,
            lever_id VARCHAR(64) NOT NULL,
            target_work_units_per_hour DOUBLE PRECISION NOT NULL
                CHECK (target_work_units_per_hour > 0),
            lead_time_minutes INTEGER NOT NULL CHECK (lead_time_minutes >= 0),
            committed_at TIMESTAMPTZ NOT NULL,
            verification_status VARCHAR(16) NOT NULL DEFAULT 'UNVERIFIED'
                CHECK (verification_status IN
                    ('UNVERIFIED', 'ACHIEVED', 'PARTIAL', 'NOT_OBSERVED')),
            verified_at TIMESTAMPTZ,
            observed_work_units_per_hour DOUBLE PRECISION,
            UNIQUE (action_id, lever_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_capacity_commitments_unverified "
        "ON capacity_commitments(facility_id, committed_at) "
        "WHERE verification_status = 'UNVERIFIED'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE capacity_commitments")
