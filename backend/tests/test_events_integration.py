"""Integration tests for the event ingestion endpoints.

These hit a real PostgreSQL database (via the app's normal DB engine) --
they are not pure/unit tests like test_scheduler.py, and require the
database from docker-compose.yml (or an equivalent local Postgres) to be
running and migrated. They exist because doc 11's integration checkpoints
1 and 2 spell out exact required behaviors for this endpoint, so it's
worth locking those in as automated tests rather than only checking them
by hand during a demo.

Skipped automatically if no database is reachable, so `pytest` still
passes cleanly for anyone who hasn't set up Postgres yet -- see
conftest.py.
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

# Phase 3 (ACCESS-01) gates the read/simulation endpoints this file exercises
# behind a session. This file's subject is scheduling, reads, and event
# ingestion -- not auth -- so every client below signs in as this one fixed
# account rather than each test growing its own signup dance. `clean_tables`
# does not touch `users`/`sessions`, so the account persists across this
# file's tests and `sign_in` (idempotent on a 409) is safe to call repeatedly.
EVENTS_TEST_ACCOUNT = "events-integration-tests@example.com"


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts from a clean slate on the mutable tables. Facility
    WH-01 is left alone -- it's seeded once by the migration.

    Uses its own throwaway asyncpg connection/event loop (via asyncio.run),
    deliberately not the app's shared async engine -- see conftest.py's
    docstring for why mixing loops on that shared pool breaks things.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM recovery_actions")
            await conn.execute("DELETE FROM orders")
            await conn.execute("DELETE FROM processed_events")
            await conn.execute("DELETE FROM fulfillment_snapshots")
            # Added for P3/P4: without these, a status transition or a parked
            # event left behind by an earlier test survives into the next one.
            # Derived throughput (P4) reads order_status_transitions for
            # WH-01 with no test-scoping of its own, so a leftover row here
            # silently changes what a later test's dashboard reports.
            await conn.execute("DELETE FROM order_status_transitions")
            await conn.execute("DELETE FROM pending_status_events")
            # A successful recovery action now writes the facility's defaults,
            # so the seeded policy has to be restored between tests too. The
            # cutoff belongs to that policy: leaving one behind made the suite
            # depend on both test order and the wall clock, because a stale
            # cutoff only changes a prediction at certain times of day.
            await conn.execute(
                "UPDATE facilities SET capacity_per_hour = 52, "
                "dispatch_promise_hours = 24, dispatch_cutoff_utc = NULL "
                "WHERE facility_id = 'WH-01'"
            )
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Each per-test `TestClient(...)` block below runs on its own
    fresh event loop. The app's shared async engine (app.db.engine) pools
    connections tied to whichever loop last used it, so without disposing
    it between tests, test N+1's loop would try to reuse a pooled
    connection that belongs to test N's already-closed loop and blow up.
    Runs as teardown (after yield) so it clears the pool right after each
    TestClient block closes, before the next test opens a new one.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _order_event(event_id: str, order_id: str, **overrides) -> dict:
    now = datetime.now(UTC)
    event = {
        "event_id": event_id,
        "event_type": "order_created",
        "source": "commerce_sim",
        "order_id": order_id,
        "facility_id": "WH-01",
        "created_at": now.isoformat(),
        "promised_dispatch_at": (now + timedelta(hours=24)).isoformat(),
        "item_count": 1,
        "work_units": 1.5,
        "order_value": 199,
    }
    event.update(overrides)
    return event


def test_checkpoint_1_valid_order_persists():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        r = client.post("/api/events/orders", json=_order_event("evt-1", "ORD-1"))
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"

        orders = client.get("/api/orders?facility_id=WH-01").json()["items"]
        assert any(o["order_id"] == "ORD-1" for o in orders)


def test_checkpoint_1_exact_retry_returns_duplicate():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        event = _order_event("evt-2", "ORD-2")
        first = client.post("/api/events/orders", json=event)
        retry = client.post("/api/events/orders", json=event)

        assert first.json()["status"] == "PROCESSED"
        assert retry.status_code == 200
        assert retry.json()["status"] == "DUPLICATE"

        orders = client.get("/api/orders?facility_id=WH-01").json()["items"]
        assert sum(1 for o in orders if o["order_id"] == "ORD-2") == 1


def test_checkpoint_1_same_order_different_event_returns_409():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-3a", "ORD-3"))
        conflict = client.post(
            "/api/events/orders", json=_order_event("evt-3b", "ORD-3")
        )

        assert conflict.status_code == 409


def test_missing_facility_fails_without_orphaning_the_event_claim():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        bad_event = _order_event("evt-bad-facility", "ORD-4", facility_id="WH-GHOST")

        first_attempt = client.post("/api/events/orders", json=bad_event)
        assert first_attempt.status_code == 422

        second_attempt = client.post("/api/events/orders", json=bad_event)
        assert second_attempt.status_code == 422


def test_checkpoint_2_fulfillment_snapshot_persists_without_overriding_backlog():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-5", "ORD-5"))

        now = datetime.now(UTC)
        snapshot = {
            "event_id": "evt-snapshot-1",
            "event_type": "fulfillment_snapshot",
            "source": "fulfillment_sim",
            "facility_id": "WH-01",
            "occurred_at": now.isoformat(),
            "open_orders": 999,  # deliberately wrong, must not leak into backlog
            "work_units_completed_last_hour": 52,
        }
        r = client.post("/api/events/fulfillment", json=snapshot)
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"

        dashboard = client.get("/api/dashboard?facility_id=WH-01").json()
        assert dashboard["backlog_orders"] == 1


def _snapshot_event(event_id: str, work_units: float, occurred_at: datetime) -> dict:
    """Build a canonical fulfillment snapshot body."""
    return {
        "event_id": event_id,
        "event_type": "fulfillment_snapshot",
        "source": "fulfillment_sim",
        "facility_id": "WH-01",
        "occurred_at": occurred_at.isoformat(),
        "open_orders": 1,
        "work_units_completed_last_hour": work_units,
    }


def test_zero_throughput_snapshot_is_reported_honestly():
    """Show a stall as observed rather than as configured capacity."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-zero-order", "ORD-ZERO"))
        client.post(
            "/api/events/fulfillment",
            json=_snapshot_event("evt-zero-capacity", 0, datetime.now(UTC)),
        )

        response = client.get("/api/dashboard?facility_id=WH-01")

        assert response.status_code == 200
        assert response.json()["fulfillment_work_units_per_hour"] == 0


