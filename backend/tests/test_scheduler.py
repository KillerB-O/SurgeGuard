"""Test deterministic scheduling, priority, timing, and SLA classification."""

from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models import Order, OrderStatus, ScheduleResult, SLAStatus, SurgeRiskLevel
from app.scheduler import (
    AGING_BONUS_WEIGHT_PER_HOUR,
    AT_RISK_THRESHOLD_HOURS,
    WATCH_THRESHOLD_HOURS,
    WORKLOAD_PENALTY_WEIGHT,
    CutoffPolicy,
    FacilityRiskThresholds,
    OperatingCalendar,
    SlaThresholds,
    _classify_sla,
    classify_facility_risk,
    compute_schedule,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _order(order_id: str, status: OrderStatus, hours_until_due: float, work_units: float = 1.0) -> Order:
    now = datetime.now(UTC)
    return Order(
        order_id=order_id,
        facility_id="WH-01",
        created_at=now - timedelta(hours=1),
        promised_dispatch_at=now + timedelta(hours=hours_until_due),
        item_count=1,
        work_units=work_units,
        order_value=Decimal("100.00"),
        status=status,
    )


def test_empty_pending_orders_returns_zero_counts():
    now = datetime.now(UTC)
    result = compute_schedule(orders=[], capacity_per_hour=52.0, now=now)

    assert result.pending_orders == 0
    assert result.scheduled_orders == ()
    assert result.safe_count == result.watch_count == result.at_risk_count == result.breached_count == 0


def test_zero_or_negative_capacity_rejected():
    now = datetime.now(UTC)
    orders = [_order("ORD-1", OrderStatus.PENDING, hours_until_due=24)]

    with pytest.raises(ValueError):
        compute_schedule(orders=orders, capacity_per_hour=0, now=now)

    with pytest.raises(ValueError):
        compute_schedule(orders=orders, capacity_per_hour=-5, now=now)


def test_non_finite_capacity_rejected():
    """A bad What-If input (e.g. an upstream divide-by-zero) could hand
    the scheduler NaN or infinity -- these must be rejected the same way
    zero/negative capacity already is, not silently propagate into every
    predicted_dispatch_at as garbage.
    """
    now = datetime.now(UTC)
    orders = [_order("ORD-1", OrderStatus.PENDING, hours_until_due=24)]

    with pytest.raises(ValueError):
        compute_schedule(orders=orders, capacity_per_hour=float("nan"), now=now)

    with pytest.raises(ValueError):
        compute_schedule(orders=orders, capacity_per_hour=float("inf"), now=now)


def test_active_orders_are_not_reordered_or_scheduled():
    now = datetime.now(UTC)
    orders = [
        _order("ORD-1", OrderStatus.PICKING, hours_until_due=24),
        _order("ORD-2", OrderStatus.PENDING, hours_until_due=24),
    ]
    result = compute_schedule(orders=orders, capacity_per_hour=52.0, now=now)

    scheduled_ids = {so.order_id for so in result.scheduled_orders}
    assert "ORD-1" not in scheduled_ids
    assert "ORD-2" in scheduled_ids


def test_deterministic_repeatability():
    now = datetime.now(UTC)
    orders = [
        _order("ORD-1", OrderStatus.PENDING, hours_until_due=24),
        _order("ORD-2", OrderStatus.PENDING, hours_until_due=6),
    ]

    result_a = compute_schedule(orders=orders, capacity_per_hour=52.0, now=now)
    result_b = compute_schedule(orders=orders, capacity_per_hour=52.0, now=now)

    assert result_a.scheduled_orders == result_b.scheduled_orders


def test_sla_urgency_outranks_arrival_order():
    """A far-more-urgent order goes first even though it arrived later --
    priority is not just FIFO by creation time.
    """
    now = datetime.now(UTC)
    urgent_but_new = Order(
        order_id="ORD-URGENT",
        facility_id="WH-01",
        created_at=now - timedelta(hours=1),
        promised_dispatch_at=now + timedelta(hours=2),
        item_count=1,
        work_units=1.0,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )
    old_but_relaxed = Order(
        order_id="ORD-RELAXED",
        facility_id="WH-01",
        created_at=now - timedelta(hours=5),
        promised_dispatch_at=now + timedelta(hours=20),
        item_count=1,
        work_units=1.0,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )

    result = compute_schedule(
        orders=[old_but_relaxed, urgent_but_new], capacity_per_hour=52.0, now=now
    )
    by_id = {so.order_id: so for so in result.scheduled_orders}
    assert by_id["ORD-URGENT"].queue_position < by_id["ORD-RELAXED"].queue_position


def test_aging_bonus_protects_older_order_from_starvation():
    """Two orders with (near-)identical deadlines: the one that's been
    waiting longer should edge ahead, so it's never starved indefinitely
    by a stream of equally-urgent newer orders.
    """
    now = datetime.now(UTC)
    old_order = Order(
        order_id="ORD-OLD",
        facility_id="WH-01",
        created_at=now - timedelta(hours=10),
        promised_dispatch_at=now + timedelta(hours=24),
        item_count=1,
        work_units=1.0,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )
    new_order = Order(
        order_id="ORD-NEW",
        facility_id="WH-01",
        created_at=now,
        promised_dispatch_at=now + timedelta(hours=24),
        item_count=1,
        work_units=1.0,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )

    result = compute_schedule(orders=[new_order, old_order], capacity_per_hour=52.0, now=now)
    by_id = {so.order_id: so for so in result.scheduled_orders}
    assert by_id["ORD-OLD"].queue_position < by_id["ORD-NEW"].queue_position


def test_workload_penalty_deprioritizes_heavier_order_on_near_tie():
    """Same deadline, same age: the heavier order should be nudged behind
    the lighter one, since it costs more queue capacity to clear.
    """
    now = datetime.now(UTC)
    light_order = Order(
        order_id="ORD-LIGHT",
        facility_id="WH-01",
        created_at=now - timedelta(hours=1),
        promised_dispatch_at=now + timedelta(hours=24),
        item_count=1,
        work_units=1.0,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )
    heavy_order = Order(
        order_id="ORD-HEAVY",
        facility_id="WH-01",
        created_at=now - timedelta(hours=1),
        promised_dispatch_at=now + timedelta(hours=24),
        item_count=1,
        work_units=2.5,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )

    result = compute_schedule(orders=[heavy_order, light_order], capacity_per_hour=52.0, now=now)
    by_id = {so.order_id: so for so in result.scheduled_orders}
    assert by_id["ORD-LIGHT"].queue_position < by_id["ORD-HEAVY"].queue_position


def test_priority_score_is_deterministic_and_tie_broken_by_order_id():
    """Identical orders in every scored dimension should still produce a
    stable, deterministic order -- via the order_id tiebreak -- regardless
    of input list order.
    """
    now = datetime.now(UTC)
    a = _order("ORD-A", OrderStatus.PENDING, hours_until_due=10)
    b = _order("ORD-B", OrderStatus.PENDING, hours_until_due=10)

    result_1 = compute_schedule(orders=[a, b], capacity_per_hour=52.0, now=now)
    result_2 = compute_schedule(orders=[b, a], capacity_per_hour=52.0, now=now)

    assert [so.order_id for so in result_1.scheduled_orders] == ["ORD-A", "ORD-B"]
    assert [so.order_id for so in result_2.scheduled_orders] == ["ORD-A", "ORD-B"]


def test_mixed_work_unit_timing_accumulates_correctly():
    """Queue simulation (doc §6) must walk the sorted queue accumulating
    *work_units*, not order count -- a light order and a heavy order
    should produce different predicted_dispatch_at gaps proportional to
    their work_units, not just "one slot each".
    """
    now = datetime.now(UTC)
    first = _order("ORD-FIRST", OrderStatus.PENDING, hours_until_due=1, work_units=10.0)
    second = _order("ORD-SECOND", OrderStatus.PENDING, hours_until_due=48, work_units=40.0)

    capacity = 50.0
    result = compute_schedule(orders=[second, first], capacity_per_hour=capacity, now=now)
    by_id = {so.order_id: so for so in result.scheduled_orders}

    expected_first = now + timedelta(hours=10.0 / capacity)
    expected_second = now + timedelta(hours=(10.0 + 40.0) / capacity)

    assert by_id["ORD-FIRST"].predicted_dispatch_at == pytest.approx(
        expected_first, abs=timedelta(seconds=1)
    )
    assert by_id["ORD-SECOND"].predicted_dispatch_at == pytest.approx(
        expected_second, abs=timedelta(seconds=1)
    )
    assert result.pending_work_units == pytest.approx(50.0)


@pytest.mark.parametrize(
    "slack_hours,expected_status",
    [
        (-0.01, SLAStatus.BREACHED),
        (0.0, SLAStatus.AT_RISK),
        (AT_RISK_THRESHOLD_HOURS, SLAStatus.AT_RISK),
        (AT_RISK_THRESHOLD_HOURS + 0.01, SLAStatus.WATCH),
        (WATCH_THRESHOLD_HOURS, SLAStatus.WATCH),
        (WATCH_THRESHOLD_HOURS + 0.01, SLAStatus.SAFE),
    ],
)
def test_sla_classification_thresholds(slack_hours, expected_status):
    """Pin down the exact boundary behavior at AT_RISK_THRESHOLD_HOURS and
    WATCH_THRESHOLD_HOURS so a future weight/threshold change can't
    silently shift which side of the line an order falls on.
    """
    now = datetime.now(UTC)
    predicted_dispatch_at = now
    promised_dispatch_at = now + timedelta(hours=slack_hours)

    assert _classify_sla(predicted_dispatch_at, promised_dispatch_at) == expected_status


def test_sla_classification_honors_an_explicit_facility_policy():
    """A facility's own thresholds must override the frozen defaults.

    Same slack, opposite classification: 6 hours of slack is WATCH under the
    global default (<=12h, >4h) but SAFE under a facility configured with a
    tighter 3h/1h policy.
    """
    now = datetime.now(UTC)
    predicted_dispatch_at = now
    promised_dispatch_at = now + timedelta(hours=6)

    assert (
        _classify_sla(predicted_dispatch_at, promised_dispatch_at)
        == SLAStatus.WATCH
    )
    tight = SlaThresholds(watch_hours=3.0, at_risk_hours=1.0)
    assert (
        _classify_sla(predicted_dispatch_at, promised_dispatch_at, tight)
        == SLAStatus.SAFE
    )


def test_compute_schedule_honors_an_explicit_facility_sla_policy():
    """The same facility override must flow through the whole scheduler run."""
    now = datetime.now(UTC)
    order = _order("ORD-1", OrderStatus.PENDING, hours_until_due=6)

    default_result = compute_schedule(orders=[order], capacity_per_hour=52.0, now=now)
    assert default_result.watch_count == 1

    tight_result = compute_schedule(
        orders=[order],
        capacity_per_hour=52.0,
        now=now,
        sla_thresholds=SlaThresholds(watch_hours=3.0, at_risk_hours=1.0),
    )
    assert tight_result.safe_count == 1
    assert tight_result.watch_count == 0


def test_facility_risk_honors_an_explicit_facility_policy():
    """A facility's own exposure ratio must override the frozen defaults.

    Same 10% exposure, opposite classification: MEDIUM under the global
    default (10% < 15% high threshold), HIGH under a facility configured
    with a tighter 5% high threshold.
    """
    result = _result(safe=900, watch=0, at_risk=100, breached=0)

    assert classify_facility_risk(result) == SurgeRiskLevel.MEDIUM
    strict = FacilityRiskThresholds(exposure_high_ratio=0.05)
    assert classify_facility_risk(result, strict) == SurgeRiskLevel.HIGH


def test_a_stalled_floor_is_never_reported_calmer_than_high():
    """The queue is scheduled at configured capacity only so it stays computable.

    With nothing actually leaving the floor, that schedule reads calm; the
    stall itself has to lift the risk.
    """
    quiet = _result(safe=100, watch=0, at_risk=0, breached=0)
    exposed = _result(safe=100, watch=0, at_risk=5, breached=0)

    assert classify_facility_risk(quiet, stalled=True) == SurgeRiskLevel.HIGH
    assert classify_facility_risk(exposed, stalled=True) == SurgeRiskLevel.HIGH
    assert classify_facility_risk(quiet) == SurgeRiskLevel.LOW


def test_a_stall_never_lowers_a_critical_facility():
    """A floor, not a replacement: a stall only ever raises the level."""
    critical = _result(safe=10, watch=0, at_risk=0, breached=10)

    assert classify_facility_risk(critical, stalled=True) == SurgeRiskLevel.CRITICAL


def test_a_stalled_floor_with_nothing_waiting_is_calm():
    """No pending work means nothing is waiting on the floor to move."""
    empty = _result(safe=0, watch=0, at_risk=0, breached=0)

    assert classify_facility_risk(empty, stalled=True) == SurgeRiskLevel.LOW


def test_timezone_aware_timestamps_from_different_offsets():
    """Orders whose timestamps come in with different UTC offsets (e.g. a
    simulator emitting IST, a promise time stored as UTC) must still
    compare correctly -- Python compares aware datetimes on their absolute
    instant, not their printed offset, so this should just work as long as
    everything stays timezone-aware.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now_utc = datetime.now(UTC)
    now_ist = now_utc.astimezone(ist)

    order = Order(
        order_id="ORD-IST",
        facility_id="WH-01",
        created_at=now_ist - timedelta(hours=2),
        promised_dispatch_at=now_utc + timedelta(hours=6),
        item_count=1,
        work_units=1.0,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
    )

    result = compute_schedule(orders=[order], capacity_per_hour=52.0, now=now_utc)
    equivalent_utc_order = _order("ORD-UTC", OrderStatus.PENDING, hours_until_due=6)
    result_utc = compute_schedule(orders=[equivalent_utc_order], capacity_per_hour=52.0, now=now_utc)

    assert result.scheduled_orders[0].sla_status == result_utc.scheduled_orders[0].sla_status


def test_what_if_reuse_does_not_mutate_or_leak_state_across_calls():
    """The scheduler must be safely reusable for a live read and then
    immediately again for a What-If hypothetical (doc §1) -- e.g. the
    frontend calls it once for the real dashboard, then again with a
    capacity override for a "what if we added a shift" simulation. Neither
    call should affect the other, and the original orders must be untouched.
    """
    now = datetime.now(UTC)
    baseline_orders = [
        _order("ORD-1", OrderStatus.PENDING, hours_until_due=3, work_units=20.0),
        _order("ORD-2", OrderStatus.PENDING, hours_until_due=8, work_units=20.0),
    ]

    baseline_result = compute_schedule(orders=baseline_orders, capacity_per_hour=40.0, now=now)

    whatif_result = compute_schedule(orders=baseline_orders, capacity_per_hour=80.0, now=now)

    baseline_by_id = {so.order_id: so for so in baseline_result.scheduled_orders}
    whatif_by_id = {so.order_id: so for so in whatif_result.scheduled_orders}
    for order_id, baseline_scheduled in baseline_by_id.items():
        assert whatif_by_id[order_id].predicted_dispatch_at <= baseline_scheduled.predicted_dispatch_at

    with pytest.raises(ValidationError):
        baseline_orders[0].work_units = 999.0

    rerun_result = compute_schedule(orders=baseline_orders, capacity_per_hour=40.0, now=now)
    assert rerun_result == baseline_result


def _result(safe: int, watch: int, at_risk: int, breached: int) -> ScheduleResult:
    """Build a schedule result carrying only the counts risk is graded on."""
    return ScheduleResult(
        generated_at=NOW,
        capacity_per_hour=52.0,
        scheduled_orders=(),
        pending_orders=safe + watch + at_risk + breached,
        pending_work_units=0.0,
        safe_count=safe,
        watch_count=watch,
        at_risk_count=at_risk,
        breached_count=breached,
    )


def test_risk_does_not_fall_as_healthy_work_arrives_behind_late_orders():
    """Hold the operator's signal steady when only safe work is added.

    A fixed number of missed promises must not read as a calmer facility just
    because the queue behind them grew.
    """
    levels = [
        classify_facility_risk(_result(safe=safe, watch=0, at_risk=0, breached=100))
        for safe in (100, 400, 900, 2400)
    ]

    assert SurgeRiskLevel.MEDIUM not in levels
    assert SurgeRiskLevel.LOW not in levels


def test_a_missed_promise_outranks_a_recoverable_one():
    """Separate a promise already broken from one that can still be saved."""
    recoverable = classify_facility_risk(_result(safe=900, watch=0, at_risk=100, breached=0))
    missed = classify_facility_risk(_result(safe=900, watch=0, at_risk=0, breached=100))

    assert recoverable == SurgeRiskLevel.MEDIUM
    assert missed == SurgeRiskLevel.HIGH


def test_any_breach_is_at_least_high():
    """Refuse to call a facility that is already missing promises MEDIUM."""
    assert classify_facility_risk(
        _result(safe=9999, watch=0, at_risk=0, breached=1)
    ) == SurgeRiskLevel.HIGH


def test_concentrated_breaches_are_critical():
    """Escalate when a large share of the queue has already missed its promise."""
    assert classify_facility_risk(
        _result(safe=800, watch=0, at_risk=0, breached=200)
    ) == SurgeRiskLevel.CRITICAL


def test_clean_queue_and_recoverable_exposure_keep_their_levels():
    """Keep the untouched ends of the scale where they were."""
    assert classify_facility_risk(_result(1000, 0, 0, 0)) == SurgeRiskLevel.LOW
    assert classify_facility_risk(_result(0, 0, 0, 0)) == SurgeRiskLevel.LOW
    assert classify_facility_risk(_result(900, 0, 100, 0)) == SurgeRiskLevel.MEDIUM
    assert classify_facility_risk(_result(800, 0, 200, 0)) == SurgeRiskLevel.HIGH
    assert classify_facility_risk(_result(500, 0, 500, 0)) == SurgeRiskLevel.CRITICAL


def _fixed_order(order_id: str, hours_until_due: float, work_units: float) -> Order:
    """Build a pending order anchored to NOW rather than the wall clock."""
    return Order(
        order_id=order_id,
        facility_id="WH-01",
        created_at=NOW - timedelta(hours=1),
        promised_dispatch_at=NOW + timedelta(hours=hours_until_due),
        item_count=1,
        work_units=work_units,
        order_value=Decimal(0),
        status=OrderStatus.PENDING,
    )


def test_committed_work_delays_predicted_dispatch():
    """Charge work already on the floor against the same throughput.

    The queue cannot start until the floor clears what it is holding, so
    ignoring it makes every prediction optimistic.
    """
    orders = [_fixed_order("O1", hours_until_due=48, work_units=100.0)]

    without = compute_schedule(orders, 50.0, NOW)
    with_wip = compute_schedule(orders, 50.0, NOW, committed_work_units=400.0)

    assert (without.scheduled_orders[0].predicted_dispatch_at - NOW) == timedelta(hours=2)
    assert (with_wip.scheduled_orders[0].predicted_dispatch_at - NOW) == timedelta(hours=10)


def test_committed_work_can_move_an_order_into_breach():
    """Surface risk that a floor backlog was previously hiding.

    100 work units at 50/hr dispatches two hours out against a twenty hour
    promise -- comfortably SAFE. Put 1000 units of already-started work in
    front of it and the same order is late before it is even picked.
    """
    orders = [_fixed_order("O1", hours_until_due=20, work_units=100.0)]

    assert compute_schedule(orders, 50.0, NOW).scheduled_orders[0].sla_status == SLAStatus.SAFE
    assert (
        compute_schedule(orders, 50.0, NOW, committed_work_units=1000.0)
        .scheduled_orders[0]
        .sla_status
        == SLAStatus.BREACHED
    )


def test_pending_work_units_excludes_committed_work():
    """Keep the reported pending queue free of work already released."""
    orders = [_fixed_order("O1", hours_until_due=48, work_units=3.0)]

    result = compute_schedule(orders, 50.0, NOW, committed_work_units=400.0)

    assert result.pending_work_units == 3.0


def test_committed_work_defaults_to_none_and_is_validated():
    """Default to no floor backlog, and reject values that are not usable."""
    orders = [_fixed_order("O1", hours_until_due=48, work_units=5.0)]

    assert compute_schedule(orders, 50.0, NOW).pending_work_units == 5.0
    with pytest.raises(ValueError):
        compute_schedule(orders, 50.0, NOW, committed_work_units=-1.0)
    with pytest.raises(ValueError):
        compute_schedule(orders, 50.0, NOW, committed_work_units=float("inf"))


def test_priority_breakdown_sums_to_the_score_the_queue_uses():
    """Expose the arithmetic, and keep it consistent with the ordering."""
    orders = [_fixed_order("O1", hours_until_due=20, work_units=2.5)]

    scheduled = compute_schedule(orders, 50.0, NOW).scheduled_orders[0]
    parts = scheduled.priority_breakdown

    assert parts.workload_penalty == WORKLOAD_PENALTY_WEIGHT * 2.5
    assert parts.sla_urgency == pytest.approx(-20.0)
    assert parts.aging_bonus == pytest.approx(AGING_BONUS_WEIGHT_PER_HOUR * 1.0)
    assert scheduled.priority_score == pytest.approx(
        parts.sla_urgency + parts.aging_bonus - parts.workload_penalty + parts.adjustment
    )


def test_work_units_ahead_explains_the_predicted_dispatch():
    """Let an operator recompute the prediction from the numbers shown."""
    orders = [
        _fixed_order("O1", hours_until_due=20, work_units=10.0),
        _fixed_order("O2", hours_until_due=20, work_units=30.0),
    ]

    result = compute_schedule(orders, 50.0, NOW, committed_work_units=5.0)

    for scheduled in result.scheduled_orders:
        order = next(o for o in orders if o.order_id == scheduled.order_id)
        cleared = scheduled.work_units_ahead + order.work_units
        assert scheduled.predicted_dispatch_at == NOW + timedelta(hours=cleared / 50.0)
    assert result.scheduled_orders[0].work_units_ahead == 5.0


def test_priority_adjustment_can_pin_an_order_to_the_front():
    """Give queue levers a hook that does not rewrite the frozen formula."""
    orders = [
        _fixed_order("URGENT", hours_until_due=2, work_units=1.0),
        _fixed_order("LATER", hours_until_due=40, work_units=1.0),
    ]

    natural = compute_schedule(orders, 50.0, NOW)
    pinned = compute_schedule(orders, 50.0, NOW, priority_adjustments={"LATER": 100.0})

    assert natural.scheduled_orders[0].order_id == "URGENT"
    assert pinned.scheduled_orders[0].order_id == "LATER"
    assert pinned.scheduled_orders[0].priority_breakdown.adjustment == 100.0
    # The formula itself is untouched; only the added term differs.
    assert (
        pinned.scheduled_orders[0].priority_breakdown.sla_urgency
        == natural.scheduled_orders[1].priority_breakdown.sla_urgency
    )


def test_minutes_to_first_breach_reports_the_earliest_promise_to_fail():
    """Bound the operator's decision window with the first real failure."""
    orders = [
        _fixed_order("SOON", hours_until_due=1, work_units=100.0),
        _fixed_order("LATER", hours_until_due=3, work_units=100.0),
    ]

    result = compute_schedule(orders, 10.0, NOW)

    assert result.breached_count == 2
    assert result.minutes_to_first_breach == pytest.approx(60.0)


def test_no_breach_means_no_deadline_pressure():
    """Report an absent window rather than a misleading zero."""
    orders = [_fixed_order("O1", hours_until_due=40, work_units=1.0)]

    assert compute_schedule(orders, 50.0, NOW).minutes_to_first_breach is None


CUTOFF_1800 = CutoffPolicy(cutoff_utc=time(18, 0))


def test_work_finished_before_the_cutoff_dispatches_at_the_cutoff():
    """Packed is not dispatched; the carrier decides when it leaves."""
    orders = [_fixed_order("O1", hours_until_due=40, work_units=50.0)]

    result = compute_schedule(orders, 50.0, NOW, dispatch_cutoff=CUTOFF_1800)
    scheduled = result.scheduled_orders[0]

    assert scheduled.work_complete_at == NOW + timedelta(hours=1)
    assert scheduled.predicted_dispatch_at == NOW.replace(hour=18, minute=0)


def test_work_finished_after_the_cutoff_waits_for_tomorrow():
    """Missing today's collection by minutes costs a whole day of dispatch."""
    orders = [_fixed_order("O1", hours_until_due=40, work_units=350.0)]

    scheduled = compute_schedule(
        orders, 50.0, NOW, dispatch_cutoff=CUTOFF_1800
    ).scheduled_orders[0]

    # Work finishes 19:00, twenty minutes past the 18:00 pickup.
    assert scheduled.work_complete_at == NOW + timedelta(hours=7)
    assert scheduled.predicted_dispatch_at == (NOW + timedelta(days=1)).replace(hour=18)


def test_extending_the_cutoff_rescues_orders_without_adding_capacity():
    """The cheapest real lever: move the deadline, not the work.

    Same throughput, same queue, same promises -- only the collection moves, and
    an order that was going to miss its promise by a day now makes it.
    """
    orders = [_fixed_order("O1", hours_until_due=10, work_units=330.0)]

    missed = compute_schedule(orders, 50.0, NOW, dispatch_cutoff=CUTOFF_1800)
    caught = compute_schedule(
        orders, 50.0, NOW, dispatch_cutoff=CUTOFF_1800.shifted_by(60)
    )

    assert missed.scheduled_orders[0].sla_status == SLAStatus.BREACHED
    assert caught.scheduled_orders[0].sla_status != SLAStatus.BREACHED
    assert missed.capacity_per_hour == caught.capacity_per_hour, "no capacity was bought"


def test_no_cutoff_keeps_continuous_dispatch():
    """An unconfigured facility must schedule exactly as it did before."""
    orders = [_fixed_order("O1", hours_until_due=40, work_units=50.0)]

    scheduled = compute_schedule(orders, 50.0, NOW).scheduled_orders[0]

    assert scheduled.predicted_dispatch_at == scheduled.work_complete_at


def test_cutoff_shift_wraps_within_the_day():
    """Keep a late shift from producing an invalid time."""
    assert CutoffPolicy(cutoff_utc=time(23, 30)).shifted_by(60).cutoff_utc == time(0, 30)
    assert CUTOFF_1800.shifted_by(90).cutoff_utc == time(19, 30)


# --- P10: operating calendar (closed hours must change queue arithmetic) ---

OPEN_0600_2200 = OperatingCalendar(opens_at=time(6, 0), closes_at=time(22, 0))


def test_operating_calendar_none_is_bit_identical_to_no_calendar():
    """The default must reproduce today's continuous-capacity arithmetic."""
    orders = [_fixed_order("O1", hours_until_due=40, work_units=1000.0)]

    without_param = compute_schedule(orders, 50.0, NOW)
    with_none = compute_schedule(orders, 50.0, NOW, operating_calendar=None)

    assert with_none.scheduled_orders[0].work_complete_at == (
        without_param.scheduled_orders[0].work_complete_at
    )


def test_closed_overnight_hours_are_skipped_not_worked_through():
    """A floor open 06:00-22:00 does not produce through the closed 8 hours.

    NOW is 12:00, inside today's window. 1000 work units at 50/hr is 20
    hours of pure capacity: without a calendar that lands at 08:00 the next
    day. With the calendar, only 10 of those hours are available before
    22:00 today; the floor is closed 22:00-06:00; the remaining 10 hours are
    worked from 06:00 the next day, landing at 16:00 -- 8 hours later than
    the wall-clock answer, exactly the width of the skipped closed window.
    """
    orders = [_fixed_order("O1", hours_until_due=100, work_units=1000.0)]

    naive = compute_schedule(orders, 50.0, NOW).scheduled_orders[0]
    calendared = compute_schedule(
        orders, 50.0, NOW, operating_calendar=OPEN_0600_2200
    ).scheduled_orders[0]

    assert naive.work_complete_at == NOW + timedelta(hours=20)
    assert calendared.work_complete_at == (NOW + timedelta(days=1)).replace(hour=16, minute=0)


def test_evaluated_while_closed_waits_for_the_floor_to_open():
    """02:00, floor closed: no capacity flows until the next open instant."""
    closed_now = datetime(2026, 9, 8, 2, 0, tzinfo=UTC)
    orders = [
        Order(
            order_id="O1",
            facility_id="WH-01",
            created_at=closed_now - timedelta(hours=1),
            promised_dispatch_at=closed_now + timedelta(hours=40),
            item_count=1,
            work_units=50.0,
            order_value=Decimal(0),
            status=OrderStatus.PENDING,
        )
    ]

    result = compute_schedule(
        orders, 50.0, closed_now, operating_calendar=OPEN_0600_2200
    ).scheduled_orders[0]

    # Opens at 06:00, then 1 hour of work at 50 wu/hr for 50 work units.
    assert result.work_complete_at == closed_now.replace(hour=7, minute=0)


def test_committed_work_also_pauses_through_closed_hours():
    """In-progress work is not exempt from a closed floor -- nobody is
    working, so committed work waits behind the closure exactly like the
    pending queue does. This mirrors `repository.py`'s own precedent for
    committed work: the honest reading is what the floor can prove, not
    what is assumed to continue regardless of circumstance.
    """
    orders = [_fixed_order("O1", hours_until_due=40, work_units=50.0)]

    result = compute_schedule(
        orders,
        50.0,
        NOW,
        committed_work_units=800.0,
        operating_calendar=OPEN_0600_2200,
    ).scheduled_orders[0]

    # 850 total work units / 50 wu/hr = 17 open-hours needed. From NOW
    # (12:00, inside today's window): 10h available today (->22:00), then
    # closed 8h, then 7 more hours from 06:00 tomorrow -> 13:00 tomorrow.
    assert result.work_complete_at == (NOW + timedelta(days=1)).replace(hour=13, minute=0)


def test_calendar_cutoff_wins_over_a_separately_passed_dispatch_cutoff():
    """`operating_calendar.cutoff` is the one source of truth once a
    calendar is passed -- a facility can never end up closed on Sunday with
    a Sunday collection. The separate `dispatch_cutoff` argument is ignored
    whenever a calendar is present.
    """
    calendar_with_cutoff = OperatingCalendar(
        opens_at=time(6, 0), closes_at=time(22, 0), cutoff=CUTOFF_1800
    )
    orders = [_fixed_order("O1", hours_until_due=40, work_units=50.0)]

    result = compute_schedule(
        orders,
        50.0,
        NOW,
        operating_calendar=calendar_with_cutoff,
        dispatch_cutoff=CutoffPolicy(cutoff_utc=time(9, 0)),
    ).scheduled_orders[0]

    assert result.predicted_dispatch_at == NOW.replace(hour=18, minute=0)


def test_operating_calendar_rejects_a_degenerate_zero_width_window():
    """opens_at == closes_at is an unusable, permanently-closed window, not
    a permissive one -- reject it at construction rather than loop forever
    inside `add_working_hours`.
    """
    with pytest.raises(ValidationError):
        OperatingCalendar(opens_at=time(6, 0), closes_at=time(6, 0))
