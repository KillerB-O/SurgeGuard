"""Persist approved recovery actions and their execution state."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable recovery-action state storage."""
    op.execute(
        """
        CREATE TABLE recovery_actions (
            action_id VARCHAR(128) PRIMARY KEY,
            plan_id VARCHAR(64) NOT NULL,
            facility_id VARCHAR(64) NOT NULL REFERENCES facilities(facility_id),
            capacity_per_hour DOUBLE PRECISION NOT NULL CHECK (capacity_per_hour > 0),
            dispatch_promise_hours DOUBLE PRECISION NOT NULL CHECK (dispatch_promise_hours > 0),
            demand_multiplier DOUBLE PRECISION NOT NULL CHECK (demand_multiplier > 0),
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED')),
            error_detail VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_recovery_actions_facility ON recovery_actions(facility_id)")
    op.execute("CREATE INDEX idx_recovery_actions_status ON recovery_actions(status)")


def downgrade() -> None:
    """Remove durable recovery-action state storage."""
    op.execute("DROP TABLE recovery_actions")
