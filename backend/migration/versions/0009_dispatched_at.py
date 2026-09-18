"""Record when an order actually left, on the same clock as its promise."""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give dispatch a timestamp that can be compared against the promise.

    `updated_at` looks like it would serve, and cannot. It is set to `NOW()` --
    the wall clock -- while `promised_dispatch_at` is stamped on the simulated
    clock a live run advances. Comparing the two would mean comparing instants
    from different timelines, and would report every order dispatched during a
    live run as catastrophically late.

    This column is filled from the status event's own `occurred_at`, which the
    simulator stamps with the instant the backend told it, so a dispatch and a
    promise are always measured on one timeline.

    Null for orders that have not been dispatched, and for any dispatched
    before this column existed -- those are simply not counted, which is honest:
    we do not know when they left.
    """
    op.execute("ALTER TABLE orders ADD COLUMN dispatched_at TIMESTAMPTZ")


def downgrade() -> None:
    """Forget when orders left."""
    op.execute("ALTER TABLE orders DROP COLUMN dispatched_at")
