"""Let a facility execute the way it actually can, not just the way the demo does.

Every capacity or cutoff lever today assumes n8n can call
`http://simulator:8010/scenario/set-capacity` and get an instant, honest
answer. A real warehouse has no such endpoint: a human calls the floor
supervisor, and there is no webhook for that. Before this, the only options
were "execute against the simulator" or "cannot execute at all" -- there was
no way to record that a human did the physical thing, and no way to say
whether it actually worked.

`facilities.execution_adapter` selects how a throughput-family action (a
`capacity_delta` or `cutoff_shift` effect) gets executed:

- `simulator` (the default, so every existing facility including WH-01 keeps
  today's path unchanged): born `PENDING`, n8n polls and executes it exactly
  as it does now. Zero n8n workflow changes -- the poller's own `status ==
  "PENDING"` filter (`recovery-action-executor.json`) is the escape hatch: an
  action never born `PENDING` is never polled.
- `manual` / `webhook`: born `AWAITING_ENACTMENT` instead, so `GET
  /recovery-actions` (which defaults to `status=PENDING`) never returns it to
  n8n. A human confirms the physical thing happened through a new endpoint,
  moving the action to `ENACTED` -- a second new value, distinct from
  `SUCCESS` on purpose. `SUCCESS` today means only "n8n successfully called
  the endpoints", not that the change is real; collapsing `ENACTED` into
  `SUCCESS` would recreate exactly that conflation for the manual path --
  do not mark SUCCESS merely because the approval endpoint returned
  successfully, and the same principle applies to a human clicking confirm.

`ENACTED` is a terminal action status, parallel to `SUCCESS`: the execution
*process* is complete either way. Whether the claimed capacity was actually
observed is a separate question, verified independently (see 0019) and never
folds back into `recovery_actions.status`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE facilities
        ADD COLUMN execution_adapter VARCHAR(16) NOT NULL DEFAULT 'simulator'
            CHECK (execution_adapter IN ('simulator', 'manual', 'webhook'))
        """
    )

    op.execute("ALTER TABLE recovery_actions DROP CONSTRAINT recovery_actions_status_check")
    # VARCHAR(16) (migration 0002) is too narrow for 'AWAITING_ENACTMENT' (18
    # chars) -- widen before the constraint, or every insert of that value
    # fails on StringDataRightTruncationError regardless of the CHECK.
    op.execute("ALTER TABLE recovery_actions ALTER COLUMN status TYPE VARCHAR(32)")
    op.execute(
        """
        ALTER TABLE recovery_actions ADD CONSTRAINT recovery_actions_status_check
            CHECK (status IN ('PENDING', 'AWAITING_ENACTMENT', 'ENACTED', 'SUCCESS', 'FAILED'))
        """
    )
    op.execute("ALTER TABLE recovery_actions ADD COLUMN enacted_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE recovery_actions DROP COLUMN enacted_at")
    op.execute("ALTER TABLE recovery_actions DROP CONSTRAINT recovery_actions_status_check")
    op.execute("ALTER TABLE recovery_actions ALTER COLUMN status TYPE VARCHAR(16)")
    op.execute(
        """
        ALTER TABLE recovery_actions ADD CONSTRAINT recovery_actions_status_check
            CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED'))
        """
    )
    op.execute("ALTER TABLE facilities DROP COLUMN execution_adapter")
