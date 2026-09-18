"""Create alert_recipients and alert_outbox backing operator alerting (phase 1)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the recipient list and the delivery outbox.

    Two tables, and the split is the whole design. `alert_recipients` is WHO
    an operator wants told. `alert_outbox` is WHAT was decided and whether it
    actually reached them. Keeping the second one is what separates this from
    a `background_tasks.add_task(deliver, ...)` fire-and-forget send:

    1. The decision to alert is committed BEFORE anything touches SMTP, so a
       process that dies mid-send loses nothing -- the row is still PENDING
       and the next dispatcher pass picks it up. `app/auth/mailer.deliver`
       deliberately swallows every failure (decision D-16, pinned by
       `tests/test_mailer_unit.py`), which is correct for a verification link
       the user can re-request but is not good enough for an alert nobody
       knows was supposed to arrive.

    2. ONE ROW PER RECIPIENT, not one per firing. A single row carrying five
       addresses fails as a unit, so one bad address would suppress the alert
       for the other four. Separate rows fail independently, and `attempts`
       and `last_error` are then per-person facts the UI can show.

    3. This table IS the deduplication key. There is no separate
       "last notified" column on facilities to drift out of agreement with
       it: admission is a query against the most recent row for
       `(rule_id, facility_id, recipient_id)`, which makes one table serve as
       the cooldown guard, the repeat-suppression, and the audit trail at
       once. That follows migration 0011's stated convention -- one table per
       distinct SHAPE of thing, not one per use case -- and the shape here is
       "an alert that was decided, and what became of it".

    `status` is CHECK-constrained rather than left free-form, matching
    `email_tokens.purpose` in migration 0011. As there, the CHECK only
    constrains the stored value; application code is still what decides a
    FAILED row is terminal rather than retried forever.

    `alert_recipients.active` is a soft delete. Removing somebody from the
    list must not erase the record of alerts they were already sent, so the
    outbox references recipients with ON DELETE RESTRICT and the application
    flips `active` instead of issuing a DELETE. A hard delete would take the
    history with it, which is the one thing an audit trail may not do.

    No `app` imports appear here, matching migrations 0010 and 0011: a
    migration runs with DDL privilege and no application context.
    """
    op.execute(
        """
        CREATE TABLE alert_recipients (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            -- The lowest severity this person is willing to be woken for, so
            -- the operational head can take CRITICAL only while a floor
            -- supervisor takes HIGH and up. Compared in application code
            -- against the firing rule's own level.
            min_level TEXT NOT NULL DEFAULT 'HIGH'
                CHECK (min_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # Case-insensitive uniqueness over ACTIVE recipients only, via a partial
    # functional unique index. Same `lower()` approach as migration 0010's
    # idx_users_email_lower (no citext extension, for the same reason), with
    # the partial predicate added so deactivating somebody and later adding
    # them back is not blocked by their own historical row.
    op.execute(
        """
        CREATE UNIQUE INDEX idx_alert_recipients_email_lower_active
            ON alert_recipients (lower(email)) WHERE active
        """
    )

    op.execute(
        """
        CREATE TABLE alert_outbox (
            id TEXT PRIMARY KEY,
            -- Which catalog rule fired. An open string, not a CHECK: rules are
            -- data (see app/catalog/), so constraining this would mean a
            -- migration every time an operator adds a rule. Same reasoning as
            -- recovery plan ids, which are catalog posture ids.
            rule_id TEXT NOT NULL,
            facility_id VARCHAR(64) NOT NULL REFERENCES facilities(facility_id),
            recipient_id TEXT NOT NULL REFERENCES alert_recipients(id)
                ON DELETE RESTRICT,
            level TEXT NOT NULL
                CHECK (level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            -- Simulated time, from app/clock.py -- never wall time. Cooldowns
            -- are measured on the same clock as the schedule, so a run at 25x
            -- compresses them exactly as it compresses everything else.
            decided_at TIMESTAMPTZ NOT NULL,
            sent_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_outbox_dedup
            ON alert_outbox (rule_id, facility_id, recipient_id, decided_at DESC)
        """
    )
    # Partial, covering only rows the dispatcher actually claims. The outbox
    # grows without bound as a log, so an index over every row would keep
    # paying for history the drain loop never reads.
    op.execute(
        """
        CREATE INDEX idx_alert_outbox_pending
            ON alert_outbox (decided_at) WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    """Drop the outbox first: it references alert_recipients."""
    op.execute("DROP TABLE alert_outbox")
    op.execute("DROP TABLE alert_recipients")
