"""Test stage orchestration and backend-compatible event emission."""

from datetime import UTC, datetime

import pytest
from simulator.models import EventIngestionResponse, IngestionStatus, OrderStatus
from simulator.runner import SimulationRunner


class RecordingClient:
    """Capture outbound events without requiring n8n during unit tests."""

    def __init__(self) -> None:
        self.orders = []
        self.statuses = []
        self.snapshots = []

    def send_order(self, event):
        self.orders.append(event)
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)

    def send_order_status(self, event):
        self.statuses.append(event)
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)

    def send_fulfillment_snapshot(self, event):
        self.snapshots.append(event)
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)


def test_run_stage_uses_recovery_capacity_in_fulfillment_snapshot():
    """A recovery capacity change is reflected in emitted telemetry."""
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)

    result = runner.run_stage(
        demand_work_units=5,
        capacity_per_hour=75,
        anchor=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
    )

    assert result["orders_sent"] > 0
    assert result["status_events_sent"] == 0
    assert client.snapshots[0].work_units_completed_last_hour == 75
    assert all(event.source.value == "commerce_sim" for event in client.orders)
    assert client.statuses == []


def test_status_progression_and_snapshots_keep_open_orders_consistent():
    """Orders remain pending until explicit operational progression occurs."""
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    runner.run_stage(demand_work_units=5, capacity_per_hour=52, anchor=anchor)
    order_count = len(client.orders)
    assert client.snapshots[-1].open_orders == order_count

    sent = runner.advance_statuses(occurred_at=anchor)
    runner.emit_snapshot(capacity_per_hour=52, occurred_at=anchor)

    assert sent == order_count
    assert all(event.status == OrderStatus.PICKING for event in client.statuses)
    assert client.snapshots[-1].open_orders == order_count

    for _ in range(3):
        runner.advance_statuses(occurred_at=anchor)
    runner.emit_snapshot(capacity_per_hour=52, occurred_at=anchor)

    assert client.snapshots[-1].open_orders == 0


def _stage_counts(runner: SimulationRunner) -> dict[OrderStatus, int]:
    counts: dict[OrderStatus, int] = {}
    for status in runner.order_statuses.values():
        counts[status] = counts.get(status, 0) + 1
    return counts


def test_a_bounded_floor_keeps_work_at_every_station():
    """Every stage should hold work, not one stage at a time.

    Spending one shared budget most-advanced-first let an empty station hand
    its share downstream, so each call drained every stage completely and the
    floor marched in lockstep: picking, packing and staging read zero three
    ticks in four. A single snapshot cannot tell a lockstep wave from a filled
    pipeline -- it only samples one beat -- so this checks several consecutive
    ticks after giving the pipeline time to fill.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    # Four ticks to fill, then observe: demand well above capacity, so there is
    # always more work waiting than the floor can take.
    for _ in range(4):
        runner.run_stage(demand_work_units=127, capacity_per_hour=52, anchor=anchor)
        runner.advance_statuses(work_units_budget=52, occurred_at=anchor)

    for tick in range(3):
        runner.run_stage(demand_work_units=127, capacity_per_hour=52, anchor=anchor)
        runner.advance_statuses(work_units_budget=52, occurred_at=anchor)
        counts = _stage_counts(runner)

        for station in (OrderStatus.PICKING, OrderStatus.PACKED, OrderStatus.READY):
            assert counts.get(station, 0) > 0, (
                f"tick {tick}: {station.value} is empty, so the floor is still "
                f"moving in lockstep rather than as a pipeline -- {counts}"
            )


def test_end_to_end_throughput_still_matches_the_floor_capacity():
    """Splitting the budget must not change how much work actually completes.

    The backend schedules against `capacity_per_hour`; if the simulator
    dispatches at a different rate the SLA picture is measuring a floor that
    does not exist. A quarter of the budget at a quarter of an order's cost per
    step is the same number of completed orders an hour.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    capacity = 52.0

    for _ in range(6):
        runner.run_stage(demand_work_units=127, capacity_per_hour=capacity, anchor=anchor)
        runner.advance_statuses(work_units_budget=capacity, occurred_at=anchor)

    dispatched = [
        order_id
        for order_id, status in runner.order_statuses.items()
        if status == OrderStatus.DISPATCHED
    ]
    # The first ticks are spent filling the pipeline, so measure the last one.
    before = len(dispatched)
    runner.run_stage(demand_work_units=127, capacity_per_hour=capacity, anchor=anchor)
    runner.advance_statuses(work_units_budget=capacity, occurred_at=anchor)
    completed = [
        runner.order_work_units[order_id]
        for order_id, status in runner.order_statuses.items()
        if status == OrderStatus.DISPATCHED
    ]

    work_units_dispatched = sum(completed) - sum(
        runner.order_work_units[order_id] for order_id in dispatched
    )
    assert len(completed) - before > 0
    assert capacity * 0.8 <= work_units_dispatched <= capacity * 1.2, (
        f"dispatched {work_units_dispatched} work units against a floor rated "
        f"{capacity}; the simulator and the scheduler now disagree about capacity"
    )


