"""Read-only SQL queries for facility and operational data.

Derived scheduler values are intentionally calculated by the scheduler, not stored here.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models import Order, OrderStatus, lifecycle_position
from app.scheduler import OperatingCalendar
from app.work_units import load_rules, remaining_work_fraction

# Freshness window for observed capacity.
SNAPSHOT_FRESHNESS_WINDOW = timedelta(hours=2)

# Producer clocks drift; anything further ahead than this is not "latest".
SNAPSHOT_CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

# Lookback window for the demand-rate estimate.
DEMAND_WINDOW = timedelta(hours=1)

# Fulfillment work already released to the floor. It is not reorderable, but it
# draws on the same throughput, so the pending queue waits behind it. READY is
# excluded: its fulfillment work is finished and it is only awaiting dispatch.
COMMITTED_STATUSES = (
    OrderStatus.PICKING,
    OrderStatus.PACKED,
)

# Every status the scheduler does not reorder, so they are listed rather than
# queued.
NON_PENDING_STATUSES = tuple(
    status for status in OrderStatus if status != OrderStatus.PENDING
)

# Canonical unfinished states used for backlog metrics.
BACKLOG_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.PICKING,
    OrderStatus.PACKED,
    OrderStatus.READY,
)


class FacilityNotFoundError(Exception):
    pass


async def get_facility(conn: AsyncConnection, facility_id: str) -> dict:
    """Return one facility configuration.

    Raises `FacilityNotFoundError` when the requested facility is unknown.
    """
    result = await conn.execute(
        text(
            "SELECT facility_id, name, capacity_per_hour, dispatch_promise_hours, "
            "dispatch_cutoff_utc, execution_adapter, watch_threshold_hours, "
            "at_risk_threshold_hours, exposure_high_ratio, exposure_critical_ratio, "
            "breach_critical_ratio, operating_opens_at, operating_closes_at "
            "FROM facilities WHERE facility_id = :facility_id"
        ),
        {"facility_id": facility_id},
    )
    row = result.mappings().first()
    if row is None:
        raise FacilityNotFoundError(facility_id)
    return dict(row)


# Window and floor for deriving throughput from transition history. A window
# shorter than an hour makes the rate noisy at low volume; the floor exists
# because one or two transitions in an hour is not a measurement, it is
# whatever happened to occur, and reporting a rate extrapolated from it would
# be less honest than admitting nothing has been measured yet.
DERIVED_THROUGHPUT_WINDOW = timedelta(hours=1)
MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT = 3
# How far back a floor counts as having been busy. Activity inside this, but
# not in the last DERIVED_THROUGHPUT_WINDOW, with work still waiting, reads as
# a floor that stopped rather than one that never reported.
STALL_LOOKBACK = timedelta(hours=4)


@dataclass(frozen=True)
class ThroughputSignal:
    """Observed fulfillment throughput and the capacity to schedule against.

    The two differ when telemetry is fresh but reports no completed work. The
    observed value is what the facility is actually achieving and belongs on the
    dashboard; the scheduling value keeps `compute_schedule`'s positive-capacity
    precondition satisfied so the queue still produces predictions.
    """

    observed_work_units_per_hour: float | None
    scheduling_capacity_per_hour: float
    # Where observed_work_units_per_hour came from, so a caller can tell a
    # measurement from a fallback rather than treating every number the same:
    #   "derived"    -- computed from this facility's own status transitions.
    #   "reported"   -- a producer-supplied fulfillment_snapshots figure.
    #   "configured" -- neither exists; observed_work_units_per_hour is None
    #                   and scheduling_capacity_per_hour is the facility's
    #                   configured rating.
    source: str
    # Work is waiting and the open floor is not moving it: telemetry measures
    # zero, or a floor that was busy has gone quiet. Scheduling still uses the
    # configured capacity so the queue stays computable, which is exactly why
    # this has to travel alongside it rather than be inferred from it.
    stalled: bool = False

    @property
    def reported_work_units_per_hour(self) -> float:
        """Return throughput for display, falling back to configured capacity.

        A stalled floor reports zero: showing the configured rating there is
        the one case where the fallback actively misleads.
        """
        if self.stalled:
            return self.observed_work_units_per_hour or 0.0
        if self.observed_work_units_per_hour is None:
            return self.scheduling_capacity_per_hour
        return self.observed_work_units_per_hour


async def _derive_throughput_from_transitions(
    conn: AsyncConnection, facility_id: str, now: datetime
) -> float | None:
    """Return work units per hour computed from this facility's own history.

    None below the transition floor, which is a different claim from "the
    floor did zero work": it means there is not yet enough history to say
    anything, and the caller should fall back rather than report a rate
    extrapolated from noise.

    `origin = 'event'` excludes backfilled history (P7): a facility's entire
    past dumped in on day one must not report a fictitious throughput spike
    at t=0.
    """
    result = await conn.execute(
        text(
            """
            SELECT count(*) AS transition_count, coalesce(sum(work_units), 0) AS total_work_units
            FROM order_status_transitions
            WHERE facility_id = :facility_id
              AND origin = 'event'
              AND occurred_at >= :since
              AND occurred_at <= :now
            """
        ),
        {
            "facility_id": facility_id,
            "since": now - DERIVED_THROUGHPUT_WINDOW,
            "now": now,
        },
    )
    row = result.first()
    if row.transition_count < MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT:
        return None
    return float(row.total_work_units) / (DERIVED_THROUGHPUT_WINDOW.total_seconds() / 3600.0)


async def get_throughput_signal(
    conn: AsyncConnection,
    facility_id: str,
    facility_capacity_per_hour: float,
    now: datetime,
    calendar: OperatingCalendar | None = None,
) -> ThroughputSignal:
    """Return the best available throughput alongside a usable scheduling capacity.

    Precedence is derived > reported > configured. A producer-supplied
    fulfillment_snapshots figure was the only measurement this function ever
    had; deriving from transition history replaces it wherever there is
    enough history to derive from, because no real WMS emits a
    "work units completed last hour" figure the way the simulator does --
    only status confirmations, which the transition table already captures.

    Stale telemetry must not override the configured fallback, and a snapshot
    timestamped in the future must not win the ordering and pin capacity
    permanently -- so the freshness window is bounded on both sides.

    A stall is flagged rather than hidden (see `ThroughputSignal.stalled`).
    `calendar` keeps a floor that is simply closed from reading as stalled;
    `None` means the floor is always open.
    """
    signal = await _measure_throughput(conn, facility_id, facility_capacity_per_hour, now)
    if calendar is not None and not calendar.is_open_at(now):
        return signal

    if signal.observed_work_units_per_hour == 0.0:
        suspect = True
    elif signal.source == "configured":
        suspect = await _was_busy_before_the_window(conn, facility_id, now)
    else:
        suspect = False
    if suspect and await _has_pending_orders(conn, facility_id):
        return replace(signal, stalled=True)
    return signal


async def _was_busy_before_the_window(
    conn: AsyncConnection, facility_id: str, now: datetime
) -> bool:
    """Whether the floor produced a measurable rate recently, before going quiet."""
    result = await conn.execute(
        text(
            """
            SELECT count(*) FROM order_status_transitions
            WHERE facility_id = :facility_id
              AND origin = 'event'
              AND occurred_at >= :since
              AND occurred_at < :quiet_since
            """
        ),
        {
            "facility_id": facility_id,
            "since": now - STALL_LOOKBACK,
            "quiet_since": now - DERIVED_THROUGHPUT_WINDOW,
        },
    )
    return result.scalar_one() >= MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT


async def _has_pending_orders(conn: AsyncConnection, facility_id: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM orders "
            "WHERE facility_id = :facility_id AND status = 'PENDING')"
        ),
        {"facility_id": facility_id},
    )
    return bool(result.scalar_one())


async def _measure_throughput(
    conn: AsyncConnection, facility_id: str, facility_capacity_per_hour: float, now: datetime
) -> ThroughputSignal:
    """Pick the best available throughput: derived, then reported, then configured."""
    derived = await _derive_throughput_from_transitions(conn, facility_id, now)
    if derived is not None:
        # Zero capacity has no finite predicted dispatch, so a measured zero
        # is scheduled at the configured capacity and flagged as a stall.
        scheduling = derived if derived > 0 else facility_capacity_per_hour
        return ThroughputSignal(derived, scheduling, source="derived")

    result = await conn.execute(
        text(
            """
            SELECT work_units_completed_last_hour
            FROM fulfillment_snapshots
            WHERE facility_id = :facility_id
              AND occurred_at >= :since
              AND occurred_at <= :until
            ORDER BY occurred_at DESC
            LIMIT 1
            """
        ),
        {
            "facility_id": facility_id,
            "since": now - SNAPSHOT_FRESHNESS_WINDOW,
            "until": now + SNAPSHOT_CLOCK_SKEW_TOLERANCE,
        },
    )
    row = result.first()
    if row is None:
        return ThroughputSignal(None, facility_capacity_per_hour, source="configured")

    observed = float(row[0])
    scheduling = observed if observed > 0 else facility_capacity_per_hour
    return ThroughputSignal(observed, scheduling, source="reported")


# Interpolated into the queries below purely to avoid repeating the column list.
# It is a module constant with no caller input; every value is still bound.
ORDER_COLUMNS = """
    order_id, facility_id, created_at, promised_dispatch_at,
    item_count, work_units, order_value, status, segment,
    original_promised_dispatch_at, dispatched_at
