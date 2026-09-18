"""Test lever application and the prevention-versus-redistribution scoring."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.levers import CostBand, LeverFamily, load_catalog
from app.models import Order, OrderStatus, SLAStatus
from app.plans import (
    ScheduleInputs,
    apply_lever,
    build_inputs,
    feasible_levers,
    is_available,
    manage_breaches,
    posture_cost,
    posture_effective_at,
    score_outcome,
)
from app.scheduler import CapacityRamp, CutoffPolicy, OperatingCalendar, SlaThresholds

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
CATALOG = load_catalog()


def _order(order_id: str, hours_until_due: float, work_units: float = 2.0, segment=None):
    """Build one pending order."""
    return Order(
        order_id=order_id,
        facility_id="WH-01",
        created_at=NOW - timedelta(hours=1),
        promised_dispatch_at=NOW + timedelta(hours=hours_until_due),
        item_count=1,
        work_units=work_units,
        order_value=Decimal("100.00"),
        status=OrderStatus.PENDING,
        segment=segment,
    )


def _inputs(orders, capacity=50.0, cutoff=None):
    """Build scheduler inputs for a plan run."""
    return ScheduleInputs(
        orders=list(orders),
        capacity_per_hour=capacity,
        committed_work_units=0.0,
        dispatch_cutoff=cutoff,
    )


# --- the catalog itself ----------------------------------------------------


def test_catalog_covers_all_four_families():
    """The families are the product's core distinction; none may be missing."""
    families = {lever.family for lever in CATALOG.levers}

    assert families == set(LeverFamily)


def test_every_posture_references_real_levers():
    """A posture naming a lever that does not exist would fail at runtime."""
    known = {lever.id for lever in CATALOG.levers}

    for posture in CATALOG.postures:
        assert set(posture.lever_ids).issubset(known), posture.id


def test_do_nothing_is_always_offered_and_costs_nothing():
    """Inaction must be a visible, costed choice rather than an absence."""
    do_nothing = next(p for p in CATALOG.postures if p.id == "do-nothing")

    assert do_nothing.lever_ids == ()
    assert posture_cost(CATALOG, do_nothing) == CostBand.NONE


# --- lever effects ---------------------------------------------------------


def test_capacity_lever_adds_throughput():
    """A throughput lever is the only kind that creates capacity."""
    lever = CATALOG.lever("EXTEND_SHIFT")

    after = apply_lever(lever, _inputs([_order("O1", 10)]), NOW)

    assert after.capacity_per_hour == 70.0


def test_suspending_value_added_services_removes_work():
    """Some throughput levers take work away instead of adding capacity."""
    lever = CATALOG.lever("SUSPEND_VAS")

    after = apply_lever(lever, _inputs([_order("O1", 10, work_units=2.0)]), NOW)

    assert after.orders[0].work_units == pytest.approx(1.7)


def test_protecting_a_segment_only_moves_that_segment():
    """A queue lever targets a cohort, not the whole queue."""
    lever = CATALOG.lever("PROTECT_SEGMENT")
    orders = [_order("NEW", 10, segment="FIRST_TIME"), _order("OLD", 10)]

    after = apply_lever(lever, _inputs(orders), NOW)

    assert after.priority_adjustments == {"NEW": 50.0}


def test_deferring_only_touches_orders_with_slack_to_spare():
    """Never defer an order that cannot afford to wait."""
    lever = CATALOG.lever("DEFER_LOW_STAKES")
    orders = [_order("RELAXED", 40), _order("TIGHT", 2)]

    after = apply_lever(lever, _inputs(orders), NOW)

    assert "RELAXED" in after.priority_adjustments
    assert "TIGHT" not in after.priority_adjustments


def test_promise_lever_moves_the_commitment_not_the_work():
    """Re-promising changes deadlines and leaves work units alone."""
    lever = CATALOG.lever("EXTEND_PROMISED_DATE")
    order = _order("O1", 10, work_units=2.0)

    after = apply_lever(lever, _inputs([order]), NOW)

    assert after.orders[0].promised_dispatch_at == order.promised_dispatch_at + timedelta(hours=6)
    assert after.orders[0].work_units == 2.0
    assert after.capacity_per_hour == 50.0


