"""Flag demo facilities and administrators, so destructive demo controls
cannot reach real operational data.

`/demo/reset` deletes across `orders`, `recovery_actions`, `processed_events`
and `fulfillment_snapshots` with no facility scoping and no role check beyond
any signed-in session -- so the only thing standing between a misconfigured
deployment and a wiped facility was the presence of `SIMULATOR_URL` in the
environment. This migration adds the two flags the containment fix needs:
which facility may be reset, and who may reset it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `facilities.is_demo` and `users.is_admin`, seeding WH-01 as demo.

    `NOT NULL DEFAULT FALSE` for both: a facility or user that predates this
    migration is not silently made resettable or an administrator.
    """
    op.execute("ALTER TABLE facilities ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("UPDATE facilities SET is_demo = TRUE WHERE facility_id = 'WH-01'")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN is_admin")
    op.execute("ALTER TABLE facilities DROP COLUMN is_demo")