def test_zero_throughput_snapshot_does_not_break_scheduling():
    """Keep the queue computable when observed throughput is zero."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-zero-order-2", "ORD-ZERO-2"))
        client.post(
            "/api/events/fulfillment",
            json=_snapshot_event("evt-zero-capacity-2", 0, datetime.now(UTC)),
        )

        orders = client.get("/api/orders?facility_id=WH-01").json()["items"]

        scheduled = next(o for o in orders if o["order_id"] == "ORD-ZERO-2")
        assert scheduled["predicted_dispatch_at"] is not None
        assert scheduled["queue_position"] == 1


def test_future_dated_snapshot_does_not_pin_effective_capacity():
    """Ignore a snapshot timestamped beyond clock-skew tolerance."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        now = datetime.now(UTC)
        client.post(
            "/api/events/fulfillment",
            json=_snapshot_event("evt-future", 999, now + timedelta(hours=6)),
        )
        client.post(
            "/api/events/fulfillment",
            json=_snapshot_event("evt-current", 40, now),
        )

        response = client.get("/api/dashboard?facility_id=WH-01")

        assert response.json()["fulfillment_work_units_per_hour"] == 40


def test_stale_snapshot_falls_back_to_configured_capacity():
    """Keep old telemetry from posing as current observed performance."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/fulfillment",
            json=_snapshot_event("evt-stale", 12, datetime.now(UTC) - timedelta(hours=5)),
        )

        response = client.get("/api/dashboard?facility_id=WH-01")

        assert response.json()["fulfillment_work_units_per_hour"] == 52


def test_checkpoint_2_status_update_applies_and_reflects_in_order_state():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-6", "ORD-6"))

        now = datetime.now(UTC)
        status_event = {
            "event_id": "evt-status-1",
            "event_type": "order_status_updated",
            "source": "fulfillment_sim",
            "order_id": "ORD-6",
            "facility_id": "WH-01",
            "occurred_at": now.isoformat(),
            "status": "PICKING",
        }
        r = client.post("/api/events/order-status", json=status_event)
        assert r.status_code == 200

        orders = client.get("/api/orders?facility_id=WH-01").json()["items"]
        updated = next(o for o in orders if o["order_id"] == "ORD-6")
        assert updated["status"] == "PICKING"
        assert updated["sla_status"] is None


def test_status_update_does_not_create_an_order():
    """A status event for an order that has never arrived is parked, not applied.

    It is held (see test_out_of_order_status.py for replay), never used to
    materialise the order itself: the order-created event is still the only
    thing that can bring ORD-GHOST into existence.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        now = datetime.now(UTC)
        status_event = {
            "event_id": "evt-status-ghost",
            "event_type": "order_status_updated",
            "source": "fulfillment_sim",
            "order_id": "ORD-GHOST",
            "facility_id": "WH-01",
            "occurred_at": now.isoformat(),
            "status": "PICKING",
        }
        r = client.post("/api/events/order-status", json=status_event)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "BUFFERED"

        orders = client.get("/api/orders?facility_id=WH-01").json()["items"]
        assert not any(o["order_id"] == "ORD-GHOST" for o in orders)


def test_status_event_retry_returns_duplicate():
    """Status events use the same idempotency contract as order events."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-status-order", "ORD-STATUS"))
        now = datetime.now(UTC).isoformat()
        event = {
            "event_id": "evt-status-retry",
            "event_type": "order_status_updated",
            "source": "fulfillment_sim",
            "order_id": "ORD-STATUS",
            "facility_id": "WH-01",
            "occurred_at": now,
            "status": "PICKING",
        }

        first = client.post("/api/events/order-status", json=event)
        retry = client.post("/api/events/order-status", json=event)

        assert first.json()["status"] == "PROCESSED"
        assert retry.json()["status"] == "DUPLICATE"


def test_status_event_cannot_update_an_order_at_another_facility():
    """Status events must identify the order's actual facility."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-fac-order", "ORD-FAC"))
        event = {
            "event_id": "evt-wrong-facility-status",
            "event_type": "order_status_updated",
            "source": "fulfillment_sim",
            "order_id": "ORD-FAC",
            "facility_id": "WH-GHOST",
            "occurred_at": datetime.now(UTC).isoformat(),
            "status": "PICKING",
        }

        response = client.post("/api/events/order-status", json=event)

        assert response.status_code == 404


