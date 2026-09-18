"""Allow CANCELLED as a real, storable order status.

Migration 0003's docstring left CANCELLED out of `orders_status_lifecycle`
deliberately: at the time, cancellation had no defined transition semantics
anywhere in the codebase, so accepting the value at the database layer would
have let an order sit in a status nothing else understood. P8 gives it real
semantics (`_require_valid_transition` in app/routers/events.py: legal from
any pre-dispatch status, refused once DISPATCHED), so it's time to let the
column store it.

`orders.status` is VARCHAR(32) and 'CANCELLED' is 9 characters -- verified
against the live schema before writing this, not assumed (see migration
0018's AWAITING_ENACTMENT/VARCHAR(16) trap this project has already hit
once). No column-width change needed here.

DELAYED stays out, same as before: still no defined transition semantics
anywhere, and adding it is not part of P8's scope.

Postgres has no ALTER-CHECK-IN-PLACE; drop and recreate under the same
constraint name (explicit since 0003, so no need to look up an auto-generated
one the way migration 0018 had to for recovery_actions.status).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders DROP CONSTRAINT orders_status_lifecycle")
    op.execute(
        """
        ALTER TABLE orders
        ADD CONSTRAINT orders_status_lifecycle
        CHECK (status IN ('PENDING', 'PICKING', 'PACKED', 'READY', 'DISPATCHED', 'CANCELLED'))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP CONSTRAINT orders_status_lifecycle")
    op.execute(
        """
        ALTER TABLE orders
        ADD CONSTRAINT orders_status_lifecycle
        CHECK (status IN ('PENDING', 'PICKING', 'PACKED', 'READY', 'DISPATCHED'))
        """
    )
