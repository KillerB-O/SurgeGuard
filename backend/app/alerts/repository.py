"""Data access for alerting: the recipient list and the delivery outbox.

Deliberately NOT in `app/repository.py`: that module's first line declares it
read-only, and these are writes. This package owns its own data access the
same way `app/auth/` does, so the alerting feature can grow an evaluator and
a dispatcher beside this file without widening a shared module.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.alerts.rules import LastAlert
from app.models import SurgeRiskLevel


class DuplicateRecipientError(Exception):
    """Raised when an address is already on the active list."""


async def list_recipients(conn: AsyncConnection) -> list[dict]:
    """Return the active recipients, oldest first.

    Inactive rows are never returned. They exist only so the outbox can keep
    referencing somebody after an operator removes them (migration 0012), and
    surfacing them here would make removal look like it had not worked.

    Args:
        conn: Request-scoped transaction.

    Returns:
        One mapping per active recipient.
    """
    result = await conn.execute(
        text(
            """
            SELECT id, name, email, min_level
            FROM alert_recipients
            WHERE active
            ORDER BY created_at
            """
        )
    )
    return [dict(row) for row in result.mappings()]


async def add_recipient(
    conn: AsyncConnection, name: str, email: str, min_level: SurgeRiskLevel
) -> dict:
    """Add somebody to the alert list.

    Uniqueness is enforced by `idx_alert_recipients_email_lower_active`, not
    by a pre-flight SELECT: two operators adding the same address at once
    would both pass a check-then-insert, and the database is the only place
    that race can actually be settled. The IntegrityError is translated here
    so the router does not have to know which constraint fired.

    Args:
        conn: Request-scoped transaction.
        name: Display name, shown in the UI rather than emailed.
        email: Address alerts are sent to.
        min_level: Lowest severity worth emailing this person about.

    Returns:
        The stored recipient.

    Raises:
        DuplicateRecipientError: The address is already active on the list.
    """
    recipient_id = f"recipient-{uuid4().hex}"
    try:
        result = await conn.execute(
            text(
                """
                INSERT INTO alert_recipients (id, name, email, min_level)
                VALUES (:id, :name, :email, :min_level)
                RETURNING id, name, email, min_level
                """
            ),
            {
                "id": recipient_id,
                "name": name,
                "email": email,
                "min_level": min_level.value,
            },
        )
    except IntegrityError as exc:
        raise DuplicateRecipientError(email) from exc

    row = result.mappings().first()
    assert row is not None  # RETURNING on a successful INSERT always yields a row.
    return dict(row)


async def deactivate_recipient(conn: AsyncConnection, recipient_id: str) -> bool:
    """Remove somebody from the list without destroying their alert history.

    A soft delete, for the reason migration 0012 records: the outbox
    references recipients, so a hard DELETE would either be refused by
    `ON DELETE RESTRICT` or, with a cascade, would take the record of what
    they were already sent with it.

    Deactivating an already-inactive recipient returns False rather than
    raising, so the router can answer a repeated delete the same way it
    answers an unknown id -- the caller's intent is satisfied either way,
    and distinguishing them would leak whether an id ever existed.

    Args:
        conn: Request-scoped transaction.
        recipient_id: Recipient to deactivate.

    Returns:
        True if this call was the one that deactivated them.
    """
    result = await conn.execute(
        text(
            """
            UPDATE alert_recipients
            SET active = FALSE
            WHERE id = :id AND active
            RETURNING id
            """
        ),
        {"id": recipient_id},
    )
    return result.first() is not None


async def claim_pending_alerts(conn: AsyncConnection, limit: int) -> list[dict]:
    """Claim a batch of undelivered alerts for this dispatcher pass.

    `FOR UPDATE SKIP LOCKED` so two dispatchers running at once take disjoint
    batches instead of both sending the same email. There is one dispatcher
    today, so this is future-proofing rather than a fix for a present
    problem -- but it costs nothing and it is not a thing anyone would think
    to add later, after the duplicate emails had already gone out.

    Ordered oldest-first so a backlog drains in the order it was decided.

    Args:
        conn: Transaction the claim and the subsequent status write share.
        limit: Maximum rows to take in this pass.

    Returns:
        One mapping per claimed alert, carrying everything needed to send it.
    """
    result = await conn.execute(
        text(
            """
            SELECT o.id, o.subject, o.body, o.attempts, r.email
            FROM alert_outbox o
            JOIN alert_recipients r ON r.id = o.recipient_id
            WHERE o.status = 'PENDING'
            ORDER BY o.decided_at
            LIMIT :limit
            FOR UPDATE OF o SKIP LOCKED
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings()]


async def mark_alert_sent(conn: AsyncConnection, alert_id: str, sent_at: datetime) -> None:
    """Record that one alert reached the relay.

    Args:
        conn: Transaction holding the claim on this row.
        alert_id: Outbox row to close out.
        sent_at: Simulated time of delivery, on the same clock as `decided_at`.
    """
    await conn.execute(
        text(
            """
            UPDATE alert_outbox
            SET status = 'SENT', attempts = attempts + 1, sent_at = :sent_at,
                last_error = NULL
            WHERE id = :id
            """
        ),
        {"id": alert_id, "sent_at": sent_at},
    )


