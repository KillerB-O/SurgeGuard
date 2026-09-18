"""Let an approved recovery plan change the world, not just the projection."""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record what an approved plan must do, and what is currently in force.

    Only a capacity change used to be executable: the action row carried
    capacity, promise and demand multiplier, and n8n knew how to apply exactly
    one of them. A queue or promise plan therefore projected a large
    improvement, reported SUCCESS, and left the queue untouched.

    Three additions close that gap.

    `recovery_actions.effects` carries the posture's typed effects, so the
    executor is told what to do rather than inferring it from three numbers
    that cannot express a reorder.

    `active_interventions` records what is currently in force at a facility.
    A queue lever has no external system to call -- its effect is a change to
    how the backend ranks the queue -- so it has to persist somewhere the read
    path can see it.

    `orders.original_promised_dispatch_at` preserves the deadline a customer was
    first given. Re-promising a placed order is defensible only if the original
    survives, so the change stays auditable and reversible.
    """
    op.execute("ALTER TABLE recovery_actions ADD COLUMN effects JSONB NOT NULL DEFAULT '[]'::jsonb")

    op.execute(
        """
        ALTER TABLE orders
        ADD COLUMN original_promised_dispatch_at TIMESTAMPTZ
        """
    )

    op.execute(
        """
        CREATE TABLE active_interventions (
            id BIGSERIAL PRIMARY KEY,
            facility_id VARCHAR(64) NOT NULL
                REFERENCES facilities(facility_id),
            action_id VARCHAR(128) NOT NULL
                REFERENCES recovery_actions(action_id) ON DELETE CASCADE,
            lever_id VARCHAR(64) NOT NULL,
            family VARCHAR(16) NOT NULL,
            effect JSONB NOT NULL,
            -- Order ids the effect was applied to, so a queue adjustment can be
            -- reproduced on every later read without re-deriving who qualified
            -- at the moment of approval.
            order_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            -- One row per lever per action: applying twice must not stack.
            UNIQUE (action_id, lever_id)
        )
        """
    )

    op.execute(
        "CREATE INDEX idx_active_interventions_facility "
        "ON active_interventions(facility_id)"
    )


def downgrade() -> None:
    """Return to a capacity-only execution path."""
    op.execute("DROP TABLE IF EXISTS active_interventions")
    op.execute("ALTER TABLE orders DROP COLUMN original_promised_dispatch_at")
    op.execute("ALTER TABLE recovery_actions DROP COLUMN effects")
