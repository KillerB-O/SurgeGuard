"""Order revision must let a known order's content change without disturbing
what makes its promise history honest (P8).

Shopify's `orders/updated` after `orders/create` is not covered by
park-and-replay: that mechanism exists for an *unknown* order (a status
event arriving before its order-created event), and this is the opposite
case -- a *known* order, a new revision of it. `POST /events/orders` 409s
today for the same order_id under a different event_id, which is correct
for a genuine duplicate-identity collision but wrong for a legitimate
revision; this is why a distinct endpoint exists rather than relaxing that
one's uniqueness check.

`original_promised_dispatch_at` is deliberately left untouched by a
revision. That field is the evidence behind this product's first honesty
claim -- separating breaches prevented from breaches merely moved -- and
`app/interventions.py`'s RE_PROMISE lever already handles it correctly via
`COALESCE(original_promised_dispatch_at, promised_dispatch_at)`: NULL means
no internal lever has shifted this order's promise yet, so the next one
captures whatever `promised_dispatch_at` is *at that time* (including a
prior revision) as the original. A revision resetting or rewriting this
field would erase the record that an operator moved the goalposts.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import engine
from app.main import app
from tests.conftest import TEST_SERVICE_TOKEN

FACILITY = "WH-01"


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    result = asyncio.run(coro)
    asyncio.run(engine.dispose())
    return result


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    asyncio.run(engine.dispose())


async def _clean_facility() -> None:
    """Wipe every WH-01 order, both before and after each test.

    Same facility-wide (not order-id-scoped) reasoning as
    test_cancellation.py's identically named fixture: a leftover order from
    another test file that this module's own tests never created would
    otherwise be free to interfere with a 404/409/revision-content
    assertion here just as easily as it would a late-dispatch count there.
    """
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM orders WHERE facility_id = $1", FACILITY)
        await conn.execute("DELETE FROM processed_events")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean_facility_orders():
    asyncio.run(_clean_facility())
    yield
    asyncio.run(_clean_facility())


async def _seed_order(
    order_id: str,
    *,
    facility_id: str = FACILITY,
    status: str = "PENDING",
    work_units: float = 4.0,
    promised_dispatch_at: datetime | None = None,
    original_promised_dispatch_at: datetime | None = None,
    dispatched_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO orders (order_id, facility_id, created_at, promised_dispatch_at,
                                item_count, work_units, order_value, status,
                                original_promised_dispatch_at, dispatched_at)
            VALUES ($1, $2, $3, $4, 1, $5, 1.00, $6, $7, $8)
            """,
            order_id,
            facility_id,
            now - timedelta(hours=2),
            promised_dispatch_at or (now + timedelta(hours=24)),
            work_units,
            status,
            original_promised_dispatch_at,
            dispatched_at,
        )
    finally:
        await conn.close()


async def _order_row(order_id: str):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchrow(
            "SELECT status, promised_dispatch_at, original_promised_dispatch_at, "
            "item_count, work_units, order_value, segment "
            "FROM orders WHERE order_id = $1",
            order_id,
        )
    finally:
        await conn.close()


def _revision_event(event_id: str, order_id: str, **overrides) -> dict:
    now = datetime.now(UTC)
    event = {
        "event_id": event_id,
        "event_type": "order_revised",
        "source": "commerce_sim",
        "order_id": order_id,
        "facility_id": FACILITY,
        "promised_dispatch_at": (now + timedelta(hours=48)).isoformat(),
        "item_count": 2,
        "work_units": 6.0,
        "order_value": "299.00",
        "segment": "HIGH_LTV",
    }
    event.update(overrides)
    return event


def _post_revision(event: dict, *, token: str | None = TEST_SERVICE_TOKEN):
    headers = {"X-Service-Token": token} if token else None
    with TestClient(app, headers=headers) as client:
        response = client.post("/api/events/order-revised", json=event)
    asyncio.run(engine.dispose())
    return response


