"""A facility's day-one backlog must become visible without lying about throughput.

Onboarding a facility means it already has orders mid-flight in the real
commerce/WMS system -- some already PICKING, PACKED, or DISPATCHED -- before
this backend ever received a live event for them. Replaying that backlog
through the ordinary ingest endpoints (`POST /events/orders` always lands an
order at PENDING; `POST /events/order-status` records every step as a fresh
`origin='event'` transition) would both misrepresent the backlog's real state
and, worse, produce a fictitious throughput spike at the moment of import
(see migration 0017 and `app.repository._derive_throughput_from_transitions`).

`POST /events/backfill` (P7) fixes the first problem by accepting the order's
real current status directly, and the second by tagging the resulting
transition `origin='backfill'` -- already a legal value on
`order_status_transitions.origin` since migration 0017, and already excluded
by `_derive_throughput_from_transitions`'s `WHERE origin = 'event'` filter.
P2's rolling late-dispatch window (already shipped) is what keeps a backfilled
order's real, possibly-already-late `created_at` from corrupting
`count_late_dispatches` forever instead of just until it ages out -- the
"P2 before P7" ordering constraint the plan calls out.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.main import app
from app.repository import count_late_dispatches, get_throughput_signal
from tests.conftest import TEST_SERVICE_TOKEN

FACILITY = "WH-01"


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts from a clean slate on the mutable tables WH-01 owns."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM orders")
            await conn.execute("DELETE FROM processed_events")
            await conn.execute("DELETE FROM order_status_transitions")
            await conn.execute("DELETE FROM pending_status_events")
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    asyncio.run(engine.dispose())


def _run(coro):
    """Run one coroutine to completion, then dispose the shared engine.

    Every helper in this file that touches the database goes through
    `engine.begin()` somewhere, directly or via a `TestClient` request. Each
    `asyncio.run(...)` -- this one included -- gets its own fresh loop, and
    `app.db.engine`'s pool holds connections tied to whichever loop last used
    them. Disposing unconditionally after every call, here in one place
    rather than at each call site, is what makes that safe regardless of
    which helper runs next -- a later call site that forgets to dispose
    cannot reintroduce the "'NoneType' object has no attribute 'send'"
    failure this was originally written around (see git history: this
    started as a per-call-site fix in `_post_backfill` alone and proved
    insufficient once more call sites were added).
    """
    result = asyncio.run(coro)
    asyncio.run(engine.dispose())
    return result


def _post_backfill(event: dict, *, token: str | None = TEST_SERVICE_TOKEN):
    """POST one backfill event through the app, then dispose the shared engine."""
    headers = {"X-Service-Token": token} if token else None
    with TestClient(app, headers=headers) as client:
        response = client.post("/api/events/backfill", json=event)
    asyncio.run(engine.dispose())
    return response


def _backfill_event(event_id: str, order_id: str, **overrides) -> dict:
    now = datetime.now(UTC)
    event = {
        "event_id": event_id,
        "event_type": "order_backfilled",
        "source": "commerce_sim",
        "order_id": order_id,
        "facility_id": FACILITY,
        "created_at": (now - timedelta(days=2)).isoformat(),
        "promised_dispatch_at": (now + timedelta(hours=6)).isoformat(),
        "item_count": 1,
        "work_units": 4.0,
        "order_value": 199,
        "status": "PACKED",
        "occurred_at": (now - timedelta(hours=1)).isoformat(),
    }
    event.update(overrides)
    return event


async def _order_row(order_id: str):
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT status, dispatched_at, created_at FROM orders "
                "WHERE order_id = :order_id"
            ),
            {"order_id": order_id},
        )
        return result.first()


async def _transition_rows(order_id: str):
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT from_status, to_status, work_units, origin FROM order_status_transitions "
                "WHERE order_id = :order_id"
            ),
            {"order_id": order_id},
        )
        return result.all()


@pytest.mark.usefixtures("require_database")
def test_backfill_lands_the_order_at_its_real_current_status():
    r = _post_backfill(_backfill_event("evt-bf-1", "BF-ORD-1", status="PACKED"))
    assert r.status_code == 200
    assert r.json()["status"] == "PROCESSED"

    row = _run(_order_row("BF-ORD-1"))
    assert row is not None
    assert row.status == "PACKED"


@pytest.mark.usefixtures("require_database")
def test_backfill_records_a_backfill_origin_transition_not_an_event_one():
    r = _post_backfill(
        _backfill_event("evt-bf-2", "BF-ORD-2", status="PACKED", work_units=4.0)
    )
    assert r.status_code == 200

    rows = _run(_transition_rows("BF-ORD-2"))
    assert len(rows) == 1
    row = rows[0]
    assert row.from_status == "PENDING"
    assert row.to_status == "PACKED"
    assert row.origin == "backfill"
    # PENDING->PICKING->PACKED: two of four equal-weight stages.
    assert row.work_units == pytest.approx(2.0)


@pytest.mark.usefixtures("require_database")
def test_backfilled_transitions_are_invisible_to_derived_throughput():
    """The core honesty guarantee: importing a backlog must not fake a spike."""
    now = datetime.now(UTC)
    r = _post_backfill(
        _backfill_event(
            "evt-bf-3",
            "BF-ORD-3",
            status="DISPATCHED",
            work_units=40.0,
            occurred_at=now.isoformat(),
        )
    )
    assert r.status_code == 200

    async def _measure():
        async with engine.begin() as conn:
            return await get_throughput_signal(conn, FACILITY, 52.0, now)

    signal = _run(_measure())
    # A live-derived rate needs MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT
    # 'event'-origin rows; the single backfill row above is 'backfill'-origin
    # and must not count toward that floor at all.
    assert signal.source != "derived"


@pytest.mark.usefixtures("require_database")
def test_backfill_stamps_dispatched_at_from_occurred_at_when_already_dispatched():
    now = datetime.now(UTC)
    occurred_at = now - timedelta(hours=3)
    r = _post_backfill(
        _backfill_event(
            "evt-bf-4",
            "BF-ORD-4",
            status="DISPATCHED",
            occurred_at=occurred_at.isoformat(),
        )
    )
    assert r.status_code == 200

    row = _run(_order_row("BF-ORD-4"))
    assert row.dispatched_at is not None
    assert abs((row.dispatched_at - occurred_at).total_seconds()) < 1


@pytest.mark.usefixtures("require_database")
def test_backfill_is_idempotent_on_exact_replay():
    event = _backfill_event("evt-bf-5", "BF-ORD-5", status="PACKED")
    first = _post_backfill(event)
    second = _post_backfill(event)

    assert first.json()["status"] == "PROCESSED"
    assert second.json()["status"] == "DUPLICATE"
    rows = _run(_transition_rows("BF-ORD-5"))
    assert len(rows) == 1


@pytest.mark.usefixtures("require_database")
def test_backfill_rejects_business_identity_collision():
    first = _post_backfill(_backfill_event("evt-bf-6a", "BF-ORD-6", status="PACKED"))
    assert first.status_code == 200
    second = _post_backfill(_backfill_event("evt-bf-6b", "BF-ORD-6", status="READY"))
    assert second.status_code == 409


@pytest.mark.usefixtures("require_database")
def test_backfill_rejects_unregistered_source():
    r = _post_backfill(
        _backfill_event("evt-bf-7", "BF-ORD-7", source="not_a_real_provider")
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("require_database")
def test_backfill_rejects_exception_states_and_rolls_back_cleanly():
    """DELAYED still has no defined transition semantics anywhere and stays
    out of scope forever, not just pre-P8. Rejecting it must not leave an
    orphaned order row behind -- the same atomicity the ordinary ingest
    endpoints already guarantee.

    CANCELLED used to be this test's subject before P8 gave it real
    transition semantics; see
    test_backfill_of_an_already_cancelled_order_now_succeeds below for the
    behavior change that made it stop belonging here.
    """
    r = _post_backfill(_backfill_event("evt-bf-8", "BF-ORD-8", status="DELAYED"))
    assert r.status_code == 422

    row = _run(_order_row("BF-ORD-8"))
    assert row is None


@pytest.mark.usefixtures("require_database")
def test_backfill_of_an_already_cancelled_order_now_succeeds():
    """P8 gave CANCELLED real transition semantics in `_apply_status_transition`,
    which the backfill path (P7) reuses rather than duplicating -- so a
    day-one backlog that includes already-cancelled orders is importable
    without any backfill-specific code, a direct consequence of that reuse
    rather than a separately built feature.
    """
    r = _post_backfill(_backfill_event("evt-bf-11", "BF-ORD-11", status="CANCELLED"))
    assert r.status_code == 200
    assert r.json()["status"] == "PROCESSED"

    row = _run(_order_row("BF-ORD-11"))
    assert row is not None
    assert row.status == "CANCELLED"

    rows = _run(_transition_rows("BF-ORD-11"))
    assert len(rows) == 1
    assert rows[0].to_status == "CANCELLED"
    assert rows[0].origin == "backfill"
    # Cancellation earns zero work regardless of origin -- see
    # test_cancellation.py's test_cancelling_records_a_zero_work_units_transition
    # for the same rule on the live-event path.
    assert rows[0].work_units == pytest.approx(0.0)


def test_backfill_requires_a_service_token():
    r = _post_backfill(_backfill_event("evt-bf-9", "BF-ORD-9"), token=None)
    assert r.status_code == 401


@pytest.mark.usefixtures("require_database")
def test_backfilled_late_dispatch_ages_out_of_the_rolling_window():
    """P2-before-P7: a backfilled order's real (possibly already-late) history
    must only count while it is inside the rolling window, exactly like a
    real order's -- not forever, and not never.
    """
    now = datetime.now(UTC)
    # Created 3 days ago, promised 2 days ago, dispatched 1 day ago: already
    # a late dispatch by the time it's backfilled in.
    r = _post_backfill(
        _backfill_event(
            "evt-bf-10",
            "BF-ORD-10",
            created_at=(now - timedelta(days=3)).isoformat(),
            promised_dispatch_at=(now - timedelta(days=2)).isoformat(),
            status="DISPATCHED",
            occurred_at=(now - timedelta(days=1)).isoformat(),
        )
    )
    assert r.status_code == 200

    async def _counts():
        # Both windows measured on the same connection/loop -- see
        # _post_backfill's docstring on why a second, separate asyncio.run
        # touching engine.begin() would otherwise reuse a pooled connection
        # tied to an already-closed loop.
        async with engine.begin() as conn:
            wide = await count_late_dispatches(conn, FACILITY, now, window_days=10)
            narrow = await count_late_dispatches(conn, FACILITY, now, window_days=1)
            return wide, narrow

    (misses_wide, placed_wide), (misses_narrow, placed_narrow) = _run(_counts())

    # A 10-day window (the product default) still sees this 3-day-old backlog.
    assert placed_wide == 1
    assert misses_wide == 1

    # A 1-day window has already aged it out.
    assert placed_narrow == 0
    assert misses_narrow == 0
