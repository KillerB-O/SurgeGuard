"""CANCELLED must become a real, honest order status (P8).

Migration 0003 deliberately kept CANCELLED out of `orders_status_lifecycle`:
at the time nothing in the codebase gave it transition semantics, so
accepting the value would have let an order sit in a status nothing else
understood. `OrderStatusUpdatedEvent.status` has always typed as `OrderStatus`
(CANCELLED included), so the gap was purely in `_require_valid_transition`
and the database CHECK constraint -- no new endpoint is needed, cancellation
travels through the same `POST /events/order-status` every other status
change does.

Three things make this honest rather than a bare status flip:

- cancelling a DISPATCHED order is refused (409) -- it already left, there is
  nothing left to cancel;
- the resulting transition is recorded with `work_units=0.0`, not whatever
  `work_units_for_step` would silently compute if fed a `None` end position
  (`stage_weights[from:None]` slices to the end and overcharges a cancelled
  order for work it never finished -- see repository.py's forward-jump
  charging and work_units.py's `work_units_for_step`);
- `count_late_dispatches` stops counting a cancelled order as a missed
  promise (it was never going to ship), without changing how many orders
  the window says were placed -- the plan's literal wording is "keep it out
  of the late-dispatch numerator", not "exclude entirely", so `placed` is
  deliberately left untouched here.

`OrderTotals.committed_work_units`/`backlog_orders` need no code change at
all: `BACKLOG_STATUSES` is an explicit allowlist (PENDING, PICKING, PACKED,
READY) that never included CANCELLED, so a cancelled order's committed work
is released automatically the moment its status flips -- covered here as a
regression guard, not a new mechanism.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import engine
from app.main import app
from app.models import OrderStatus, lifecycle_position
from app.repository import count_late_dispatches, get_order_totals
from app.work_units import load_rules, remaining_work_fraction
from tests.conftest import TEST_SERVICE_TOKEN

FACILITY = "WH-01"


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    asyncio.run(engine.dispose())


async def _clean_facility() -> None:
    """Wipe every WH-01 order and transition, not just this test's own ids.

    `count_late_dispatches` and `get_order_totals` both aggregate across the
    *whole facility* with no per-test scoping of their own, so a leftover
    order from a completely different test file (test_backfill.py's
    `clean_tables` fixture only cleans at the START of each of its own
    tests, leaving its last test's rows in place once that file finishes)
    silently inflates a count here. This is the same class of leak the plan
    file already records for P4 (order_status_transitions) and P6
    (recovery_actions/capacity_commitments): a facility-wide aggregate reader
    needs a facility-wide cleanup, not an allowlist of ids this file happens
    to know about. Runs before AND after each test (not just before) so this
    file cannot leave the same trap for whichever test module runs next.
    """
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM order_status_transitions WHERE facility_id = $1", FACILITY)
        await conn.execute("DELETE FROM orders WHERE facility_id = $1", FACILITY)
        await conn.execute("DELETE FROM processed_events")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean_facility_orders():
    asyncio.run(_clean_facility())
    yield
    asyncio.run(_clean_facility())


async def _clean_up(*order_ids: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM orders WHERE order_id = ANY($1::text[])", list(order_ids))
        await conn.execute(
            "DELETE FROM order_status_transitions WHERE order_id = ANY($1::text[])", list(order_ids)
        )
        await conn.execute("DELETE FROM processed_events")
    finally:
        await conn.close()


async def _seed_order(
    order_id: str,
    *,
    status: str = "PENDING",
    work_units: float = 4.0,
    created_at: datetime | None = None,
    promised_dispatch_at: datetime | None = None,
    dispatched_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO orders (order_id, facility_id, created_at, promised_dispatch_at,
                                item_count, work_units, order_value, status, dispatched_at)
            VALUES ($1, $2, $3, $4, 1, $5, 0, $6, $7)
            ON CONFLICT (order_id) DO UPDATE SET status = EXCLUDED.status
            """,
            order_id,
            FACILITY,
            created_at or (now - timedelta(hours=2)),
            promised_dispatch_at or (now + timedelta(hours=24)),
            work_units,
            status,
            dispatched_at,
        )
    finally:
        await conn.close()


async def _order_row(order_id: str):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchrow(
            "SELECT status, dispatched_at FROM orders WHERE order_id = $1", order_id
        )
    finally:
        await conn.close()


async def _transition_rows(order_id: str):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(
            "SELECT from_status, to_status, work_units, origin FROM order_status_transitions "
            "WHERE order_id = $1",
            order_id,
        )
    finally:
        await conn.close()


def _status_event(*, event_id: str, order_id: str, status: str, occurred_at: str | None = None) -> dict:
    return {
        "event_id": event_id,
        "event_type": "order_status_updated",
        "source": "fulfillment_sim",
        "order_id": order_id,
        "facility_id": FACILITY,
        "occurred_at": occurred_at or datetime.now(UTC).isoformat(),
        "status": status,
    }


