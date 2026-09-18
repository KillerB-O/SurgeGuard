"""Give a facility a daily operating window (open/close), left unset.

`compute_schedule` has always treated capacity as flowing continuously:
`work_complete_at = now + cumulative_work / capacity_per_hour` assumes the
floor keeps producing at the same rate through the night. `CutoffPolicy`
(migration 0006) only ever affected when a finished order gets picked up,
never how fast work finishes -- a floor that never opens can still show a
2 a.m. `work_complete_at`.

Both columns are nullable and left null for every existing facility,
including WH-01, on this migration -- the same precedent 0006 already
argued for the cutoff column: a bounded daily window quantises the demo's
compressed-time SLA distribution the same way a single daily cutoff does,
only more so. `compute_schedule`'s `operating_calendar` argument stays
`None` for any facility without both columns set, which is bit-identical to
today's continuous-capacity arithmetic.

Set both together for a facility whose floor genuinely closes overnight:

    UPDATE facilities
    SET operating_opens_at = '06:00', operating_closes_at = '22:00'
    WHERE ...

Both null or both set -- a facility half-configured this way has an
unusable calendar, not a permissive one, so the CHECK constraint rejects it
rather than silently falling back to `None` for one bound but not the
other.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE facilities
        ADD COLUMN operating_opens_at TIME,
        ADD COLUMN operating_closes_at TIME
        """
    )
    op.execute(
        """
        ALTER TABLE facilities
        ADD CONSTRAINT facilities_operating_calendar_both_or_neither
            CHECK ((operating_opens_at IS NULL) = (operating_closes_at IS NULL))
        """
    )
    op.execute(
        """
        ALTER TABLE facilities
        ADD CONSTRAINT facilities_operating_calendar_nondegenerate
            CHECK (
                operating_opens_at IS NULL
                OR operating_opens_at <> operating_closes_at
            )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE facilities DROP CONSTRAINT facilities_operating_calendar_nondegenerate"
    )
    op.execute(
        "ALTER TABLE facilities DROP CONSTRAINT facilities_operating_calendar_both_or_neither"
    )
    op.execute(
        """
        ALTER TABLE facilities
        DROP COLUMN operating_opens_at,
        DROP COLUMN operating_closes_at
        """
    )
