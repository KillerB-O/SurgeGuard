"""Compose recovery plans from the lever catalog and score what they trade.

Every projection here comes from re-running the same `compute_schedule` on
modified inputs, never from an estimate. That is what lets a plan's numbers be
trusted: they are produced by the engine that will actually run.

The scoring exists to stop a plan overclaiming. A queue lever cannot reduce the
number of missed promises -- it decides which orders miss -- so a plan built only
from queue levers must report itself as redistribution and name the orders whose
promises were traded away. Nothing in the UI is allowed to say such a plan
"saved" anything.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app.levers import (
    Catalog,
    CostBand,
    Lever,
    LeverFamily,
    Posture,
    Preconditions,
)
from app.models import Order, OrderStatus, ScheduleResult, SLAStatus
from app.scheduler import (
    CapacityRamp,
    CutoffPolicy,
    OperatingCalendar,
    SlaThresholds,
    compute_schedule,
)

# Statuses that mean a promise is in trouble. BREACHED_MANAGED belongs here: an
# apology and a credit do not make an order arrive sooner, and letting a managed
# breach count as prevented would be exactly the overclaiming this scoring
# exists to stop.
EXPOSED = (SLAStatus.AT_RISK, SLAStatus.BREACHED, SLAStatus.BREACHED_MANAGED)

# Ranked worst-first so an order's fate can be compared before and after.
SEVERITY = {
    SLAStatus.SAFE: 0,
    SLAStatus.WATCH: 1,
    SLAStatus.AT_RISK: 2,
    SLAStatus.BREACHED: 3,
    SLAStatus.BREACHED_MANAGED: 3,
}

COST_RANK = {CostBand.NONE: 0, CostBand.LOW: 1, CostBand.MEDIUM: 2, CostBand.HIGH: 3}


@dataclass(frozen=True)
class ScheduleInputs:
    """Everything a lever is allowed to modify before the engine runs."""

    orders: list[Order]
    capacity_per_hour: float
    committed_work_units: float
    dispatch_cutoff: CutoffPolicy | None
    priority_adjustments: dict[str, float] = field(default_factory=dict)
    # Applied to projected arrivals rather than to the queue, because inflow
    # levers cannot touch an order that has already been placed.
    arrival_multiplier: float = 1.0
    sla_thresholds: SlaThresholds | None = None
    operating_calendar: OperatingCalendar | None = None
    # Set by a throughput lever: capacity_per_hour is not reached until its
    # lead time has passed.
    capacity_ramp: CapacityRamp | None = None

    def run(self, now: datetime) -> ScheduleResult:
        """Run the one scheduler over these inputs."""
        return compute_schedule(
            self.orders,
            self.capacity_per_hour,
            now,
            self.committed_work_units,
            priority_adjustments=self.priority_adjustments,
            dispatch_cutoff=self.dispatch_cutoff,
            sla_thresholds=self.sla_thresholds,
            operating_calendar=self.operating_calendar,
            capacity_ramp=self.capacity_ramp,
        )


def apply_lever(lever: Lever, inputs: ScheduleInputs, now: datetime) -> ScheduleInputs:
    """Return new scheduler inputs with one lever applied.

    Args:
        lever: The intervention to apply.
        inputs: Inputs as they stand before this lever.
        now: Evaluation time, needed to measure slack.

    Returns:
        Modified inputs; the originals are left untouched.
    """
    effect = lever.effect

    match effect.kind:
        case "capacity_delta":
            capacity = max(inputs.capacity_per_hour + effect.work_units_per_hour, 0.1)
            ramp = inputs.capacity_ramp
            if capacity > inputs.capacity_per_hour and lever.lead_time_minutes > 0:
                # Two throughput levers are treated as landing together at the
                # slower one's lead time: never earlier than either really does.
                ramp = CapacityRamp(
                    from_capacity_per_hour=(
                        ramp.from_capacity_per_hour if ramp else inputs.capacity_per_hour
                    ),
                    hours=max(ramp.hours if ramp else 0.0, lever.lead_time_minutes / 60.0),
                )
            return replace(inputs, capacity_per_hour=capacity, capacity_ramp=ramp)

        case "work_unit_multiplier":
            return replace(
                inputs,
                orders=[
                    o.model_copy(update={"work_units": o.work_units * effect.factor})
                    if o.status == OrderStatus.PENDING
                    else o
                    for o in inputs.orders
                ],
            )

        case "promise_shift":
            return replace(
                inputs,
                orders=[
                    o.model_copy(
                        update={
                            "promised_dispatch_at": o.promised_dispatch_at
                            + timedelta(hours=effect.hours)
                        }
                    )
                    if _targets(o, effect.segment)
                    else o
                    for o in inputs.orders
                ],
            )

        case "priority_adjustment":
            adjustments = dict(inputs.priority_adjustments)
            for order in inputs.orders:
                if order.status != OrderStatus.PENDING:
                    continue
                if not _targets(order, effect.segment):
                    continue
                if effect.min_slack_hours is not None:
                    slack = (order.promised_dispatch_at - now).total_seconds() / 3600.0
                    if slack < effect.min_slack_hours:
                        continue
                adjustments[order.order_id] = (
                    adjustments.get(order.order_id, 0.0) + effect.points
                )
            return replace(inputs, priority_adjustments=adjustments)

        case "inflow_reduction":
            return replace(inputs, arrival_multiplier=inputs.arrival_multiplier * effect.factor)

        case "cutoff_shift":
            # A calendar's cutoff is the only one compute_schedule actually
            # applies once a calendar is present (see OperatingCalendar's own
            # docstring), so the projection must shift that one, not the
            # separate dispatch_cutoff field, or the projected "rescued"
            # orders would not match what running the plan for real produces.
            if inputs.operating_calendar is not None:
                if inputs.operating_calendar.cutoff is None:
                    return inputs
                return replace(
                    inputs,
                    operating_calendar=inputs.operating_calendar.model_copy(
                        update={
                            "cutoff": inputs.operating_calendar.cutoff.shifted_by(
                                effect.minutes
                            )
                        }
                    ),
                )
            if inputs.dispatch_cutoff is None:
                return inputs
            return replace(
                inputs, dispatch_cutoff=inputs.dispatch_cutoff.shifted_by(effect.minutes)
            )

        case "manage_breach":
            # Applied after scheduling, not before: it changes how a breach is
            # reported, never whether it happens.
            return inputs

    return inputs


def _targets(order: Order, segment: str | None) -> bool:
    """Return whether a lever aimed at a segment applies to this order."""
    return segment is None or order.segment == segment


def is_available(
    lever: Lever,
    orders: list[Order],
    has_cutoff: bool,
    has_projection: bool,
) -> bool:
    """Return whether a lever's preconditions hold at all."""
    pre: Preconditions = lever.preconditions
    if pre.requires_cutoff and not has_cutoff:
        return False
    if pre.requires_projection and not has_projection:
        return False
    return pre.requires_segment is None or any(
        o.segment == pre.requires_segment for o in orders
    )


