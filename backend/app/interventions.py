"""Execute the half of a recovery plan that no external system can perform.

A capacity change has somewhere to go: n8n tells the warehouse to add people.
A queue reorder does not. Its entire effect is a change to how this backend
ranks pending work, so unless it is persisted here, an approved queue plan
projects an improvement and then changes nothing -- which is exactly what it
did before this module existed.

Four effects are ours to apply:

- `priority_adjustment` persists the per-order points a queue lever adds, and
  the read path feeds them back into the scheduler's existing hook;
- `promise_shift` rewrites the deadline on orders customers have already placed,
  preserving the original so the change stays auditable;
- `manage_breach` records the orders whose miss has been acknowledged, so a live
  read can report them as managed rather than as silently late;
- `cutoff_shift` moves the facility's carrier collection time. Physically it
  is a phone call to the carrier, but the number it changes --
  `facilities.dispatch_cutoff_utc` -- is backend-owned policy, the same class
  of fact `dispatch_promise_hours` already is, not a claim about achieved
  throughput. It belongs here rather than behind a verified commitment (P6).

Everything else -- capacity, inflow -- is somebody else's to do, and travels to
the simulator through n8n.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models import Order, OrderStatus, ScheduleResult, SLAStatus
from app.scheduler import CutoffPolicy

logger = logging.getLogger(__name__)

# Effect kinds this backend applies itself. The rest leave through n8n.
INTERNAL_KINDS = frozenset(
    {"priority_adjustment", "promise_shift", "manage_breach", "cutoff_shift"}
)


def targeted_orders(effect: dict, orders: list[Order], now: datetime) -> list[Order]:
    """Return the orders one effect applies to.

    Deliberately mirrors the rule the projection used. If execution targeted a
    different set from the one that was projected, the operator would be shown
    one plan and handed another.

    Args:
        effect: Serialised lever effect.
        orders: Candidate orders; only pending ones can be acted on.
        now: Evaluation time, for slack-based filters.
    """
    segment = effect.get("segment")
    minimum_slack = effect.get("min_slack_hours")
    chosen = []

    for order in orders:
        if order.status != OrderStatus.PENDING:
            continue
        if segment is not None and order.segment != segment:
            continue
        if minimum_slack is not None:
            slack = (order.promised_dispatch_at - now).total_seconds() / 3600.0
            if slack < minimum_slack:
                continue
        chosen.append(order)

    return chosen


async def apply_internal_effects(
    conn: AsyncConnection,
    *,
    action_id: str,
    facility_id: str,
    effects: list[dict],
    orders: list[Order],
    schedule: ScheduleResult,
    now: datetime,
) -> list[str]:
    """Apply the effects this backend owns, once.

    Idempotent by construction: `active_interventions` is unique on
    (action_id, lever_id), and a conflicting insert skips the write entirely.
    n8n retries, and a retry must not move a customer's deadline a second time.

    Returns:
        Ids of the levers actually applied by this call.
    """
    applied: list[str] = []

    for effect in effects:
        kind = effect.get("kind")
        if kind not in INTERNAL_KINDS:
            continue

        lever_id = effect.get("lever_id", kind)
        claimed = await conn.execute(
            text(
                """
                INSERT INTO active_interventions (
                    facility_id, action_id, lever_id, family, effect, order_ids
                ) VALUES (
                    :facility_id, :action_id, :lever_id, :family,
                    CAST(:effect AS JSONB), CAST(:order_ids AS JSONB)
                )
                ON CONFLICT (action_id, lever_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "facility_id": facility_id,
                "action_id": action_id,
                "lever_id": lever_id,
                "family": effect.get("family", "QUEUE"),
                "effect": json.dumps(effect),
                "order_ids": json.dumps(_order_ids_for(effect, orders, schedule, now)),
            },
        )
        if claimed.first() is None:
            logger.info("lever %s of action %s already applied; skipping", lever_id, action_id)
            continue

        if kind == "promise_shift":
            await _shift_promises(conn, effect, orders, now)
        elif kind == "cutoff_shift":
            await _apply_cutoff_shift(conn, effect, facility_id)

        applied.append(lever_id)

    return applied