def test_orders_are_paginated_after_full_queue_scheduling():
    """Pagination must not reset queue positions on later pages."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for index in range(3):
            client.post(
                "/api/events/orders",
                json=_order_event(f"evt-page-{index}", f"ORD-PAGE-{index}"),
            )

        first_page = client.get("/api/orders?facility_id=WH-01&limit=2&offset=0").json()
        second_page = client.get("/api/orders?facility_id=WH-01&limit=2&offset=2").json()

        assert first_page["total"] == second_page["total"] == 3
        assert [item["queue_position"] for item in first_page["items"]] == [1, 2]
        assert [item["queue_position"] for item in second_page["items"]] == [3]


def test_at_risk_endpoint_filters_scheduler_output():
    """Return only pending orders classified as AT_RISK or BREACHED."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event(
                "evt-risk", "ORD-RISK", promised_dispatch_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
            ),
        )
        client.post("/api/events/orders", json=_order_event("evt-safe", "ORD-SAFE"))
        client.post(
            "/api/events/fulfillment",
            json={
                "event_id": "evt-risk-capacity",
                "event_type": "fulfillment_snapshot",
                "source": "fulfillment_sim",
                "facility_id": "WH-01",
                "occurred_at": datetime.now(UTC).isoformat(),
                "open_orders": 2,
                "work_units_completed_last_hour": 1,
            },
        )

        response = client.get("/api/orders/at-risk?facility_id=WH-01")

        assert response.status_code == 200
        assert [item["order_id"] for item in response.json()["items"]] == ["ORD-RISK"]


def test_simulation_returns_baseline_and_applies_capacity_without_persisting():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-sim", "ORD-SIM"))

        response = client.post("/api/simulations?facility_id=WH-01", json={"capacity_per_hour": 75})

        assert response.status_code == 200
        body = response.json()
        assert body["applied_capacity_per_hour"] == 75
        assert body["baseline"]["backlog_orders"] == body["simulated"]["backlog_orders"] == 1
        assert client.get("/api/orders?facility_id=WH-01").json()["items"][0]["promised_dispatch_at"]


def test_dispatch_promise_simulation_is_ephemeral():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-promise", "ORD-PROMISE"))

        response = client.post("/api/simulations?facility_id=WH-01", json={"dispatch_promise_hours": 4})

        assert response.status_code == 200
        assert response.json()["applied_dispatch_promise_hours"] == 4
        order = client.get("/api/orders?facility_id=WH-01").json()["items"][0]
        assert order["promised_dispatch_at"] > order["created_at"]


def test_recovery_plans_have_one_recommendation():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        response = client.get("/api/recovery-plans?facility_id=WH-01")

        assert response.status_code == 200
        plans = response.json()["plans"]
        assert {"do-nothing", "buy-the-hour"}.issubset({p["plan_id"] for p in plans})
        assert all("projected" in plan and "actions" in plan for plan in plans)
        # Nothing is queued here, so nothing is at risk, so no plan is worth its
        # cost. Recommending one anyway is the failure this guards against.
        assert sum(plan["recommended"] for plan in plans) == 0
        assert any(plan["plan_id"] == "do-nothing" for plan in plans)


def test_approval_persists_pending_action_for_n8n():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        approved = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01")

        assert approved.status_code == 200
        approval = approved.json()
        assert approval["status"] == "PENDING"

        action = client.get(f"/api/recovery-actions/{approval['action_id']}")
        assert action.status_code == 200
        assert action.json()["plan_id"] == "buy-the-hour"
        assert action.json()["status"] == "PENDING"
        assert action.json()["capacity_per_hour"] > 0


def test_action_can_complete_once_and_rejects_later_conflicting_state():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        action_id = approval["action_id"]

        completed = client.post(
            f"/api/recovery-actions/{action_id}/status", json={"status": "SUCCESS"}
        )
        repeated = client.post(
            f"/api/recovery-actions/{action_id}/status", json={"status": "FAILED", "error_detail": "late"}
        )

        assert completed.status_code == 200
        assert completed.json()["status"] == "SUCCESS"
        assert repeated.status_code == 409


def test_failed_action_requires_error_detail():
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        action_id = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()["action_id"]

        response = client.post(
            f"/api/recovery-actions/{action_id}/status", json={"status": "FAILED"}
        )

        assert response.status_code == 422


def _status_event(event_id: str, order_id: str, status: str, **overrides) -> dict:
    """Build a canonical order-status event body."""
    event = {
        "event_id": event_id,
        "event_type": "order_status_updated",
        "source": "fulfillment_sim",
        "order_id": order_id,
        "facility_id": "WH-01",
        "occurred_at": datetime.now(UTC).isoformat(),
        "status": status,
    }
    event.update(overrides)
    return event


def _status_of(client: TestClient, order_id: str) -> str:
    """Read one order's persisted status back through the read API."""
    orders = client.get("/api/orders?facility_id=WH-01").json()["items"]
    return next(o for o in orders if o["order_id"] == order_id)["status"]


