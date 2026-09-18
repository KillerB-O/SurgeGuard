"""An approved plan must change the world, not just the projection.

Before this, only a capacity change was executable: the action row carried
capacity, promise and demand multiplier, and n8n knew how to apply exactly one
of them. A QUEUE or PROMISE plan therefore projected a large improvement,
reported SUCCESS, and left the queue untouched.

These tests pin the executable half of the loop: what an approved plan writes,
and what a live read shows afterwards.
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

# Same reasoning as test_events_integration.py: Phase 3 (ACCESS-01) gates the
# read/simulation/recovery-plan endpoints this file exercises behind a
# session, and this file's subject is executable recovery plans, not auth.
INTERVENTIONS_TEST_ACCOUNT = "interventions-tests@example.com"


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
            # P4's derived throughput reads order_status_transitions for
            # WH-01 with no per-test scoping, so a leftover row pollutes a
            # later test's dashboard figures.
            await conn.execute("DELETE FROM order_status_transitions")
            await conn.execute("DELETE FROM pending_status_events")
            # P6: capacity_commitments has the same per-facility leak risk,
            # and execution_adapter must reset to the default or a test in
            # this file could inherit 'manual' from test_execution_adapters.py
            # running earlier in the same session.
            await conn.execute("DELETE FROM capacity_commitments")
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


def _order(client, event_id: str, order_id: str, *, hours: float, segment=None):
    """Place one order promised `hours` from now."""
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


def _queue(client) -> list[str]:
    """Order ids in the live queue, most urgent first."""
    items = client.get(
        "/api/orders", params={"facility_id": "WH-01", "status": "PENDING", "limit": 100}
    ).json()["items"]
    return [o["order_id"] for o in sorted(items, key=lambda o: o["queue_position"])]


def test_an_approved_queue_plan_reorders_the_live_queue():
    """A protected segment must actually move up the real queue.

    The plan projects a reorder by feeding priority adjustments to the
    scheduler. Unless those adjustments survive approval, the projection
    describes a queue that never exists.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        # Identical promises, so urgency and age cannot separate them and the
        # lever's adjustment is the only thing that can decide the order.
        _order(client, "evt-q1", "ORD-PLAIN", hours=20)
        _order(client, "evt-q2", "ORD-NEW", hours=20, segment="FIRST_TIME")

        before = _queue(client)
        assert set(before) == {"ORD-PLAIN", "ORD-NEW"}, before

        approval = client.post("/api/recovery-plans/protect-new-customers/approve?facility_id=WH-01").json()
        applied = client.post(f"/api/recovery-actions/{approval['action_id']}/apply")

        assert applied.status_code == 200
        assert _queue(client) == ["ORD-NEW", "ORD-PLAIN"], (
            "the approved queue lever did not reach the live queue"
        )


def _set_cutoff(hour: int, minute: int = 0) -> None:
    """Give WH-01 a carrier collection time, so cutoff-gated levers are feasible.

    EXTEND_CARRIER_CUTOFF's `requires_cutoff` precondition (levers.json) makes
    the whole catch-the-late-pickup posture unavailable when the facility has
    none -- and the clean_tables fixture resets WH-01 to NULL (continuous
    dispatch) before every test, matching the demo default.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _set() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "UPDATE facilities SET dispatch_cutoff_utc = $1 WHERE facility_id = 'WH-01'",
                datetime(2000, 1, 1, hour, minute, tzinfo=UTC).time(),
            )
        finally:
            await conn.close()

    asyncio.run(_set())


def _read_cutoff() -> object:
    """Return WH-01's currently persisted cutoff time."""
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _read():
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(
                "SELECT dispatch_cutoff_utc FROM facilities WHERE facility_id = 'WH-01'"
            )
        finally:
            await conn.close()

    return asyncio.run(_read())