"""


def _to_order(row) -> Order:
    """Build a canonical order from one database row."""
    return Order(
        order_id=row.order_id,
        facility_id=row.facility_id,
        created_at=row.created_at,
        promised_dispatch_at=row.promised_dispatch_at,
        item_count=row.item_count,
        work_units=row.work_units,
        order_value=row.order_value,
        status=OrderStatus(row.status),
        segment=row.segment,
        original_promised_dispatch_at=row.original_promised_dispatch_at,
        dispatched_at=row.dispatched_at,
    )


async def list_pending_orders(conn: AsyncConnection, facility_id: str) -> list[Order]:
    """Return the orders the scheduler is allowed to reorder.

    Only PENDING orders are reorderable, so no read needs to
    materialise the facility's finished orders just to run the queue.
    """
    result = await conn.execute(
        text(
            f"""
            SELECT {ORDER_COLUMNS}
            FROM orders
            WHERE facility_id = :facility_id AND status = 'PENDING'
            ORDER BY created_at ASC
            """
        ),
        {"facility_id": facility_id},
    )
    return [_to_order(row) for row in result]


async def list_non_pending_orders(
    conn: AsyncConnection, facility_id: str, limit: int, offset: int,
    statuses: Sequence[OrderStatus] | None = None,
) -> list[Order]:
    """Return one page of orders that carry no scheduler fields.

    These sort after the scheduled queue, so they can be paged in SQL instead of
    being loaded whole and sliced in memory.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility whose orders should be returned.
        limit: Maximum rows to return.
        offset: Rows to skip.
        statuses: Restrict to these statuses. PENDING is never included, since
            those orders are scheduled rather than listed here.
    """
    if limit <= 0:
        return []

    wanted = [s.value for s in (statuses or NON_PENDING_STATUSES) if s != OrderStatus.PENDING]
    if not wanted:
        return []

    result = await conn.execute(
        text(
            f"""
            SELECT {ORDER_COLUMNS}
            FROM orders
            WHERE facility_id = :facility_id
              AND status <> 'PENDING'
              AND status = ANY(:statuses)
            ORDER BY created_at ASC, order_id ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        {
            "facility_id": facility_id,
            "statuses": wanted,
            "limit": limit,
            "offset": max(offset, 0),
        },
    )
    return [_to_order(row) for row in result]


