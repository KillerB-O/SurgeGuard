"""Test simulator control endpoints without requiring n8n."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from simulator.app import app, runner


def test_scenario_controls_update_status(monkeypatch):
    """Expose reset, start, stage, and capacity controls for orchestration."""
    monkeypatch.setattr(runner, "emit_snapshot", lambda **kwargs: None)

    with TestClient(app) as client:
        reset = client.post("/scenario/reset")
        started = client.post("/scenario/start")
        advanced = client.post("/scenario/next-stage")
        capacity = client.post("/scenario/set-capacity", json={"capacity_per_hour": 75})
        promise = client.post(
            "/scenario/set-dispatch-promise", json={"dispatch_promise_hours": 30}
        )

    assert reset.json()["stage"] == "NORMAL"
    assert started.json()["started"] is True
    assert advanced.json()["stage"] == "SURGE_1"
    assert capacity.json()["capacity_per_hour"] == 75
    assert promise.json()["dispatch_promise_hours"] == 30


def test_run_stage_returns_event_counts(monkeypatch):
    """Run-stage control returns the counts n8n can use for observability."""
    monkeypatch.setattr(
        runner,
        "run_stage",
        lambda **kwargs: {
            "orders_sent": 4,
            "status_events_sent": 0,
            "fulfillment_snapshots_sent": 1,
        },
    )

    with TestClient(app) as client:
        response = client.post("/scenario/run-stage")

    assert response.status_code == 200
    assert response.json()["orders_sent"] == 4
    assert response.json()["status_events_sent"] == 0


def test_capacity_change_and_status_advance_publish_telemetry(monkeypatch):
    """Operational controls publish the fresh facts consumed by the backend."""
    emitted = []
    monkeypatch.setattr(runner, "emit_snapshot", lambda **kwargs: emitted.append(kwargs))
    monkeypatch.setattr(runner, "advance_statuses", lambda **kwargs: 3)

    with TestClient(app) as client:
        capacity = client.post("/scenario/set-capacity", json={"capacity_per_hour": 75})
        advance = client.post("/scenario/advance-statuses")

    assert capacity.status_code == 200
    assert advance.json()["status_events_sent"] == 3
    assert len(emitted) == 2


def test_arrival_multiplier_scales_the_demand_a_stage_generates(monkeypatch):
    """An approved inflow lever has to reach the thing generating orders.

    PAUSE_PROMO and HIDE_EXPRESS_AT_CHECKOUT reduce future arrivals. Without a
    control here they could only ever be projected, never executed, so the plan
    would report a change that nothing carried out.
    """
    from simulator import app as module

    client = TestClient(module.app)
    baseline = client.get("/scenario/status").json()["demand_target_wu_per_hour"]

    response = client.post("/scenario/set-arrival-multiplier", json={"arrival_multiplier": 0.5})

    assert response.status_code == 200
    assert response.json()["demand_target_wu_per_hour"] == baseline // 2


def test_run_stage_with_at_stamps_the_given_instant(monkeypatch):
    """A tick given an absolute `at` stamps its events there, not at hours_ago."""
    captured = {}

    def fake_run_stage(**kwargs):
        captured.update(kwargs)
        return {"orders_sent": 0, "status_events_sent": 0, "fulfillment_snapshots_sent": 1}

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)

    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with TestClient(app) as client:
        response = client.post("/scenario/run-stage", json={"at": at.isoformat()})

    assert response.status_code == 200
    assert captured["anchor"] == at


def test_run_stage_with_naive_at_is_treated_as_utc(monkeypatch):
    """A caller that omits the offset still gets an unambiguous instant.

    Every other timestamp in the simulator is UTC-aware (see `_tick_anchor`'s
    hours_ago path, and `runner`'s `datetime.now(UTC)` fallbacks); a naive `at`
    would otherwise slip an un-comparable timestamp into that world.
    """
    captured = {}

    def fake_run_stage(**kwargs):
        captured.update(kwargs)
        return {"orders_sent": 0, "status_events_sent": 0, "fulfillment_snapshots_sent": 1}

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)

    naive_at = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001 -- naive on purpose
    with TestClient(app) as client:
        response = client.post("/scenario/run-stage", json={"at": naive_at.isoformat()})

    assert response.status_code == 200
    assert captured["anchor"] == naive_at.replace(tzinfo=UTC)


def test_run_stage_without_at_behaves_as_before(monkeypatch):
    """Omitting `at` falls back to the existing hours_ago-from-now behaviour."""
    captured = {}

    def fake_run_stage(**kwargs):
        captured.update(kwargs)
        return {"orders_sent": 0, "status_events_sent": 0, "fulfillment_snapshots_sent": 1}

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)

    before = datetime.now(UTC)
    with TestClient(app) as client:
        response = client.post("/scenario/run-stage", json={"hours_ago": 3})
    after = datetime.now(UTC)

    assert response.status_code == 200
    anchor = captured["anchor"]
    assert before - timedelta(hours=3) <= anchor <= after - timedelta(hours=3)


def test_advance_statuses_with_at_stamps_the_given_instant(monkeypatch):
    """`advance-statuses` fans `at` out to both the status events and the
    snapshot it emits -- both must land on the given instant, not on now."""
    captured = {}

    def fake_advance_statuses(**kwargs):
        captured["advance_statuses"] = kwargs
        return 0

    def fake_emit_snapshot(**kwargs):
        captured["emit_snapshot"] = kwargs

    monkeypatch.setattr(runner, "advance_statuses", fake_advance_statuses)
    monkeypatch.setattr(runner, "emit_snapshot", fake_emit_snapshot)

    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with TestClient(app) as client:
        response = client.post("/scenario/advance-statuses", json={"at": at.isoformat()})

    assert response.status_code == 200
    assert captured["advance_statuses"]["occurred_at"] == at
    assert captured["emit_snapshot"]["occurred_at"] == at


def test_run_stage_with_sim_hours_elapsed_uses_the_wave_not_the_ladder(monkeypatch):
    """A tick naming its place on the wave gets wave demand, not stage demand.

    The scenario is left at NORMAL (ladder demand 32), but `sim_hours_elapsed`
    names hour 8 -- the wave's peak, SURGE_3 (127). If the endpoint still
    called `_current_demand()` under the hood, this would see 32, not 127.
    """
    captured = {}

    def fake_run_stage(**kwargs):
        captured.update(kwargs)
        return {"orders_sent": 0, "status_events_sent": 0, "fulfillment_snapshots_sent": 1}

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)

    with TestClient(app) as client:
        client.post("/scenario/reset")
        response = client.post("/scenario/run-stage", json={"sim_hours_elapsed": 8})

    assert response.status_code == 200
    assert captured["demand_work_units"] == 127
    assert captured["demand_work_units"] != 32  # the NORMAL-stage ladder value


def test_run_stage_duration_hours_scales_demand_proportionally(monkeypatch):
    """A short tick gets a proportionally short slice of the wave's rate.

    At 60x speed a live tick covers about 2 simulated minutes (duration_hours
    ~= 0.033), not a full hour -- so it must generate proportionally less
    demand than a full-hour tick at the same point on the wave.
    """
    captured = []

    def fake_run_stage(**kwargs):
        captured.append(kwargs)
        return {"orders_sent": 0, "status_events_sent": 0, "fulfillment_snapshots_sent": 1}

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)

    with TestClient(app) as client:
        client.post("/scenario/reset")
        client.post(
            "/scenario/run-stage", json={"sim_hours_elapsed": 8, "duration_hours": 1.0}
        )
        client.post(
            "/scenario/run-stage", json={"sim_hours_elapsed": 8, "duration_hours": 0.033}
        )

    full_hour_demand = captured[0]["demand_work_units"]
    short_tick_demand = captured[1]["demand_work_units"]

    assert full_hour_demand == 127
    assert short_tick_demand == pytest.approx(127 * 0.033)
    assert short_tick_demand < full_hour_demand


def test_advance_statuses_duration_hours_scales_the_floor_budget(monkeypatch):
    """A half-hour tick hands the floor half its hourly budget, not the whole thing.

    Hardcoding a full hour here would let a fraction-of-a-minute live tick
    drain the backlog at a full hour's throughput -- the backlog the wave is
    supposed to build would never accumulate.
    """
    captured = {}

    def fake_advance_statuses(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(runner, "advance_statuses", fake_advance_statuses)
    monkeypatch.setattr(runner, "emit_snapshot", lambda **kwargs: None)

    with TestClient(app) as client:
        client.post("/scenario/reset")
        client.post("/scenario/set-capacity", json={"capacity_per_hour": 100})
        response = client.post("/scenario/advance-statuses", json={"duration_hours": 0.5})

    assert response.status_code == 200
    assert captured["work_units_budget"] == 50


def test_run_stage_and_advance_statuses_without_new_fields_are_unaffected(monkeypatch):
    """Omitting `sim_hours_elapsed` and `duration_hours` reproduces today's
    behaviour exactly: ladder demand for run-stage, a full hour's budget for
    advance-statuses."""
    run_stage_captured = {}
    advance_captured = {}

    def fake_run_stage(**kwargs):
        run_stage_captured.update(kwargs)
        return {"orders_sent": 0, "status_events_sent": 0, "fulfillment_snapshots_sent": 1}

    def fake_advance_statuses(**kwargs):
        advance_captured.update(kwargs)
        return 0

    monkeypatch.setattr(runner, "run_stage", fake_run_stage)
    monkeypatch.setattr(runner, "advance_statuses", fake_advance_statuses)
    monkeypatch.setattr(runner, "emit_snapshot", lambda **kwargs: None)

    with TestClient(app) as client:
        client.post("/scenario/reset")
        client.post("/scenario/set-capacity", json={"capacity_per_hour": 100})
        client.post("/scenario/run-stage")
        client.post("/scenario/advance-statuses")

    assert run_stage_captured["demand_work_units"] == 32  # NORMAL-stage ladder value
    assert advance_captured["work_units_budget"] == 100  # full hour, unscaled


def test_arrival_multiplier_is_restored_by_a_reset():
    """A demo reset must return the shop to full demand."""
    from simulator import app as module

    client = TestClient(module.app)
    client.post("/scenario/reset")
    baseline = client.get("/scenario/status").json()["demand_target_wu_per_hour"]
    client.post("/scenario/set-arrival-multiplier", json={"arrival_multiplier": 0.25})

    client.post("/scenario/reset")

    assert client.get("/scenario/status").json()["demand_target_wu_per_hour"] == baseline
