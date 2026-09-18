"""Throughput must be measured from what actually happened, not reported by the producer.

`FulfillmentSnapshotEvent.work_units_completed_last_hour` required a
producer to compute and send its own throughput number -- no real WMS emits
that metric; real WMSs emit discrete pick/pack/ship confirmations per order,
which this backend already receives as status-transition events and, until
now, threw away everything except the order's current status.

These cover deriving throughput from the transition history instead: a
status change now leaves a record with its own work-unit cost snapshotted at
the moment it happened, and `get_throughput_signal` prefers a derived rate
over a producer-reported one, and a producer-reported one over the
configured fallback -- so an operator can tell which kind of number they are
looking at.

Honesty caveat, not a defect: deriving throughput as `work_units / 4` per
lifecycle step agrees with the simulator by construction, since both commit
to the same four-step canonical lifecycle (PENDING -> PICKING -> PACKED ->
READY -> DISPATCHED). That agreement is not evidence the model would hold
against a real WMS with a different pick/pack process -- it is evidence the
two sides of this codebase describe the same lifecycle.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.main import app
from app.models import ORDER_LIFECYCLE
from app.repository import get_throughput_signal
from app.scheduler import OperatingCalendar
from tests.conftest import TEST_SERVICE_TOKEN
from tests.helpers import sign_in

FACILITY = "WH-01"


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    asyncio.run(engine.dispose())


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


async def _seed_order(order_id: str, *, work_units: float) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO orders (order_id, facility_id, created_at, promised_dispatch_at,
                                item_count, work_units, order_value, status)
            VALUES ($1, $2, NOW(), NOW() + INTERVAL '24 hours', 1, $3, $4, 'PENDING')
            ON CONFLICT (order_id) DO UPDATE SET work_units = EXCLUDED.work_units
            """,
            order_id,
            FACILITY,
            work_units,
            Decimal(0),
        )
    finally:
        await conn.close()


async def _record_transition(
    order_id: str, *, from_status: str | None, to_status: str, occurred_at: datetime, work_units: float
) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO order_status_transitions
                (order_id, facility_id, from_status, to_status, occurred_at,
                 event_id, work_units, origin)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'event')
            """,
            order_id,
            FACILITY,
            from_status,
            to_status,
            occurred_at,
            f"txn-{order_id}-{to_status}",
            work_units,
        )
    finally:
        await conn.close()


async def _clean_up(*order_ids: str) -> None:
    """Clear this test's own orders, and ALL of WH-01's transition history.

    Not just the synthetic orders created here: derived throughput sums every
    transition for the facility in the last hour, so a real order advanced by
    an unrelated test file run in the same session -- test_events_integration,
    test_interventions, and others all use WH-01 -- would otherwise leak into
    what this test measures. Safe because these tests own the derived-rate
    contract, not any specific transition; every other suite that cares about
    a specific throughput figure already clears this table itself (see the
    same fix applied to their own clean_tables fixtures).
    """
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "DELETE FROM order_status_transitions WHERE facility_id = $1", FACILITY
        )
        for order_id in order_ids:
            await conn.execute("DELETE FROM orders WHERE order_id = $1", order_id)
    finally:
        await conn.close()


@pytest.mark.usefixtures("require_database")
def test_a_status_transition_leaves_a_record_of_the_work_it_cost():
    """The step's cost is recorded, not merely the fact that a step happened.

    Recorded via _apply_status_transition, exercised through the events
    router in test_out_of_order_status.py and test_events_integration.py;
    this asserts the resulting row directly.
    """
    from app.models import OrderStatus
    from app.routers.events import _apply_status_transition

    order_id = "THR-RECORD-0001"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, work_units=4.0))

    async def _scenario():
        async with engine.begin() as conn:
            await _apply_status_transition(
                conn,
                order_id=order_id,
                facility_id=FACILITY,
                current_status=OrderStatus.PENDING,
                target_status=OrderStatus.PICKING,
                occurred_at=datetime.now(UTC),
                event_id="txn-record-test",
                order_work_units=4.0,
            )
            result = await conn.execute(
                text(
                    "SELECT from_status, to_status, work_units FROM order_status_transitions "
                    "WHERE order_id = :order_id"
                ),
                {"order_id": order_id},
            )
            return result.first()

    row = _run(_scenario())
    assert row is not None
    assert row.from_status == "PENDING"
    assert row.to_status == "PICKING"
    # One of four lifecycle steps: a quarter of the order's total cost.
    assert row.work_units == pytest.approx(1.0)

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_throughput_is_derived_from_recent_transitions_when_there_are_enough():
    """A busy floor's rate comes from what it actually did, not a producer claim."""
    order_id = "THR-DERIVE-0001"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, work_units=4.0))
    now = datetime.now(UTC)

    for i, status in enumerate(ORDER_LIFECYCLE[1:], start=1):
        _run(
            _record_transition(
                order_id,
                from_status=ORDER_LIFECYCLE[i - 1].value,
                to_status=status.value,
                occurred_at=now - timedelta(minutes=10 * i),
                work_units=1.0,
            )
        )

    async def _measure():
        async with engine.begin() as conn:
            return await get_throughput_signal(conn, FACILITY, 52.0, now)

    signal = _run(_measure())

    assert signal.source == "derived"
    # Four steps of 1.0 work unit each inside the last hour.
    assert signal.observed_work_units_per_hour == pytest.approx(4.0)

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_a_lone_transition_is_not_enough_to_derive_a_rate():
    """One data point is noise, not a measurement.

    Falls back to the configured capacity (no fulfillment_snapshots row
    seeded here) rather than reporting a rate extrapolated from a single
    event.
    """
    order_id = "THR-SPARSE-0001"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, work_units=4.0))
    now = datetime.now(UTC)

    _run(
        _record_transition(
            order_id,
            from_status="PENDING",
            to_status="PICKING",
            occurred_at=now - timedelta(minutes=5),
            work_units=1.0,
        )
    )

    async def _measure():
        async with engine.begin() as conn:
            return await get_throughput_signal(conn, FACILITY, 52.0, now)

    signal = _run(_measure())

    assert signal.source == "configured"
    assert signal.observed_work_units_per_hour is None

    _run(_clean_up(order_id))