@dataclass(frozen=True)
class OrderFate:
    """How one order's outlook changed under a plan."""

    order_id: str
    before: SLAStatus
    after: SLAStatus


@dataclass(frozen=True)
class PlanOutcome:
    """What a plan actually did, split into prevention and redistribution."""

    breaches_avoided: int
    breaches_relocated: int
    net_breach_change: int
    orders_improved: tuple[OrderFate, ...]
    orders_worsened: tuple[OrderFate, ...]
    is_redistribution: bool


def score_outcome(
    baseline: ScheduleResult,
    projected: ScheduleResult,
    exclude_ids: frozenset[str] = frozenset(),
) -> PlanOutcome:
    """Compare two scheduler runs order by order.

    Args:
        baseline: The schedule with no intervention.
        projected: The schedule under the plan.
        exclude_ids: Orders to leave out, e.g. projected arrivals nobody placed.

    Returns:
        Counts of exposure prevented versus merely moved, and the orders behind
        each. A plan that prevents nothing while moving exposure around is
        marked as redistribution.
    """
    before = {
        o.order_id: o.sla_status
        for o in baseline.scheduled_orders
        if o.order_id not in exclude_ids
    }
    after = {
        o.order_id: o.sla_status
        for o in projected.scheduled_orders
        if o.order_id not in exclude_ids
    }

    avoided = relocated = 0
    improved: list[OrderFate] = []
    worsened: list[OrderFate] = []

    for order_id, was in before.items():
        now_status = after.get(order_id)
        if now_status is None:
            continue

        if SEVERITY[now_status] < SEVERITY[was]:
            improved.append(OrderFate(order_id, was, now_status))
        elif SEVERITY[now_status] > SEVERITY[was]:
            worsened.append(OrderFate(order_id, was, now_status))

        was_exposed = was in EXPOSED
        is_exposed = now_status in EXPOSED
        if was_exposed and not is_exposed:
            avoided += 1
        elif is_exposed and not was_exposed:
            relocated += 1

    net = avoided - relocated
    return PlanOutcome(
        breaches_avoided=avoided,
        breaches_relocated=relocated,
        net_breach_change=net,
        orders_improved=tuple(improved),
        orders_worsened=tuple(worsened),
        # Moved exposure around without reducing it. Real for any queue-only
        # plan, and the operator must be told rather than shown a win.
        is_redistribution=net <= 0 and relocated > 0,
    )


