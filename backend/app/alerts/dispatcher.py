"""Drain the alert outbox: turn decided alerts into sent email, or into a
recorded failure.

The counterpart to `app/auth/router.py`'s `background_tasks.add_task(deliver,
...)`. That shape is right for a verification link -- the user can always
request another -- and wrong for an alert, because nobody knows an alert was
supposed to arrive. So the decision is committed to `alert_outbox` first and
this module is what closes the row out, one way or the other.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from app.alerts import repository
from app.auth.mailer import build_message, send_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DrainResult:
    """What one dispatcher pass did, for logging and for tests."""

    sent: int = 0
    retrying: int = 0
    failed: int = 0

    @property
    def attempted(self) -> int:
        """Return how many rows this pass touched."""
        return self.sent + self.retrying + self.failed


async def drain_outbox(
    conn: AsyncConnection, now: datetime, max_attempts: int, batch_size: int
) -> DrainResult:
    """Send every pending alert this pass can claim.

    One failure never stops the pass. Each row is independent -- that is the
    reason migration 0012 stores one row per recipient rather than one per
    firing -- so a bad address retires only its own row and everybody else on
    the list still gets the email.

    `send_message` is blocking `smtplib` with no `await` points, so it is
    handed to a thread rather than awaited on this loop. Calling it directly
    here would stall the whole event loop for the SMTP round trip, which is
    the same trap `app/auth/mailer.py`'s docstring warns about for
    `BackgroundTask`.

    Args:
        conn: Transaction the claim and the status writes share, so a crash
            mid-pass rolls back to PENDING rather than losing the alert.
        now: Simulated time, on the same clock as `decided_at`.
        max_attempts: Attempts a row gets before it is retired as FAILED.
        batch_size: Maximum rows to claim in this pass.

    Returns:
        Counts of what was sent, left for retry, and retired.
    """
    claimed = await repository.claim_pending_alerts(conn, batch_size)
    sent = retrying = failed = 0

    for alert in claimed:
        message = build_message(
            to=alert["email"], subject=alert["subject"], body=alert["body"]
        )
        try:
            await asyncio.to_thread(send_message, message)
        except Exception as exc:  # noqa: BLE001 -- one bad send must not abort the whole batch
            retired = await repository.record_alert_failure(
                conn, alert["id"], f"{type(exc).__name__}: {exc}", max_attempts
            )
            if retired:
                failed += 1
                logger.error(
                    "alert %s to %s retired after %d attempts: %s",
                    alert["id"],
                    alert["email"],
                    max_attempts,
                    exc,
                )
            else:
                retrying += 1
                logger.warning(
                    "alert %s to %s failed, will retry: %s",
                    alert["id"],
                    alert["email"],
                    exc,
                )
        else:
            await repository.mark_alert_sent(conn, alert["id"], now)
            sent += 1

    return DrainResult(sent=sent, retrying=retrying, failed=failed)
