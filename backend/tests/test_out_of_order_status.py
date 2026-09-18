"""A status event for an order that has not arrived yet must not be lost.

Real webhooks are not ordered: a WMS "picking started" event can reach the
backend before the commerce feed's "order created" event does, even though
they describe the same order. The endpoint used to 404 and drop such an
event outright -- "status events must not create orders" is the right
principle, but dropping the event entirely also throws away a fact that
becomes true the moment the order arrives.

These cover the fix: an out-of-order status event is parked rather than
dropped, and replayed -- in the order it actually happened, not the order
it arrived -- once the order it describes exists.

Setup and teardown use a raw asyncpg connection rather than the app's shared
SQLAlchemy engine: that engine's pooled connections are tied to whichever
event loop last used them, and TestClient runs its own loop per `with` block,
so mixing the two inside one test reproduces the same
"'NoneType' object has no attribute 'send'" failure documented in
test_alert_recipients.py.
"""

import asyncio

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.conftest import TEST_SERVICE_TOKEN

ORDER_BODY = {
    "event_id": "oos-order-0001",
    "order_id": "OOS-0001",
    "facility_id": "WH-01",
    "created_at": "2026-09-15T08:00:00Z",
    "promised_dispatch_at": "2026-09-16T08:00:00Z",
    "item_count": 1,
    "work_units": 1.0,
    "order_value": "10.00",
    "source": "commerce_sim",
}


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


async def _clean_up(order_id: str, *event_ids: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM orders WHERE order_id = $1", order_id)
        await conn.execute(
            "DELETE FROM pending_status_events WHERE order_id = $1", order_id
        )
        # _claim_event's rows too, or a second run of this test against the
        # same database sees every event_id already claimed and gets
        # DUPLICATE instead of exercising the park/replay path again.
        if event_ids:
            await conn.execute(
                "DELETE FROM processed_events WHERE event_id = ANY($1::text[])",
                list(event_ids),
            )
    finally:
        await conn.close()


async def _read_status(order_id: str) -> str | None:
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchval(
            "SELECT status FROM orders WHERE order_id = $1", order_id
        )
    finally:
        await conn.close()


def _status_event(*, event_id: str, order_id: str, status: str, occurred_at: str) -> dict:
    return {
        "event_id": event_id,
        "order_id": order_id,
        "facility_id": "WH-01",
        "occurred_at": occurred_at,
        "status": status,
        "source": "fulfillment_sim",
    }


@pytest.mark.usefixtures("require_database")
def test_a_status_event_for_an_unknown_order_is_parked_not_dropped():
    """The event survives even though the order it describes does not exist yet."""
    order_id = "OOS-PARK-0001"
    event_id = "oos-park-status-1"
    _run(_clean_up(order_id, event_id))

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        response = client.post(
            "/api/events/order-status",
            json=_status_event(
                event_id=event_id,
                order_id=order_id,
                status="PICKING",
                occurred_at="2026-09-15T09:00:00Z",
            ),
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "BUFFERED"

    _run(_clean_up(order_id, event_id))


@pytest.mark.usefixtures("require_database")
def test_parked_events_replay_in_the_order_they_actually_happened():
    """Replay must use occurred_at, not arrival order.

    A WMS can deliver PICKING and PACKED out of arrival order too, once both
    are parked ahead of the order they belong to.
    """
    order_id = "OOS-REPLAY-0001"
    packed_event_id = "oos-replay-packed"
    picking_event_id = "oos-replay-picking"
    order_event_id = f"oos-replay-order-{order_id}"
    _run(_clean_up(order_id, packed_event_id, picking_event_id, order_event_id))

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        # PACKED arrives before PICKING, but PICKING happened first.
        packed = client.post(
            "/api/events/order-status",
            json=_status_event(
                event_id=packed_event_id,
                order_id=order_id,
                status="PACKED",
                occurred_at="2026-09-15T09:10:00Z",
            ),
        )
        picking = client.post(
            "/api/events/order-status",
            json=_status_event(
                event_id=picking_event_id,
                order_id=order_id,
                status="PICKING",
                occurred_at="2026-09-15T09:05:00Z",
            ),
        )
        assert packed.json()["status"] == "BUFFERED"
        assert picking.json()["status"] == "BUFFERED"

        created = client.post(
            "/api/events/orders",
            json={**ORDER_BODY, "order_id": order_id, "event_id": order_event_id},
        )
        assert created.status_code == 200, created.text

    assert _run(_read_status(order_id)) == "PACKED", (
        "replaying PACKED before PICKING (arrival order) would have been "
        "rejected as a backward transition or left the order on the wrong "
        "status; replaying by occurred_at must land on PACKED"
    )

    _run(_clean_up(order_id, packed_event_id, picking_event_id, order_event_id))


@pytest.mark.usefixtures("require_database")
def test_a_retried_parked_event_is_a_duplicate_not_a_second_park():
    """The same event_id sent twice must not park twice.

    `_claim_event` already claims the event_id globally before the park
    decision is made, so a retry of an already-parked event is recognised as
    a duplicate the same way a retry of any other event is -- not by a
    second mechanism specific to the buffer.
    """
    order_id = "OOS-DUP-0001"
    event_id = "oos-dup-status"
    _run(_clean_up(order_id, event_id))

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        body = _status_event(
            event_id=event_id,
            order_id=order_id,
            status="PICKING",
            occurred_at="2026-09-15T09:00:00Z",
        )
        first = client.post("/api/events/order-status", json=body)
        second = client.post("/api/events/order-status", json=body)

    assert first.json()["status"] == "BUFFERED"
    assert second.json()["status"] == "DUPLICATE"

    _run(_clean_up(order_id, event_id))
