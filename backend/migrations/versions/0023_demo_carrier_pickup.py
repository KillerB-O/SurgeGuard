"""Give the demo facility a daily carrier pickup: 12:30 UTC (18:00 IST).

Migration 0006 left `dispatch_cutoff_utc` null for WH-01 on purpose: under the
demo's compressed clock a single daily collection quantises predicted dispatch
and thins the AT_RISK band. The cost of that choice turned out to be larger:
with no cutoff, `EXTEND_CARRIER_CUTOFF` ("Book a late carrier pickup") and its
`catch-the-late-pickup` plan can never be offered, and every real warehouse
has a collection time. This accepts the coarser SLA distribution in exchange
for a demo that shows the whole lever catalog.

Only the demo facility is touched, and only if nobody has configured a cutoff
yet. `POST /api/demo/reset` restores the same value, so an approved cutoff
shift does not accumulate across demo runs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE facilities
        SET dispatch_cutoff_utc = '12:30'
        WHERE facility_id = 'WH-01' AND is_demo AND dispatch_cutoff_utc IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE facilities
        SET dispatch_cutoff_utc = NULL
        WHERE facility_id = 'WH-01' AND is_demo AND dispatch_cutoff_utc = '12:30'
        """
    )