@pytest.mark.usefixtures("require_database")
def test_revising_an_order_updates_its_fields():
    order_id = "REV-0001"
    _run(_seed_order(order_id, work_units=4.0))

    new_promise = datetime.now(UTC) + timedelta(hours=48)
    r = _post_revision(
        _revision_event(
            "evt-rev-1",
            order_id,
            promised_dispatch_at=new_promise.isoformat(),
            item_count=3,
            work_units=9.0,
            order_value="450.00",
            segment="SUBSCRIBER",
        )
    )
    assert r.status_code == 200
    assert r.json()["status"] == "PROCESSED"

    row = _run(_order_row(order_id))
    assert row["item_count"] == 3
    assert row["work_units"] == pytest.approx(9.0)
    assert row["order_value"] == Decimal("450.00")
    assert row["segment"] == "SUBSCRIBER"
    assert abs((row["promised_dispatch_at"] - new_promise).total_seconds()) < 1


@pytest.mark.usefixtures("require_database")
def test_revision_reclassifies_work_units_when_line_and_unit_counts_present():
    """The provider is authoritative about what is in the order; it is not
    authoritative about how much work that costs -- the same boundary
    order-created already enforces, now on revision too.
    """
    order_id = "REV-0002"
    _run(_seed_order(order_id, work_units=4.0))

    r = _post_revision(
        _revision_event(
            "evt-rev-2",
            order_id,
            work_units=999.0,  # Ignored: line_count/unit_count present below.
            line_count=1,
            unit_count=1,
        )
    )
    assert r.status_code == 200

    row = _run(_order_row(order_id))
    assert row["work_units"] != pytest.approx(999.0)


@pytest.mark.usefixtures("require_database")
def test_revision_leaves_original_promised_dispatch_at_untouched():
    order_id = "REV-0003"
    frozen_original = datetime.now(UTC) - timedelta(days=1)
    _run(_seed_order(order_id, original_promised_dispatch_at=frozen_original))

    r = _post_revision(_revision_event("evt-rev-3", order_id))
    assert r.status_code == 200

    row = _run(_order_row(order_id))
    assert abs((row["original_promised_dispatch_at"] - frozen_original).total_seconds()) < 1


@pytest.mark.usefixtures("require_database")
def test_revision_of_nonexistent_order_is_refused():
    r = _post_revision(_revision_event("evt-rev-4", "REV-DOES-NOT-EXIST"))
    assert r.status_code == 404


@pytest.mark.usefixtures("require_database")
def test_revision_facility_mismatch_is_refused():
    order_id = "REV-0005"
    _run(_seed_order(order_id, facility_id="WH-01"))

    r = _post_revision(_revision_event("evt-rev-5", order_id, facility_id="WH-99"))
    assert r.status_code == 404

    row = _run(_order_row(order_id))
    assert row["item_count"] == 1  # Unchanged -- the revision never applied.


@pytest.mark.usefixtures("require_database")
def test_revision_of_a_dispatched_order_is_refused():
    order_id = "REV-0006"
    now = datetime.now(UTC)
    _run(_seed_order(order_id, status="DISPATCHED", dispatched_at=now))

    r = _post_revision(_revision_event("evt-rev-6", order_id))
    assert r.status_code == 409

    row = _run(_order_row(order_id))
    assert row["item_count"] == 1


@pytest.mark.usefixtures("require_database")
def test_revision_of_a_cancelled_order_is_refused():
    order_id = "REV-0007"
    _run(_seed_order(order_id, status="CANCELLED"))

    r = _post_revision(_revision_event("evt-rev-7", order_id))
    assert r.status_code == 409


@pytest.mark.usefixtures("require_database")
def test_revision_is_idempotent_on_exact_replay():
    order_id = "REV-0008"
    _run(_seed_order(order_id))

    event = _revision_event("evt-rev-8", order_id)
    first = _post_revision(event)
    second = _post_revision(event)

    assert first.json()["status"] == "PROCESSED"
    assert second.json()["status"] == "DUPLICATE"


@pytest.mark.usefixtures("require_database")
def test_revision_rejects_unregistered_source():
    order_id = "REV-0009"
    _run(_seed_order(order_id))

    r = _post_revision(_revision_event("evt-rev-9", order_id, source="not_a_real_provider"))
    assert r.status_code == 422


def test_revision_requires_a_service_token():
    r = _post_revision(_revision_event("evt-rev-10", "REV-0010"), token=None)
    assert r.status_code == 401