def test_status_cannot_regress_to_an_earlier_lifecycle_state():
    """Keep a dispatched order from being pushed back into the pending queue."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-reg-1", "ORD-REG-1"))
        client.post(
            "/api/events/order-status", json=_status_event("evt-reg-2", "ORD-REG-1", "PICKING")
        )

        r = client.post(
            "/api/events/order-status", json=_status_event("evt-reg-3", "ORD-REG-1", "PENDING")
        )

        assert r.status_code == 409
        assert "backward" in r.json()["detail"]
        assert _status_of(client, "ORD-REG-1") == "PICKING"


def test_rejected_transition_does_not_consume_the_event_id():
    """Roll the processed-event claim back so a rejected event stays retryable."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-reg-4", "ORD-REG-2"))
        client.post(
            "/api/events/order-status", json=_status_event("evt-reg-5", "ORD-REG-2", "READY")
        )
        rejected = client.post(
            "/api/events/order-status", json=_status_event("evt-reg-6", "ORD-REG-2", "PICKING")
        )
        assert rejected.status_code == 409

        # The same event_id replayed where it is now a valid transition.
        retried = client.post(
            "/api/events/order-status",
            json=_status_event("evt-reg-6", "ORD-REG-2", "DISPATCHED"),
        )

        assert retried.status_code == 200
        assert retried.json()["status"] == "PROCESSED"
        assert _status_of(client, "ORD-REG-2") == "DISPATCHED"


def test_forward_jump_is_tolerated():
    """Accept a skipped step so a reordered delivery cannot wedge an order."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-fwd-1", "ORD-FWD-1"))

        r = client.post(
            "/api/events/order-status", json=_status_event("evt-fwd-2", "ORD-FWD-1", "READY")
        )

        assert r.status_code == 200
        assert _status_of(client, "ORD-FWD-1") == "READY"


def test_restating_the_current_status_is_accepted():
    """Treat a repeated status as progress that has already been applied."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-same-1", "ORD-SAME-1"))
        client.post(
            "/api/events/order-status", json=_status_event("evt-same-2", "ORD-SAME-1", "PACKED")
        )

        r = client.post(
            "/api/events/order-status", json=_status_event("evt-same-3", "ORD-SAME-1", "PACKED")
        )

        assert r.status_code == 200
        assert _status_of(client, "ORD-SAME-1") == "PACKED"


def test_deferred_exception_states_are_rejected():
    """Refuse statuses whose lifecycle semantics remain deferred.

    CANCELLED is no longer one of them (P8) -- it moved to
    test_cancelling_via_the_live_endpoint_is_accepted below, and to the
    dedicated test_cancellation.py, which covers its behavior in full
    (zero-work transitions, refusal once DISPATCHED, restating as a no-op).
    DELAYED and RECEIVED remain genuinely unsupported.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-exc-1", "ORD-EXC-1"))

        for status in ("DELAYED", "RECEIVED"):
            r = client.post(
                "/api/events/order-status",
                json=_status_event(f"evt-exc-{status}", "ORD-EXC-1", status),
            )
            assert r.status_code == 422, status

        assert _status_of(client, "ORD-EXC-1") == "PENDING"


def test_cancelling_via_the_live_endpoint_is_accepted():
    """CANCELLED (P8) travels through the same order-status endpoint as
    every other status change -- no new endpoint, no new status code.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-exc-2", "ORD-EXC-2"))

        r = client.post(
            "/api/events/order-status",
            json=_status_event("evt-exc-cancel", "ORD-EXC-2", "CANCELLED"),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"
        assert _status_of(client, "ORD-EXC-2") == "CANCELLED"


def test_pagination_spans_the_scheduled_and_unscheduled_boundary():
    """Page continuously across the queue and the orders that follow it."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(6):
            client.post("/api/events/orders", json=_order_event(f"evt-pg-{i}", f"ORD-PG-{i}"))
        # Two orders leave the reorderable set, so four scheduled precede two not.
        for i in range(2):
            client.post(
                "/api/events/order-status",
                json=_status_event(f"evt-pg-s-{i}", f"ORD-PG-{i}", "PICKING"),
            )

        first = client.get(
            "/api/orders", params={"facility_id": "WH-01", "limit": 3, "offset": 0}
        ).json()
        second = client.get(
            "/api/orders", params={"facility_id": "WH-01", "limit": 3, "offset": 3}
        ).json()

        assert first["total"] == second["total"] == 6
        assert [o["queue_position"] for o in first["items"]] == [1, 2, 3]
        assert [o["queue_position"] for o in second["items"]] == [4, None, None]

        ids = [o["order_id"] for o in first["items"] + second["items"]]
        assert len(set(ids)) == 6


def test_pagination_past_the_queue_returns_only_unscheduled_orders():
    """Offset into the tail without re-reading the scheduled queue."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(4):
            client.post("/api/events/orders", json=_order_event(f"evt-tl-{i}", f"ORD-TL-{i}"))
        for i in range(3):
            client.post(
                "/api/events/order-status",
                json=_status_event(f"evt-tl-s-{i}", f"ORD-TL-{i}", "READY"),
            )

        page = client.get(
            "/api/orders", params={"facility_id": "WH-01", "limit": 10, "offset": 1}
        ).json()

        assert page["total"] == 4
        assert [o["queue_position"] for o in page["items"]] == [None, None, None]
        assert all(o["status"] == "READY" for o in page["items"])


def test_dashboard_counts_every_status_from_the_database():
    """Aggregate order states without materialising the order rows."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(3):
            client.post("/api/events/orders", json=_order_event(f"evt-ds-{i}", f"ORD-DS-{i}"))
        client.post(
            "/api/events/order-status", json=_status_event("evt-ds-a", "ORD-DS-0", "PICKING")
        )
        client.post(
            "/api/events/order-status", json=_status_event("evt-ds-b", "ORD-DS-1", "DISPATCHED")
        )

        body = client.get("/api/dashboard?facility_id=WH-01").json()

        assert body["order_states"]["pending"] == 1
        assert body["order_states"]["picking"] == 1
        assert body["order_states"]["dispatched"] == 1
        # DISPATCHED is not backlog.
        assert body["backlog_orders"] == 2
        assert body["backlog_work_units"] == 3.0


async def _noop_execution(action_id: str, plan_id: str, facility_id: str) -> None:
    """Stand in for the n8n handoff in tests that do not exercise it."""


def _approve(client: TestClient, plan_id: str = "buy-the-hour"):
    """Approve one recovery plan for the demo facility."""
    return client.post(f"/api/recovery-plans/{plan_id}/approve?facility_id=WH-01")


def test_approval_hands_the_action_to_n8n_after_it_is_committed(monkeypatch):
    """Trigger execution, and only once the action is readable."""
    seen: list[dict] = []

    async def _capture(action_id: str, plan_id: str, facility_id: str) -> None:
        """Check the action is durable from outside the request's transaction."""
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT status FROM recovery_actions WHERE action_id = $1", action_id
            )
        finally:
            await conn.close()
        seen.append({"action_id": action_id, "committed": row is not None})

    monkeypatch.setattr("app.routers.simulation.request_execution", _capture)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-act-1", "ORD-ACT-1"))
        approval = _approve(client)

    assert approval.status_code == 200
    assert len(seen) == 1
    assert seen[0]["action_id"] == approval.json()["action_id"]
    # An uncommitted transaction would have made this invisible.
    assert seen[0]["committed"] is True


