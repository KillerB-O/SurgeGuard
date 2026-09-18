"""Give each facility its own SLA and surge-risk thresholds.

`WATCH_THRESHOLD_HOURS`/`AT_RISK_THRESHOLD_HOURS` and the three exposure
ratios have been frozen module constants in `app/scheduler.py`, applied
identically to every facility. A real warehouse with a tighter promise
window or a different risk appetite has no legal way to say so -- the
constant is named after one demo facility's calibration, the same shape
of problem P1 already fixed for `EventSource`.

The frozen values become these columns' defaults, not a second source of
truth: every existing facility (including WH-01) keeps today's
classification unchanged on migration.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE facilities
        ADD COLUMN watch_threshold_hours DOUBLE PRECISION NOT NULL DEFAULT 12.0
            CHECK (watch_threshold_hours > 0),
        ADD COLUMN at_risk_threshold_hours DOUBLE PRECISION NOT NULL DEFAULT 4.0
            CHECK (at_risk_threshold_hours > 0),
        ADD COLUMN exposure_high_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.15
            CHECK (exposure_high_ratio > 0 AND exposure_high_ratio <= 1),
        ADD COLUMN exposure_critical_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.40
            CHECK (exposure_critical_ratio > 0 AND exposure_critical_ratio <= 1),
        ADD COLUMN breach_critical_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.15
            CHECK (breach_critical_ratio > 0 AND breach_critical_ratio <= 1)
        """
    )
    op.execute(
        """
        ALTER TABLE facilities
        ADD CONSTRAINT facilities_watch_above_at_risk
            CHECK (watch_threshold_hours > at_risk_threshold_hours)
        """
    )
    op.execute(
        """
        ALTER TABLE facilities
        ADD CONSTRAINT facilities_critical_above_high_exposure
            CHECK (exposure_critical_ratio >= exposure_high_ratio)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE facilities DROP CONSTRAINT facilities_critical_above_high_exposure"
    )
    op.execute("ALTER TABLE facilities DROP CONSTRAINT facilities_watch_above_at_risk")
    op.execute(
        """
        ALTER TABLE facilities
        DROP COLUMN watch_threshold_hours,
        DROP COLUMN at_risk_threshold_hours,
        DROP COLUMN exposure_high_ratio,
        DROP COLUMN exposure_critical_ratio,
        DROP COLUMN breach_critical_ratio
        """
    )