def test_an_approved_cutoff_plan_actually_moves_the_collection_time():
    """EXTEND_CARRIER_CUTOFF must move the real cutoff, not just the projection.

    Before this, cutoff_shift was computed correctly for the plan's projected
    outcome but never applied anywhere: _apply_facility_policy wrote only
    capacity_per_hour and dispatch_promise_hours. Approving catch-the-late-
    pickup reported SUCCESS and changed nothing -- the next schedule reverted
    to the old cutoff as if nothing had happened. No endpoint exposes the raw
    facility row, so this reads the persisted column directly, the same way
    _set_cutoff writes it.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        # A cutoff fixed against the wall clock (not "hours from now") used to
        # sit inside EXTEND_CARRIER_CUTOFF's 15-minute lead time on whatever
        # rare run landed within 15 minutes of it, making catch-the-late-pickup
        # infeasible and this test flake with a KeyError on 'action_id'. Three
        # hours out is always well clear of that window.
        cutoff_at = datetime.now(UTC) + timedelta(hours=3)
        _set_cutoff(cutoff_at.hour, cutoff_at.minute)
        _order(client, "evt-cut1", "ORD-CUTOFF", hours=1)

        approval = client.post("/api/recovery-plans/catch-the-late-pickup/approve?facility_id=WH-01").json()
        applied = client.post(f"/api/recovery-actions/{approval['action_id']}/apply")

        assert applied.status_code == 200
        moved = _read_cutoff()
        shifted = (cutoff_at + timedelta(hours=1)).time()
        assert moved.hour == shifted.hour and moved.minute == shifted.minute, (
            f"EXTEND_CARRIER_CUTOFF's 60-minute shift was not persisted, got {moved}"
        )


def test_an_approved_promise_plan_rewrites_the_promise_and_keeps_the_original():
    """Re-promising is a real action, and it must be auditable.

    Moving a customer's deadline is only defensible if the original is kept and
    the change is attributable, so this checks both the new promise and the
    preserved one.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        _order(client, "evt-p1", "ORD-LATE", hours=1)
        original = client.get("/api/orders/ORD-LATE?facility_id=WH-01").json()["promised_dispatch_at"]

        approval = client.post("/api/recovery-plans/manage-the-miss/approve?facility_id=WH-01").json()
        client.post(f"/api/recovery-actions/{approval['action_id']}/apply")

        after = client.get("/api/orders/ORD-LATE?facility_id=WH-01").json()
        moved = datetime.fromisoformat(after["promised_dispatch_at"])
        was = datetime.fromisoformat(original)

        assert moved > was, "the promise was not extended"
        assert after["original_promised_dispatch_at"] == original, (
            "the original promise must be preserved for audit"
        )


def test_applying_an_action_twice_is_idempotent():
    """n8n retries. A second apply must not shift the promise again."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        _order(client, "evt-i1", "ORD-TWICE", hours=1)

        approval = client.post("/api/recovery-plans/manage-the-miss/approve?facility_id=WH-01").json()
        client.post(f"/api/recovery-actions/{approval['action_id']}/apply")
        once = client.get("/api/orders/ORD-TWICE?facility_id=WH-01").json()["promised_dispatch_at"]
        client.post(f"/api/recovery-actions/{approval['action_id']}/apply")
        twice = client.get("/api/orders/ORD-TWICE?facility_id=WH-01").json()["promised_dispatch_at"]

        assert once == twice, "re-applying moved the deadline a second time"


def test_an_approved_action_carries_the_effects_it_must_execute():
    """n8n cannot execute what the action does not describe.

    The three legacy columns can express a capacity change and nothing else, so
    the action also carries the posture's effects.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        _order(client, "evt-e1", "ORD-EFFECTS", hours=1, segment="FIRST_TIME")

        approval = client.post("/api/recovery-plans/protect-new-customers/approve?facility_id=WH-01").json()
        action = client.get(f"/api/recovery-actions/{approval['action_id']}").json()

        kinds = {e["kind"] for e in action["effects"]}
        assert "priority_adjustment" in kinds, action.get("effects")


def test_a_capacity_plan_still_reports_its_capacity_in_the_legacy_columns():
    """The existing n8n capacity path must keep working unchanged."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        _order(client, "evt-c1", "ORD-CAP", hours=1)

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        action = client.get(f"/api/recovery-actions/{approval['action_id']}").json()

        assert action["capacity_per_hour"] > 52.0


def _read_facility_capacity() -> float:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _read():
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(
                "SELECT capacity_per_hour FROM facilities WHERE facility_id = 'WH-01'"
            )
        finally:
            await conn.close()

    return asyncio.run(_read())


def test_success_no_longer_writes_capacity_straight_to_the_facility():
    """A capacity claim becomes a verified commitment, not an instant fact (P6).

    Before this, SUCCESS wrote capacity_per_hour to facilities the instant
    n8n's callback arrived, on every adapter including the simulator, and
    the number was believed forever with nothing to later say it was wrong.
    Now it becomes a capacity_commitments row, verified against measured
    throughput (app/verification.py) instead. get_throughput_signal already
    prefers derived telemetry over configured capacity, so the scheduler is
    unaffected -- capacity_per_hour stays the configured fallback, not a
    number a single approval can move on its own.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        _order(client, "evt-nc1", "ORD-NOCAP", hours=1)
        before = _read_facility_capacity()

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        status = client.post(
            f"/api/recovery-actions/{approval['action_id']}/status",
            json={"status": "SUCCESS"},
        )

        assert status.status_code == 200
        assert _read_facility_capacity() == before, (
            "capacity_per_hour must not move on SUCCESS alone"
        )


