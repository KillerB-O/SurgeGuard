"""Pure scheduling and facility-risk calculations.

The functions are database-independent and reused by live reads and simulations.
"""

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    Order,
    OrderStatus,
    PriorityBreakdown,
    ScheduledOrder,
    ScheduleResult,
    SLAStatus,
    SurgeRiskLevel,
)

# The scheduler consumes work units supplied by ingestion or simulation.

# Centralized SLA thresholds.
WATCH_THRESHOLD_HOURS = 12.0
AT_RISK_THRESHOLD_HOURS = 4.0

# Centralized facility-risk thresholds.
EXPOSURE_HIGH_RATIO = 0.15
EXPOSURE_CRITICAL_RATIO = 0.40
BREACH_CRITICAL_RATIO = 0.15

# Priority formula: urgency + aging bonus - workload penalty.
AGING_BONUS_WEIGHT_PER_HOUR = 0.1
WORKLOAD_PENALTY_WEIGHT = 0.1


class CutoffPolicy(BaseModel):
    """When the carrier actually collects.

    Finishing the work is not dispatching it. A warehouse that packs an order at
    18:20 against an 18:00 pickup has not dispatched it today -- it goes
    tomorrow. Modelling that is what makes extending a cutoff a real lever:
    moving the collection time rescues orders without touching the work.

    One collection per day, held in UTC. Multiple waves, weekends and holidays
    are deliberately out of scope.
    """

    model_config = ConfigDict(frozen=True)

    cutoff_utc: time

    def next_dispatch_after(self, work_complete_at: datetime) -> datetime:
        """Return the first collection at or after the work is finished."""
        today = datetime.combine(
            work_complete_at.date(), self.cutoff_utc, tzinfo=work_complete_at.tzinfo
        )
        return today if today >= work_complete_at else today + timedelta(days=1)

    def shifted_by(self, minutes: float) -> "CutoffPolicy":
        """Return the same policy with the collection held later.

        This is `EXTEND_CARRIER_CUTOFF`: the cheapest intervention available,
        because it buys dispatch time without buying capacity.
        """
        minute_of_day = self.cutoff_utc.hour * 60 + self.cutoff_utc.minute + minutes
        wrapped = int(minute_of_day) % (24 * 60)
        return CutoffPolicy(cutoff_utc=time(hour=wrapped // 60, minute=wrapped % 60))


class OperatingCalendar(BaseModel):
    """When the floor is staffed and producing.

    `compute_schedule` has always treated capacity as flowing continuously:
    `work_complete_at = now + cumulative_work / capacity_per_hour` assumes
    the floor keeps producing at the same rate through the night.
    `CutoffPolicy` only ever changed when a *finished* order gets picked up,
    never how fast work finishes. This type fixes the actual queue
    arithmetic, not just lever feasibility.

    A single daily open/close window (UTC), with wraparound support for an
    overnight shift (e.g. `opens_at=22:00, closes_at=06:00`). No
    day-of-week or holiday variation -- the same boundary `CutoffPolicy`
    already drew for collections, and `cutoff` is a *field* here rather
    than a second, independent argument, so a facility can never end up
    closed on Sunday with a Sunday collection: once a calendar is passed to
    `compute_schedule`, `calendar.cutoff` is the only cutoff that applies.

    `None` on `compute_schedule` (the default) means continuous, 24/7
    capacity -- how the scheduler behaved before this type existed, and how
    every existing facility, including the demo's WH-01, keeps behaving
    unless a calendar is explicitly configured. `0006_facility_cutoff.py`
    already argued why WH-01 itself must stay unconstrained even by a
    single daily cutoff: the demo's compressed-time arrivals quantise onto
    a handful of instants and the AT_RISK band empties out. A bounded
    operating window is a stronger version of the same problem, so demo
    facilities are never given one -- this is a decision to cite, not a
    new clock-rate-aware calendar to build.

    Decision this type also settles (documented here rather than left
    implicit, per this project's own convention -- see
    `repository.py`'s `committed_work_units` for the precedent this
    follows): work already in progress does **not** continue through a
    closure. Nobody is on the floor while it is closed, so committed work
    waits behind the closure exactly like the pending queue does --
    `add_working_hours` is applied to the combined committed-plus-pending
    total, not split into an exempt and a non-exempt portion. Lever lead
    times (`minutes_to_first_breach`, a posture's `posture_effective_at`)
    are a separate decision, deliberately left in wall-clock minutes, not
    working-minutes: a 90-minute `EXTEND_SHIFT` approved at 21:30 is still
    90 real-world minutes away, whether or not the floor is open for all of
    them. Wall-minutes is simpler and is the reading that does not require
    a second, calendar-aware notion of "minutes" alongside the one this
    file already uses everywhere else.
    """

    model_config = ConfigDict(frozen=True)

    opens_at: time
    closes_at: time
    cutoff: "CutoffPolicy | None" = None

    def _model_post_init_check(self) -> None:
        if self.opens_at == self.closes_at:
            raise ValueError(
                "opens_at and closes_at must differ -- an equal pair is a "
                "permanently-closed window, not a permissive one, and "
                "add_working_hours would loop forever trying to find open "
                "time that never comes"
            )

    def model_post_init(self, context: object, /) -> None:
        self._model_post_init_check()

    def _daily_open_duration(self) -> timedelta:
        """Minutes open per day, correctly handling an overnight wraparound."""
        open_minutes = self.opens_at.hour * 60 + self.opens_at.minute
        close_minutes = self.closes_at.hour * 60 + self.closes_at.minute
        return timedelta(minutes=(close_minutes - open_minutes) % (24 * 60))

    def _open_interval_covering_or_after(self, cursor: datetime) -> tuple[datetime, datetime]:
        """Return the open window containing `cursor`, or the next one after it.

        Anchored at `opens_at` each calendar day. Only yesterday's and
        today's anchors can possibly cover `cursor`, since the daily open
        duration is always < 24h (enforced by the non-degenerate check
        above), so checking those two is exhaustive.
        """
        duration = self._daily_open_duration()
        day = cursor.date()
        for offset in (-1, 0):
            start = datetime.combine(
                day + timedelta(days=offset), self.opens_at, tzinfo=cursor.tzinfo
            )
            end = start + duration
            if start <= cursor < end:
                return start, end

        today_start = datetime.combine(day, self.opens_at, tzinfo=cursor.tzinfo)
        if cursor < today_start:
            return today_start, today_start + duration
        tomorrow_start = today_start + timedelta(days=1)
        return tomorrow_start, tomorrow_start + duration

    def is_open_at(self, instant: datetime) -> bool:
        """Whether the floor is producing at this instant."""
        start, end = self._open_interval_covering_or_after(instant)
        return start <= instant < end

    def next_open_at(self, instant: datetime) -> datetime:
        """The next instant at or after `instant` when the floor is open."""
        start, _ = self._open_interval_covering_or_after(instant)
        return max(start, instant) if self.is_open_at(instant) else start

    def add_working_hours(self, start: datetime, hours: float) -> datetime:
        """Advance `start` by `hours` of open time, skipping closed periods.

        This is the fix P10 exists for: capacity only flows while the floor
        is open, so elapsed wall time must skip whatever is closed in
        between rather than dividing straight through the night.
        """
        if hours <= 0:
            return start

        remaining = timedelta(hours=hours)
        cursor = start
        while True:
            window_start, window_end = self._open_interval_covering_or_after(cursor)
            cursor = max(cursor, window_start)
            available = window_end - cursor
            if available >= remaining:
                return cursor + remaining
            remaining -= available
            cursor = window_end


class CapacityRamp(BaseModel):
    """Capacity a lever has promised but the floor does not have yet.

    A throughput lever (extend the shift) is approved now but its people arrive
    after its lead time. Until then the floor runs at the capacity it had, and
    after it at the new one. Granting the new capacity from approval understated
    a plan's residual breaches; charging the gap as a flat debt ahead of the
    queue overstated them and could make adding capacity look worse than doing
    nothing. The piecewise form is exact on both sides of the ramp.
    """

    model_config = ConfigDict(frozen=True)

    from_capacity_per_hour: float = Field(gt=0)
    # Under an operating calendar these are open-floor hours, like every other
    # duration the scheduler accumulates.
    hours: float = Field(ge=0)

    def hours_to_clear(self, work_units: float, capacity_per_hour: float) -> float:
        """Hours for the floor to finish `work_units` while ramping to `capacity_per_hour`."""
        if capacity_per_hour <= self.from_capacity_per_hour:
            return work_units / capacity_per_hour
        cleared_before_ramp = self.from_capacity_per_hour * self.hours
        if work_units <= cleared_before_ramp:
            return work_units / self.from_capacity_per_hour
        return self.hours + (work_units - cleared_before_ramp) / capacity_per_hour


class SlaThresholds(BaseModel):
    """How much slack separates SAFE / WATCH / AT_RISK / BREACHED.

    Was two frozen module constants applied identically to every facility,
    which is the `EventSource`-named-after-the-simulator problem (P1) in a
    different place: a real warehouse with its own promise window has no
    legal way to say so. The frozen values are these fields' defaults, not
    a second source of truth.
    """

    model_config = ConfigDict(frozen=True)

    watch_hours: float = WATCH_THRESHOLD_HOURS
    at_risk_hours: float = AT_RISK_THRESHOLD_HOURS


class FacilityRiskThresholds(BaseModel):
    """Exposure ratios separating LOW / MEDIUM / HIGH / CRITICAL.

    Was three frozen module constants, same problem as `SlaThresholds`
    above: a facility's risk appetite is not a property of the code, it is
    a property of the facility.
    """

    model_config = ConfigDict(frozen=True)

    exposure_high_ratio: float = EXPOSURE_HIGH_RATIO
    exposure_critical_ratio: float = EXPOSURE_CRITICAL_RATIO
    breach_critical_ratio: float = BREACH_CRITICAL_RATIO


def sla_thresholds(facility: dict) -> SlaThresholds:
    """Build a facility's SLA thresholds from its own configured columns.

    Lives beside `cutoff_policy` for the same reason: the scheduler owns the
    type, and `app/alerts/` needs the same policy the dashboard schedules
    against.

    Args:
        facility: Facility row, carrying migration 0021's threshold columns.

    Returns:
        This facility's own SLA thresholds.
    """
    return SlaThresholds(
        watch_hours=facility["watch_threshold_hours"],
        at_risk_hours=facility["at_risk_threshold_hours"],
    )


def risk_thresholds(facility: dict) -> FacilityRiskThresholds:
    """Build a facility's surge-risk thresholds from its own configured columns.

    Args:
        facility: Facility row, carrying migration 0021's threshold columns.

    Returns:
        This facility's own surge-risk thresholds.
    """
    return FacilityRiskThresholds(
        exposure_high_ratio=facility["exposure_high_ratio"],
        exposure_critical_ratio=facility["exposure_critical_ratio"],
        breach_critical_ratio=facility["breach_critical_ratio"],
    )


def compute_schedule(
    orders: list[Order],
    capacity_per_hour: float,
    now: datetime,
    committed_work_units: float = 0.0,
    priority_adjustments: Mapping[str, float] | None = None,
    dispatch_cutoff: "CutoffPolicy | None" = None,
    sla_thresholds: "SlaThresholds | None" = None,
    operating_calendar: "OperatingCalendar | None" = None,
    capacity_ramp: CapacityRamp | None = None,
) -> ScheduleResult:
    """Compute queue position, predicted dispatch, and SLA status.

    Only pending orders are reordered; active operational states remain
    untouched. Work already released to the floor is not reordered either, but
    it does compete for the same throughput, so it is charged against capacity
    ahead of the queue.

    Args:
        orders: Operational orders; only PENDING ones are scheduled.
        capacity_per_hour: Effective throughput in work units per hour.
        now: Evaluation time.
        committed_work_units: Fulfillment work already started and not finished.
            The floor must clear this before it reaches anything still pending,
            so omitting it makes every prediction optimistic.
        priority_adjustments: Per-order additions to the priority score, keyed by
            order id. This is how a queue lever pins or defers a segment without
            rewriting the frozen priority formula, and the adjustment stays
            visible in each order's breakdown.
        dispatch_cutoff: Daily carrier collection. Finishing the work is not
            dispatching it, so predicted dispatch snaps forward to the next
            pickup. `None` means continuous dispatch, which is how the scheduler
            behaved before cutoffs existed.
        sla_thresholds: This facility's own SLA thresholds. `None` falls back
            to the frozen defaults, which is how the scheduler behaved before
            per-facility thresholds existed.
        operating_calendar: This facility's daily open/close window. `None`
            means continuous, 24/7 capacity, which is how the scheduler
            behaved before calendars existed. When set, it also supplies the
            cutoff (`operating_calendar.cutoff`) -- the separate
            `dispatch_cutoff` argument is ignored in that case, so a
            facility can never end up closed on Sunday with a Sunday
            collection.
        capacity_ramp: When `capacity_per_hour` includes a lever that has not
            landed yet, the capacity the floor runs at until it does. `None`
            means `capacity_per_hour` applies from `now`.

    Raises:
        ValueError: If capacity or committed work is not a usable number.
    """
    if capacity_per_hour <= 0 or not isfinite(capacity_per_hour):
        raise ValueError("capacity_per_hour must be a finite number > 0")
    if committed_work_units < 0 or not isfinite(committed_work_units):
        raise ValueError("committed_work_units must be a finite number >= 0")

    thresholds = sla_thresholds or SlaThresholds()

    def hours_to_clear(work_units: float) -> float:
        if capacity_ramp is None:
            return work_units / capacity_per_hour
        return capacity_ramp.hours_to_clear(work_units, capacity_per_hour)

    adjustments = priority_adjustments or {}
    pending = [o for o in orders if o.status == OrderStatus.PENDING]
    effective_cutoff = (
        operating_calendar.cutoff if operating_calendar is not None else dispatch_cutoff
    )

    breakdowns = {
        o.order_id: _priority_breakdown(o, now, adjustments.get(o.order_id, 0.0))
        for o in pending
    }
    # order_id makes equal scores deterministic.
    sorted_pending = sorted(
        pending, key=lambda o: (-breakdowns[o.order_id].total, o.order_id)
    )

    scheduled: list[ScheduledOrder] = []
    # The queue starts behind whatever the floor is already holding.
    cumulative_work = committed_work_units
    pending_work = 0.0
    safe = watch = at_risk = breached = 0
    first_breach_at: datetime | None = None
    # Only used when a calendar is present: the elapsed-open-time pointer,
    # advanced by each order's marginal share of capacity rather than
    # recomputed from a wall-clock division. Committed work is charged here
    # too -- it waits behind a closure exactly like the pending queue does.
    calendar_cursor = (
        operating_calendar.add_working_hours(now, hours_to_clear(committed_work_units))
        if operating_calendar is not None
        else None
    )

    for position, order in enumerate(sorted_pending, start=1):
        work_units_ahead = cumulative_work
        cumulative_work += order.work_units
        pending_work += order.work_units
        if operating_calendar is not None:
            # The marginal share of open time, so a ramp stays exact.
            calendar_cursor = operating_calendar.add_working_hours(
                calendar_cursor,
                hours_to_clear(cumulative_work) - hours_to_clear(work_units_ahead),
            )
            work_complete_at = calendar_cursor
        else:
            hours_to_complete = hours_to_clear(cumulative_work)
            work_complete_at = now + timedelta(hours=hours_to_complete)
        # Packed is not dispatched: an order that misses today's collection
        # leaves on the next one.
        predicted_dispatch_at = (
            effective_cutoff.next_dispatch_after(work_complete_at)
            if effective_cutoff is not None
            else work_complete_at
        )

        sla_status = _classify_sla(predicted_dispatch_at, order.promised_dispatch_at, thresholds)
        match sla_status:
            case SLAStatus.SAFE:
                safe += 1
            case SLAStatus.WATCH:
                watch += 1
            case SLAStatus.AT_RISK:
                at_risk += 1
            case SLAStatus.BREACHED:
                breached += 1
                # The earliest promise to fail, which is when the operator's
                # window to act actually closes.
                if first_breach_at is None or order.promised_dispatch_at < first_breach_at:
                    first_breach_at = order.promised_dispatch_at

        scheduled.append(
            ScheduledOrder(
                order_id=order.order_id,
                priority_score=breakdowns[order.order_id].total,
                priority_breakdown=breakdowns[order.order_id],
                work_complete_at=work_complete_at,
                predicted_dispatch_at=predicted_dispatch_at,
                promised_dispatch_at=order.promised_dispatch_at,
                sla_status=sla_status,
                queue_position=position,
                work_units_ahead=work_units_ahead,
            )
        )

    return ScheduleResult(
        generated_at=now,
        capacity_per_hour=capacity_per_hour,
        scheduled_orders=tuple(scheduled),
        pending_orders=len(sorted_pending),
        # Reports the reorderable queue only; committed work is not pending.
        pending_work_units=pending_work,
        safe_count=safe,
        watch_count=watch,
        at_risk_count=at_risk,
        breached_count=breached,
        minutes_to_first_breach=(
            None
            if first_breach_at is None
            else (first_breach_at - now).total_seconds() / 60.0
        ),
    )



def cutoff_policy(facility: dict) -> CutoffPolicy | None:
    """Build the facility's collection policy, or None for continuous dispatch.

    Lives beside `CutoffPolicy` rather than in a router: the scheduler owns
    the type, and the evaluator in `app/alerts/` needs the same policy the
    dashboard schedules against. A worker importing a router to get it would
    have the dependency pointing the wrong way.

    Args:
        facility: Facility row, which may or may not carry a cutoff.

    Returns:
        The collection policy, or None when dispatch is continuous.
    """
    cutoff = facility.get("dispatch_cutoff_utc")
    return None if cutoff is None else CutoffPolicy(cutoff_utc=cutoff)


def operating_calendar(facility: dict) -> OperatingCalendar | None:
    """Build the facility's operating calendar, or None for 24/7 capacity.

    Lives beside `cutoff_policy` for the same reason. Reuses that same
    function for the calendar's `cutoff` field rather than reading
    `dispatch_cutoff_utc` a second time -- one column, one reader.

    Args:
        facility: Facility row, which may or may not carry migration 0022's
            operating-window columns.

    Returns:
        The operating calendar, or None when capacity is continuous.
    """
    opens_at = facility.get("operating_opens_at")
    closes_at = facility.get("operating_closes_at")
    if opens_at is None or closes_at is None:
        return None
    return OperatingCalendar(opens_at=opens_at, closes_at=closes_at, cutoff=cutoff_policy(facility))


def classify_facility_risk(
    schedule: ScheduleResult,
    thresholds: "FacilityRiskThresholds | None" = None,
    *,
    stalled: bool = False,
) -> SurgeRiskLevel:
    """Classify facility risk from exposed pending orders.

    Severity is graded on the proportion of the pending queue that is exposed,
    with two operational distinctions a bare ratio cannot make.

    A breached order is a promise already missed; an at-risk one can still be
    saved. They are not the same condition, so any breach puts the facility at
    HIGH regardless of proportion. That also keeps the signal from inverting:
    without it, a facility holding a fixed number of late orders is reported as
    calmer purely because more healthy work arrived behind them, which is
    exactly when an operator must not be told things improved.

    Args:
        schedule: Result returned by `compute_schedule`.
        thresholds: This facility's own exposure ratios. `None` falls back to
            the frozen defaults, which is how this behaved before per-facility
            thresholds existed.
        stalled: Work is waiting and the floor is not moving it. The schedule
            was computed at configured capacity only to stay computable, so
            its calm reading is not evidence; a stall is at least HIGH.
    """
    if schedule.pending_orders == 0:
        return SurgeRiskLevel.LOW

    level = _exposure_risk(schedule, thresholds or FacilityRiskThresholds())
    if stalled and level in (SurgeRiskLevel.LOW, SurgeRiskLevel.MEDIUM):
        return SurgeRiskLevel.HIGH
    return level


def _exposure_risk(schedule: ScheduleResult, ratios: "FacilityRiskThresholds") -> SurgeRiskLevel:
    """Grade risk on the exposed share of a non-empty pending queue."""

    exposed = schedule.at_risk_count + schedule.breached_count
    if exposed == 0:
        return SurgeRiskLevel.LOW

    exposure_ratio = exposed / schedule.pending_orders
    breach_ratio = schedule.breached_count / schedule.pending_orders

    if (
        exposure_ratio >= ratios.exposure_critical_ratio
        or breach_ratio >= ratios.breach_critical_ratio
    ):
        return SurgeRiskLevel.CRITICAL
    if exposure_ratio >= ratios.exposure_high_ratio or schedule.breached_count > 0:
        return SurgeRiskLevel.HIGH
    return SurgeRiskLevel.MEDIUM


def _priority_breakdown(
    order: Order, now: datetime, adjustment: float = 0.0
) -> PriorityBreakdown:
    """Score an order using SLA urgency, waiting age, and workload.

    Higher scores make an order earlier in the deterministic queue. The terms are
    returned separately rather than summed so a status is explainable term by
    term.

    Note that under a single facility-wide promise policy, urgency and the aging
    bonus are both linear in the order's age and rank orders identically. The
    aging term becomes an independent anti-starvation signal only once promise
    windows differ between orders.
    """
    hours_until_due = (order.promised_dispatch_at - now).total_seconds() / 3600.0
    age_hours = (now - order.created_at).total_seconds() / 3600.0

    return PriorityBreakdown(
        sla_urgency=-hours_until_due,
        aging_bonus=AGING_BONUS_WEIGHT_PER_HOUR * age_hours,
        workload_penalty=WORKLOAD_PENALTY_WEIGHT * order.work_units,
        adjustment=adjustment,
    )


def _classify_sla(
    predicted_dispatch_at: datetime,
    promised_dispatch_at: datetime,
    thresholds: "SlaThresholds | None" = None,
) -> SLAStatus:
    """Classify an order from the hours of slack before its promise."""
    limits = thresholds or SlaThresholds()
    slack_hours = (promised_dispatch_at - predicted_dispatch_at).total_seconds() / 3600.0
    if slack_hours < 0:
        return SLAStatus.BREACHED
    if slack_hours <= limits.at_risk_hours:
        return SLAStatus.AT_RISK
    if slack_hours <= limits.watch_hours:
        return SLAStatus.WATCH
    return SLAStatus.SAFE
