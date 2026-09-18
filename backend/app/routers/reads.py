"""Frontend read APIs backed by repository data and the scheduler."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app import repository
from app.db import get_connection
from app.models import Order, OrderStatus, ScheduleResult, SLAStatus
from app.repository import FacilityNotFoundError
from app.scheduler import (
    CutoffPolicy,
    OperatingCalendar,
    SlaThresholds,
    classify_facility_risk,
    compute_schedule,
    cutoff_policy,
    operating_calendar,
    risk_thresholds,
    sla_thresholds,
)
from app.schemas import (
    DashboardResponse,
    OrderResponse,
    OrdersResponse,
    OrderStateCounts,
    PriorityBreakdownResponse,
    SLACounts,
)

router = APIRouter(tags=["reads"])

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    facility_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> DashboardResponse:
    """Return current operational metrics for one facility."""
    now = datetime.now(timezone.utc)

    try:
        facility = await repository.get_facility(conn, facility_id)
    except FacilityNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown facility_id {facility_id}")

    throughput = await repository.get_throughput_signal(
        conn, facility_id, facility["capacity_per_hour"], now, operating_calendar(facility)
    )
    totals = await repository.get_order_totals(conn, facility_id)
    pending = await repository.list_pending_orders(conn, facility_id)
    cutoff = cutoff_policy(facility)
    schedule = compute_schedule(
        pending,
        throughput.scheduling_capacity_per_hour,
        now,
        committed_work_units=totals.committed_work_units,
        dispatch_cutoff=cutoff,
        operating_calendar=operating_calendar(facility),
        sla_thresholds=sla_thresholds(facility),
        priority_adjustments=None,
    )
    demand = await repository.get_demand_work_units_per_hour(conn, facility_id, now)

    return DashboardResponse(
        facility_id=facility_id,
        generated_at=now,
        demand_work_units_per_hour=demand,
        fulfillment_work_units_per_hour=throughput.reported_work_units_per_hour,
        throughput_source=throughput.source,
        throughput_stalled=throughput.stalled,
        backlog_orders=totals.backlog_orders,
        backlog_work_units=totals.backlog_work_units,
        dispatch_promise_hours=facility["dispatch_promise_hours"],
        risk_level=classify_facility_risk(
            schedule, risk_thresholds(facility), stalled=throughput.stalled
        ),
        order_states=_count_order_states(totals),
        sla_counts=SLACounts(
            safe=schedule.safe_count,
            watch=schedule.watch_count,
            at_risk=schedule.at_risk_count,
            breached=schedule.breached_count,
        ),
        backlog_growth_wu_per_hour=demand - throughput.reported_work_units_per_hour,
        minutes_to_first_breach=schedule.minutes_to_first_breach,
        minutes_to_cutoff=(
            None
            if cutoff is None
            else (cutoff.next_dispatch_after(now) - now).total_seconds() / 60.0
        ),
    )


@router.get("/orders", response_model=OrdersResponse)
async def list_orders(
    facility_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    status: list[OrderStatus] | None = Query(None),
    conn: AsyncConnection = Depends(get_connection),
) -> OrdersResponse:
    """Return scheduled orders with optional pagination and status filtering."""
    now = datetime.now(timezone.utc)
    capacity, totals, cutoff, thresholds, calendar = await _scheduling_inputs(
        conn, facility_id, now
    )
    wanted = set(status) if status else set(OrderStatus)

    scheduled: list[OrderResponse] = []
    if OrderStatus.PENDING in wanted:
        pending = await repository.list_pending_orders(conn, facility_id)
        scheduled = _scheduled_responses(
            pending,
            compute_schedule(
                pending,
                capacity,
                now,
                totals.committed_work_units,
                dispatch_cutoff=cutoff,
                operating_calendar=calendar,
                sla_thresholds=thresholds,
                priority_adjustments=None,
            ),
        )

    items = scheduled[offset : offset + limit]
    remaining = limit - len(items)
    if remaining > 0:
        tail = await repository.list_non_pending_orders(
            conn,
            facility_id,
            limit=remaining,
            offset=max(offset - len(scheduled), 0),
            statuses=sorted(wanted - {OrderStatus.PENDING}, key=lambda s: s.value),
        )
        items = items + [_unscheduled_response(order) for order in tail]

    total = sum(totals.counts.get(state, 0) for state in wanted)
    return OrdersResponse(items=items, total=total)


@router.get("/orders/at-risk", response_model=OrdersResponse)
async def list_at_risk_orders(
    facility_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    conn: AsyncConnection = Depends(get_connection),
) -> OrdersResponse:
    """Return orders classified as at risk or breached."""
    now = datetime.now(timezone.utc)
    capacity, totals, cutoff, thresholds, calendar = await _scheduling_inputs(
        conn, facility_id, now
    )

    pending = await repository.list_pending_orders(conn, facility_id)
    schedule = compute_schedule(
        pending,
        capacity,
        now,
        totals.committed_work_units,
        dispatch_cutoff=cutoff,
        operating_calendar=calendar,
        sla_thresholds=thresholds,
        priority_adjustments=None,
    )
    matching = [
        response
        for response in _scheduled_responses(pending, schedule)
        if response.sla_status in (SLAStatus.AT_RISK, SLAStatus.BREACHED)
    ]
    return OrdersResponse(items=matching[offset : offset + limit], total=len(matching))


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    facility_id: str,
    conn: AsyncConnection = Depends(get_connection),
) -> OrderResponse:
    """Return one order with its scheduler fields."""
    now = datetime.now(timezone.utc)

    order = await repository.get_order(conn, facility_id, order_id)
    if order is None:
        raise HTTPException(
            status_code=404, detail=f"no order {order_id} at facility {facility_id}"
        )

    if order.status != OrderStatus.PENDING:
        return _unscheduled_response(order)

    capacity, totals, cutoff, thresholds, calendar = await _scheduling_inputs(
        conn, facility_id, now
    )
    pending = await repository.list_pending_orders(conn, facility_id)
    scheduled = _scheduled_responses(
        pending,
        compute_schedule(
            pending,
            capacity,
            now,
            totals.committed_work_units,
            dispatch_cutoff=cutoff,
            operating_calendar=calendar,
            sla_thresholds=thresholds,
            priority_adjustments=None,
        ),
    )
    return next(
        (response for response in scheduled if response.order_id == order_id),
        _unscheduled_response(order),
    )


async def _scheduling_inputs(
    conn: AsyncConnection, facility_id: str, now: datetime
) -> tuple[
    float, repository.OrderTotals, CutoffPolicy | None, SlaThresholds, OperatingCalendar | None
]:
    """Return everything `compute_schedule` needs beyond the pending queue."""
    try:
        facility = await repository.get_facility(conn, facility_id)
    except FacilityNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown facility_id {facility_id}")

    throughput = await repository.get_throughput_signal(
        conn, facility_id, facility["capacity_per_hour"], now
    )
    totals = await repository.get_order_totals(conn, facility_id)
    return (
        throughput.scheduling_capacity_per_hour,
        totals,
        cutoff_policy(facility),
        sla_thresholds(facility),
        operating_calendar(facility),
    )


def _scheduled_responses(pending: list[Order], schedule: ScheduleResult) -> list[OrderResponse]:
    """Build ordered responses for the scheduled queue."""
    by_id = {order.order_id: order for order in pending}
    return [
        OrderResponse(
            order_id=scheduled.order_id,
            facility_id=by_id[scheduled.order_id].facility_id,
            created_at=by_id[scheduled.order_id].created_at,
            promised_dispatch_at=scheduled.promised_dispatch_at,
            predicted_dispatch_at=scheduled.predicted_dispatch_at,
            item_count=by_id[scheduled.order_id].item_count,
            work_units=by_id[scheduled.order_id].work_units,
            order_value=by_id[scheduled.order_id].order_value,
            status=by_id[scheduled.order_id].status,
            segment=by_id[scheduled.order_id].segment,
            original_promised_dispatch_at=by_id[scheduled.order_id].original_promised_dispatch_at,
            sla_status=scheduled.sla_status,
            priority_score=scheduled.priority_score,
            queue_position=scheduled.queue_position,
            priority_breakdown=PriorityBreakdownResponse(
                sla_urgency=scheduled.priority_breakdown.sla_urgency,
                aging_bonus=scheduled.priority_breakdown.aging_bonus,
                workload_penalty=scheduled.priority_breakdown.workload_penalty,
                adjustment=scheduled.priority_breakdown.adjustment,
            ),
            work_units_ahead=scheduled.work_units_ahead,
            throughput_assumed=schedule.capacity_per_hour,
            work_complete_at=scheduled.work_complete_at,
        )
        for scheduled in schedule.scheduled_orders
    ]


def _unscheduled_response(order: Order) -> OrderResponse:
    """Build a response for an order the scheduler does not reorder."""
    return OrderResponse(
        order_id=order.order_id,
        facility_id=order.facility_id,
        created_at=order.created_at,
        promised_dispatch_at=order.promised_dispatch_at,
        predicted_dispatch_at=None,
        item_count=order.item_count,
        work_units=order.work_units,
        order_value=order.order_value,
        status=order.status,
        segment=order.segment,
        original_promised_dispatch_at=order.original_promised_dispatch_at,
        sla_status=None,
        priority_score=None,
        queue_position=None,
    )


def _count_order_states(totals: repository.OrderTotals) -> OrderStateCounts:
    """Map database-side status totals onto the dashboard response fields."""
    counts = totals.counts
    return OrderStateCounts(
        received=counts.get(OrderStatus.RECEIVED, 0),
        pending=counts.get(OrderStatus.PENDING, 0),
        picking=counts.get(OrderStatus.PICKING, 0),
        packed=counts.get(OrderStatus.PACKED, 0),
        ready=counts.get(OrderStatus.READY, 0),
        dispatched=counts.get(OrderStatus.DISPATCHED, 0),
    )