def test_second_approval_is_rejected_while_one_is_executing(monkeypatch):
    """Stop a double-click from making n8n apply the intervention twice."""
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-act-2", "ORD-ACT-2"))
        first = _approve(client)

        second = _approve(client, "manage-the-miss")

        assert first.status_code == 200
        assert second.status_code == 409
        actions = client.get("/api/recovery-actions?facility_id=WH-01").json()
        assert len(actions) == 1


def test_approval_is_allowed_again_once_the_action_completes(monkeypatch):
    """Let the operator act again after an execution resolves."""
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-act-3", "ORD-ACT-3"))
        first = _approve(client)
        client.post(
            f"/api/recovery-actions/{first.json()['action_id']}/status",
            json={"status": "SUCCESS"},
        )

        second = _approve(client, "manage-the-miss")

        assert second.status_code == 200


def test_successful_action_updates_only_the_facility_policy(monkeypatch):
    """Apply the executed parameters as defaults without touching orders."""
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-act-4", "ORD-ACT-4"))
        before = client.get("/api/orders?facility_id=WH-01").json()["items"][0]["promised_dispatch_at"]
        approval = _approve(client, "manage-the-miss")
        planned = client.get(
            f"/api/recovery-actions/{approval.json()['action_id']}"
        ).json()["capacity_per_hour"]

        client.post(
            f"/api/recovery-actions/{approval.json()['action_id']}/status",
            json={"status": "SUCCESS"},
        )

        # No fresh telemetry yet, so the dashboard falls back to the new baseline.
        dashboard = client.get("/api/dashboard?facility_id=WH-01").json()
        assert dashboard["fulfillment_work_units_per_hour"] == pytest.approx(planned)
        # The customer's original deadline is untouched.
        assert client.get("/api/orders?facility_id=WH-01").json()["items"][0]["promised_dispatch_at"] == before


def test_failed_action_leaves_the_facility_policy_alone(monkeypatch):
    """Refuse to bank an intervention that never happened."""
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        # Established rather than assumed, so a fixture regression fails loudly.
        before = client.get("/api/dashboard?facility_id=WH-01").json()["fulfillment_work_units_per_hour"]
        assert before == 52

        approval = _approve(client, "manage-the-miss")
        client.post(
            f"/api/recovery-actions/{approval.json()['action_id']}/status",
            json={"status": "FAILED", "error_detail": "simulator unreachable"},
        )

        assert client.get("/api/dashboard?facility_id=WH-01").json()["fulfillment_work_units_per_hour"] == before


def test_recovery_actions_can_be_filtered_by_status(monkeypatch):
    """Expose execution history, not only work waiting to be collected."""
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        approval = _approve(client)
        client.post(
            f"/api/recovery-actions/{approval.json()['action_id']}/status",
            json={"status": "FAILED", "error_detail": "simulator unreachable"},
        )

        default = client.get("/api/recovery-actions?facility_id=WH-01").json()
        pending = client.get(
            "/api/recovery-actions", params={"facility_id": "WH-01", "status": "PENDING"}
        ).json()
        failed = client.get(
            "/api/recovery-actions", params={"facility_id": "WH-01", "status": "FAILED"}
        ).json()

        # The n8n executor polls the unfiltered endpoint and takes the first
        # item, so a completed action must never appear there.
        assert default == []
        assert pending == []
        assert failed[0]["error_detail"] == "simulator unreachable"


