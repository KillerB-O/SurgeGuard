"""Evaluating alert rules against real facility state.

`test_alert_rules.py` already pins the fire/don't-fire decision without a
database. What needs a database is everything around it: that the metrics
come off the same schedule the dashboard reads, that a firing fans out to one
outbox row per matching recipient, and that severity routing actually filters.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.alerts import evaluator, repository
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


async def _clear_alert_state(conn) -> None:
    """Start from no recipients and no alert history.

    The outbox IS the cooldown state, so a row left by an earlier test would
    suppress the very rule the next test is trying to observe. Outbox rows go
    first: they reference recipients with ON DELETE RESTRICT.
    """
    await conn.execute(text("DELETE FROM alert_outbox"))
    await conn.execute(text("DELETE FROM alert_recipients"))


async def _breach_some_orders(conn, count: int) -> None:
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
                "order_id": f"alert-eval-{uuid4().hex[:10]}-{index}",
                "facility_id": FACILITY,
                "created_at": now - timedelta(hours=8),
                "promised": now - timedelta(hours=2),
                "value": Decimal("25.00"),
            },
        )


async def _outbox_rows(conn) -> list[dict]:
    """Return every queued alert."""
    result = await conn.execute(
        text("SELECT rule_id, recipient_id, level, subject FROM alert_outbox")
    )
    return [dict(row) for row in result.mappings()]


async def test_a_breach_queues_one_alert_per_matching_recipient():
    """One row per recipient is what lets a single bad address fail alone."""
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await _breach_some_orders(conn, 2)
        for name in ("head", "lead"):
            await repository.add_recipient(
                conn, name, f"{name}-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.HIGH
            )

        result = await evaluator.evaluate_facility(conn, FACILITY)

        assert "promises-already-missed" in result.rules_fired
        rows = await _outbox_rows(conn)
        breach_rows = [r for r in rows if r["rule_id"] == "promises-already-missed"]
        assert len({r["recipient_id"] for r in breach_rows}) == 2


async def test_a_second_pass_inside_the_cooldown_queues_nothing_new():
    """The outbox is the cooldown state. Without this the evaluator would
    re-queue on every tick for as long as the breach persists."""
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await _breach_some_orders(conn, 2)
        await repository.add_recipient(
            conn, "head", f"head-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.HIGH
        )

        first = await evaluator.evaluate_facility(conn, FACILITY)
        second = await evaluator.evaluate_facility(conn, FACILITY)

        assert first.alerts_queued > 0
        assert second.alerts_queued == 0


async def test_a_recipient_above_the_rule_level_is_not_emailed():
    """Severity routing: the ops head takes CRITICAL only, so a HIGH rule
    must not reach them."""
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await _breach_some_orders(conn, 2)
        critical_only = await repository.add_recipient(
            conn, "head", f"head-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.CRITICAL
        )

        await evaluator.evaluate_facility(conn, FACILITY)

        rows = await _outbox_rows(conn)
        high_rows = [r for r in rows if r["level"] == "HIGH"]
        assert critical_only["id"] not in {r["recipient_id"] for r in high_rows}


async def test_nothing_is_queued_when_the_list_is_empty():
    """A rule that fires with no addressee must write NOTHING. A row with
    nobody to send it to would sit PENDING forever and, because the outbox is
    the cooldown state, silently gag the rule for the whole window."""
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await _breach_some_orders(conn, 2)

        result = await evaluator.evaluate_facility(conn, FACILITY)

        assert result.alerts_queued == 0
        assert await _outbox_rows(conn) == []


async def test_metrics_match_the_schedule_the_dashboard_reads():
    """The anti-drift property: alerts and the screen share one calculation."""
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await _breach_some_orders(conn, 3)

        metrics = await evaluator.collect_metrics(conn, FACILITY)

        assert metrics.facility_id == FACILITY
        assert metrics.breached_count >= 3
        assert metrics.risk_level in tuple(SurgeRiskLevel)


async def test_an_unknown_facility_is_skipped_rather_than_raising():
    """An evaluator tick that raised would take the whole worker loop down."""
    async with engine.begin() as conn:
        result = await evaluator.evaluate_facility(conn, "WH-DOES-NOT-EXIST")

        assert result.alerts_queued == 0
        assert result.rules_fired == ()


async def test_a_facility_gone_quiet_is_reported_as_stale():
    """The payoff for running alerting in a worker instead of on the request
    path: this fires BECAUSE nothing arrived, which a handler triggered by
    arrivals could never notice.

    It also contradicts the dashboard on purpose. With no fresh telemetry,
    `get_throughput_signal` falls back to the configured capacity, so the risk
    level looks better than the floor actually is.
    """
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await conn.execute(
            text("DELETE FROM fulfillment_snapshots WHERE facility_id = :f"),
            {"f": FACILITY},
        )
        await conn.execute(
            text(
                """
                INSERT INTO fulfillment_snapshots (
                    facility_id, occurred_at, open_orders,
                    work_units_completed_last_hour
                ) VALUES (:f, :occurred_at, 5, 40.0)
                """
            ),
            {"f": FACILITY, "occurred_at": datetime.now(UTC) - timedelta(hours=3)},
        )

        metrics = await evaluator.collect_metrics(conn, FACILITY)

        assert metrics.snapshot_age_minutes is not None
        assert metrics.snapshot_age_minutes > 20

        await repository.add_recipient(
            conn, "ops", f"ops-{uuid4().hex[:8]}@example.com", SurgeRiskLevel.HIGH
        )
        result = await evaluator.evaluate_facility(conn, FACILITY)

        assert "telemetry-gone-quiet" in result.rules_fired


async def test_a_facility_that_never_reported_is_not_reported_as_stale():
    """Never started is not the same as stopped. Alerting here would fire on
    every fresh database and every demo reset."""
    async with engine.begin() as conn:
        await _clear_alert_state(conn)
        await conn.execute(
            text("DELETE FROM fulfillment_snapshots WHERE facility_id = :f"),
            {"f": FACILITY},
        )

        metrics = await evaluator.collect_metrics(conn, FACILITY)

        assert metrics.snapshot_age_minutes is None
