"""Persist a compressed demo clock, so the world can drift without wall time."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the single-row table that lets simulated time run faster than real time.

    Every read and simulation `now()` used to be `datetime.now(UTC)` directly,
    so a compressed demo run (an hour of simulator arrivals posted in seconds)
    never made the backend's age, slack, or minutes-to-breach numbers move
    faster than real time -- the risk numbers stood still while the simulator
    raced ahead.

    The clock persists rather than living in process memory for two reasons:
    a restart mid-run must not silently snap the world back to wall-clock time,
    and the API process and whatever drives the live loop (`scripts/
    demo_driver.py` today) have to agree on the same instant without a direct
    channel between them -- the database row is that channel.

    `id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id)` is the one-row-table
    trick: TRUE is the only value the check permits, so the primary key alone
    caps the table at a single row and an upsert on that key is how a restart
    re-anchors the clock instead of stacking a second one.
    """
    op.execute(
        """
        CREATE TABLE demo_clock (
            id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
            sim_anchor  TIMESTAMPTZ NOT NULL,
            wall_anchor TIMESTAMPTZ NOT NULL,
            rate        DOUBLE PRECISION NOT NULL DEFAULT 1.0
        )
        """
    )


def downgrade() -> None:
    """Drop the clock table; every caller's fallback is wall-clock time."""
    op.execute("DROP TABLE IF EXISTS demo_clock")