def test_pending_actions_are_returned_oldest_first(monkeypatch):
    """Keep executor collection FIFO across repeated approvals."""
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        first = _approve(client)
        client.post(
            f"/api/recovery-actions/{first.json()['action_id']}/status",
            json={"status": "SUCCESS"},
        )
        second = _approve(client, "manage-the-miss")

        queue = client.get("/api/recovery-actions?facility_id=WH-01").json()

        assert [a["action_id"] for a in queue] == [second.json()["action_id"]]
        assert queue[0]["status"] == "PENDING"


def test_single_order_is_reachable_beyond_the_first_page():
    """Look an order up by id rather than paging the whole queue to find it."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(6):
            client.post("/api/events/orders", json=_order_event(f"evt-one-{i}", f"ORD-ONE-{i}"))

        # Deliberately outside a small page taken from the top of the queue.
        page = client.get(
            "/api/orders", params={"facility_id": "WH-01", "limit": 2, "offset": 0}
        ).json()
        assert not any(o["order_id"] == "ORD-ONE-5" for o in page["items"])

        r = client.get("/api/orders/ORD-ONE-5?facility_id=WH-01")

        assert r.status_code == 200
        assert r.json()["order_id"] == "ORD-ONE-5"


def test_single_pending_order_carries_its_scheduler_fields():
    """Describe a queued order the same way the list endpoint does."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-one-a", "ORD-ONE-A"))

        detail = client.get("/api/orders/ORD-ONE-A?facility_id=WH-01").json()
        listed = next(
            o
            for o in client.get("/api/orders?facility_id=WH-01").json()["items"]
            if o["order_id"] == "ORD-ONE-A"
        )

        # Anything derived from the evaluation time legitimately differs between
        # two separate requests; the breakdown carries the same urgency and age
        # terms the score is built from.
        time_dependent = (
            "priority_score",
            "predicted_dispatch_at",
            "priority_breakdown",
            "work_complete_at",
        )
        invariant = {
            key: value for key, value in detail.items() if key not in time_dependent
        }
        assert invariant == {k: v for k, v in listed.items() if k in invariant}
        assert detail["queue_position"] == listed["queue_position"]
        assert detail["sla_status"] == listed["sla_status"]
        assert detail["queue_position"] is not None
        assert detail["sla_status"] is not None
        assert detail["predicted_dispatch_at"] is not None


def test_single_non_pending_order_has_null_scheduler_fields():
    """Leave scheduler fields empty for an order the queue no longer owns."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-one-b", "ORD-ONE-B"))
        client.post(
            "/api/events/order-status", json=_status_event("evt-one-c", "ORD-ONE-B", "DISPATCHED")
        )

        detail = client.get("/api/orders/ORD-ONE-B?facility_id=WH-01").json()

        assert detail["status"] == "DISPATCHED"
        assert detail["queue_position"] is None
        assert detail["sla_status"] is None
        assert detail["predicted_dispatch_at"] is None


def test_unknown_order_returns_404():
    """Distinguish a missing order from an empty page."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        r = client.get("/api/orders/ORD-NOPE?facility_id=WH-01")

        assert r.status_code == 404
        assert "ORD-NOPE" in r.json()["detail"]


def test_order_from_another_facility_is_not_returned():
    """Keep the lookup scoped to the requested facility."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-one-d", "ORD-ONE-D"))

        r = client.get("/api/orders/ORD-ONE-D", params={"facility_id": "WH-99"})

        assert r.status_code == 404


def test_at_risk_path_still_wins_over_the_order_id_parameter():
    """Keep /orders/at-risk from being captured as an order identity."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        r = client.get("/api/orders/at-risk?facility_id=WH-01")

        assert r.status_code == 200
        assert "items" in r.json() and "total" in r.json()


def test_work_in_progress_delays_predicted_dispatch():
    """Stop a floor backlog from hiding SLA risk in the read APIs.

    An order already PICKING is not reordered, but it draws on the same
    throughput, so the pending queue behind it must be predicted later.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event("evt-wip-1", "ORD-WIP-1", work_units=5.0),
        )
        before = client.get("/api/orders/ORD-WIP-1?facility_id=WH-01").json()["work_complete_at"]

        # Release a large order to the floor; it leaves the reorderable queue.
        client.post(
            "/api/events/orders",
            json=_order_event("evt-wip-2", "ORD-WIP-2", work_units=500.0),
        )
        client.post(
            "/api/events/order-status",
            json=_status_event("evt-wip-3", "ORD-WIP-2", "PICKING"),
        )

        after = client.get("/api/orders/ORD-WIP-1?facility_id=WH-01").json()

        assert after["queue_position"] == 1, "still first in the pending queue"
        # Asserted on completion rather than dispatch: with a carrier cutoff,
        # both may still make the same collection, and dispatch is what the
        # collection decides. The work itself genuinely takes longer.
        assert after["work_complete_at"] > before, "but the floor finishes it later"


def test_ready_orders_do_not_delay_the_queue():
    """Leave finished fulfillment work out of the capacity calculation."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event("evt-rdy-1", "ORD-RDY-1", work_units=5.0),
        )
        baseline = client.get("/api/orders/ORD-RDY-1?facility_id=WH-01").json()["work_complete_at"]

        client.post(
            "/api/events/orders",
            json=_order_event("evt-rdy-2", "ORD-RDY-2", work_units=500.0),
        )
        client.post(
            "/api/events/order-status",
            json=_status_event("evt-rdy-3", "ORD-RDY-2", "READY"),
        )

        after = client.get("/api/orders/ORD-RDY-1?facility_id=WH-01").json()["work_complete_at"]

        # READY work is complete and only awaiting dispatch, so it draws nothing.
        # The two predictions differ only by the wall-clock time between reads;
        # 500 work units of capacity draw would have moved this by hours.
        drift = abs(
            datetime.fromisoformat(after) - datetime.fromisoformat(baseline)
        )
        assert drift < timedelta(seconds=30), drift


