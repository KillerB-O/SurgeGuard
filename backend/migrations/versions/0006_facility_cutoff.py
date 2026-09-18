"""Give a facility a daily carrier pickup cutoff."""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the daily pickup cutoff, left unset.

    Null means continuous dispatch, which is how the scheduler behaved before
    this column existed, so an unconfigured facility keeps its predictions.

    WH-01 is deliberately left null. A single daily collection quantises
    predicted dispatch onto a handful of instants, and slack -- the difference
    between the promise and that instant -- becomes discrete with it. Under the
    demo's compressed arrivals that collapses the SLA distribution into two or
    three buckets and empties the AT_RISK band entirely, which guts the named
    at-risk order list the product exists to produce.

    Real traffic arrives spread across hours, so slack stays continuous even
    with one collection; it is the compression that makes this degenerate. Set a
    cutoff for a facility whose collection genuinely binds:

        UPDATE facilities SET dispatch_cutoff_utc = '18:00' WHERE ...
    """
    op.execute("ALTER TABLE facilities ADD COLUMN dispatch_cutoff_utc TIME")


def downgrade() -> None:
    """Return the facility to continuous dispatch."""
    op.execute("ALTER TABLE facilities DROP COLUMN dispatch_cutoff_utc")