def test_cutoff_lever_moves_the_collection():
    """Extending a pickup buys dispatch time without buying capacity."""
    lever = CATALOG.lever("EXTEND_CARRIER_CUTOFF")

    after = apply_lever(
        lever, _inputs([_order("O1", 10)], cutoff=CutoffPolicy(cutoff_utc=time(18, 0))), NOW
    )

    assert after.dispatch_cutoff.cutoff_utc == time(19, 0)
    assert after.capacity_per_hour == 50.0


def test_inflow_lever_leaves_the_existing_queue_untouched():
    """Closing the tap cannot rescue an order already placed."""
    lever = CATALOG.lever("PAUSE_PROMO")
    orders = [_order("O1", 10)]

    after = apply_lever(lever, _inputs(orders), NOW)

    assert after.arrival_multiplier == 0.7
    assert after.orders == orders
    assert after.priority_adjustments == {}


# --- feasibility -----------------------------------------------------------


def test_cutoff_lever_needs_a_cutoff_to_move():
    """A facility with continuous dispatch has no collection to extend."""
    lever = CATALOG.lever("EXTEND_CARRIER_CUTOFF")

    assert is_available(lever, [], has_cutoff=True, has_projection=False)
    assert not is_available(lever, [], has_cutoff=False, has_projection=False)


def test_segment_lever_needs_orders_in_that_segment():
    """Offering to protect a cohort that is not present would be noise."""
    lever = CATALOG.lever("PROTECT_SEGMENT")

    assert not is_available(lever, [_order("O1", 10)], True, False)
    assert is_available(lever, [_order("O1", 10, segment="FIRST_TIME")], True, False)


def test_inflow_lever_needs_a_projection_to_act_on():
    """Without projected arrivals an inflow lever provably does nothing."""
    lever = CATALOG.lever("PAUSE_PROMO")

    assert not is_available(lever, [], True, has_projection=False)
    assert is_available(lever, [], True, has_projection=True)


def test_levers_arriving_after_the_collection_are_shown_not_hidden():
    """Teach the operator where the decision window closes."""
    usable, blocked = feasible_levers(
        CATALOG, [], has_cutoff=True, has_projection=True, minutes_to_cutoff=20.0
    )

    late = {lever.id for lever, reason in blocked if reason == "too late to help"}
    assert "EXTEND_SHIFT" in late, "90 minutes of lead time cannot land in 20"
    assert "EXTEND_CARRIER_CUTOFF" in {lever.id for lever in usable}


def test_floor_closed_blocks_capacity_levers_with_its_own_reason():
    """02:00, floor closed: EXTEND_SHIFT is not 'too late to help', it is
    'the floor is closed until 06:00'. There is no shift to extend.

    Distinct from the cutoff-lead-time reason above: plenty of minutes
    remain before the collection (200 > EXTEND_SHIFT's 90-minute lead time),
    so without the floor-closed check this would read as usable, not blocked
    for the wrong reason.
    """
    reopens_at = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)
    usable, blocked = feasible_levers(
        CATALOG,
        [],
        has_cutoff=True,
        has_projection=True,
        minutes_to_cutoff=200.0,
        floor_reopens_at=reopens_at,
    )

    reasons = {lever.id: reason for lever, reason in blocked}
    assert reasons["EXTEND_SHIFT"] == "floor closed until 06:00"
    assert "EXTEND_SHIFT" not in {lever.id for lever in usable}


def test_floor_open_never_blocks_on_the_closed_reason():
    """`floor_reopens_at=None` (open, or no calendar at all) must reproduce
    today's behaviour exactly -- no lever is ever blocked for this reason.
    """
    usable, blocked = feasible_levers(
        CATALOG, [], has_cutoff=True, has_projection=True, minutes_to_cutoff=200.0
    )

    assert not any(reason.startswith("floor closed") for _, reason in blocked)
    assert "EXTEND_SHIFT" in {lever.id for lever in usable}


def test_floor_closed_does_not_block_non_capacity_levers():
    """Deciding to reprioritize the queue or re-promise an order does not
    require anyone to be on the floor -- only levers that physically need
    the floor running (capacity_delta) are blocked by a closure.
    """
    reopens_at = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)
    usable, blocked = feasible_levers(
        CATALOG,
        [],
        has_cutoff=True,
        has_projection=True,
        minutes_to_cutoff=200.0,
        floor_reopens_at=reopens_at,
    )

    reasons = {lever.id: reason for lever, reason in blocked}
    assert reasons.get("EXTEND_CARRIER_CUTOFF") != "floor closed until 06:00"
    assert "EXTEND_CARRIER_CUTOFF" in {lever.id for lever in usable}


