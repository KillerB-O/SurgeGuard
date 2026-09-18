"""Draining the alert outbox.

The point of the outbox is that a send failure is recorded rather than lost,
so these tests care mostly about what happens when delivery does NOT work:
a failure retries until its cap and then retires, and one bad address never
suppresses anybody else's alert.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.alerts import dispatcher, repository
from app.db import engine
from app.models import SurgeRiskLevel

pytestmark = pytest.mark.usefixtures("require_database")

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared engine's pool around each test.

    Same reason as `test_auth.py`'s identical fixture: each test runs on its
    own event loop, and `app.db.engine` pools connections tied to whichever
    loop last used it, so a test would otherwise reuse a connection belonging
    to a closed loop.

    Disposed BEFORE as well as after. Disposing only afterwards protects the
    next test in this file but not the first one, which inherits whatever
    pool the previously-run test FILE left behind -- a failure that shows up
    only in a full-suite run, never when the file is run alone.
    """
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _recipient(conn, local: str) -> str:
    """Add a recipient unique to this run and return its id."""
    row = await repository.add_recipient(
        conn, local, f"{local}-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.HIGH
    )
    return row["id"]


async def _queue(conn, recipient_id: str) -> str:
    """Put one PENDING alert in the outbox and return its id."""
    alert_id = f"alert-{uuid4().hex}"
    await conn.execute(
        text(
            """
            INSERT INTO alert_outbox (
                id, rule_id, facility_id, recipient_id, level, subject, body,
                decided_at
            ) VALUES (
                :id, 'surge', 'WH-01', :recipient_id, 'HIGH', 'Surge', 'Body',
                :decided_at
            )
            """
        ),
        {"id": alert_id, "recipient_id": recipient_id, "decided_at": NOW},
    )
    return alert_id


async def _status(conn, alert_id: str) -> dict:
    """Return the outbox row's delivery state."""
    result = await conn.execute(
        text(
            "SELECT status, attempts, last_error, sent_at FROM alert_outbox WHERE id = :id"
        ),
        {"id": alert_id},
    )
    return dict(result.mappings().one())


async def test_a_deliverable_alert_is_marked_sent():
    """SMTP is unconfigured in tests, so the console backend is the transport
    (D-05). It must count as a real delivery, not a skipped one."""
    async with engine.begin() as conn:
        alert_id = await _queue(conn, await _recipient(conn, "sent"))

        result = await dispatcher.drain_outbox(conn, NOW, max_attempts=3, batch_size=10)

        assert result.sent >= 1
        row = await _status(conn, alert_id)
        assert row["status"] == "SENT"
        assert row["attempts"] == 1
        assert row["sent_at"] == NOW


async def test_a_failing_alert_stays_pending_until_its_attempts_run_out(monkeypatch):
    """Retries are bounded: Brevo's free tier is 300 emails a day, shared with
    signup and reset, so an unbounded retry would eat the allowance."""

    def _explode(_message):
        raise OSError("relay refused")

    monkeypatch.setattr(dispatcher, "send_message", _explode)

    async with engine.begin() as conn:
        alert_id = await _queue(conn, await _recipient(conn, "failing"))

        first = await dispatcher.drain_outbox(conn, NOW, max_attempts=2, batch_size=10)

        assert first.retrying == 1
        row = await _status(conn, alert_id)
        assert row["status"] == "PENDING"
        assert row["attempts"] == 1
        assert "relay refused" in row["last_error"]

        second = await dispatcher.drain_outbox(conn, NOW, max_attempts=2, batch_size=10)

        assert second.failed == 1
        retired = await _status(conn, alert_id)
        assert retired["status"] == "FAILED"
        assert retired["attempts"] == 2


async def test_a_retired_alert_is_not_claimed_again(monkeypatch):
    """FAILED is terminal. A row that keeps being retried forever is the
    budget leak the attempt cap exists to prevent."""

    def _explode(_message):
        raise OSError("nope")

    monkeypatch.setattr(dispatcher, "send_message", _explode)

    async with engine.begin() as conn:
        alert_id = await _queue(conn, await _recipient(conn, "retired"))
        await dispatcher.drain_outbox(conn, NOW, max_attempts=1, batch_size=10)
        assert (await _status(conn, alert_id))["status"] == "FAILED"

        again = await dispatcher.drain_outbox(conn, NOW, max_attempts=1, batch_size=10)

        assert again.attempted == 0


async def test_one_bad_address_does_not_suppress_the_others(monkeypatch):
    """Why migration 0012 stores one row per recipient rather than one per
    firing: a shared row would fail as a unit and silence the whole list."""
    async with engine.begin() as conn:
        bad_recipient = await _recipient(conn, "bad")
        good_recipient = await _recipient(conn, "good")
        bad = await _queue(conn, bad_recipient)
        good = await _queue(conn, good_recipient)

        bad_email = (
            await conn.execute(
                text("SELECT email FROM alert_recipients WHERE id = :id"),
                {"id": bad_recipient},
            )
        ).scalar_one()

        def _selective(message):
            if message["To"] == bad_email:
                raise OSError("mailbox unavailable")

        monkeypatch.setattr(dispatcher, "send_message", _selective)

        result = await dispatcher.drain_outbox(conn, NOW, max_attempts=1, batch_size=10)

        assert result.sent >= 1
        assert (await _status(conn, good))["status"] == "SENT"
        assert (await _status(conn, bad))["status"] == "FAILED"