def test_an_unbounded_floor_still_advances_everything():
    """No budget means no station limit -- the old behaviour is preserved."""
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    runner.run_stage(demand_work_units=127, capacity_per_hour=52, anchor=anchor)
    sent = runner.advance_statuses(occurred_at=anchor)

    assert sent == len(client.orders)


def test_every_tick_event_is_sent_exactly_once():
    """Posting a tick's events concurrently must not drop or duplicate any.

    The events in one tick are independent, so they go out several at a time
    rather than paying a round trip each in sequence. That is only safe if the
    fan-out still delivers each event once -- a dropped order silently shrinks
    the surge, and a duplicated one is rejected by the backend's idempotency
    claim and quietly lost too.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    result = runner.run_stage(demand_work_units=127, capacity_per_hour=52, anchor=anchor)
    order_ids = [event.order_id for event in client.orders]

    assert len(order_ids) == result["orders_sent"]
    assert len(set(order_ids)) == len(order_ids), "an order was sent twice"
    assert set(order_ids) == set(runner.order_statuses), "an order was never sent"

    sent = runner.advance_statuses(work_units_budget=52, occurred_at=anchor)
    status_ids = [event.order_id for event in client.statuses]

    assert len(status_ids) == sent
    assert len(set(status_ids)) == len(status_ids), "a status step was sent twice"


LIVE_TICK_SECONDS = 2.0
MAX_ORDER_WORK_UNITS = 2.5
LIFECYCLE = (
    OrderStatus.PENDING,
    OrderStatus.PICKING,
    OrderStatus.PACKED,
    OrderStatus.READY,
    OrderStatus.DISPATCHED,
)


def _work_completed(runner: SimulationRunner) -> float:
    """Work units the floor has actually performed so far.

    One order costs its work units to take all the way through, so a step is a
    quarter of that and an order's contribution is its position in the
    lifecycle.
    """
    return sum(
        LIFECYCLE.index(status) * runner.order_work_units[order_id] / (len(LIFECYCLE) - 1)
        for order_id, status in runner.order_statuses.items()
    )


@pytest.mark.parametrize("speed", [1.0, 25.0, 60.0])
def test_throughput_matches_capacity_at_any_clock_speed(speed: float):
    """A fractional budget must not buy a whole step at every station.

    The per-station guard tests spend *before* spending, so a station with a
    budget of 0.007 work units still advances an order costing 0.25. At 1x that
    makes the floor run about fifty times its rated capacity, so the backlog the
    wave is supposed to build drains as fast as it forms and the dashboard shows
    a calm facility in the middle of a surge.
    """
    capacity = 52.0
    duration_hours = LIVE_TICK_SECONDS * speed / 3600.0
    ticks = round(4.0 / duration_hours)

    runner = SimulationRunner(run_id="run-test", client=RecordingClient())
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    # Far more work than the floor can clear, so it is never starved and the
    # only thing limiting it is its own budget.
    runner.run_stage(demand_work_units=6000, capacity_per_hour=capacity, anchor=anchor)

    before = _work_completed(runner)
    for _ in range(ticks):
        runner.advance_statuses(
            work_units_budget=capacity * duration_hours, occurred_at=anchor
        )
    performed = _work_completed(runner) - before

    expected = capacity * duration_hours * ticks
    assert performed == pytest.approx(expected, rel=0.1), (
        f"at {speed}x the floor performed {performed:.1f} work units against a "
        f"rated {expected:.1f}; the simulator and the scheduler disagree"
    )


def test_irregular_tick_durations_still_tile_the_timeline():
    """Ticks fire late and unevenly, so throughput must follow summed duration.

    The live loop bills each tick for the real time that actually passed, which
    varies. Throughput has to track the total simulated time covered, not the
    number of ticks.
    """
    capacity = 52.0
    jitter = [0.004, 0.011, 0.002, 0.019, 0.007, 0.013, 0.003, 0.021] * 6

    runner = SimulationRunner(run_id="run-test", client=RecordingClient())
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    runner.run_stage(demand_work_units=6000, capacity_per_hour=capacity, anchor=anchor)

    before = _work_completed(runner)
    for duration in jitter:
        runner.advance_statuses(
            work_units_budget=capacity * duration, occurred_at=anchor
        )
    performed = _work_completed(runner) - before

    expected = capacity * sum(jitter)
    assert performed == pytest.approx(expected, rel=0.1)


def test_an_idle_station_does_not_bank_an_unbounded_burst():
    """Unspent capacity is carried, but a quiet floor must not store an hour.

    Carrying the remainder is what fixes fractional ticks, but a station with
    nothing in its status would otherwise accumulate every tick it sat out and
    discharge the lot the moment work arrived -- a floor that clears a backlog
    instantly because it was idle yesterday.
    """
    capacity = 52.0
    duration_hours = LIVE_TICK_SECONDS * 60.0 / 3600.0

    runner = SimulationRunner(run_id="run-test", client=RecordingClient())
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    # A long quiet stretch with nothing on the floor at all.
    for _ in range(200):
        runner.advance_statuses(
            work_units_budget=capacity * duration_hours, occurred_at=anchor
        )

    runner.run_stage(demand_work_units=600, capacity_per_hour=capacity, anchor=anchor)
    before = _work_completed(runner)
    runner.advance_statuses(
        work_units_budget=capacity * duration_hours, occurred_at=anchor
    )
    performed = _work_completed(runner) - before

    # One tick's allocation, plus the bounded carry, across four stations.
    ceiling = capacity * duration_hours + 4 * 2 * (MAX_ORDER_WORK_UNITS / 4)
    assert performed <= ceiling, (
        f"an idle floor banked {performed:.1f} work units and discharged them at "
        f"once; the carry is unbounded"
    )


def test_a_snapshot_reports_work_the_floor_actually_did():
    """Telemetry must be a measurement, not the configured number echoed back.

    Reporting `capacity_per_hour` made the backend's observed-versus-configured
    distinction decorative: the floor could never be seen falling behind,
    because the only evidence of its rate was the assumption about its rate.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    capacity = 52.0

    # Only a sliver of work exists, so the floor cannot spend its budget.
    runner.run_stage(demand_work_units=3, capacity_per_hour=capacity, anchor=anchor)
    runner.advance_statuses(
        work_units_budget=capacity, duration_hours=1.0, occurred_at=anchor
    )
    runner.emit_snapshot(capacity_per_hour=capacity, occurred_at=anchor)

    reported = client.snapshots[-1].work_units_completed_last_hour
    assert reported < capacity, (
        f"a floor with almost nothing to do reported {reported} against a "
        f"configured {capacity}; the snapshot is echoing the assumption"
    )