def manage_breaches(schedule: ScheduleResult, segment_ids: frozenset[str]) -> ScheduleResult:
    """Relabel unavoidable breaches as acknowledged ones.

    This is deliberately a reporting change applied after scheduling. Nothing
    about the work moves: the order is still late, the customer just hears it
    from us first.
    """
    relabelled = tuple(
        order.model_copy(update={"sla_status": SLAStatus.BREACHED_MANAGED})
        if order.sla_status == SLAStatus.BREACHED and order.order_id in segment_ids
        else order
        for order in schedule.scheduled_orders
    )
    managed = sum(1 for o in relabelled if o.sla_status == SLAStatus.BREACHED_MANAGED)
    return schedule.model_copy(
        update={
            "scheduled_orders": relabelled,
            "breached_count": schedule.breached_count - managed,
            "breached_managed_count": managed,
        }
    )


def feasible_levers(
    catalog: Catalog,
    orders: list[Order],
    has_cutoff: bool,
    has_projection: bool,
    minutes_to_cutoff: float | None,
    floor_reopens_at: datetime | None = None,
) -> tuple[list[Lever], list[tuple[Lever, str]]]:
    """Split the catalog into what can help now and what cannot.

    Levers that arrive after the collection are returned as unavailable rather
    than hidden, so an operator learns where their decision window closes
    instead of silently being offered fewer options.

    Args:
        catalog: The lever catalog to filter.
        orders: Current orders, used by `is_available`'s own preconditions.
        has_cutoff: Whether this facility has a collection policy at all.
        has_projection: Whether a projection horizon was requested.
        minutes_to_cutoff: Minutes until the next collection, or `None` for
            continuous dispatch.
        floor_reopens_at: When the floor next opens, or `None` when it is
            currently open (or has no operating calendar at all). Only
            `capacity_delta` levers are blocked by this -- a closed floor has
            no shift to extend, but deciding to reprioritize the queue or
            re-promise an order needs nobody physically present, so those
            stay available. This is a distinct reason from "too late to
            help": a floor can be closed with plenty of minutes left before
            the next collection, and reporting the wrong one would send an
            operator looking for a decision window that was never the
            actual blocker.

    Returns:
        Applicable levers, and unavailable ones paired with the reason.
    """
    usable: list[Lever] = []
    blocked: list[tuple[Lever, str]] = []

    for lever in catalog.levers:
        if not is_available(lever, orders, has_cutoff, has_projection):
            blocked.append((lever, "preconditions not met"))
        elif floor_reopens_at is not None and lever.effect.kind == "capacity_delta":
            blocked.append((lever, f"floor closed until {floor_reopens_at:%H:%M}"))
        elif minutes_to_cutoff is not None and lever.lead_time_minutes > minutes_to_cutoff:
            blocked.append((lever, "too late to help"))
        else:
            usable.append(lever)

    return usable, blocked


def posture_cost(catalog: Catalog, posture: Posture) -> CostBand:
    """Return the highest cost band any of a posture's levers carries."""
    bands = [catalog.lever(lever_id).cost for lever_id in posture.lever_ids]
    if not bands:
        return CostBand.NONE
    return max(bands, key=lambda band: COST_RANK[band])


def posture_effective_at(catalog: Catalog, posture: Posture, now: datetime) -> datetime:
    """Return when a posture is fully in effect, i.e. its slowest lever."""
    leads = [catalog.lever(lever_id).lead_time_minutes for lever_id in posture.lever_ids]
    return now + timedelta(minutes=max(leads, default=0))


def posture_families(catalog: Catalog, posture: Posture) -> tuple[LeverFamily, ...]:
    """Return the distinct families a posture draws on."""
    seen: dict[LeverFamily, None] = {}
    for lever_id in posture.lever_ids:
        seen[catalog.lever(lever_id).family] = None
    return tuple(seen)


def build_inputs(
    base: ScheduleInputs,
    posture: Posture,
    catalog: Catalog,
    now: datetime,
    available: Mapping[str, Lever],
) -> ScheduleInputs | None:
    """Apply every lever in a posture, or return None if any is unavailable."""
    inputs = base
    for lever_id in posture.lever_ids:
        lever = available.get(lever_id)
        if lever is None:
            return None
        inputs = apply_lever(lever, inputs, now)
    return inputs