def test_live_reads_and_what_if_share_one_engine():
    """Prove the two entry points are the same engine (spec section 12).

    A What-If with no overrides is the live state by definition. If these two
    paths ever disagreed, the product's central promise -- that the simulation
    you trust is the system you run -- would be broken.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(6):
            client.post(
                "/api/events/orders",
                json=_order_event(f"evt-parity-{i}", f"ORD-PARITY-{i}", work_units=40.0),
            )
        client.post(
            "/api/events/order-status",
            json=_status_event("evt-parity-s", "ORD-PARITY-0", "PICKING"),
        )

        dashboard = client.get("/api/dashboard?facility_id=WH-01").json()
        simulated = client.post("/api/simulations?facility_id=WH-01", json={}).json()

        assert simulated["baseline"] == simulated["simulated"], (
            "an empty What-If must reproduce the baseline exactly"
        )
        assert dashboard["sla_counts"] == {
            "safe": simulated["baseline"]["safe_count"],
            "watch": simulated["baseline"]["watch_count"],
            "at_risk": simulated["baseline"]["at_risk_count"],
            "breached": simulated["baseline"]["breached_count"],
        }
        assert dashboard["backlog_orders"] == simulated["baseline"]["backlog_orders"]
        assert dashboard["risk_level"] == simulated["baseline"]["risk_level"]


def test_order_status_is_explainable_number_by_number():
    """Every status must be drillable to the arithmetic behind it."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event("evt-why-1", "ORD-WHY-1", work_units=2.5),
        )

        order = client.get("/api/orders/ORD-WHY-1?facility_id=WH-01").json()

        parts = order["priority_breakdown"]
        assert order["priority_score"] == pytest.approx(
            parts["sla_urgency"]
            + parts["aging_bonus"]
            - parts["workload_penalty"]
            + parts["adjustment"]
        )
        # Queue position, work ahead and throughput together reproduce the
        # predicted dispatch time the status was derived from.
        assert order["queue_position"] == 1
        assert order["work_units_ahead"] == 0.0
        assert order["throughput_assumed"] > 0


def test_ingest_classifies_work_units_from_attributes():
    """Derive work units server-side rather than trusting the producer."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event(
                "evt-cls-1",
                "ORD-CLS-1",
                work_units=99.0,  # deliberately absurd
                line_count=8,
                unit_count=8,
                special_handling=False,
            ),
        )

        order = client.get("/api/orders/ORD-CLS-1?facility_id=WH-01").json()

        assert order["work_units"] == 2.0, "classified, not the declared 99.0"


def test_ingest_without_attributes_keeps_the_declared_value():
    """Producers that have not adopted the attributes still ingest cleanly."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event("evt-cls-2", "ORD-CLS-2", work_units=1.5),
        )

        assert client.get("/api/orders/ORD-CLS-2?facility_id=WH-01").json()["work_units"] == 1.5


def test_segment_round_trips_for_queue_levers_to_target():
    """Persist the cohort a recovery lever protects or defers."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post(
            "/api/events/orders",
            json=_order_event("evt-seg-1", "ORD-SEG-1", segment="FIRST_TIME"),
        )
        client.post("/api/events/orders", json=_order_event("evt-seg-2", "ORD-SEG-2"))

        first = client.get("/api/orders/ORD-SEG-1?facility_id=WH-01").json()
        plain = client.get("/api/orders/ORD-SEG-2?facility_id=WH-01").json()

        assert first["segment"] == "FIRST_TIME"
        assert plain["segment"] is None


def test_projected_arrivals_are_reported_apart_from_placed_orders():
    """Never let a hypothetical order be counted as a real one.

    Projected arrivals show what is coming, but nobody has placed them. Folding
    them into the same counts would let a plan claim it rescued orders that do
    not exist.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(4):
            client.post(
                "/api/events/orders",
                json=_order_event(f"evt-proj-{i}", f"ORD-PROJ-{i}", work_units=2.0),
            )

        without = client.post("/api/simulations?facility_id=WH-01", json={}).json()
        with_horizon = client.post(
            "/api/simulations?facility_id=WH-01", json={"projection_horizon_minutes": 120}
        ).json()

        assert without["baseline"]["projected_arrivals"] is None
        arrivals = with_horizon["baseline"]["projected_arrivals"]
        assert arrivals is not None and arrivals["orders"] > 0

        placed = ("safe_count", "watch_count", "at_risk_count", "breached_count")
        assert [with_horizon["baseline"][k] for k in placed] == [
            without["baseline"][k] for k in placed
        ], "projecting the future must not change the count of real orders"