async def get_order(conn: AsyncConnection, facility_id: str, order_id: str) -> Order | None:
    """Return one order, or `None` when the facility does not have it.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility the order must belong to.
        order_id: Order identity to look up.

    Returns:
        The persisted order, or `None`.
    """
    result = await conn.execute(
        text(
            f"""
            SELECT {ORDER_COLUMNS}
            FROM orders
            WHERE facility_id = :facility_id AND order_id = :order_id
            """
        ),
        {"facility_id": facility_id, "order_id": order_id},
    )
    row = result.first()
    return _to_order(row) if row is not None else None


@dataclass(frozen=True)
class OrderTotals:
    """Per-status order counts and work units for one facility."""

    counts: dict[OrderStatus, int]
    work_units: dict[OrderStatus, float]

    @property
    def total_orders(self) -> int:
        """Return every persisted order for the facility."""
        return sum(self.counts.values())

    @property
    def backlog_orders(self) -> int:
        """Return orders in an unfinished operational state."""
        return sum(self.counts.get(status, 0) for status in BACKLOG_STATUSES)

    @property
    def backlog_work_units(self) -> float:
        """Return outstanding work across unfinished operational states.

        Unlike `committed_work_units`, this is not fraction-adjusted: it
        answers "how much work do these orders represent", for recovery-hours
        and similar totals, not "how much is still left for the floor to do".
        The two intentionally disagree for a PICKING/PACKED order -- that is
        not a bug to reconcile, it is two different questions.
        """
        return sum(self.work_units.get(status, 0.0) for status in BACKLOG_STATUSES)

    @property
    def committed_work_units(self) -> float:
        """Return work already started that the pending queue must wait behind.

        This used to count started work in full, on the reasoning that biasing
        predicted dispatch pessimistic is the safe direction for an SLA
        warning. That is true of a bounded error, but full-charging is not
        bounded: a PACKED order has already had its PENDING->PICKING and
        PICKING->PACKED steps done, and charging it as if none of that
        happened overstates its remaining floor work by a fixed, avoidable
        amount every single time a PACKED order exists. "Pessimistic" implied
        the error shrinks as the model improves; this one does not shrink,
        because it was never measuring the actual remaining work in the first
        place. The concrete cost is on `breaches_avoided`: a capacity lever is
        priced against how much sooner the queue clears, and a queue that
        looks permanently more backed up than it is deflates that number
        every time, in a direction the operator can never see.

        Charging only the remaining fraction is possible because of P4:
        `order_status_transitions` records what each lifecycle step actually
        costs, from the same catalog weights (`work_units.json`
        `stage_weights`) this looks up here by status, so the two stay
        consistent without a per-order join -- committed status alone
        determines how much of an order's work is left, since
        `remaining_work_fraction` depends only on lifecycle position, not on
        how an order arrived there (a forward-jumped order and a step-by-step
        one at the same status have the same work remaining).
        """
        rules = load_rules()
        return sum(
            self.work_units.get(status, 0.0)
            * remaining_work_fraction(lifecycle_position(status), rules)
            for status in COMMITTED_STATUSES
        )