def test_a_capacity_commitment_is_visible_on_the_action_once_success_lands():
    """The frontend's verification badge (P6 companion work) needs this to exist.

    A capacity_commitments row is written internally when SUCCESS lands, but
    it is only useful to an operator if the action response actually carries
    it -- otherwise the UI has nowhere to read UNVERIFIED/ACHIEVED/PARTIAL/
    NOT_OBSERVED from.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        _order(client, "evt-cc1", "ORD-COMMIT", hours=1)

        approval = client.post("/api/recovery-plans/buy-the-hour/approve?facility_id=WH-01").json()
        client.post(
            f"/api/recovery-actions/{approval['action_id']}/status",
            json={"status": "SUCCESS"},
        )

        action = client.get(f"/api/recovery-actions/{approval['action_id']}").json()

        commitments = action["capacity_commitments"]
        assert len(commitments) == 1, commitments
        assert commitments[0]["lever_id"] == "EXTEND_SHIFT"
        assert commitments[0]["verification_status"] == "UNVERIFIED"
        assert commitments[0]["target_work_units_per_hour"] == action["capacity_per_hour"]


def test_what_if_baseline_matches_the_live_dashboard_under_an_intervention():
    """What-If's baseline must be the queue the operator is actually looking at.

    Live reads apply the priority adjustments an approved plan put in force.
    If the simulation baseline ignores them it compares a hypothetical against
    a queue that does not exist, and every recovery outcome is measured from
    the wrong starting point.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        # The queue has to be saturated for order to matter. With headroom
        # every order makes its promise whatever the sequence, and a reorder
        # changes nothing -- which is why a three-order version of this test
        # passes against the bug it is meant to catch.
        #
        # 80 orders x 2 work units against 52/hour is about 3 hours of
        # clearance, so the tight-promise cohort sits right on the edge and
        # any reshuffle moves somebody across it.
        for i in range(40):
            _order(client, f"evt-par-t{i}", f"ORD-PAR-T{i}", hours=3)
        for i in range(40):
            _order(client, f"evt-par-f{i}", f"ORD-PAR-F{i}", hours=14, segment="FIRST_TIME")

        approval = client.post("/api/recovery-plans/protect-new-customers/approve?facility_id=WH-01").json()
        client.post(f"/api/recovery-actions/{approval['action_id']}/apply")

        live = client.get("/api/dashboard?facility_id=WH-01").json()["sla_counts"]
        baseline = client.post("/api/simulations?facility_id=WH-01", json={}).json()["baseline"]

        assert (
            live["safe"],
            live["watch"],
            live["at_risk"],
            live["breached"],
        ) == (
            baseline["safe_count"],
            baseline["watch_count"],
            baseline["at_risk_count"],
            baseline["breached_count"]
        ), "What-If baseline diverged from the live dashboard"


def test_do_nothing_costs_more_than_buying_capacity():
    """Inaction must carry a number, or overtime can never be justified.

    Every lever has a cost band; do-nothing had NONE, so the operator's central
    comparison had a blank on one side. The late dispatch rate is what fills
    it: doing nothing leaves the facility measurably worse against the ceiling
    it is judged on.
    """
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        for i in range(60):
            _order(client, f"evt-cost-{i}", f"ORD-COST-{i}", hours=1)

        plans = {
            p["plan_id"]: p
            for p in client.get("/api/recovery-plans?facility_id=WH-01").json()["plans"]
        }
        nothing = plans["do-nothing"]["outcome"]["projected_late_dispatch_rate"]
        buying = plans["buy-the-hour"]["outcome"]["projected_late_dispatch_rate"]

        assert nothing > buying, "doing nothing must project a worse rate than acting"


def test_an_order_already_past_its_promise_is_one_miss_not_two():
    """The projected late rate is a share of orders placed, so it cannot pass 100%.

    An unfinished order past its promise is a realised miss in the database
    count and also a predicted breach in the schedule. Adding both counted it
    twice, and doing nothing on a queue of late orders projected 200%.
    """
    now = datetime.now(UTC)
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        for i in range(10):
            body = {
                "event_id": f"evt-past-{i}",
                "event_type": "order_created",
                "source": "commerce_sim",
                "order_id": f"ORD-PAST-{i}",
                "facility_id": "WH-01",
                "created_at": (now - timedelta(hours=3)).isoformat(),
                "promised_dispatch_at": (now - timedelta(hours=1)).isoformat(),
                "item_count": 1,
                "work_units": 2.0,
                "order_value": 500,
            }
            assert client.post("/api/events/orders", json=body).status_code == 200

        plans = {
            p["plan_id"]: p
            for p in client.get("/api/recovery-plans?facility_id=WH-01").json()["plans"]
        }

        assert plans["do-nothing"]["outcome"]["projected_late_dispatch_rate"] == pytest.approx(1.0)


def test_the_dashboard_separates_a_realised_miss_from_a_predicted_one():
    """One is a receipt, the other a warning; they must not be added together."""
    with TestClient(app, headers={"X-Service-Token": TEST_SERVICE_TOKEN}) as client:
        sign_in(client, INTERVENTIONS_TEST_ACCOUNT)
        for i in range(40):
            _order(client, f"evt-real-{i}", f"ORD-REAL-{i}", hours=1)

        dashboard = client.get("/api/dashboard?facility_id=WH-01").json()

        assert dashboard["realised_breaches"] == 0, "nothing is past its promise yet"
        assert dashboard["preventable_breaches"] >= 0
        assert dashboard["late_dispatch_threshold"] == pytest.approx(0.04)