def test_effective_time_is_the_slowest_lever_in_the_posture():
    """A plan is only in effect once its last lever lands."""
    posture = next(p for p in CATALOG.postures if p.id == "buy-the-hour")

    assert posture_effective_at(CATALOG, posture, NOW) == NOW + timedelta(minutes=90)


# --- the honest core -------------------------------------------------------


def test_a_queue_only_plan_reports_redistribution_not_rescue():
    """Resequencing decides who misses, never how many.

    This is the distinction the product exists to make. A plan that moves
    exposure between customers without reducing it must say so.
    """
    # More work than the day can clear, so someone misses either way.
    orders = [_order(f"O{i}", 6, work_units=20.0, segment="FIRST_TIME" if i > 6 else None)
              for i in range(10)]
    base = _inputs(orders, capacity=20.0)

    baseline = base.run(NOW)
    protected = build_inputs(
        base,
        next(p for p in CATALOG.postures if p.id == "protect-new-customers"),
        CATALOG,
        NOW,
        {lever.id: lever for lever in CATALOG.levers},
    ).run(NOW)

    outcome = score_outcome(baseline, protected)

    assert outcome.net_breach_change <= 0, "reordering cannot clear work"
    assert outcome.is_redistribution
    assert outcome.orders_worsened, "someone paid for the protection"


def test_a_throughput_plan_reports_real_prevention():
    """Adding capacity genuinely reduces the number of misses."""
    orders = [_order(f"O{i}", 6, work_units=20.0) for i in range(10)]
    base = _inputs(orders, capacity=20.0)

    baseline = base.run(NOW)
    boosted = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW).run(NOW)

    outcome = score_outcome(baseline, boosted)

    assert outcome.breaches_avoided > 0
    assert outcome.net_breach_change > 0
    assert not outcome.is_redistribution


def test_projected_arrivals_are_excluded_from_the_score():
    """A plan must never be credited with rescuing an order nobody placed."""
    placed = [_order("REAL", 6, work_units=20.0)]
    projected = [_order("PROJ-0001", 6, work_units=20.0)]
    base = _inputs(placed + projected, capacity=20.0)

    baseline = base.run(NOW)
    boosted = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW).run(NOW)

    scored = score_outcome(baseline, boosted, frozenset({"PROJ-0001"}))

    assert all(fate.order_id != "PROJ-0001" for fate in scored.orders_improved)


def test_managing_a_breach_is_not_a_rescue():
    """An apology does not make an order arrive sooner."""
    orders = [_order("LATE", 1, work_units=100.0)]
    schedule = _inputs(orders, capacity=10.0).run(NOW)
    assert schedule.breached_count == 1

    managed = manage_breaches(schedule, frozenset({"LATE"}))

    assert managed.scheduled_orders[0].sla_status == SLAStatus.BREACHED_MANAGED
    assert managed.breached_managed_count == 1
    assert managed.breached_count == 0
    # The order is still late: dispatch has not moved.
    assert (
        managed.scheduled_orders[0].predicted_dispatch_at
        == schedule.scheduled_orders[0].predicted_dispatch_at
    )


def test_an_apology_is_never_counted_as_a_breach_avoided():
    """A managed breach is still a breach.

    Converting a miss into a notified miss is worth doing, but presenting it as
    prevention would be the exact overclaiming this scoring exists to stop.
    """
    orders = [_order("LATE", 1, work_units=100.0)]
    base = _inputs(orders, capacity=10.0)

    baseline = base.run(NOW)
    managed = manage_breaches(base.run(NOW), frozenset({"LATE"}))

    outcome = score_outcome(baseline, managed)

    assert outcome.breaches_avoided == 0
    assert outcome.net_breach_change == 0


# --- lead time: a capacity lever is not there until people are ----------------


def test_capacity_lever_ramps_in_after_its_lead_time():
    """EXTEND_SHIFT adds 20 wu/h, but only 90 minutes after approval."""
    after = apply_lever(CATALOG.lever("EXTEND_SHIFT"), _inputs([_order("O1", 10)], 20.0), NOW)

    assert after.capacity_per_hour == 40.0
    assert after.capacity_ramp == CapacityRamp(from_capacity_per_hour=20.0, hours=1.5)


