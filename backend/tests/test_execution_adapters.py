"""A manual/webhook facility must never be polled by n8n, and a human must be
able to confirm what actually happened.

Every capacity or cutoff lever today assumes n8n can call the simulator and
get an instant answer. These tests pin the escape hatch that makes a real
facility possible without any n8n workflow change: an action born anything
other than PENDING is never returned by n8n's poll.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.conftest import TEST_SERVICE_TOKEN
from tests.helpers import sign_in

pytestmark = pytest.mark.usefixtures("require_database")

ADAPTERS_TEST_ACCOUNT = "execution-adapters-tests@example.com"


@pytest.fixture(autouse=True)
def clean_tables():
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM active_interventions")
            await conn.execute("DELETE FROM recovery_actions")
            await conn.execute("DELETE FROM orders")
            await conn.execute("DELETE FROM processed_events")
            await conn.execute("DELETE FROM fulfillment_snapshots")
            await conn.execute("DELETE FROM order_status_transitions")
            await conn.execute("DELETE FROM pending_status_events")
            await conn.execute(
                "UPDATE facilities SET capacity_per_hour = 52, "
                "dispatch_promise_hours = 24, dispatch_cutoff_utc = NULL, "
                "execution_adapter = 'simulator' WHERE facility_id = 'WH-01'"
            )
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _set_adapter(adapter: str) -> None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _set() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "UPDATE facilities SET execution_adapter = $1 WHERE facility_id = 'WH-01'",
                adapter,
            )
        finally:
            await conn.close()

    asyncio.run(_set())


def _order(client, event_id: str, order_id: str, *, hours: float = 1, segment=None) -> None:
    now = datetime.now(UTC)
    body = {
        "event_id": event_id,
        "event_type": "order_created",
        "source": "commerce_sim",
        "order_id": order_id,
        "facility_id": "WH-01",
        "created_at": now.isoformat(),
        "promised_dispatch_at": (now + timedelta(hours=hours)).isoformat(),
        "item_count": 1,
        "work_units": 2.0,
        "order_value": 500,
    }
    if segment:
        body["segment"] = segment
    assert client.post("/api/events/orders", json=body).status_code == 200


def test_a_throughput_plan_on_a_manual_facility_is_born_awaiting_enactment():
    """A manual facility's capacity lever must never be born PENDING.

    n8n's poller has no way to distinguish "nothing to do yet" from "this
    facility handles execution itself" -- the only safe signal is the status
    it never polls for in the first place.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a1", "ORD-A1")

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()

        assert approval["status"] == "AWAITING_ENACTMENT"


def test_an_awaiting_enactment_action_is_invisible_to_the_n8n_poll():
    """Proven directly against the real filter n8n's poller uses.

    GET /recovery-actions defaults to status=PENDING (reads.py's own
    docstring: n8n takes the first item and expects work still needing
    doing). An AWAITING_ENACTMENT action must never appear there.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a2", "ORD-A2")

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()

        pending = client.get(
            "/api/recovery-actions", params={"facility_id": "WH-01"}
        ).json()
        assert approval["action_id"] not in {a["action_id"] for a in pending}


def test_a_queue_only_plan_is_unaffected_by_adapter_choice():
    """QUEUE/PROMISE-only postures have nothing external to enact, on any adapter."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a3", "ORD-A3", hours=20, segment="FIRST_TIME")

        approval = client.post("/api/recovery-plans/protect-new-customers/approve?facility_id=WH-01").json()

        assert approval["status"] == "PENDING"


def test_confirming_enactment_applies_effects_and_reaches_enacted():
    """A human confirming the real-world change must move the world, not just a flag."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a4", "ORD-A4")

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        confirmed = client.post(
            f"/api/recovery-actions/{approval['action_id']}/confirm-enactment",
            json={"succeeded": True},
        )

        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "ENACTED"


def test_a_failed_enactment_moves_to_failed_with_a_reason():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a5", "ORD-A5")

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        failed = client.post(
            f"/api/recovery-actions/{approval['action_id']}/confirm-enactment",
            json={"succeeded": False, "error_detail": "floor supervisor declined"},
        )

        assert failed.status_code == 200
        assert failed.json()["status"] == "FAILED"
        assert failed.json()["error_detail"] == "floor supervisor declined"


def test_an_enacted_action_does_not_block_a_new_approval():
    """The execution process is finished at ENACTED; only observation is outstanding.

    A 90-minute EXTEND_SHIFT lead time must not lock the facility out of an
    unrelated second approval for that long.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a6", "ORD-A6", hours=20, segment="FIRST_TIME")

        first = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        client.post(
            f"/api/recovery-actions/{first['action_id']}/confirm-enactment",
            json={"succeeded": True},
        )

        second = client.post("/api/recovery-plans/protect-new-customers/approve?facility_id=WH-01")
        assert second.status_code == 200, second.json()


def test_an_awaiting_enactment_action_blocks_a_second_approval():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _set_adapter("manual")
        _order(client, "evt-a7", "ORD-A7")

        client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01")
        second = client.post("/api/recovery-plans/protect-new-customers/approve?facility_id=WH-01")

        assert second.status_code == 409


def test_the_simulator_adapter_path_is_unchanged():
    """The default adapter must reproduce today's exact behaviour."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, ADAPTERS_TEST_ACCOUNT)
        _order(client, "evt-a8", "ORD-A8")

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()

        assert approval["status"] == "PENDING"