async def count_late_dispatches(
    conn: AsyncConnection, facility_id: str, now: datetime, window_days: int = 10
) -> tuple[int, int]:
    """Count orders that missed their promise, and everything placed, in a window.

    Counted in the database rather than by loading every order, because this
    spans the whole run including dispatched history, which the scheduler never
    materialises.

    A miss is either an order that left after its promise, or one still sitting
    unfinished with its promise already behind it. Both are facts: no recovery
    reaches back and makes them on time. CANCELLED is excluded from that
    second case (P8): a cancelled order was never going to ship, so it is not
    a missed promise, it is a moot one. It still counts toward `placed` --
    the plan's own wording is "keep it out of the late-dispatch numerator",
    not "exclude entirely" -- a cancelled order genuinely was placed in this
    window; only whether it counts as a *miss* is what changed.

    Both the numerator and the denominator are bounded to the same trailing
    window, matching how Amazon itself computes Late Shipment Rate. An
    all-time denominator would let the rate drift toward insensitivity as a
    facility accumulates history: a catastrophic week would barely move a
    number averaged over years, exactly when an operator most needs it to
    react.

    Args:
        conn: Request-scoped database transaction.
        facility_id: Facility being measured.
        now: Evaluation instant, from the shared clock.
        window_days: Trailing window in days, from LateDispatchPolicy.

    Returns:
        Misses and total orders placed within the window.
    """
    result = await conn.execute(
        text(
            """
            SELECT
                count(*) FILTER (
                    WHERE (dispatched_at IS NOT NULL
                           AND dispatched_at > promised_dispatch_at)
                       OR (status NOT IN ('DISPATCHED', 'CANCELLED')
                           AND promised_dispatch_at < :now)
                ) AS misses,
                count(*) AS placed
            FROM orders
            WHERE facility_id = :facility_id
              AND created_at >= :window_start
            """
        ),
        {
            "facility_id": facility_id,
            "now": now,
            "window_start": now - timedelta(days=window_days),
        },
    )
    row = result.first()
    return (row.misses or 0, row.placed or 0)