async def record_alert_failure(
    conn: AsyncConnection, alert_id: str, error: str, max_attempts: int
) -> bool:
    """Record a failed send, retiring the row once it has been tried enough.

    The row stays PENDING while attempts remain, so the next pass retries it.
    At the cap it becomes FAILED and is never retried again -- see
    `settings.alert_max_attempts` for why that bound exists rather than an
    unbounded backoff.

    `last_error` is stored, not just logged, because the operator UI is where
    a failed alert has to be visible. An alert nobody knows was supposed to
    arrive is the failure this whole table exists to prevent, and "check the
    backend logs" does not prevent it.

    Args:
        conn: Transaction holding the claim on this row.
        alert_id: Outbox row that failed.
        error: Short description of the transport failure.
        max_attempts: Attempts allowed before the row is retired.

    Returns:
        True if this failure retired the row.
    """
    result = await conn.execute(
        text(
            """
            UPDATE alert_outbox
            SET attempts = attempts + 1,
                last_error = :error,
                status = CASE
                    WHEN attempts + 1 >= :max_attempts THEN 'FAILED'
                    ELSE 'PENDING'
                END
            WHERE id = :id
            RETURNING status
            """
        ),
        {"id": alert_id, "error": error[:500], "max_attempts": max_attempts},
    )
    row = result.mappings().first()
    return row is not None and row["status"] == "FAILED"


async def last_alert_per_rule(conn: AsyncConnection, facility_id: str) -> dict:
    """Return the most recent alert decided for each rule at this facility.

    The outbox IS the cooldown state -- there is no separate "last notified"
    column to drift out of agreement with it (migration 0012). One row per
    rule is enough: every recipient of one firing shares its `decided_at`, so
    the newest row for a rule dates the whole firing.

    Args:
        conn: Request-scoped transaction.
        facility_id: Facility being evaluated.

    Returns:
        Rule id mapped to that rule's most recent level and decision time.
    """
    result = await conn.execute(
        text(
            """
            SELECT DISTINCT ON (rule_id) rule_id, level, decided_at
            FROM alert_outbox
            WHERE facility_id = :facility_id
            ORDER BY rule_id, decided_at DESC
            """
        ),
        {"facility_id": facility_id},
    )
    return {
        row["rule_id"]: LastAlert(
            level=SurgeRiskLevel(row["level"]), decided_at=row["decided_at"]
        )
        for row in result.mappings()
    }


async def queue_alert(
    conn: AsyncConnection,
    rule_id: str,
    facility_id: str,
    recipient_id: str,
    level: SurgeRiskLevel,
    subject: str,
    body: str,
    decided_at: datetime,
) -> str:
    """Commit one alert for one recipient, for the dispatcher to send later.

    Writing the decision down before anything touches SMTP is the whole point
    of the outbox: a process that dies here loses nothing, because the row is
    already PENDING and the next drain picks it up.

    Args:
        conn: Transaction the whole evaluation shares, so a facility either
            queues all of its alerts or none of them.
        rule_id: Catalog rule that fired.
        facility_id: Facility the rule fired for.
        recipient_id: Person this copy is addressed to.
        level: Severity the rule declares.
        subject: Rendered subject line.
        body: Rendered message body.
        decided_at: Simulated time of the decision.

    Returns:
        The new outbox row's id.
    """
    alert_id = f"alert-{uuid4().hex}"
    await conn.execute(
        text(
            """
            INSERT INTO alert_outbox (
                id, rule_id, facility_id, recipient_id, level, subject, body,
                decided_at
            ) VALUES (
                :id, :rule_id, :facility_id, :recipient_id, :level, :subject,
                :body, :decided_at
            )
            """
        ),
        {
            "id": alert_id,
            "rule_id": rule_id,
            "facility_id": facility_id,
            "recipient_id": recipient_id,
            "level": level.value,
            "subject": subject,
            "body": body,
            "decided_at": decided_at,
        },
    )
    return alert_id


async def list_recent_alerts(conn: AsyncConnection, limit: int) -> list[dict]:
    """Return the most recently decided alerts, newest first.

    Joined to recipients so the UI can name who each copy was for, and
    LEFT joined so a deactivated recipient's history still appears -- that is
    the whole reason removal is a soft delete (migration 0012).

    Args:
        conn: Request-scoped transaction.
        limit: Maximum rows to return.

    Returns:
        One mapping per alert, carrying its delivery state.
    """
    result = await conn.execute(
        text(
            """
            SELECT o.id, o.rule_id, o.facility_id, o.level, o.subject,
                   o.status, o.attempts, o.last_error, o.decided_at, o.sent_at,
                   r.name AS recipient_name, r.email AS recipient_email
            FROM alert_outbox o
            LEFT JOIN alert_recipients r ON r.id = o.recipient_id
            ORDER BY o.decided_at DESC, o.id
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings()]
