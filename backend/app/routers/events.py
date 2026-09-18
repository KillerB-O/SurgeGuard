"""Ingest canonical commerce and fulfillment events idempotently."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection


from app.db import get_connection
from app.models import (
    ORDER_LIFECYCLE,
    EventIngestionResponse,
    FulfillmentSnapshotEvent,
    IngestionStatus,
    OrderBackfillEvent,
    OrderCreatedEvent,
    OrderRevisedEvent,
    OrderStatus,
    OrderStatusUpdatedEvent,
    lifecycle_position,
)
from app.work_units import classify_order_event, work_units_for_step

router = APIRouter(prefix="/events", tags=["events"])

logger = logging.getLogger(__name__)


async def _require_registered_source(
    conn: AsyncConnection, source: str, expected_kind: str
) -> None:
    """Refuse an event whose `source` is not a registered provider of this kind.

    `source` was widened from a two-value `Literal` enum to any string short
    enough to be a provider id, so *something* still has to say which ids are
    real. The old enum also incidentally guaranteed that a commerce event
    could not claim the fulfillment producer's identity or vice versa, purely
    because `OrderCreatedEvent.source` and `FulfillmentSnapshotEvent.source`
    were pinned to different literal values -- widening to a plain string
    would silently drop that guarantee unless it is re-asserted here against
    `event_providers.kind`.

    Full per-provider credential verification (so a caller with the one
    shared service token could not attribute events to an arbitrary
    registered provider of the right kind) is a deliberate follow-up, not
    covered here.

    Args:
        conn: Active database connection and transaction.
        source: The event's claimed provider id.
        expected_kind: 'commerce' or 'fulfillment', matching the endpoint.

    Raises:
        HTTPException: 422 if no provider of this kind is registered under
            this id.
    """
    result = await conn.execute(
        text(
            "SELECT 1 FROM event_providers WHERE provider_id = :source "
            "AND kind = :expected_kind"
        ),
        {"source": source, "expected_kind": expected_kind},
    )
    if result.first() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"source {source!r} is not a registered {expected_kind} "
                "event provider"
            ),
        )


async def _claim_event(
    conn: AsyncConnection, event_id: str, source: str, event_type: str
) -> bool:
    """Claim an event ID inside the caller's transaction.

    Args:
        conn: Active database connection and transaction.
        event_id: Producer-generated idempotency key.
        source: Registered provider id.
        event_type: Canonical event type value.

    Returns:
        `True` for a new claim and `False` for an already processed event.
    """
    result = await conn.execute(
        text(
            """
            INSERT INTO processed_events (event_id, source, event_type)
            VALUES (:event_id, :source, :event_type)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """
        ),
        {"event_id": event_id, "source": source, "event_type": event_type},
    )
    return result.first() is not None


@router.post(
    "/orders",
    response_model=EventIngestionResponse,
    # n8n workflow commerce-orders-to-backend.json (plan 03, ACCESS-02).
)
async def ingest_order_created(
    event: OrderCreatedEvent,
    conn: AsyncConnection = Depends(get_connection),
) -> EventIngestionResponse:
    """Persist one order-created event and reject duplicate order identities.

    Args:
        event: Validated canonical commerce event.
        conn: Request-scoped database transaction.

    Returns:
        Whether the event was newly processed or was a retry.
    """
    await _require_registered_source(conn, event.source, "commerce")
    claimed = await _claim_event(
        conn, event.event_id, event.source, event.event_type.value
    )
    if not claimed:
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.DUPLICATE)

    # How much work an order costs is our judgement, not the producer's, so it
    # is derived here whenever the event carries enough to derive it from.
    work_units = classify_order_event(
        line_count=event.line_count,
        unit_count=event.unit_count,
        special_handling=event.special_handling,
        declared_work_units=event.work_units,
    )

    try:
        await conn.execute(
            text(
                """
                INSERT INTO orders (
                    order_id, facility_id, created_at, promised_dispatch_at,
                    item_count, work_units, order_value, status,
                    line_count, unit_count, special_handling, segment
                ) VALUES (
                    :order_id, :facility_id, :created_at, :promised_dispatch_at,
                    :item_count, :work_units, :order_value, 'PENDING',
                    :line_count, :unit_count, :special_handling, :segment
                )
                """
            ),
            {
                "order_id": event.order_id,
                "facility_id": event.facility_id,
                "created_at": event.created_at,
                "promised_dispatch_at": event.promised_dispatch_at,
                "item_count": event.item_count,
                "work_units": work_units,
                "order_value": event.order_value,
                "line_count": event.line_count,
                "unit_count": event.unit_count,
                "special_handling": event.special_handling,
                "segment": event.segment,
            },
        )
    except IntegrityError as exc:
        _raise_for_integrity_error(exc, order_id=event.order_id, facility_id=event.facility_id)

    # The order now exists, so any status event that arrived ahead of it and
    # was parked can be applied.
    await _replay_parked_status_events(
        conn,
        order_id=event.order_id,
        facility_id=event.facility_id,
        order_work_units=work_units,
    )

    return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)


@router.post(
    "/fulfillment",
    response_model=EventIngestionResponse,
    # n8n workflow fulfillment-snapshot-to-backend.json (plan 03, ACCESS-02).
)
async def ingest_fulfillment_snapshot(
    event: FulfillmentSnapshotEvent,
    conn: AsyncConnection = Depends(get_connection),
) -> EventIngestionResponse:
    """Persist fulfillment telemetry without changing canonical orders.

    Args:
        event: Validated fulfillment snapshot.
        conn: Request-scoped database transaction.

    Returns:
        Whether the event was newly processed or was a retry.
    """
    await _require_registered_source(conn, event.source, "fulfillment")
    claimed = await _claim_event(
        conn, event.event_id, event.source, event.event_type.value
    )
    if not claimed:
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.DUPLICATE)

    # Telemetry does not modify canonical order state.
    try:
        await conn.execute(
            text(
                """
                INSERT INTO fulfillment_snapshots (
                    facility_id, occurred_at, open_orders, work_units_completed_last_hour
                ) VALUES (
                    :facility_id, :occurred_at, :open_orders, :work_units_completed_last_hour
                )
                """
            ),
            {
                "facility_id": event.facility_id,
                "occurred_at": event.occurred_at,
                "open_orders": event.open_orders,
                "work_units_completed_last_hour": event.work_units_completed_last_hour,
            },
        )
    except IntegrityError as exc:
        _raise_for_integrity_error(exc, order_id=None, facility_id=event.facility_id)

    return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)


@router.post(
    "/order-status",
    response_model=EventIngestionResponse,
    # n8n workflow fulfillment-status-to-backend.json (plan 03, ACCESS-02).

)
async def ingest_order_status_updated(
    event: OrderStatusUpdatedEvent,
    conn: AsyncConnection = Depends(get_connection),
) -> EventIngestionResponse:
    """Apply a status update to an existing order, or park it if none exists yet.

    Args:
        event: Validated order-status event.
        conn: Request-scoped database transaction.

    Returns:
        Whether the event was newly processed, was a retry, or was parked
        pending the order-created event it depends on.
    """
    await _require_registered_source(conn, event.source, "fulfillment")
    claimed = await _claim_event(
        conn, event.event_id, event.source, event.event_type.value
    )
    if not claimed:
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.DUPLICATE)

    # Locking the row first enforces facility ownership, prevents implicit
    # inserts, and makes the transition check safe against concurrent events.
    # order_id alone is the primary key on orders, so this looks the order up
    # by id first and checks facility separately -- that distinguishes "does
    # not exist yet" (park it; the order-created event will still arrive)
    # from "exists, but not at this facility" (a genuine mismatch that
    # parking would never resolve, since the real order will never appear
    # under the wrong facility_id).
    result = await conn.execute(
        text(
            "SELECT facility_id, status, work_units FROM orders "
            "WHERE order_id = :order_id FOR UPDATE"
        ),
        {"order_id": event.order_id},
    )
    row = result.first()
    if row is None:
        # Real webhooks are not ordered: a WMS "picking started" can reach
        # here before the commerce feed's "order created" does, even though
        # both describe the same order. The event_id is already claimed
        # above, so a retry of this same event correctly returns DUPLICATE
        # rather than parking a second time -- this table needs no id
        # uniqueness of its own.
        await conn.execute(
            text(
                """
                INSERT INTO pending_status_events
                    (order_id, facility_id, event_id, occurred_at, status, source)
                VALUES (:order_id, :facility_id, :event_id, :occurred_at, :status, :source)
                """
            ),
            {
                "order_id": event.order_id,
                "facility_id": event.facility_id,
                "event_id": event.event_id,
                "occurred_at": event.occurred_at,
                "status": event.status.value,
                "source": event.source,
            },
        )
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.BUFFERED)

    if row.facility_id != event.facility_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"order {event.order_id} belongs to facility {row.facility_id!r}, "
                f"not {event.facility_id!r} -- status events must identify the "
                "order's actual facility"
            ),
        )

    current_status = OrderStatus(row.status)
    await _apply_status_transition(
        conn,
        order_id=event.order_id,
        facility_id=event.facility_id,
        current_status=current_status,
        target_status=event.status,
        occurred_at=event.occurred_at,
        event_id=event.event_id,
        order_work_units=row.work_units,
    )

    return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)


@router.post(
    "/order-revised",
    response_model=EventIngestionResponse,
    # n8n workflow #5 (additive, plan section "the constraint that shapes
    # everything"): does not disturb the four locked-in workflow ids.
)
async def ingest_order_revised(
    event: OrderRevisedEvent,
    conn: AsyncConnection = Depends(get_connection),
) -> EventIngestionResponse:
    """Apply a content/promise revision to an existing order.

    Shopify's `orders/updated` after `orders/create`: the order already
    exists, and this carries its new values. Not covered by
    park-and-replay -- that mechanism exists for an *unknown* order (a
    status event arriving before its order-created event); this is the
    opposite case, a *known* order, so a missing order here is a genuine
    404, not something to park and wait for.

    Deliberately leaves `original_promised_dispatch_at` untouched: that
    field is the evidence behind this product's first honesty claim
    (separating breaches prevented from breaches merely moved), and
    `_apply_facility_policy`'s RE_PROMISE lever already handles it correctly
    via `COALESCE(original_promised_dispatch_at, promised_dispatch_at)` --
    NULL means no internal lever has shifted this order's promise yet, so
    the next one captures whatever `promised_dispatch_at` is at that time
    (including this revision) as the original. Resetting or rewriting it
    here would erase the record that an operator moved the goalposts.

    Args:
        event: Validated revision -- new content and promise for an
            existing order.
        conn: Request-scoped database transaction.

    Returns:
        Whether the event was newly processed or was a retry.

    Raises:
        HTTPException: 404 if the order does not exist or belongs to a
            different facility; 409 if the order has already dispatched or
            been cancelled -- nothing is left to revise either way.
    """
    await _require_registered_source(conn, event.source, "commerce")
    claimed = await _claim_event(
        conn, event.event_id, event.source, event.event_type.value
    )
    if not claimed:
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.DUPLICATE)

    result = await conn.execute(
        text(
            "SELECT facility_id, status FROM orders WHERE order_id = :order_id FOR UPDATE"
        ),
        {"order_id": event.order_id},
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"order {event.order_id} does not exist -- revision requires a known order",
        )
    if row.facility_id != event.facility_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"order {event.order_id} belongs to facility {row.facility_id!r}, "
                f"not {event.facility_id!r} -- revision events must identify the "
                "order's actual facility"
            ),
        )

    current_status = OrderStatus(row.status)
    if current_status in (OrderStatus.DISPATCHED, OrderStatus.CANCELLED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"order {event.order_id} is {current_status.value} and cannot be revised"
            ),
        )

    work_units = classify_order_event(
        line_count=event.line_count,
        unit_count=event.unit_count,
        special_handling=event.special_handling,
        declared_work_units=event.work_units,
    )

    try:
        await conn.execute(
            text(
                """
                UPDATE orders
                SET promised_dispatch_at = :promised_dispatch_at,
                    item_count = :item_count,
                    work_units = :work_units,
                    order_value = :order_value,
                    segment = :segment,
                    updated_at = NOW()
                WHERE order_id = :order_id AND facility_id = :facility_id
                """
            ),
            {
                "order_id": event.order_id,
                "facility_id": event.facility_id,
                "promised_dispatch_at": event.promised_dispatch_at,
                "item_count": event.item_count,
                "work_units": work_units,
                "order_value": event.order_value,
                "segment": event.segment,
            },
        )
    except IntegrityError as exc:
        # migration 0001's CHECK (promised_dispatch_at > created_at): a
        # revision carries no created_at of its own to validate against at
        # the Pydantic layer (creation time does not change on revision), so
        # a malformed revised promise can only be caught here, against the
        # order's real, already-persisted created_at.
        pgcode = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if pgcode == "23514":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"revised promised_dispatch_at must be after order "
                    f"{event.order_id}'s creation time"
                ),
            ) from exc
        raise

    return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)


@router.post(
    "/backfill",
    response_model=EventIngestionResponse,
    # A one-time onboarding import, not part of the four locked-in n8n
    # workflows -- gated the same way as the other event endpoints so it
    # shares the provider-agnostic trust model, not because n8n calls it.

)
async def ingest_order_backfilled(
    event: OrderBackfillEvent,
    conn: AsyncConnection = Depends(get_connection),
) -> EventIngestionResponse:
    """Import one pre-existing order at its real current lifecycle status.

    A facility onboarding on day one already has orders mid-flight in the
    real commerce/WMS system -- created, and possibly already picked, packed
    or dispatched, before this backend ever received a live event for them.
    Replaying that history through the ordinary ingest endpoints would land
    every order at PENDING (`ingest_order_created` always does) and, worse,
    record each already-completed step as a fresh `origin='event'`
    transition, producing a fictitious throughput spike at the moment of
    import (see migration 0017 and
    `app.repository._derive_throughput_from_transitions`).

    Inserts the order at PENDING exactly like `ingest_order_created`, then
    reuses `_apply_status_transition` to move it to its real current status
    as one forward jump, tagged `origin='backfill'` so it stays invisible to
    derived throughput while remaining fully visible to the scheduler and to
    `count_late_dispatches` -- P2's rolling window is what keeps a
    backfilled order's real (possibly already-late) `created_at` from
    corrupting that metric forever instead of just until it ages out.
    Reusing `_apply_status_transition` also means this path inherits whatever
    that function validates: DELAYED backfill is rejected the same way a live
    status event outside `ORDER_LIFECYCLE` is, and CANCELLED backfill is
    accepted the same way a live cancellation is (P8) -- an already-cancelled
    order in the day-one backlog is importable with no backfill-specific code
    at all.

    Args:
        event: Validated backfill snapshot -- an OrderCreatedEvent's fields
            plus the order's current status and when it reached that status.
        conn: Request-scoped database transaction.

    Returns:
        Whether the event was newly processed or was a retry.
    """
    await _require_registered_source(conn, event.source, "commerce")
    claimed = await _claim_event(
        conn, event.event_id, event.source, event.event_type.value
    )
    if not claimed:
        return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.DUPLICATE)

    work_units = classify_order_event(
        line_count=event.line_count,
        unit_count=event.unit_count,
        special_handling=event.special_handling,
        declared_work_units=event.work_units,
    )

    try:
        await conn.execute(
            text(
                """
                INSERT INTO orders (
                    order_id, facility_id, created_at, promised_dispatch_at,
                    item_count, work_units, order_value, status,
                    line_count, unit_count, special_handling, segment
                ) VALUES (
                    :order_id, :facility_id, :created_at, :promised_dispatch_at,
                    :item_count, :work_units, :order_value, 'PENDING',
                    :line_count, :unit_count, :special_handling, :segment
                )
                """
            ),
            {
                "order_id": event.order_id,
                "facility_id": event.facility_id,
                "created_at": event.created_at,
                "promised_dispatch_at": event.promised_dispatch_at,
                "item_count": event.item_count,
                "work_units": work_units,
                "order_value": event.order_value,
                "line_count": event.line_count,
                "unit_count": event.unit_count,
                "special_handling": event.special_handling,
                "segment": event.segment,
            },
        )
    except IntegrityError as exc:
        _raise_for_integrity_error(exc, order_id=event.order_id, facility_id=event.facility_id)

    await _apply_status_transition(
        conn,
        order_id=event.order_id,
        facility_id=event.facility_id,
        current_status=OrderStatus.PENDING,
        target_status=event.status,
        occurred_at=event.occurred_at,
        event_id=event.event_id,
        order_work_units=work_units,
        origin="backfill",
    )

    return EventIngestionResponse(event_id=event.event_id, status=IngestionStatus.PROCESSED)


async def _apply_status_transition(
    conn: AsyncConnection,
    *,
    order_id: str,
    facility_id: str,
    current_status: OrderStatus,
    target_status: OrderStatus,
    occurred_at,
    event_id: str,
    order_work_units: float,
    origin: str = "event",
) -> None:
    """Validate and apply one status transition to an order known to exist.

    Shared by the live ingest path, by replaying parked events once their
    order arrives, and by the backfill path (P7) importing an order's real
    current status as one forward jump from PENDING -- all three go through
    the exact same transition rules and leave the same shape of
    transition-history record behind (see `app.repository.get_throughput_signal`).

    Args:
        conn: Active database connection and transaction.
        order_id: Order being updated.
        facility_id: Facility the order belongs to.
        current_status: The order's status before this transition.
        target_status: The status the event asks to move to.
        occurred_at: Timestamp to stamp dispatch with, if this is a dispatch.
        event_id: The event responsible for this transition, recorded for
            traceability.
        order_work_units: The order's total cost, used to charge this step
            (or steps, for a forward jump) its fair share.
        origin: 'event' for a live or replayed transition, 'backfill' for
            one imported at onboarding -- excluded from derived throughput
            (migration 0017) so a day-one backlog import cannot fake a
            throughput spike.

    Raises:
        HTTPException: 422 or 409, per `_require_valid_transition`.
    """
    _require_valid_transition(current_status, target_status, order_id=order_id)

    # Restating the current status is accepted but changes nothing -- no
    # transition happened, so nothing is recorded either.
    if current_status == target_status:
        return

    await conn.execute(
        text(
            """
            UPDATE orders
            SET status = :status,
                updated_at = NOW(),
                -- Stamped from the event, not from NOW(): the promise this
                -- will be compared against lives on the simulated clock,
                -- and NOW() is the wall clock. See migration 0009.
                -- Null for any non-dispatch transition, so COALESCE leaves
                -- an already-recorded dispatch alone.
                dispatched_at = COALESCE(:dispatched_at, dispatched_at)
            WHERE order_id = :order_id AND facility_id = :facility_id
            """
        ),
        {
            "status": target_status.value,
            "order_id": order_id,
            "facility_id": facility_id,
            "dispatched_at": occurred_at if target_status == OrderStatus.DISPATCHED else None,
        },
    )

    if target_status == OrderStatus.CANCELLED:
        # CANCELLED has no position in ORDER_LIFECYCLE by design (see
        # _require_valid_transition), so work_units_for_step's
        # `stage_weights[from_position:to_position]` would receive a `None`
        # end and Python's own slicing rules would silently resolve that to
        # "to the end" -- charging a cancelled order for every remaining
        # step's work as if it had actually been done. A cancellation is not
        # completed work; it earns zero. This row still counts toward
        # MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT's activity floor (real
        # activity happened), it just contributes nothing to the summed rate.
        step_work_units = 0.0
    else:
        # A forward jump (a retried or reordered delivery skipping a status) is
        # tolerated by _require_valid_transition, and is charged the work of
        # every step it covers rather than just one, so a skipped PICKING is not
        # invisible to throughput. Charged per the catalog's stage_weights (P5),
        # not an equal split, so this stays consistent with the remaining-fraction
        # charge OrderTotals.committed_work_units computes from the same weights.
        step_work_units = work_units_for_step(
            order_work_units,
            lifecycle_position(current_status),
            lifecycle_position(target_status),
        )
    await conn.execute(
        text(
            """
            INSERT INTO order_status_transitions
                (order_id, facility_id, from_status, to_status, occurred_at,
                 event_id, work_units, origin)
            VALUES (:order_id, :facility_id, :from_status, :to_status, :occurred_at,
                    :event_id, :work_units, :origin)
            """
        ),
        {
            "order_id": order_id,
            "facility_id": facility_id,
            "from_status": current_status.value,
            "to_status": target_status.value,
            "occurred_at": occurred_at,
            "event_id": event_id,
            "work_units": step_work_units,
            "origin": origin,
        },
    )


async def _replay_parked_status_events(
    conn: AsyncConnection, *, order_id: str, facility_id: str, order_work_units: float
) -> None:
    """Apply every status event parked ahead of this order's arrival.

    Replayed in `occurred_at` order -- the order the events actually
    happened in, not the order they arrived in, since an out-of-order WMS can
    reorder several events ahead of the same missing order. A parked event
    that turns out to be an invalid transition (relative to PENDING, or to an
    earlier replayed event) is dropped and logged rather than allowed to fail
    the order-created request that triggered the replay: the order itself is
    a valid fact regardless of what a malformed buffered event claims.

    Args:
        conn: Active database connection and transaction, same one the
            order-created insert ran in.
        order_id: The order that just started existing.
        facility_id: Facility the order belongs to.
        order_work_units: The order's total cost, passed through to
            `_apply_status_transition` for each replayed step.
    """
    result = await conn.execute(
        text(
            """
            DELETE FROM pending_status_events
            WHERE order_id = :order_id AND facility_id = :facility_id
            RETURNING status, occurred_at, event_id
            """
        ),
        {"order_id": order_id, "facility_id": facility_id},
    )
    parked = sorted(result.all(), key=lambda row: row.occurred_at)

    current_status = OrderStatus.PENDING
    for row in parked:
        target_status = OrderStatus(row.status)
        try:
            await _apply_status_transition(
                conn,
                order_id=order_id,
                facility_id=facility_id,
                current_status=current_status,
                target_status=target_status,
                occurred_at=row.occurred_at,
                event_id=row.event_id,
                order_work_units=order_work_units,
            )
        except HTTPException:
            logger.warning(
                "dropping invalid parked status transition for %s at %s: %s -> %s",
                order_id,
                facility_id,
                current_status.value,
                target_status.value,
            )
            continue
        current_status = target_status


def _require_valid_transition(
    current: OrderStatus, target: OrderStatus, *, order_id: str
) -> None:
    """Reject status changes that the MVP lifecycle cannot produce.

    Progress is monotonic: an order may move forward along the canonical
    lifecycle or restate its current status, never move backward. Forward jumps
    are tolerated so a retried or reordered delivery cannot wedge an order.

    CANCELLED (P8) is a side-exit, not a lifecycle step, and is deliberately
    kept out of `ORDER_LIFECYCLE` itself: `WorkUnitRules._check_stage_weights`
    requires exactly `len(ORDER_LIFECYCLE) - 1` stage weights summing to 1.0,
    so a fifth lifecycle entry would fail catalog validation at import, not
    just be conceptually wrong. It is legal from any pre-dispatch status
    (including restating an already-cancelled order, a harmless no-op) and
    refused once DISPATCHED -- an order that already left has nothing left to
    cancel. DELAYED remains outside the lifecycle entirely (docs/10 section 9),
    unchanged by P8.

    Args:
        current: Status persisted for the order.
        target: Status carried by the incoming event.
        order_id: Order identity, used in error detail.

    Raises:
        HTTPException: 422 for a status outside the lifecycle, 409 for a
            regression, a cancellation of an already-dispatched order, or
            any attempt to move a cancelled order onward.
    """
    if target == OrderStatus.CANCELLED:
        if current == OrderStatus.DISPATCHED:
            raise HTTPException(
                status_code=409,
                detail=f"order {order_id} has already dispatched and cannot be cancelled",
            )
        return
    if current == OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=409,
            detail=f"order {order_id} is cancelled and cannot move to {target.value}",
        )

    current_position = lifecycle_position(current)
    target_position = lifecycle_position(target)

    if target_position is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"status {target.value} has no defined transition semantics in this MVP "
                f"-- expected one of {[s.value for s in ORDER_LIFECYCLE]}"
            ),
        )
    if current_position is None:  # Defensive; the schema constrains stored values.
        raise HTTPException(
            status_code=409,
            detail=f"order {order_id} is in unmanaged status {current.value}",
        )
    if target_position < current_position:
        raise HTTPException(
            status_code=409,
            detail=(
                f"order {order_id} cannot move backward from {current.value} "
                f"to {target.value}"
            ),
        )


def _raise_for_integrity_error(
    exc: IntegrityError, *, order_id: str | None, facility_id: str
) -> None:
    """Convert known integrity failures into stable API errors.

    Args:
        exc: Database integrity exception to classify.
        order_id: Order identity involved in the operation, if applicable.
        facility_id: Facility identity involved in the operation.

    Raises:
        HTTPException: For known conflict or foreign-key failures.
    """
    pgcode = getattr(getattr(exc, "orig", None), "sqlstate", None)

    if pgcode == "23505" and order_id is not None:  # unique_violation on orders.order_id
        raise HTTPException(
            status_code=409,
            detail=f"order_id {order_id} already exists under a different event_id",
        ) from exc
    if pgcode == "23503":  # foreign_key_violation
        raise HTTPException(
            status_code=422,
            detail=f"facility_id {facility_id} does not exist",
        ) from exc
    raise exc
