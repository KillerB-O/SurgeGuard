"""The alert worker's tick.

The evaluator and dispatcher are tested separately. What is only true of the
worker is that one tick joins them end to end, that a failing tick does not
kill the loop, and that it refreshes the cached module state a second process
would otherwise pin forever.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from app import clock
from app.alerts import repository, worker
from app.alerts.rules import load_rules
from app.db import engine
from app.models import SurgeRiskLevel

pytestmark = pytest.mark.usefixtures("require_database")

FACILITY = "WH-01"


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared engine's pool around each test.

    Before AND after: disposing only afterwards leaves the FIRST test in this
    file using whatever pool the previously-run test file left behind, which
    fails only in a full-suite run. See `test_alert_dispatcher.py`.
    """
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _reset(conn) -> None:
    """Clear alert state. Outbox first: it references recipients.

    P6: run_once now also runs a verification pass over every facility's
    capacity_commitments, so a row left by an unrelated test file (or an
    earlier run in this same file) would have its own tick resolve it --
    the same cross-file leak P4 hit with order_status_transitions, applied
    here to verification instead of dashboard figures. capacity_commitments
    first: it references recovery_actions.
    """
    await conn.execute(text("DELETE FROM alert_outbox"))
    await conn.execute(text("DELETE FROM alert_recipients"))
    await conn.execute(text("DELETE FROM capacity_commitments"))
    await conn.execute(text("DELETE FROM recovery_actions"))


async def _breach(conn, count: int = 2) -> None:
    """Insert orders already past their promised dispatch time."""
    now = datetime.now(UTC)
    for index in range(count):
        await conn.execute(
            text(
                """
                INSERT INTO orders (
                    order_id, facility_id, created_at, promised_dispatch_at,
                    item_count, work_units, order_value, status
                ) VALUES (
                    :order_id, :facility_id, :created_at, :promised, 1, 5.0,
                    :value, 'PENDING'
                )
                """
            ),
            {
                "order_id": f"worker-{uuid4().hex[:10]}-{index}",
                "facility_id": FACILITY,
                "created_at": now - timedelta(hours=8),
                "promised": now - timedelta(hours=2),
                "value": Decimal("25.00"),
            },
        )


async def test_one_tick_queues_and_then_sends():
    """End to end: the evaluation pass queues, the delivery pass drains, and
    both happen within a single tick."""
    async with engine.begin() as conn:
        await _reset(conn)
        await _breach(conn)
        await repository.add_recipient(
            conn, "head", f"head-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.HIGH
        )

    queued, drained = await worker.run_once(engine)

    assert queued > 0
    assert drained.sent == queued

    async with engine.begin() as conn:
        pending = await conn.execute(
            text("SELECT count(*) FROM alert_outbox WHERE status = 'PENDING'")
        )
        assert pending.scalar_one() == 0


async def test_the_second_tick_is_quiet():
    """Cooldown holds across ticks. Without it the worker would re-alert every
    15 seconds for as long as the breach persisted."""
    async with engine.begin() as conn:
        await _reset(conn)
        await _breach(conn)
        await repository.add_recipient(
            conn, "head", f"head-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.HIGH
        )

    first, _ = await worker.run_once(engine)
    second, _ = await worker.run_once(engine)

    assert first > 0
    assert second == 0


async def test_a_tick_reloads_the_rule_catalog(monkeypatch):
    """`load_rules` is lru_cached, so without a per-tick clear an operator's
    edit to alert_rules.json would need a process restart -- making "rules are
    data, no deploy" only half true.

    Asserted by spying on the clear rather than on `cache_info()`, because
    `cache_clear()` resets the hit/miss counters, so comparing them across it
    always reads as "nothing happened".
    """
    cleared = {"count": 0}

    class _SpyingLoader:
        """Delegates to the real loader, counting cache clears."""

        def __call__(self):
            return load_rules()

        def cache_clear(self) -> None:
            cleared["count"] += 1
            load_rules.cache_clear()

    monkeypatch.setattr(worker, "load_rules", _SpyingLoader())

    await worker.run_once(engine)

    assert cleared["count"] == 1


async def test_a_tick_refreshes_the_demo_clock():
    """The clock cache is refreshed only by start()/stop(), both of which run
    in the API process. A worker that never invalidated would pin its anchors
    on the first tick and drift away from the dashboard's clock."""
    async with engine.begin() as conn:
        await clock.state(conn)
    assert clock._loaded is True

    clock.invalidate()

    assert clock._loaded is False

    await worker.run_once(engine)

    # The tick invalidates, then reads again, so the worker is always working
    # from the row as it stands rather than its first-ever observation.
    assert clock._loaded is True


async def test_a_failing_tick_does_not_stop_the_loop(monkeypatch, caplog):
    """A crashed worker and a quiet warehouse look identical from outside, so
    one bad tick must never end the loop."""
    calls = {"n": 0}

    async def _explode_once(_db):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", _explode_once)

    with pytest.raises(asyncio.CancelledError):
        await worker.run_forever(engine, tick_seconds=0)

    assert calls["n"] == 2
    assert "tick failed" in caplog.text