def test_a_stalled_floor_reports_zero():
    """A floor that did nothing must say so.

    This is the condition the risk engine understates today, and it cannot even
    be demonstrated while telemetry reports the configured capacity.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    capacity = 52.0

    runner.run_stage(demand_work_units=200, capacity_per_hour=capacity, anchor=anchor)
    # A budget of zero is a floor that has stopped.
    runner.advance_statuses(
        work_units_budget=0.0, duration_hours=1.0, occurred_at=anchor
    )
    runner.emit_snapshot(capacity_per_hour=capacity, occurred_at=anchor)

    assert client.snapshots[-1].work_units_completed_last_hour == 0.0


def test_setting_capacity_does_not_claim_it_is_already_achieved():
    """Approving a capacity lever must not manufacture its own evidence.

    The snapshot published on a capacity change exists to close the approval
    loop, and it still does. What it must not do is assert
    the new rate as work already completed, because that makes a plan's
    projected `breaches_avoided` come true by construction.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    runner.run_stage(demand_work_units=200, capacity_per_hour=52.0, anchor=anchor)
    runner.advance_statuses(
        work_units_budget=52.0, duration_hours=1.0, occurred_at=anchor
    )
    # The tick's own telemetry, published the way the live loop publishes it.
    runner.emit_snapshot(capacity_per_hour=52.0, occurred_at=anchor)
    measured = client.snapshots[-1].work_units_completed_last_hour

    # An operator approves overtime: capacity rises to 200 an hour.
    runner.emit_snapshot(capacity_per_hour=200.0, occurred_at=anchor)
    after = client.snapshots[-1]

    assert after.work_units_completed_last_hour != 200.0, (
        "the capacity change reported itself as work already completed"
    )
    assert after.work_units_completed_last_hour == pytest.approx(measured)
    # The loop still closes: a snapshot was published and open_orders is fresh.
    assert after.open_orders > 0


def test_measured_throughput_matches_capacity_once_the_floor_is_busy():
    """With tick fidelity fixed, measurement and configuration should agree.

    This is the acceptance test for replacing the echo with a measurement: on a
    saturated floor running at its rated capacity, the two must land together.
    Any gap now means a real disagreement rather than a hidden one.
    """
    client = RecordingClient()
    runner = SimulationRunner(run_id="run-test", client=client)
    anchor = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    capacity = 52.0
    duration_hours = LIVE_TICK_SECONDS * 60.0 / 3600.0

    runner.run_stage(demand_work_units=6000, capacity_per_hour=capacity, anchor=anchor)
    for _ in range(round(2.0 / duration_hours)):
        runner.advance_statuses(
            work_units_budget=capacity * duration_hours,
            duration_hours=duration_hours,
            occurred_at=anchor,
        )
    runner.emit_snapshot(capacity_per_hour=capacity, occurred_at=anchor)

    assert client.snapshots[-1].work_units_completed_last_hour == pytest.approx(
        capacity, rel=0.15
    )


def test_a_failed_post_is_not_swallowed_by_the_fan_out():
    """A dead backend must surface, not vanish into a worker thread."""

    class FailingClient(RecordingClient):
        def send_order(self, event):
            raise RuntimeError("backend unreachable")

    runner = SimulationRunner(run_id="run-test", client=FailingClient())

    with pytest.raises(RuntimeError, match="backend unreachable"):
        runner.run_stage(
            demand_work_units=127,
            capacity_per_hour=52,
            anchor=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        )
