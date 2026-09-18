"""What-if simulations, recovery plans, and action state APIs."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock, repository
from app.auth.dependencies import (
    require_service_token,
    require_session,
    require_session_or_service_token,
)
from app.consequences import EXPOSED, LateDispatchPolicy, late_dispatch_rate
from app.db import get_connection
from app.executor import request_execution
from app.interventions import active_priority_adjustments, apply_internal_effects
from app.levers import Catalog, Lever, Posture, load_catalog
from app.models import (
    ActionStatus,
    Order,
    OrderStatus,
    ScheduleResult,
    SLAStatus,
)
from app.plans import (
    ScheduleInputs,
    build_inputs,
    feasible_levers,
    manage_breaches,
    posture_cost,
    posture_effective_at,
    posture_families,
    score_outcome,
)
from app.projection import (
    DEFAULT_SEED,
    ArrivalBurst,
    ArrivalProjection,
    project_arrivals,
    synthetic_ids,
)
from app.repository import FacilityNotFoundError
from app.routers.reads import _scheduling_inputs
from app.scheduler import (
    CapacityRamp,
    FacilityRiskThresholds,
    classify_facility_risk,
    compute_schedule,
    cutoff_policy,
    operating_calendar,
    risk_thresholds,
    sla_thresholds,
)
from app.schemas import (
    CapacityCommitmentResponse,
    ConfirmEnactmentRequest,
    LeverResponse,
    PlanOutcomeResponse,
    ProjectedArrivalCounts,
    RecoveryActionApplied,
    RecoveryActionResponse,
    RecoveryActionStatusRequest,
    RecoveryApprovalResponse,
    RecoveryPlanActions,
    RecoveryPlanResponse,
    RecoveryPlansResponse,
    SimulationRequest,
    SimulationResponse,
    SimulationSummary,
)

router = APIRouter(tags=["simulation"])

# Demand shaping is modelled elsewhere (app/projection.py); this is just the
# no-op default value when a caller supplies no multiplier assumption.
NO_OP_DEMAND_MULTIPLIER = 1.0

# Cheapest first, for choosing between plans that prevent the same exposure.
COST_ORDER = ("NONE", "LOW", "MEDIUM", "HIGH")


async def _state(conn: AsyncConnection, facility_id: str):
    """Load the facility, effective capacity, and current orders.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility used for the simulation or recovery plan.

    Returns:
        Evaluation time, facility configuration, capacity, the reorderable
        pending orders, database-side order totals, the collection policy,
        the operating calendar, and whether the floor is stalled.
    """
    try:
        facility = await repository.get_facility(conn, facility_id)
    except FacilityNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown facility_id {facility_id}")

    now = await clock.now(conn)
    throughput = await repository.get_throughput_signal(
        conn, facility_id, facility["capacity_per_hour"], now, operating_calendar(facility)
    )
    # Only pending orders are reorderable, and backlog is an aggregate -- neither
    # needs the facility's finished orders materialised.
    pending = await repository.list_pending_orders(conn, facility_id)
    totals = await repository.get_order_totals(conn, facility_id)
    return (
        now,
        facility,
        throughput.scheduling_capacity_per_hour,
        pending,
        totals,
        cutoff_policy(facility),
        operating_calendar(facility),
        throughput.stalled,
    )


def _count_by_status(scheduled) -> dict[SLAStatus, int]:
    """Tally SLA statuses across a slice of the scheduled queue."""
    counts = dict.fromkeys(SLAStatus, 0)
    for order in scheduled:
        counts[order.sla_status] += 1
    return counts


def _summary(
    totals: repository.OrderTotals,
    schedule,
    capacity: float,
    projected_ids: frozenset[str] = frozenset(),
    projected_work_units: float = 0.0,
    risk_thresholds: "FacilityRiskThresholds | None" = None,
    capacity_ramp: "CapacityRamp | None" = None,
    stalled: bool = False,
) -> SimulationSummary:
    """Convert scheduler output into the frontend summary shape.

    Args:
        totals: Database-side order counts and work units by status. A promise or
            capacity assumption never changes an order's status, so the same
            totals describe both the baseline and the simulated run.
        schedule: Result returned by `compute_schedule`.
        capacity: Capacity used for the projected recovery time.
        projected_ids: Ids of orders nobody has placed yet. Their exposure is
            reported separately so a plan can never claim to have rescued an
            order that does not exist.
        projected_work_units: Work those projected arrivals carry.
        risk_thresholds: This facility's own surge-risk thresholds. `None`
            falls back to the frozen defaults.
        capacity_ramp: Set when `capacity` includes a lever that has not
            landed yet, so recovery waits for it the way the queue does.
        stalled: The live floor is not moving work. No plan or assumption
            restarts it, so every projection carries the same risk floor.

    Returns:
        A frontend-ready summary without persisting derived values.
    """
    # Clearing the backlog means finishing every unfinished order, not only the
    # pending ones. Work already started still consumes capacity, so scoring
    # recovery on pending work alone contradicts backlog_work_units below.
    recovery_hours = (
        totals.backlog_work_units / capacity
        if capacity_ramp is None
        else capacity_ramp.hours_to_clear(totals.backlog_work_units, capacity)
    )

    placed = _count_by_status(
        o for o in schedule.scheduled_orders if o.order_id not in projected_ids
    )
    # Same rule as consequences.count_preventable: exposed with the deadline
    # still ahead is saveable, past it is already lost.
    exposed = [
        o
        for o in schedule.scheduled_orders
        if o.order_id not in projected_ids and o.sla_status in EXPOSED
    ]
    already_late = sum(1 for o in exposed if o.promised_dispatch_at <= schedule.generated_at)
    arrivals = None
    if projected_ids:
        projected = _count_by_status(
            o for o in schedule.scheduled_orders if o.order_id in projected_ids
        )
        arrivals = ProjectedArrivalCounts(
            orders=len(projected_ids),
            work_units=projected_work_units,
            safe_count=projected[SLAStatus.SAFE],
            watch_count=projected[SLAStatus.WATCH],
            at_risk_count=projected[SLAStatus.AT_RISK],
            breached_count=projected[SLAStatus.BREACHED],
        )

    return SimulationSummary(
        backlog_orders=totals.backlog_orders,
        backlog_work_units=totals.backlog_work_units,
        safe_count=placed[SLAStatus.SAFE],
        watch_count=placed[SLAStatus.WATCH],
        at_risk_count=placed[SLAStatus.AT_RISK],
        breached_count=placed[SLAStatus.BREACHED],
        breached_managed_count=placed[SLAStatus.BREACHED_MANAGED],
        already_late_count=already_late,
        saveable_count=len(exposed) - already_late,
        projected_recovery_hours=recovery_hours,
        risk_level=classify_facility_risk(schedule, risk_thresholds, stalled=stalled),
        projected_arrivals=arrivals,
    )


def _projected_misses(
    schedule: ScheduleResult, projected_ids: frozenset[str] = frozenset()
) -> int:
    """Placed orders a plan still leaves missing a promise that is ahead of now.

    The late-dispatch rate adds these to the misses the database has already
    realised, which include every unfinished order past its promise. Counting
    only promises still ahead keeps each miss in exactly one of the two. An
    apology relabels a miss without preventing it, so a managed breach counts.

    Args:
        schedule: A plan's projected schedule.
        projected_ids: Arrivals nobody has placed, which are outside `placed`.
    """
    return sum(
        1
        for o in schedule.scheduled_orders
        if o.order_id not in projected_ids
        and o.sla_status in (SLAStatus.BREACHED, SLAStatus.BREACHED_MANAGED)
        and o.promised_dispatch_at >= schedule.generated_at
    )


def _hypothetical_orders(orders: list[Order], promise_hours: float | None) -> list[Order]:
    """Copy pending orders with a hypothetical promise deadline.

    Args:
        orders: Current operational orders.
        promise_hours: Simulated promise policy in hours, or `None` to keep each
            order's persisted deadline.

    Returns:
        New order models suitable for a non-persistent scheduler run.
    """
    # Without an explicit hypothetical policy the persisted customer deadline is
    # the only correct one to schedule against. Substituting the facility
    # default here would silently re-promise every pending order and
    # make a capacity-only What-If change SLA status for the wrong reason.
    if promise_hours is None:
        return list(orders)

    return [
        order.model_copy(
            update={
                "promised_dispatch_at": order.created_at + timedelta(hours=promise_hours)
            }
        )
        if order.status == OrderStatus.PENDING
        else order
        for order in orders
    ]


def _require_projection_for_demand_multiplier(request: SimulationRequest) -> None:
    """Refuse a demand assumption with no future to apply it to.

    A multiplier scales arrivals that have not happened yet. Without a horizon
    there are none, so honouring it would report an assumption the scheduler
    never applied.

    Args:
        request: The simulation request as received.

    Raises:
        HTTPException: 422 when a multiplier is sent without a horizon.
    """
    scaled = (
        request.demand_multiplier is not None
        and request.demand_multiplier != NO_OP_DEMAND_MULTIPLIER
    )
    if scaled and request.projection_horizon_minutes is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "demand_multiplier scales future arrivals, so it needs "
                "projection_horizon_minutes as well. Without a horizon there are "
                "no future arrivals to scale."
            ),
        )


async def _simulate(
    conn: AsyncConnection,
    facility_id: str,
    request: SimulationRequest,
) -> SimulationResponse:
    """Run a non-persistent simulation against current database facts.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility to simulate.
        request: Capacity, promise, and demand assumptions from the frontend.

    Returns:
        Baseline and hypothetical scheduler summaries.
    """
    _require_projection_for_demand_multiplier(request)

    now, facility, current_capacity, orders, totals, cutoff, calendar, stalled = await _state(
        conn, facility_id
    )
    thresholds = sla_thresholds(facility)
    ratios = risk_thresholds(facility)
    applied_capacity = request.capacity_per_hour or current_capacity
    applied_demand = request.demand_multiplier or NO_OP_DEMAND_MULTIPLIER
    promise = float(facility["dispatch_promise_hours"])

    # Only an explicit request overrides deadlines; the reported value is the
    # policy the simulation assumed, which is the facility default when unset.
    promise_override = request.dispatch_promise_hours
    applied_promise = promise_override or promise

    # Both runs get the same projected arrivals at the observed rate; only the
    # simulated one scales them, so the comparison isolates the assumption.
    observed_demand = await repository.get_demand_work_units_per_hour(conn, facility_id, now)
    baseline_arrivals = _project(request, observed_demand, now, facility_id, promise, 1.0)
    simulated_arrivals = _project(
        request, observed_demand, now, facility_id, promise, applied_demand
    )

    # Work already on the floor is charged against both runs: a What-If changes
    # the assumption, not what the warehouse is already holding.
    committed = totals.committed_work_units
    # Adjustments an approved plan already put in force are part of the live
    # queue, not part of the hypothesis. Both sides start from them, or the
    # comparison is measured against a queue that does not exist.
    in_force = await active_priority_adjustments(conn, facility_id)
    baseline_schedule = compute_schedule(
        orders + baseline_arrivals,
        current_capacity,
        now,
        committed,
        dispatch_cutoff=cutoff,
        operating_calendar=calendar,
        sla_thresholds=thresholds,
        priority_adjustments=in_force,
    )
    simulated_orders = _hypothetical_orders(orders, promise_override)
    simulated_schedule = compute_schedule(
        simulated_orders + simulated_arrivals,
        applied_capacity,
        now,
        committed,
        dispatch_cutoff=cutoff,
        operating_calendar=calendar,
        sla_thresholds=thresholds,
        priority_adjustments=in_force,
    )

    return SimulationResponse(
        generated_at=now,
        baseline=_summary(
            totals,
            baseline_schedule,
            current_capacity,
            synthetic_ids(baseline_arrivals),
            sum(o.work_units for o in baseline_arrivals),
            ratios,
            stalled=stalled,
        ),
        simulated=_summary(
            totals,
            simulated_schedule,
            applied_capacity,
            synthetic_ids(simulated_arrivals),
            sum(o.work_units for o in simulated_arrivals),
            ratios,
            stalled=stalled,
        ),
        applied_capacity_per_hour=applied_capacity,
        applied_dispatch_promise_hours=applied_promise,
        applied_demand_multiplier=applied_demand,
        applied_projection_horizon_minutes=request.projection_horizon_minutes,
    )


def _project(
    request: SimulationRequest,
    observed_demand: float,
    now: datetime,
    facility_id: str,
    promise_hours: float,
    multiplier: float,
) -> list[Order]:
    """Build projected arrivals for one side of the comparison.

    Returns nothing unless the caller asked for a horizon: without one there is
    no future to project, and inventing one would put orders on screen that
    nobody asked about.
    """
    if request.projection_horizon_minutes is None:
        return []

    projection = ArrivalProjection(
        horizon_minutes=request.projection_horizon_minutes,
        arrival_multiplier=multiplier,
        burst=(
            ArrivalBurst(
                at_minute=request.burst.at_minute, work_units=request.burst.work_units
            )
            if request.burst
            else None
        ),
        seed=request.seed if request.seed is not None else DEFAULT_SEED,
    )
    return project_arrivals(observed_demand, projection, now, facility_id, promise_hours)


@router.post(
    "/simulations",
    response_model=SimulationResponse,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def create_simulation(
    request: SimulationRequest,
    facility_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> SimulationResponse:
    """Return baseline and simulated scheduler results.

    Args:
        request: Hypothetical assumptions; omitted values use live settings.
        facility_id: Facility to simulate.
        conn: Request-scoped database transaction.

    Returns:
        Frontend-compatible simulation comparison.
    """
    return await _simulate(conn, facility_id, request)


@router.get(
    "/recovery-plans",
    response_model=RecoveryPlansResponse,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def get_recovery_plans(
    facility_id: str,
    projection_horizon_minutes: int | None = Query(default=None, gt=0, le=24 * 60),
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryPlansResponse:
    """Compose recovery plans from the lever catalog and score what each trades.

    Every projection is the same `compute_schedule` re-run on modified inputs,
    never an estimate, and each plan reports exposure prevented separately from
    exposure merely moved.

    Args:
        facility_id: Facility for which plans are evaluated.
        projection_horizon_minutes: Minutes of future arrivals to include.
            Inflow levers act only on arrivals, so without this they are
            reported as unavailable rather than offered as if they helped.
        conn: Request-scoped database transaction.

    Returns:
        Feasible postures with their outcomes, plus the levers that cannot help.
    """
    now, facility, current_capacity, orders, totals, cutoff, calendar, stalled = await _state(
        conn, facility_id
    )
    thresholds = sla_thresholds(facility)
    ratios = risk_thresholds(facility)
    current_promise = float(facility["dispatch_promise_hours"])
    catalog = load_catalog()

    minutes_to_cutoff = (
        None
        if cutoff is None
        else (cutoff.next_dispatch_after(now) - now).total_seconds() / 60.0
    )
    floor_reopens_at = (
        None
        if calendar is None or calendar.is_open_at(now)
        else calendar.next_open_at(now)
    )

    arrivals: list[Order] = []
    if projection_horizon_minutes is not None:
        observed = await repository.get_demand_work_units_per_hour(conn, facility_id, now)
        arrivals = project_arrivals(
            observed,
            ArrivalProjection(horizon_minutes=projection_horizon_minutes),
            now,
            facility_id,
            current_promise,
        )

    usable, blocked = feasible_levers(
        catalog,
        orders,
        has_cutoff=cutoff is not None,
        has_projection=bool(arrivals),
        minutes_to_cutoff=minutes_to_cutoff,
        floor_reopens_at=floor_reopens_at,
    )
    available = {lever.id: lever for lever in usable}

    late_dispatch_policy = LateDispatchPolicy.load()
    realised_misses, orders_placed = await repository.count_late_dispatches(
        conn, facility_id, now, window_days=late_dispatch_policy.window_days
    )
    base = ScheduleInputs(
        orders=orders + arrivals,
        capacity_per_hour=current_capacity,
        committed_work_units=totals.committed_work_units,
        dispatch_cutoff=cutoff,
        operating_calendar=calendar,
        sla_thresholds=thresholds,
        priority_adjustments=await active_priority_adjustments(conn, facility_id),
    )
    # Every posture re-runs the full scheduler, which is synchronous CPU work
    # of up to a couple of seconds on a large backlog. Running it on the event
    # loop froze every other request on this worker for that long.
    plans = await asyncio.to_thread(
        _evaluate_postures,
        base=base,
        catalog=catalog,
        available=available,
        arrivals=arrivals,
        orders=orders,
        totals=totals,
        now=now,
        current_promise=current_promise,
        ratios=ratios,
        realised_misses=realised_misses,
        orders_placed=orders_placed,
        late_dispatch_policy=late_dispatch_policy,
        stalled=stalled,
    )
    return RecoveryPlansResponse(
        plans=plans,
        unavailable_levers=[_lever_response(lever, reason) for lever, reason in blocked],
    )


def _evaluate_postures(
    *,
    base: ScheduleInputs,
    catalog: Catalog,
    available: dict[str, Lever],
    arrivals: list[Order],
    orders: list[Order],
    totals: repository.OrderTotals,
    now: datetime,
    current_promise: float,
    ratios: FacilityRiskThresholds,
    realised_misses: int,
    orders_placed: int,
    late_dispatch_policy: LateDispatchPolicy,
    stalled: bool = False,
) -> list[RecoveryPlanResponse]:
    """Score every feasible posture against the no-action baseline.

    Pure computation with no database access, so it can run off the event loop.
    """
    baseline_schedule = base.run(now)
    placed_ids = frozenset(o.order_id for o in orders)
    projected_ids = synthetic_ids(arrivals)
    projected_work_units = sum(o.work_units for o in arrivals)

    plans: list[RecoveryPlanResponse] = []
    for posture in catalog.postures:
        inputs = build_inputs(base, posture, catalog, now, available)
        if inputs is None:
            continue  # one of its levers cannot help right now

        schedule = _run_with_arrivals(inputs, arrivals, now)
        if any(
            catalog.lever(lever_id).effect.kind == "manage_breach"
            for lever_id in posture.lever_ids
        ):
            schedule = manage_breaches(schedule, placed_ids)

        outcome = score_outcome(baseline_schedule, schedule, projected_ids)
        projected = _summary(
            totals,
            schedule,
            inputs.capacity_per_hour,
            projected_ids,
            projected_work_units,
            ratios,
            inputs.capacity_ramp,
            stalled,
        )
        plans.append(
            RecoveryPlanResponse(
                plan_id=posture.id,
                title=posture.title,
                description=_posture_description(catalog, posture),
                relative_cost=posture_cost(catalog, posture).value,
                trades=posture.trades,
                families=[family.value for family in posture_families(catalog, posture)],
                levers=[_lever_response(catalog.lever(i)) for i in posture.lever_ids],
                actions=RecoveryPlanActions(
                    capacity_per_hour=inputs.capacity_per_hour,
                    dispatch_promise_hours=current_promise,
                    demand_multiplier=inputs.arrival_multiplier,
                ),
                projected=projected,
                outcome=PlanOutcomeResponse(
                    projected_late_dispatch_rate=late_dispatch_rate(
                        # Misses already realised cannot be undone by any plan,
                        # so they carry into every projection. What a plan
                        # changes is how many of the still-open orders join
                        # them.
                        misses=realised_misses + _projected_misses(schedule, projected_ids),
                        placed=orders_placed,
                        policy=late_dispatch_policy,
                    ).rate,
                    breaches_avoided=outcome.breaches_avoided,
                    breaches_relocated=outcome.breaches_relocated,
                    net_breach_change=outcome.net_breach_change,
                    orders_improved=len(outcome.orders_improved),
                    orders_worsened=len(outcome.orders_worsened),
                    is_redistribution=outcome.is_redistribution,
                ),
                effective_at=posture_effective_at(catalog, posture, now),
                recommended=False,
            )
        )

    _recommend_plan(plans)
    return plans


def _run_with_arrivals(
    inputs: ScheduleInputs, arrivals: list[Order], now: datetime
) -> ScheduleResult:
    """Run a posture, thinning projected arrivals if an inflow lever applies.

    Inflow levers reduce what arrives next, so they change the projected orders
    rather than the queue. Nothing already placed is affected, which is exactly
    why they cannot be presented as a rescue.
    """
    if inputs.arrival_multiplier == 1.0 or not arrivals:
        return inputs.run(now)

    arrival_ids = {a.order_id for a in arrivals}
    placed = [o for o in inputs.orders if o.order_id not in arrival_ids]
    return replace(
        inputs, orders=placed + _thin_evenly(arrivals, inputs.arrival_multiplier)
    ).run(now)


def _thin_evenly(arrivals: list[Order], multiplier: float) -> list[Order]:
    """Keep a share of arrivals spread across the whole horizon, not a prefix.

    `arrivals[:kept]` used to drop everything after some point in time: a 0.7
    multiplier over a 120-minute horizon looked like full demand for 84
    minutes then a cliff to zero, rather than demand reduced by 30% across the
    whole window -- understating an inflow lever's effect on the back half of
    the projection. `arrivals` is already chronological (app/projection.py
    spreads steady arrivals evenly), so picking indices spaced evenly across
    it approximates thinning demand uniformly over time instead.
    """
    keep = round(len(arrivals) * multiplier)
    if keep <= 0:
        return []
    if keep >= len(arrivals):
        return list(arrivals)
    if keep == 1:
        return [arrivals[0]]
    # Evenly spaced, always including the first and last arrival, so thinning
    # never reads as a clean cutoff at either end of the horizon.
    spacing = (len(arrivals) - 1) / (keep - 1)
    indices = sorted({round(i * spacing) for i in range(keep)})
    return [arrivals[i] for i in indices]


def _posture_description(catalog: Catalog, posture: Posture) -> str:
    """Describe a posture by the levers it actually pulls."""
    if not posture.lever_ids:
        return (
            "No intervention. Shown so the cost of inaction is a visible choice "
            "rather than an absence of one."
        )
    return " ".join(catalog.lever(lever_id).description for lever_id in posture.lever_ids)


def _lever_response(lever: Lever, unavailable_reason: str | None = None) -> LeverResponse:
    """Map a catalog lever to its API shape."""
    return LeverResponse(
        id=lever.id,
        name=lever.name,
        family=lever.family.value,
        description=lever.description,
        cost=lever.cost.value,
        lead_time_minutes=lever.lead_time_minutes,
        reversible=lever.reversible,
        side_effects=list(lever.side_effects),
        unavailable_reason=unavailable_reason,
    )


def _recommend_plan(plans: list[RecoveryPlanResponse]) -> None:
    """Mark the cheapest plan that genuinely prevents the most exposure.

    Redistribution never wins. Moving exposure between customers is a decision
    an operator takes deliberately, not one the system should nudge them into.
    """
    real = [plan for plan in plans if plan.outcome and plan.outcome.net_breach_change > 0]
    if not real:
        return

    best = max(
        real,
        key=lambda plan: (
            plan.outcome.net_breach_change,
            -COST_ORDER.index(plan.relative_cost),
        ),
    )
    best.recommended = True


@router.post(
    "/recovery-plans/{plan_id}/approve",
    response_model=RecoveryApprovalResponse,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def approve_recovery_plan(
    plan_id: str,
    background_tasks: BackgroundTasks,
    facility_id: str,
    projection_horizon_minutes: int | None = Query(default=None, gt=0, le=24 * 60),
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryApprovalResponse:
    """Persist a selected recovery action and hand it to n8n for execution.

    Args:
        plan_id: Stable recovery plan identifier selected by the operator.
        background_tasks: Runs the n8n handoff after this transaction commits.
        facility_id: Facility receiving the action.
        projection_horizon_minutes: The horizon the operator's plans were
            built with. Approval re-derives the plans, so an inflow plan is
            only approvable when this matches what was shown.
        conn: Request-scoped database transaction.

    Returns:
        The frontend-compatible pending action reference.

    Raises:
        HTTPException: 404 for an unknown plan, 409 when an action is in flight.
    """
    # Postures come from the catalog, so an operator can add one without this
    # endpoint needing to know about it.
    if plan_id not in {posture.id for posture in load_catalog().postures}:
        raise HTTPException(status_code=404, detail=f"unknown recovery plan {plan_id}")

    await _require_no_action_in_flight(conn, facility_id)

    plans = await get_recovery_plans(
        facility_id, projection_horizon_minutes=projection_horizon_minutes, conn=conn
    )
    plan = next((item for item in plans.plans if item.plan_id == plan_id), None)
    if plan is None:
        # Known posture, but not applicable right now -- e.g. every lever it
        # needs arrives after the carrier collection.
        raise HTTPException(
            status_code=409,
            detail=f"recovery plan {plan_id} cannot be applied in the current state",
        )

    effects = _posture_effects(load_catalog(), plan_id)
    facility = await repository.get_facility(conn, facility_id)
    born_status = (
        ActionStatus.AWAITING_ENACTMENT
        if _requires_enactment(effects) and facility["execution_adapter"] != "simulator"
        else ActionStatus.PENDING
    )

    action_id = f"action-{uuid4().hex}"
    await conn.execute(
        text(
            """
            INSERT INTO recovery_actions (
                action_id, plan_id, facility_id, capacity_per_hour,
                dispatch_promise_hours, demand_multiplier, status, effects
            ) VALUES (
                :action_id, :plan_id, :facility_id, :capacity_per_hour,
                :dispatch_promise_hours, :demand_multiplier, :status,
                CAST(:effects AS JSONB)
            )
            """
        ),
        {
            "action_id": action_id,
            "plan_id": plan.plan_id,
            "facility_id": facility_id,
            "capacity_per_hour": plan.actions.capacity_per_hour,
            "dispatch_promise_hours": plan.actions.dispatch_promise_hours,
            "demand_multiplier": plan.actions.demand_multiplier,
            "status": born_status.value,
            "effects": json.dumps(effects),
        },
    )
    # Background tasks run BEFORE a yield-dependency's exit code in this FastAPI
    # version -- verified, not assumed -- so relying on get_connection to commit
    # would hand n8n an action_id whose row is not visible yet, and its first
    # GET /recovery-actions/{action_id} would 404. Commit here instead. This must
    # stay the last database statement in the request.
    await conn.commit()

    if born_status == ActionStatus.PENDING:
        # Queued rather than awaited so a slow or unreachable n8n cannot stall
        # the operator's approval. Failure leaves the action PENDING for
        # collection. An AWAITING_ENACTMENT action has no n8n leg at all --
        # GET /recovery-actions defaults to status=PENDING, so n8n would
        # never see it regardless, and notifying it would be pure noise.
        background_tasks.add_task(
            request_execution, action_id=action_id, plan_id=plan.plan_id, facility_id=facility_id
        )

    return RecoveryApprovalResponse(
        plan_id=plan_id,
        action_id=action_id,
        status=born_status,
    )


# Effect kinds an execution_adapter other than 'simulator' gates: each assumes
# a physical, external change (added capacity, a later carrier collection),
# so a manual/webhook facility must have a human confirm it happened before
# it becomes real, rather than n8n calling a simulator that does not exist.
ADAPTER_GATED_KINDS = frozenset({"capacity_delta", "cutoff_shift"})


def _requires_enactment(effects: list[dict]) -> bool:
    """Return whether any effect in this posture needs a physical execution adapter.

    A QUEUE/PROMISE-only posture (e.g. protect-new-customers) has nothing
    external to enact on any facility, so it is unaffected by adapter choice
    -- same as today, on every adapter.
    """
    return any(effect.get("kind") in ADAPTER_GATED_KINDS for effect in effects)


async def _require_no_action_in_flight(conn: AsyncConnection, facility_id: str) -> None:
    """Reject a second approval while one is still executing.

    Locks matching rows so two concurrent approvals cannot both pass the check
    and have n8n apply the same intervention twice.

    Blocks on PENDING and AWAITING_ENACTMENT only -- not ENACTED. Once a
    manual/webhook action reaches ENACTED, its execution process is finished;
    only observation (verification, P6) is outstanding, and a lever's lead
    time (up to 90 minutes for EXTEND_SHIFT) must not lock the facility out of
    an unrelated second approval for that long.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility being approved for.

    Raises:
        HTTPException: 409 when the facility already has an in-flight action.
    """
    result = await conn.execute(
        text(
            """
            SELECT action_id
            FROM recovery_actions
            WHERE facility_id = :facility_id
              AND status IN ('PENDING', 'AWAITING_ENACTMENT')
            FOR UPDATE
            """
        ),
        {"facility_id": facility_id},
    )
    row = result.first()
    if row is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"recovery action {row[0]} is still executing for {facility_id}; "
                "wait for it to reach SUCCESS, ENACTED, or FAILED before approving another"
            ),
        )


def _posture_effects(catalog, posture_id: str) -> list[dict]:
    """Serialise a posture's levers into the effects an executor must carry out.

    Each effect keeps the lever and family it came from, so the executor can
    route it -- capacity and inflow leave through n8n, a queue reorder or a
    re-promise is applied here -- and so an operator reading the action row can
    see which lever produced which change.
    """
    posture = next((p for p in catalog.postures if p.id == posture_id), None)
    if posture is None:
        return []

    effects = []
    for lever_id in posture.lever_ids:
        lever = catalog.lever(lever_id)
        effect = lever.effect.model_dump(mode="json")
        effect["lever_id"] = lever.id
        effect["family"] = lever.family.value
        effects.append(effect)
    return effects


@router.post(
    "/recovery-actions/{action_id}/apply",
    response_model=RecoveryActionApplied,
    # n8n executor node "Apply Internal Effects" (plan 03, ACCESS-02).
    dependencies=[Depends(require_service_token)],
)
async def apply_recovery_action(
    action_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryActionApplied:
    """Apply the parts of an approved action that only this backend can apply.

    n8n calls this alongside the simulator calls it already makes. A capacity or
    inflow change belongs to the outside world and leaves through n8n; a queue
    reorder or a re-promise has no external system to call, because its whole
    effect is a change to state this service owns.

    Safe to call for any action, including one with nothing internal to do, and
    safe to call twice: applying is idempotent per lever, so an n8n retry cannot
    move a customer deadline a second time.

    Args:
        action_id: Approved action to apply.
        conn: Request-scoped database transaction.

    Returns:
        Which levers this call applied.

    Raises:
        HTTPException: 404 when the action is unknown.
    """
    result = await conn.execute(
        text("SELECT * FROM recovery_actions WHERE action_id = :action_id"),
        {"action_id": action_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recovery action {action_id}")

    facility_id = row["facility_id"]
    now = await clock.now(conn)
    capacity, totals, cutoff, thresholds, calendar = await _scheduling_inputs(
        conn, facility_id, now
    )
    orders = await repository.list_pending_orders(conn, facility_id)
    schedule = compute_schedule(
        orders,
        capacity,
        now,
        committed_work_units=totals.committed_work_units,
        dispatch_cutoff=cutoff,
        operating_calendar=calendar,
        sla_thresholds=thresholds,
    )

    applied = await apply_internal_effects(
        conn,
        action_id=action_id,
        facility_id=facility_id,
        effects=row["effects"] or [],
        orders=orders,
        schedule=schedule,
        now=now,
    )
    await conn.commit()

    return RecoveryActionApplied(action_id=action_id, applied_levers=applied)


async def _action_response(conn: AsyncConnection, row) -> RecoveryActionResponse:
    """Map a recovery-action database row to its API schema.

    Args:
        conn: Request-scoped database transaction, to read this action's
            capacity_commitments alongside it.
        row: Mapping returned by a recovery-action query.

    Returns:
        Typed action state, execution parameters, and any capacity claims
        made under it with their verification status.
    """
    commitments = await conn.execute(
        text(
            """
            SELECT lever_id, target_work_units_per_hour, verification_status,
                   verified_at, observed_work_units_per_hour
            FROM capacity_commitments
            WHERE action_id = :action_id
            ORDER BY id ASC
            """
        ),
        {"action_id": row["action_id"]},
    )
    return RecoveryActionResponse(
        action_id=row["action_id"],
        plan_id=row["plan_id"],
        facility_id=row["facility_id"],
        status=ActionStatus(row["status"]),
        capacity_per_hour=row["capacity_per_hour"],
        dispatch_promise_hours=row["dispatch_promise_hours"],
        demand_multiplier=row["demand_multiplier"],
        effects=row["effects"] or [],
        error_detail=row["error_detail"],
        enacted_at=row["enacted_at"],
        capacity_commitments=[
            CapacityCommitmentResponse(
                lever_id=commitment.lever_id,
                target_work_units_per_hour=commitment.target_work_units_per_hour,
                verification_status=commitment.verification_status,
                verified_at=commitment.verified_at,
                observed_work_units_per_hour=commitment.observed_work_units_per_hour,
            )
            for commitment in commitments
        ],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

@router.get(
    "/recovery-actions",
    response_model=list[RecoveryActionResponse],
    # Dual caller (D-11): n8n executor node "Get Pending Actions" (token) AND
    # the frontend's listRecoveryActions (session cookie). Plan 03, ACCESS-02.
    dependencies=[Depends(require_session_or_service_token)],
)
async def list_recovery_actions(
    facility_id: str,
    status: ActionStatus | None = None,
    conn: AsyncConnection = Depends(get_connection),
) -> list[RecoveryActionResponse]:
    """Return recovery actions for a facility, oldest first.

    Defaults to PENDING because the n8n executor polls this endpoint, takes the
    first item, and expects work that still needs doing. Returning completed
    actions by default would hand it a finished one and silently execute
    nothing, and oldest-first keeps that collection FIFO.

    Args:
        facility_id: Facility whose actions should be returned.
        status: Which state to return; defaults to PENDING. Pass SUCCESS or
            FAILED to read execution history instead.
        conn: Request-scoped database transaction.

    Returns:
        Matching actions with their execution parameters and outcome.
    """
    result = await conn.execute(
        text(
            """
            SELECT *
            FROM recovery_actions
            WHERE facility_id = :facility_id AND status = :status
            ORDER BY created_at ASC
            """
        ),
        {"facility_id": facility_id, "status": (status or ActionStatus.PENDING).value},
    )
    return [await _action_response(conn, row) for row in result.mappings().all()]


@router.get(
    "/recovery-actions/{action_id}",
    response_model=RecoveryActionResponse,
    # Frontend-only (session): ACCESS-01, phase 3. Distinct from the two
    # neighbouring `/recovery-actions*` routes -- the plural GET is dual
    # caller (D-11) and `/apply`+`/status` are n8n-only (require_service_token).
    dependencies=[Depends(require_session)],
)
async def get_recovery_action(
    action_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryActionResponse:
    """Return one persisted recovery action.

    Args:
        action_id: Action identifier returned by the approval endpoint.
        conn: Request-scoped database transaction.

    Returns:
        Current action state and the parameters n8n should execute.
    """
    result = await conn.execute(
        text("SELECT * FROM recovery_actions WHERE action_id = :action_id"),
        {"action_id": action_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recovery action {action_id}")
    return await _action_response(conn, row)


@router.post(
    "/recovery-actions/{action_id}/status",
    response_model=RecoveryActionResponse,
    # n8n executor nodes "Mark Action SUCCESS" and "Mark Action FAILED"
    # (plan 03, ACCESS-02).
    dependencies=[Depends(require_service_token)],
)
async def update_recovery_action_status(
    action_id: str,
    request: RecoveryActionStatusRequest,
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryActionResponse:
    """Move a pending action to success or failure exactly once.

    Args:
        action_id: Action being completed by the executor.
        request: Terminal state and optional failure detail.
        conn: Request-scoped database transaction.

    Returns:
        Updated action state.
    """
    if request.status == ActionStatus.PENDING:
        raise HTTPException(status_code=422, detail="action status can only move to SUCCESS or FAILED")
    if request.status == ActionStatus.FAILED and not request.error_detail:
        raise HTTPException(status_code=422, detail="error_detail is required for FAILED actions")

    result = await conn.execute(
        text("SELECT * FROM recovery_actions WHERE action_id = :action_id FOR UPDATE"),
        {"action_id": action_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recovery action {action_id}")
    current = ActionStatus(row["status"])
    if current == request.status:
        return await _action_response(conn, row)
    if current != ActionStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"action is already {current.value}")

    result = await conn.execute(
        text(
            """
            UPDATE recovery_actions
            SET status = :status, error_detail = :error_detail, updated_at = NOW()
            WHERE action_id = :action_id AND status = 'PENDING'
            RETURNING *
            """
        ),
        {
            "action_id": action_id,
            "status": request.status.value,
            "error_detail": request.error_detail if request.status == ActionStatus.FAILED else None,
        },
    )
    updated = result.mappings().first()
    if updated is None:
        raise HTTPException(status_code=409, detail="action was updated concurrently")

    if request.status == ActionStatus.SUCCESS:
        await _apply_facility_policy(conn, updated)

    return await _action_response(conn, updated)


@router.post(
    "/recovery-actions/{action_id}/confirm-enactment",
    response_model=RecoveryActionResponse,
    # The operator's own act, not n8n's: this is the human affirming a real
    # phone call happened, so it is session-gated like approval itself,
    # never token-gated.
    dependencies=[Depends(require_session)],
)
async def confirm_recovery_action_enactment(
    action_id: str,
    request: ConfirmEnactmentRequest,
    conn: AsyncConnection = Depends(get_connection),
) -> RecoveryActionResponse:
    """Move an AWAITING_ENACTMENT action to ENACTED or FAILED, once.

    ENACTED, not SUCCESS: SUCCESS already means only "n8n successfully called
    the endpoints", and reusing it here for "a human clicked confirm" would
    recreate the exact conflation that distinction is meant to avoid, just
    for a different caller. On success, applies this posture's
    internal effects (queue, promise, cutoff) and records the facility policy
    change -- exactly what n8n's synchronous chain does for the simulator
    adapter, at the moment execution actually completed for this one instead.

    Args:
        action_id: Action awaiting a human's confirmation.
        request: Whether the real-world change happened, and why not if not.
        conn: Request-scoped database transaction.

    Raises:
        HTTPException: 404 unknown action, 422 missing error_detail on a
            failure, 409 if the action is not AWAITING_ENACTMENT or was
            updated concurrently.
    """
    if not request.succeeded and not request.error_detail:
        raise HTTPException(
            status_code=422, detail="error_detail is required when succeeded is false"
        )

    result = await conn.execute(
        text("SELECT * FROM recovery_actions WHERE action_id = :action_id FOR UPDATE"),
        {"action_id": action_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown recovery action {action_id}")
    current = ActionStatus(row["status"])
    if current != ActionStatus.AWAITING_ENACTMENT:
        raise HTTPException(
            status_code=409,
            detail=f"action {action_id} is {current.value}, not awaiting enactment",
        )

    new_status = ActionStatus.ENACTED if request.succeeded else ActionStatus.FAILED
    result = await conn.execute(
        text(
            """
            UPDATE recovery_actions
            SET status = :status,
                error_detail = :error_detail,
                -- :is_enacted is bound separately from :status, not compared
                -- to it in SQL: reusing one bind parameter as both a plain
                -- column value and a text-literal comparison makes asyncpg
                -- unable to infer a single type for it (AmbiguousParameterError).
                enacted_at = CASE WHEN :is_enacted THEN NOW() ELSE enacted_at END,
                updated_at = NOW()
            WHERE action_id = :action_id AND status = 'AWAITING_ENACTMENT'
            RETURNING *
            """
        ),
        {
            "action_id": action_id,
            "status": new_status.value,
            "is_enacted": new_status == ActionStatus.ENACTED,
            "error_detail": request.error_detail if not request.succeeded else None,
        },
    )
    updated = result.mappings().first()
    if updated is None:
        raise HTTPException(status_code=409, detail="action was updated concurrently")

    if new_status == ActionStatus.ENACTED:
        facility_id = updated["facility_id"]
        now = await clock.now(conn)
        capacity, totals, cutoff, thresholds, calendar = await _scheduling_inputs(
            conn, facility_id, now
        )
        orders = await repository.list_pending_orders(conn, facility_id)
        schedule = compute_schedule(
            orders,
            capacity,
            now,
            committed_work_units=totals.committed_work_units,
            dispatch_cutoff=cutoff,
            operating_calendar=calendar,
            sla_thresholds=thresholds,
        )
        await apply_internal_effects(
            conn,
            action_id=action_id,
            facility_id=facility_id,
            effects=updated["effects"] or [],
            orders=orders,
            schedule=schedule,
            now=now,
        )
        await _apply_facility_policy(conn, updated)

    # No explicit commit: unlike approve_recovery_plan, nothing here spawns a
    # background task that needs the row visible before this request's own
    # transaction ends, so this follows update_recovery_action_status's
    # existing pattern and lets get_connection's dependency commit at the end
    # of the request. An explicit commit() here previously closed the
    # transaction before _action_response's own read, which failed with
    # "Can't operate on closed transaction inside context manager."
    return await _action_response(conn, updated)


async def _apply_facility_policy(conn: AsyncConnection, action) -> None:
    """Record an executed action's policy, and its capacity claim, honestly.

    Only `dispatch_promise_hours` writes to `facilities` now:
    backend-owned policy, no physical assumption, applies to future orders
    only. `capacity_per_hour` used to write here too,
    the instant execution completed -- on any adapter, including the
    simulator. That was optimistic in a way full-charging in
    `committed_work_units` (P5) was not: a written number is believed
    immediately and forever, with nothing to later say it was wrong. A
    capacity_delta effect's target becomes a `capacity_commitments` row
    instead, verified against measured throughput (`app/verification.py`)
    rather than trusted on arrival. `get_throughput_signal` already prefers
    derived telemetry over configured capacity, so scheduling is unaffected
    by this change -- the number now moves only once the floor's own
    transitions show it moved.

    Args:
        conn: Request-scoped database transaction.
        action: The recovery-action row that just reached SUCCESS or ENACTED.
    """
    await conn.execute(
        text(
            """
            UPDATE facilities
            SET dispatch_promise_hours = :dispatch_promise_hours
            WHERE facility_id = :facility_id
            """
        ),
        {
            "dispatch_promise_hours": action["dispatch_promise_hours"],
            "facility_id": action["facility_id"],
        },
    )
    await _record_capacity_commitments(conn, action)


async def _record_capacity_commitments(conn: AsyncConnection, action) -> None:
    """Record what each capacity_delta effect in this action actually claimed.

    `committed_at` is this moment (`NOW()`) -- SUCCESS for the simulator
    adapter, ENACTED for manual/webhook -- so the verifier's lead-time window
    starts from when execution actually completed, not from approval.
    `lead_time_minutes` is read from the catalog now and snapshotted onto the
    row, so a later retune of the lever cannot rewrite an already-running
    verification's deadline (the same reasoning migration 0017 already
    applied to a transition's `work_units`).

    A posture with two capacity_delta levers would insert two rows sharing
    the same aggregate target (`action["capacity_per_hour"]` is already the
    posture's total, not a per-lever figure) -- imprecise attribution, but no
    current catalog posture has more than one capacity_delta lever, so this
    is not worth a per-lever breakdown yet.
    """
    effects = action["effects"] or []
    if not any(effect.get("kind") == "capacity_delta" for effect in effects):
        return

    catalog = load_catalog()
    for effect in effects:
        if effect.get("kind") != "capacity_delta":
            continue
        lever_id = effect.get("lever_id")
        lead_time_minutes = 0
        if lever_id is not None:
            try:
                lead_time_minutes = catalog.lever(lever_id).lead_time_minutes
            except KeyError:
                pass
        await conn.execute(
            text(
                """
                INSERT INTO capacity_commitments (
                    facility_id, action_id, lever_id, target_work_units_per_hour,
                    lead_time_minutes, committed_at
                ) VALUES (
                    :facility_id, :action_id, :lever_id, :target,
                    :lead_time_minutes, NOW()
                )
                ON CONFLICT (action_id, lever_id) DO NOTHING
                """
            ),
            {
                "facility_id": action["facility_id"],
                "action_id": action["action_id"],
                "lever_id": lever_id,
                "target": action["capacity_per_hour"],
                "lead_time_minutes": lead_time_minutes,
            },
        )