def _post_status(event: dict):
    """POST one status event and immediately dispose the shared engine.

    Each `with TestClient(app) as client:` block runs its own portal
    thread/loop; without disposing `app.db.engine`'s pool right after, a
    later call (another `_post_status`, or a direct `engine.begin()`) can
    reuse a pooled connection tied to that closed portal and blow up -- see
    test_backfill.py's `_post_backfill` for the identically-purposed fix.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        response = client.post("/api/events/order-status", json=event)
    asyncio.run(engine.dispose())
    return response


@pytest.mark.usefixtures("require_database")
def test_cancelling_a_pending_order_succeeds():
    order_id = "CANCEL-0001"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, status="PENDING"))

    r = _post_status(_status_event(event_id="evt-cancel-1", order_id=order_id, status="CANCELLED"))
    assert r.status_code == 200
    assert r.json()["status"] == "PROCESSED"

    row = _run(_order_row(order_id))
    assert row["status"] == "CANCELLED"

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_cancelling_records_a_zero_work_units_transition():
    order_id = "CANCEL-0002"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, status="PICKING", work_units=4.0))

    r = _post_status(_status_event(event_id="evt-cancel-2", order_id=order_id, status="CANCELLED"))
    assert r.status_code == 200

    rows = _run(_transition_rows(order_id))
    assert len(rows) == 1
    assert rows[0]["from_status"] == "PICKING"
    assert rows[0]["to_status"] == "CANCELLED"
    assert rows[0]["work_units"] == pytest.approx(0.0)
    assert rows[0]["origin"] == "event"

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_cancelling_a_dispatched_order_is_refused():
    order_id = "CANCEL-0003"
    _run(_clean_up(order_id))
    now = datetime.now(UTC)
    _run(_seed_order(order_id, status="DISPATCHED", dispatched_at=now))

    r = _post_status(_status_event(event_id="evt-cancel-3", order_id=order_id, status="CANCELLED"))
    assert r.status_code == 409

    row = _run(_order_row(order_id))
    assert row["status"] == "DISPATCHED"

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_cancelling_an_already_cancelled_order_is_a_harmless_no_op():
    """Restating CANCELLED (a new event_id, not a replay) must not error --
    the same idempotent-restate rule every other status already gets.
    """
    order_id = "CANCEL-0004"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, status="PENDING"))

    first = _post_status(_status_event(event_id="evt-cancel-4a", order_id=order_id, status="CANCELLED"))
    assert first.status_code == 200

    second = _post_status(_status_event(event_id="evt-cancel-4b", order_id=order_id, status="CANCELLED"))
    assert second.status_code == 200
    assert second.json()["status"] == "PROCESSED"

    rows = _run(_transition_rows(order_id))
    # The restate is a no-op at the transition layer -- only the first
    # cancellation actually changed anything.
    assert len(rows) == 1

    _run(_clean_up(order_id))


def _totals(facility: str):
    """Fetch OrderTotals and immediately dispose the shared engine.

    Each call below is its own top-level `asyncio.run(...)`, and this test
    also calls `_post_status` (a `TestClient` block, its own portal thread
    and loop) between two of them. Without disposing right after every
    engine.begin() use, a later call reuses a pooled connection tied to an
    already-closed loop -- the same failure mode `_post_backfill` in
    test_backfill.py exists to prevent, needed here across three loop
    boundaries instead of one.
    """

    async def _get():
        async with engine.begin() as conn:
            return await get_order_totals(conn, facility)

    result = asyncio.run(_get())
    asyncio.run(engine.dispose())
    return result


@pytest.mark.usefixtures("require_database")
def test_cancelling_releases_committed_work():
    """A cancelled order's work must stop counting toward the backlog the
    pending queue is scheduled to wait behind -- BACKLOG_STATUSES already
    excludes CANCELLED, so this is a regression guard, not new logic.
    """
    order_id = "CANCEL-0005"
    _run(_clean_up(order_id))
    _run(_seed_order(order_id, status="PICKING", work_units=8.0))

    before = _totals(FACILITY)
    assert before.work_units.get(OrderStatus.PICKING, 0.0) >= 8.0

    r = _post_status(_status_event(event_id="evt-cancel-5", order_id=order_id, status="CANCELLED"))
    assert r.status_code == 200

    after = _totals(FACILITY)
    # After cancellation, none of this order's work should still be charged
    # under a committed (PICKING/PACKED) status. remaining_work_fraction at
    # PICKING is 0.75 (3 of the 4 equal-weight stages still ahead), not a
    # hardcoded guess -- computed the same way the product itself does.
    rules = load_rules()
    contribution = 8.0 * remaining_work_fraction(lifecycle_position(OrderStatus.PICKING), rules)
    assert after.committed_work_units == pytest.approx(before.committed_work_units - contribution)

    _run(_clean_up(order_id))


@pytest.mark.usefixtures("require_database")
def test_cancelled_orders_are_excluded_from_the_late_dispatch_numerator_not_the_denominator():
    """The plan's literal wording: 'keep it out of the late-dispatch
    numerator' -- a cancelled order was placed (still counts toward
    `placed`), but was never going to ship, so it must not count as a miss.
    """
    order_id = "CANCEL-0006"
    _run(_clean_up(order_id))
    now = datetime.now(UTC)
    # Placed inside the window, promise already in the past, never dispatched.
    _run(
        _seed_order(
            order_id,
            status="CANCELLED",
            created_at=now - timedelta(hours=1),
            promised_dispatch_at=now - timedelta(minutes=30),
        )
    )

    async def _count():
        async with engine.begin() as conn:
            return await count_late_dispatches(conn, FACILITY, now, window_days=10)

    misses, placed = _run(_count())
    assert placed >= 1
    assert misses == 0

    _run(_clean_up(order_id))