async def get_order_totals(conn: AsyncConnection, facility_id: str) -> OrderTotals:
    """Aggregate order counts and work units by status in the database.

    The dashboard needs totals, not rows, so this keeps the most frequently
    polled endpoint off a full table read.
    """
    result = await conn.execute(
        text(
            """
            SELECT status, COUNT(*) AS order_count, COALESCE(SUM(work_units), 0) AS work_units
            FROM orders
            WHERE facility_id = :facility_id
            GROUP BY status
            """
        ),
        {"facility_id": facility_id},
    )
    counts: dict[OrderStatus, int] = {}
    work_units: dict[OrderStatus, float] = {}
    for row in result:
        status = OrderStatus(row.status)
        counts[status] = int(row.order_count)
        work_units[status] = float(row.work_units)
    return OrderTotals(counts=counts, work_units=work_units)


async def get_demand_work_units_per_hour(
    conn: AsyncConnection, facility_id: str, now: datetime
) -> float:
    """Calculate recent accepted workload in work-units per hour.

    This is a demand signal, not a replacement for canonical backlog state.
    """
    result = await conn.execute(
        text(
            """
            SELECT COALESCE(SUM(work_units), 0)
            FROM orders
            WHERE facility_id = :facility_id AND created_at >= :since
            """
        ),
        {"facility_id": facility_id, "since": now - DEMAND_WINDOW},
    )
    total_work_units = float(result.scalar_one())
    return total_work_units / (DEMAND_WINDOW.total_seconds() / 3600.0)


async def list_facility_ids(conn: AsyncConnection) -> list[str]:
    """Return every configured facility id.

    The alert worker evaluates all of them rather than a configured list, so
    adding a facility does not also require remembering to add it to the
    worker's configuration.

    Args:
        conn: Active database connection.

    Returns:
        Facility ids, in stable order.
    """
    result = await conn.execute(text("SELECT facility_id FROM facilities ORDER BY facility_id"))
    return [row[0] for row in result]


async def get_latest_snapshot_at(
    conn: AsyncConnection, facility_id: str, now: datetime
) -> datetime | None:
    """Return when this facility last reported, or None if it never has.

    Deliberately NOT filtered by `SNAPSHOT_FRESHNESS_WINDOW`, unlike
    `get_throughput_signal`: that function asks "is there usable telemetry?"
    and correctly ignores anything stale, whereas this one asks "how long has
    it been quiet?" and a stale snapshot is precisely the answer.

    The same clock-skew bound applies, though. A snapshot stamped in the
    future would otherwise win the ordering and report a negative age,
    disguising a stall as perfect freshness.

    None means the facility has never reported at all. That is a different
    condition from having gone quiet -- a facility that has never started is
    not a facility that stopped -- and the caller decides what to do with it.

    Args:
        conn: Active database connection.
        facility_id: Facility being checked.
        now: Evaluation time, used to bound clock skew.

    Returns:
        The most recent snapshot time, or None.
    """
    result = await conn.execute(
        text(
            """
            SELECT occurred_at
            FROM fulfillment_snapshots
            WHERE facility_id = :facility_id
              AND occurred_at <= :until
            ORDER BY occurred_at DESC
            LIMIT 1
            """
        ),
        {"facility_id": facility_id, "until": now + SNAPSHOT_CLOCK_SKEW_TOLERANCE},
    )
    row = result.first()
    return None if row is None else row[0]