def _order_ids_for(
    effect: dict, orders: list[Order], schedule: ScheduleResult, now: datetime
) -> list[str]:
    """Order ids the effect binds to, frozen at the moment it is applied."""
    if effect.get("kind") == "manage_breach":
        # Only a miss can be managed. Acknowledging an order that is still going
        # to arrive on time would be recording an apology nobody needs.
        return [
            scheduled.order_id
            for scheduled in schedule.scheduled_orders
            if scheduled.sla_status == SLAStatus.BREACHED
        ]
    if effect.get("kind") == "cutoff_shift":
        # A facility-level change, not an order-level one -- targeted_orders'
        # segment/slack filters would return every PENDING order by default
        # (neither key is set on this effect), which is not what the shift
        # actually targets.
        return []
    return [order.order_id for order in targeted_orders(effect, orders, now)]


async def _shift_promises(
    conn: AsyncConnection, effect: dict, orders: list[Order], now: datetime
) -> None:
    """Move the deadline on already-placed orders, keeping the original.

    This is the one effect that rewrites a fact rather than a prediction, so it
    is written narrowly: only pending orders, only those the lever targets, and
    the original deadline is preserved the first time it moves.
    """
    hours = float(effect.get("hours", 0.0))
    if hours == 0:
        return

    ids = [order.order_id for order in targeted_orders(effect, orders, now)]
    if not ids:
        return

    await conn.execute(
        text(
            """
            UPDATE orders
            SET original_promised_dispatch_at =
                    COALESCE(original_promised_dispatch_at, promised_dispatch_at),
                promised_dispatch_at = promised_dispatch_at + :shift,
                updated_at = NOW()
            WHERE order_id = ANY(:ids) AND status = 'PENDING'
            """
        ),
        {"shift": timedelta(hours=hours), "ids": ids},
    )


async def _apply_cutoff_shift(conn: AsyncConnection, effect: dict, facility_id: str) -> None:
    """Move the facility's carrier collection time later, for real this time.

    Reads the facility's current cutoff in this same transaction and reuses
    `CutoffPolicy.shifted_by` so the persisted value matches exactly what the
    projection showed the operator before approval, rather than a second,
    independent computation that could drift from it.

    A facility with no cutoff configured has nothing to shift -- continuous
    dispatch, the demo default for WH-01 (migration 0006's docstring: discrete
    cutoffs degenerate at a compressed clock speed) -- so this is a silent
    no-op rather than an error. `EXTEND_CARRIER_CUTOFF`'s own precondition
    (`requires_cutoff`) already keeps the lever from being offered in that
    case; this is the second, independent guard for a facility whose cutoff
    changed between projection and approval.
    """
    minutes = float(effect.get("minutes", 0.0))
    if minutes == 0:
        return

    result = await conn.execute(
        text("SELECT dispatch_cutoff_utc FROM facilities WHERE facility_id = :facility_id"),
        {"facility_id": facility_id},
    )
    row = result.first()
    if row is None or row[0] is None:
        return

    shifted = CutoffPolicy(cutoff_utc=row[0]).shifted_by(minutes)
    await conn.execute(
        text(
            "UPDATE facilities SET dispatch_cutoff_utc = :cutoff WHERE facility_id = :facility_id"
        ),
        {"cutoff": shifted.cutoff_utc, "facility_id": facility_id},
    )


async def active_priority_adjustments(
    conn: AsyncConnection, facility_id: str
) -> dict[str, float]:
    """Return the priority points currently in force, keyed by order id.

    This is what makes an approved queue lever real: the adjustments the plan
    projected are handed back to the scheduler on every later read.
    """
    result = await conn.execute(
        text(
            """
            SELECT effect, order_ids
            FROM active_interventions
            WHERE facility_id = :facility_id
              AND effect ->> 'kind' = 'priority_adjustment'
            """
        ),
        {"facility_id": facility_id},
    )

    adjustments: dict[str, float] = {}
    for row in result:
        effect = row.effect if isinstance(row.effect, dict) else json.loads(row.effect)
        ids = row.order_ids if isinstance(row.order_ids, list) else json.loads(row.order_ids)
        points = float(effect.get("points", 0.0))
        for order_id in ids:
            adjustments[order_id] = adjustments.get(order_id, 0.0) + points
    return adjustments


async def managed_order_ids(conn: AsyncConnection, facility_id: str) -> frozenset[str]:
    """Return orders whose miss has been acknowledged to the customer."""
    result = await conn.execute(
        text(
            """
            SELECT order_ids
            FROM active_interventions
            WHERE facility_id = :facility_id
              AND effect ->> 'kind' = 'manage_breach'
            """
        ),
        {"facility_id": facility_id},
    )
    ids: set[str] = set()
    for row in result:
        raw = row.order_ids if isinstance(row.order_ids, list) else json.loads(row.order_ids)
        ids.update(raw)
    return frozenset(ids)