def test_inflow_plan_is_offered_and_approvable_with_the_same_horizon(monkeypatch):
    """An inflow lever acts only on arrivals, so the horizon must reach approval too.

    Approval re-derives the plans. Without the horizon it re-derived them with
    no projection, so a plan the operator had just been shown was refused.
    """
    monkeypatch.setattr("app.routers.simulation.request_execution", _noop_execution)
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(4):
            client.post(
                "/api/events/orders",
                json=_order_event(f"evt-tap-{i}", f"ORD-TAP-{i}", work_units=2.0),
            )

        plans = client.get(
            "/api/recovery-plans?facility_id=WH-01&projection_horizon_minutes=240"
        ).json()
        offered = {plan["plan_id"] for plan in plans["plans"]}
        blocked = {lever["id"] for lever in plans["unavailable_levers"]}
        assert "close-the-tap" in offered
        assert "PAUSE_PROMO" not in blocked

        without = client.post("/api/recovery-plans/close-the-tap/approve?facility_id=WH-01")
        assert without.status_code == 409, "no horizon means no projection to act on"

        approved = client.post(
            "/api/recovery-plans/close-the-tap/approve"
            "?facility_id=WH-01&projection_horizon_minutes=240"
        )
        assert approved.status_code == 200, approved.text


def test_demand_multiplier_scales_projected_arrivals():
    """Make closing the tap upstream answerable by the same scheduler."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(4):
            client.post(
                "/api/events/orders",
                json=_order_event(f"evt-mult-{i}", f"ORD-MULT-{i}", work_units=2.0),
            )

        response = client.post(
            "/api/simulations?facility_id=WH-01",
            json={"projection_horizon_minutes": 120, "demand_multiplier": 0.5},
        ).json()

        baseline = response["baseline"]["projected_arrivals"]["work_units"]
        simulated = response["simulated"]["projected_arrivals"]["work_units"]

        assert simulated < baseline, "half the arrivals is half the work"
        assert response["applied_demand_multiplier"] == 0.5
        assert response["applied_projection_horizon_minutes"] == 120


def test_demand_multiplier_without_a_horizon_is_refused():
    """Reject an assumption with no future to apply it to."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        response = client.post("/api/simulations?facility_id=WH-01", json={"demand_multiplier": 0.5})

        assert response.status_code == 422
        assert "projection_horizon_minutes" in response.json()["detail"]


def test_projected_orders_never_leak_into_live_reads():
    """A simulation must not put orders nobody placed onto the dashboard."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-leak-1", "ORD-LEAK-1"))
        before = client.get("/api/orders?facility_id=WH-01").json()["total"]

        client.post("/api/simulations?facility_id=WH-01", json={"projection_horizon_minutes": 240})

        assert client.get("/api/orders?facility_id=WH-01").json()["total"] == before
        assert all(
            not o["order_id"].startswith("PROJ-")
            for o in client.get("/api/orders?facility_id=WH-01").json()["items"]
        )


def test_orders_can_be_filtered_by_status():
    """Filter in the database, because a page of results cannot be filtered.

    Scheduled orders sort first, so a client filtering one page would see only
    PENDING and report every other state as empty -- which is exactly what the
    Orders screen did.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        for i in range(6):
            client.post(
                "/api/events/orders", json=_order_event(f"evt-flt-{i}", f"ORD-FLT-{i}")
            )
        client.post(
            "/api/events/order-status", json=_status_event("evt-flt-a", "ORD-FLT-0", "PICKING")
        )
        client.post(
            "/api/events/order-status",
            json=_status_event("evt-flt-b", "ORD-FLT-1", "DISPATCHED"),
        )

        working = client.get(
            "/api/orders",
            params={"facility_id": "WH-01", "status": ["PICKING", "PACKED", "READY"]},
        ).json()
        done = client.get(
            "/api/orders", params={"facility_id": "WH-01", "status": "DISPATCHED"}
        ).json()
        queued = client.get(
            "/api/orders", params={"facility_id": "WH-01", "status": "PENDING"}
        ).json()
        every = client.get("/api/orders?facility_id=WH-01").json()

        assert working["total"] == 1
        assert [o["order_id"] for o in working["items"]] == ["ORD-FLT-0"]
        assert done["total"] == 1 and done["items"][0]["order_id"] == "ORD-FLT-1"
        assert queued["total"] == 4
        assert every["total"] == 6, "an unfiltered read still returns everything"


def test_filtered_orders_keep_their_scheduler_fields():
    """A filtered pending read is still a scheduled read."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        client.post("/api/events/orders", json=_order_event("evt-flt-s", "ORD-FLT-S"))

        queued = client.get(
            "/api/orders", params={"facility_id": "WH-01", "status": "PENDING"}
        ).json()

        assert queued["items"][0]["queue_position"] == 1
        assert queued["items"][0]["sla_status"] is not None


def test_demo_controls_are_absent_without_a_simulator(monkeypatch):
    """Controls that reset data must not exist where they were not configured."""
    monkeypatch.setattr(settings, "simulator_url", None)

    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, EVENTS_TEST_ACCOUNT)
        status = client.get("/api/demo/status").json()
        reset = client.post("/api/demo/reset?facility_id=WH-01")

        assert status["enabled"] is False
        assert reset.status_code == 404
        assert "disabled" in reset.json()["detail"]