def test_work_finished_inside_the_lead_time_gets_no_extra_capacity():
    """10 wu at 20 wu/h is 30 minutes, lever or not: the extra staff are not there yet."""
    base = _inputs([_order("O1", 10, work_units=10.0)], capacity=20.0)

    boosted = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW).run(NOW)

    assert boosted.scheduled_orders[0].work_complete_at == NOW + timedelta(hours=0.5)


def test_work_finished_after_the_lead_time_uses_the_ramp():
    """20 wu/h for 1.5h clears 30 wu; the last 10 wu then take 0.25h at 40 wu/h."""
    base = _inputs([_order("O1", 10, work_units=40.0)], capacity=20.0)

    boosted = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW).run(NOW)

    assert boosted.scheduled_orders[0].work_complete_at == NOW + timedelta(hours=1.75)


def test_the_ramp_counts_open_floor_hours_under_a_calendar():
    """Open until 13:00: the ramp's second hour of work continues after 06:00 reopening."""
    base = replace(
        _inputs(
            [_order("A", 30, work_units=10.0), _order("B", 40, work_units=30.0)], capacity=20.0
        ),
        operating_calendar=OperatingCalendar(opens_at=time(6, 0), closes_at=time(13, 0)),
    )

    boosted = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW).run(NOW)
    done = {o.order_id: o.work_complete_at for o in boosted.scheduled_orders}

    assert done["A"] == NOW + timedelta(minutes=30)
    assert done["B"] == datetime(2026, 9, 8, 6, 45, tzinfo=UTC)


def test_a_capacity_lever_never_projects_worse_than_doing_nothing():
    """Adding people late can fail to help, but it can never make an order later."""
    orders = [_order(f"O{i}", 1, work_units=5.0) for i in range(12)]
    base = _inputs(orders, capacity=20.0)

    baseline = base.run(NOW)
    boosted = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW).run(NOW)

    before = {o.order_id: o.predicted_dispatch_at for o in baseline.scheduled_orders}
    assert all(o.predicted_dispatch_at <= before[o.order_id] for o in boosted.scheduled_orders)
    assert boosted.breached_count <= baseline.breached_count


def test_lead_time_raises_residual_breaches_the_instant_version_hid():
    """Granting capacity at approval understates what a plan leaves behind.

    It can also overstate what the plan rescues: an order due after the lead
    time but deep in the queue can be saved by instant capacity and not by the
    real ramp. Never the other way round.
    """
    orders = [_order(f"O{i}", 1 + i * 0.2, work_units=10.0) for i in range(30)]
    base = _inputs(orders, capacity=20.0)
    baseline = base.run(NOW)

    honest = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW)
    instant = replace(honest, capacity_ramp=None)
    honest_run, instant_run = honest.run(NOW), instant.run(NOW)

    assert honest_run.breached_count > instant_run.breached_count
    assert (
        score_outcome(baseline, honest_run).breaches_avoided
        <= score_outcome(baseline, instant_run).breaches_avoided
    )


def test_instant_capacity_overclaimed_a_rescue_due_after_the_lead_time():
    """TARGET is due at 2.6h, after the ramp, yet the ramp still cannot save it.

    Instant 40 wu/h clears BLOCKER by 1.0h and TARGET by 2.0h. The real ramp
    clears 30 wu by 1.5h, BLOCKER at 1.75h, and TARGET only at 2.75h.
    """
    orders = [_order("BLOCKER", 1.9, work_units=40.0), _order("TARGET", 2.6, work_units=40.0)]
    # No at-risk band, so leaving exposure means making the promise at all.
    base = replace(
        _inputs(orders, capacity=20.0), sla_thresholds=SlaThresholds(at_risk_hours=0.0)
    )
    baseline = base.run(NOW)

    honest = apply_lever(CATALOG.lever("EXTEND_SHIFT"), base, NOW)
    instant = replace(honest, capacity_ramp=None)

    assert score_outcome(baseline, instant.run(NOW)).breaches_avoided == 2
    assert score_outcome(baseline, honest.run(NOW)).breaches_avoided == 1


def test_levers_without_capacity_have_no_ramp():
    """Only a throughput lever waits on people; the others act on the queue."""
    after = apply_lever(CATALOG.lever("SUSPEND_VAS"), _inputs([_order("O1", 10)]), NOW)

    assert after.capacity_ramp is None