# --- a stalled floor must not read as a healthy one ---------------------------


async def _clear_snapshots() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM fulfillment_snapshots WHERE facility_id = $1", FACILITY)
    finally:
        await conn.close()


async def _clear_pending_orders() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "DELETE FROM orders WHERE facility_id = $1 AND status = 'PENDING'", FACILITY
        )
    finally:
        await conn.close()


async def _record_snapshot(occurred_at: datetime, work_units: float) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "INSERT INTO fulfillment_snapshots (facility_id, occurred_at, open_orders, "
            "work_units_completed_last_hour) VALUES ($1, $2, 1, $3)",
            FACILITY,
            occurred_at,
            work_units,
        )
    finally:
        await conn.close()


def _busy_earlier(order_id: str, now: datetime) -> None:
    """Four transitions two to three hours ago, and none since."""
    for i in range(4):
        _run(
            _record_transition(
                f"{order_id}-{i}" if i else order_id,
                from_status="PENDING",
                to_status="PICKING",
                occurred_at=now - timedelta(hours=2, minutes=15 * i),
                work_units=1.0,
            )
        )


def _signal(now: datetime, calendar=None):
    async def _measure():
        async with engine.begin() as conn:
            return await get_throughput_signal(conn, FACILITY, 52.0, now, calendar)

    return _run(_measure())


@pytest.mark.usefixtures("require_database")
def test_a_floor_that_went_quiet_with_orders_waiting_is_stalled():
    """It was working two hours ago and nothing has moved since: that is a stall."""
    order_id = "THR-STALL-0001"
    _run(_clean_up(order_id))
    _run(_clear_snapshots())
    _run(_seed_order(order_id, work_units=4.0))
    now = datetime.now(UTC)
    _busy_earlier(order_id, now)

    signal = _signal(now)

    assert signal.stalled
    assert signal.reported_work_units_per_hour == 0.0
    # Still schedulable: the queue needs a positive capacity to predict at all.
    assert signal.scheduling_capacity_per_hour == 52.0

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_a_facility_that_never_reported_is_not_stalled():
    """No history is not evidence of a stall, only of no telemetry yet."""
    order_id = "THR-NEW-0001"
    _run(_clean_up(order_id))
    _run(_clear_snapshots())
    _run(_seed_order(order_id, work_units=4.0))

    signal = _signal(datetime.now(UTC))

    assert signal.source == "configured"
    assert not signal.stalled
    assert signal.reported_work_units_per_hour == 52.0

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_a_quiet_floor_with_nothing_waiting_is_not_stalled():
    """Finishing the work and going quiet is what a healthy floor does."""
    order_id = "THR-DONE-0001"
    _run(_clean_up(order_id))
    _run(_clear_snapshots())
    # "Nothing waiting" is about the whole facility, so a pending order left by
    # any other test in WH-01 would make this a stall.
    _run(_clear_pending_orders())
    now = datetime.now(UTC)
    _busy_earlier(order_id, now)

    assert not _signal(now).stalled

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_a_closed_floor_is_not_stalled():
    """Nothing moves overnight because nobody is there, not because the floor broke."""
    order_id = "THR-CLOSED-0001"
    _run(_clean_up(order_id))
    _run(_clear_snapshots())
    _run(_seed_order(order_id, work_units=4.0))
    now = datetime.now(UTC)
    _busy_earlier(order_id, now)
    minute = now.replace(second=0, microsecond=0)
    closed_now = OperatingCalendar(
        opens_at=(minute + timedelta(hours=1)).time(),
        closes_at=(minute + timedelta(hours=2)).time(),
    )

    assert not _signal(now, closed_now).stalled

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_a_fresh_snapshot_reporting_zero_is_stalled():
    """A producer saying "nothing done this hour" is a measured stall, not a gap."""
    order_id = "THR-ZERO-0001"
    _run(_clean_up(order_id))
    _run(_clear_snapshots())
    _run(_seed_order(order_id, work_units=4.0))
    now = datetime.now(UTC)
    _run(_record_snapshot(now - timedelta(minutes=2), 0.0))

    signal = _signal(now)

    assert signal.source == "reported"
    assert signal.stalled
    assert signal.reported_work_units_per_hour == 0.0

    _run(_clear_snapshots())
    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_the_dashboard_shows_a_stall_instead_of_configured_capacity():
    """The operator sees the stall: its source, zero throughput, and raised risk."""
    order_id = "THR-DASH-0001"
    _run(_clean_up(order_id))
    _run(_clear_snapshots())
    _run(_seed_order(order_id, work_units=4.0))
    _busy_earlier(order_id, datetime.now(UTC))

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, "throughput-tests@example.com")
        dashboard = client.get("/api/dashboard", params={"facility_id": FACILITY}).json()

    assert dashboard["throughput_stalled"] is True
    assert dashboard["throughput_source"] == "configured"
    assert dashboard["fulfillment_work_units_per_hour"] == 0.0
    assert dashboard["risk_level"] in ("HIGH", "CRITICAL")

    _run(_clean_up(order_id))
