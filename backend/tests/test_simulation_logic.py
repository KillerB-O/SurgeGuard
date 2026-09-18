"""Test simulation and recovery-plan logic without a database."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models import Order, OrderStatus, SurgeRiskLevel
from app.plans import ScheduleInputs, manage_breaches
from app.repository import OrderTotals
from app.routers.simulation import (
    NO_OP_DEMAND_MULTIPLIER,
    _hypothetical_orders,
    _projected_misses,
    _recommend_plan,
    _require_projection_for_demand_multiplier,
    _run_with_arrivals,
    _summary,
)
from app.scheduler import CapacityRamp, compute_schedule
from app.schemas import (
    PlanOutcomeResponse,
    RecoveryPlanActions,
    RecoveryPlanResponse,
    SimulationRequest,
    SimulationSummary,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _order(order_id: str, promise_hours: float, work_units: float = 1.0) -> Order:
    """Build a pending order promised a given number of hours after creation."""
    created_at = NOW - timedelta(hours=1)
    return Order(
        order_id=order_id,
        facility_id="WH-01",
        created_at=created_at,
        promised_dispatch_at=created_at + timedelta(hours=promise_hours),
        item_count=1,
        work_units=work_units,
        order_value=0,
        status=OrderStatus.PENDING,
    )


def test_no_promise_override_keeps_persisted_deadlines():
    """Keep customer deadlines when the caller supplies no promise assumption."""
    orders = [_order("O1", promise_hours=6.0)]

    result = _hypothetical_orders(orders, None)

    assert result[0].promised_dispatch_at == orders[0].promised_dispatch_at


def test_capacity_only_simulation_does_not_re_promise_orders():
    """Isolate the capacity lever so SLA status cannot move for the wrong reason."""
    # Promised 6h after creation; 100 work units at 10 wu/hr cannot make it.
    orders = [_order("O1", promise_hours=6.0, work_units=100.0)]

    baseline = compute_schedule(orders, 10.0, NOW)
    unchanged_capacity = compute_schedule(_hypothetical_orders(orders, None), 10.0, NOW)

    assert baseline.scheduled_orders[0].sla_status == "BREACHED"
    assert unchanged_capacity.scheduled_orders[0].sla_status == "BREACHED"


def test_explicit_promise_override_still_applies_to_pending_orders():
    """Keep the dispatch-promise What-If working when it is actually requested."""
    orders = [_order("O1", promise_hours=6.0, work_units=100.0)]

    simulated = _hypothetical_orders(orders, 24.0)

    assert simulated[0].promised_dispatch_at == orders[0].created_at + timedelta(hours=24)
    assert compute_schedule(simulated, 10.0, NOW).scheduled_orders[0].sla_status == "SAFE"


def test_promise_override_never_mutates_the_source_orders():
    """Keep the hypothetical transform non-destructive for the baseline run."""
    orders = [_order("O1", promise_hours=6.0)]
    original = orders[0].promised_dispatch_at

    _hypothetical_orders(orders, 48.0)

    assert orders[0].promised_dispatch_at == original


def test_active_orders_keep_their_promise_under_an_override():
    """Leave operationally committed orders untouched by a promise What-If."""
    active = _order("O2", promise_hours=6.0).model_copy(
        update={"status": OrderStatus.PICKING}
    )

    simulated = _hypothetical_orders([active], 24.0)

    assert simulated[0].promised_dispatch_at == active.promised_dispatch_at


def _plan(
    plan_id: str,
    relative_cost: str,
    prevented: int,
    relocated: int = 0,
) -> RecoveryPlanResponse:
    """Build a scored plan for the recommendation logic to choose between.

    `prevented` is net breach change -- exposure actually removed, not moved.
    A plan with `prevented=0` and a non-zero `relocated` is a redistribution:
    it rearranges who misses without rescuing anyone.
    """
    return RecoveryPlanResponse(
        plan_id=plan_id,
        title=plan_id,
        description="",
        relative_cost=relative_cost,
        actions=RecoveryPlanActions(
            capacity_per_hour=50.0, dispatch_promise_hours=24.0, demand_multiplier=1.0
        ),
        projected=SimulationSummary(
            backlog_orders=0,
            backlog_work_units=0.0,
            safe_count=0,
            watch_count=0,
            at_risk_count=0,
            breached_count=0,
            projected_recovery_hours=0.0,
            risk_level=SurgeRiskLevel.LOW,
        ),
        outcome=PlanOutcomeResponse(
            breaches_avoided=prevented,
            breaches_relocated=relocated,
            net_breach_change=prevented,
            orders_improved=prevented + relocated,
            orders_worsened=relocated,
            is_redistribution=prevented == 0 and relocated > 0,
        ),
    )


def _totals(**work_units_by_status: float) -> OrderTotals:
    """Build order totals with one order per named status."""
    return OrderTotals(
        counts={OrderStatus(name.upper()): 1 for name in work_units_by_status},
        work_units={OrderStatus(name.upper()): wu for name, wu in work_units_by_status.items()},
    )


def test_recovery_hours_cover_every_unfinished_order():
    """Score recovery on the same backlog the response reports."""
    pending = _order("O1", promise_hours=48.0, work_units=2.0)
    totals = _totals(pending=2.0, picking=100.0)

    summary = _summary(totals, compute_schedule([pending], 10.0, NOW), 10.0)

    assert summary.backlog_work_units == 102.0
    assert summary.projected_recovery_hours == 10.2


def test_recovery_hours_wait_for_a_lever_lead_time():
    """30 wu/h only arrives after 1.5h at 10: 15 wu, then 87 wu at 30 is 4.4h."""
    summary = _summary(
        _totals(pending=2.0, picking=100.0),
        compute_schedule([], 30.0, NOW),
        30.0,
        capacity_ramp=CapacityRamp(from_capacity_per_hour=10.0, hours=1.5),
    )

    assert summary.projected_recovery_hours == pytest.approx(4.4)


def test_a_backlog_cleared_before_the_lever_lands_ignores_it():
    """12 wu at 10 wu/h is 1.2h, inside the 1.5h lead: the lever does not shorten it."""
    summary = _summary(
        _totals(pending=12.0),
        compute_schedule([], 30.0, NOW),
        30.0,
        capacity_ramp=CapacityRamp(from_capacity_per_hour=10.0, hours=1.5),
    )

    assert summary.projected_recovery_hours == pytest.approx(1.2)


def test_recovery_hours_are_zero_for_an_empty_backlog():
    """Report a cleared backlog as zero hours rather than an absent value."""
    summary = _summary(_totals(), compute_schedule([], 10.0, NOW), 10.0)

    assert summary.projected_recovery_hours == 0.0


def test_dispatched_orders_are_excluded_from_the_backlog():
    """Keep finished work out of the recovery estimate."""
    totals = _totals(pending=5.0, dispatched=1000.0)

    summary = _summary(totals, compute_schedule([], 10.0, NOW), 10.0)

    assert summary.backlog_orders == 1
    assert summary.backlog_work_units == 5.0


def test_projected_misses_skip_the_already_late_and_keep_the_apologised():
    """Count each future miss once, and an apology never unmisses an order.

    LATE is past its promise, so the database already counts it as a realised
    miss. A, B and C are predicted to miss with their promises still ahead; B
    and C get an apology, which changes how the miss is reported, not whether
    it happens.
    """
    late = _order("LATE", promise_hours=0.5)
    soon = [
        _order(order_id, promise_hours=hours, work_units=30.0)
        for order_id, hours in (("A", 3.0), ("B", 3.1), ("C", 3.2))
    ]
    schedule = manage_breaches(
        compute_schedule([late, *soon], 10.0, NOW), frozenset({"B", "C"})
    )
    statuses = {o.order_id: o.sla_status for o in schedule.scheduled_orders}
    assert statuses == {
        "LATE": "BREACHED",
        "A": "BREACHED",
        "B": "BREACHED_MANAGED",
        "C": "BREACHED_MANAGED",
    }

    assert _projected_misses(schedule) == 3


def test_projected_arrivals_are_not_projected_misses():
    """Arrivals are outside the placed denominator, so they stay out of the numerator."""
    arrival = _order("PROJ-0001", promise_hours=3.0, work_units=50.0)
    schedule = compute_schedule([arrival], 10.0, NOW)
    assert schedule.scheduled_orders[0].sla_status == "BREACHED"

    assert _projected_misses(schedule, frozenset({"PROJ-0001"})) == 0


def test_thinning_arrivals_keeps_the_tail_of_the_horizon():
    """A multiplier must shrink demand across the window, not cut off its end.

    Chronologically slicing the first N arrivals looked like full demand for
    most of the horizon followed by a cliff to zero, understating a plan's
    effect on the back half of the projection.
    """
    arrivals = [
        _order(f"PROJ-{i:04d}", promise_hours=10, work_units=1.0) for i in range(10)
    ]
    inputs = ScheduleInputs(
        orders=list(arrivals),
        capacity_per_hour=50.0,
        committed_work_units=0.0,
        dispatch_cutoff=None,
        arrival_multiplier=0.7,
    )

    result = _run_with_arrivals(inputs, arrivals, NOW)

    kept_ids = {o.order_id for o in result.scheduled_orders}
    assert len(kept_ids) == 7
    # Proof this isn't a chronological prefix cut: the last two arrivals in
    # time order both survive thinning.
    assert arrivals[-1].order_id in kept_ids
    assert arrivals[-2].order_id in kept_ids


def test_thinning_never_drops_below_a_positive_keep_count():
    """A tiny multiplier over a short list must not silently keep zero."""
    arrivals = [_order("PROJ-0000", promise_hours=10, work_units=1.0)]
    inputs = ScheduleInputs(
        orders=list(arrivals),
        capacity_per_hour=50.0,
        committed_work_units=0.0,
        dispatch_cutoff=None,
        arrival_multiplier=0.6,
    )

    result = _run_with_arrivals(inputs, arrivals, NOW)

    # round(1 * 0.6) == 1, so the one arrival is still kept.
    assert len(result.scheduled_orders) == 1


def test_exposure_splits_already_late_from_still_saveable():
    """A plan card must not count lost orders as exposure it could still fix."""
    # Promised 30 minutes before NOW: already past its deadline, lost to any plan.
    late = _order("LATE", promise_hours=0.5)
    # Promised 3h after creation but queued behind 100 work units at 10 wu/hr:
    # predicted to miss, with the deadline still ahead. A plan can act on it.
    blocker = _order("BIG", promise_hours=48.0, work_units=100.0)
    saveable = _order("SOON", promise_hours=3.0)
    schedule = compute_schedule([late, blocker, saveable], 10.0, NOW)
    statuses = {o.order_id: o.sla_status for o in schedule.scheduled_orders}
    assert statuses["LATE"] == "BREACHED" and statuses["SOON"] in ("AT_RISK", "BREACHED")

    summary = _summary(_totals(pending=102.0), schedule, 10.0)

    assert summary.already_late_count == 1
    assert summary.saveable_count == 1
    assert summary.already_late_count + summary.saveable_count == (
        summary.at_risk_count + summary.breached_count
    )


def test_nothing_is_recommended_when_no_plan_prevents_a_breach():
    """Refuse to recommend a paid lever that removes no exposure.

    The old logic always crowned a winner, so with nothing at risk it still
    nudged the operator into spending. Recommending nothing is the correct
    answer when nothing is worth its cost.
    """
    plans = [
        _plan("do-nothing", "NONE", prevented=0),
        _plan("buy-the-hour", "HIGH", prevented=0),
    ]

    _recommend_plan(plans)

    assert sum(1 for p in plans if p.recommended) == 0


def test_redistribution_is_never_recommended():
    """Keep a plan that only moves exposure from being suggested.

    Deciding which customers absorb the miss is the operator's call, not a
    default the system steers them into.
    """
    plans = [_plan("defer-the-comfortable", "NONE", prevented=0, relocated=5)]

    _recommend_plan(plans)

    assert plans[0].outcome.is_redistribution
    assert not plans[0].recommended


def test_cheapest_plan_is_recommended_among_those_preventing_equally():
    """Prefer the least expensive plan when two prevent the same exposure."""
    plans = [
        _plan("buy-the-hour", "HIGH", prevented=2),
        _plan("catch-the-late-pickup", "LOW", prevented=2),
    ]

    _recommend_plan(plans)

    assert [p.plan_id for p in plans if p.recommended] == ["catch-the-late-pickup"]


def test_prevention_outranks_cost():
    """Let a costlier plan win when it genuinely rescues more orders."""
    plans = [
        _plan("catch-the-late-pickup", "LOW", prevented=1),
        _plan("buy-the-hour", "HIGH", prevented=6),
    ]

    _recommend_plan(plans)

    assert [p.plan_id for p in plans if p.recommended] == ["buy-the-hour"]


def test_at_most_one_plan_is_recommended():
    """Keep the guarantee that the recommended flag is never carried twice."""
    plans = [
        _plan("defer-the-comfortable", "NONE", prevented=0, relocated=4),
        _plan("catch-the-late-pickup", "LOW", prevented=4),
        _plan("buy-the-hour", "HIGH", prevented=4),
    ]

    _recommend_plan(plans)

    assert sum(1 for p in plans if p.recommended) == 1


def test_demand_multiplier_without_a_horizon_is_rejected():
    """Refuse to scale arrivals that the request never asked to project."""
    with pytest.raises(HTTPException) as exc:
        _require_projection_for_demand_multiplier(
            SimulationRequest(demand_multiplier=0.8)
        )

    assert exc.value.status_code == 422
    assert "projection_horizon_minutes" in exc.value.detail


def test_demand_multiplier_with_a_horizon_is_accepted():
    """Scaling future arrivals is honest once there is a future to scale."""
    _require_projection_for_demand_multiplier(
        SimulationRequest(demand_multiplier=0.8, projection_horizon_minutes=120)
    )
    _require_projection_for_demand_multiplier(
        SimulationRequest(demand_multiplier=NO_OP_DEMAND_MULTIPLIER)
    )
    _require_projection_for_demand_multiplier(SimulationRequest())
