"""Project future order arrivals for What-If runs.

This is extrapolation, not forecasting. Arrivals are generated at the rate the
facility is *observed* to be receiving right now, optionally scaled by an
assumption the operator is testing. Nothing here learns a pattern or predicts a
trend -- that stays a non-goal.

Its purpose is to make inflow levers answerable. Pausing a promotion or hiding
express checkout changes what arrives next, not what is already queued, so
without projected arrivals those levers would change nothing and could only be
presented as theatre. With them, the same scheduler answers what they are worth.

Synthetic orders are marked, never persisted, and never returned by a live read.
They exist only inside one simulation call, and every count derived from them is
reported separately from orders customers have actually placed.
"""

import random
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import Order, OrderStatus
from app.work_units import WorkUnitRules, classify, load_rules

# Marks an order that has not been placed by anyone. Never persisted.
SYNTHETIC_PREFIX = "PROJ-"

# Arrivals are spread evenly across the horizon rather than clustered, so a
# projection is reproducible and readable. Bursts model the clustering.
DEFAULT_HORIZON_MINUTES = 120
DEFAULT_SEED = 42


class ArrivalBurst(BaseModel):
    """A flash-sale spike dropped into the middle of a projection."""

    model_config = ConfigDict(frozen=True)

    at_minute: int = Field(ge=0)
    work_units: float = Field(gt=0)


class ArrivalProjection(BaseModel):
    """What the operator is assuming about the next stretch of demand."""

    model_config = ConfigDict(frozen=True)

    horizon_minutes: int = Field(default=DEFAULT_HORIZON_MINUTES, gt=0, le=24 * 60)
    # 1.0 keeps the observed rate; 0.7 is what pausing a promotion might buy.
    arrival_multiplier: float = Field(default=1.0, gt=0)
    burst: ArrivalBurst | None = None
    seed: int = DEFAULT_SEED


def project_arrivals(
    observed_work_units_per_hour: float,
    projection: ArrivalProjection,
    now: datetime,
    facility_id: str,
    dispatch_promise_hours: float,
    rules: WorkUnitRules | None = None,
) -> list[Order]:
    """Build the orders the facility is on course to receive.

    Args:
        observed_work_units_per_hour: Arrival rate measured from real orders.
        projection: Horizon, multiplier and optional burst being assumed.
        now: Evaluation time; arrivals are spread forward from here.
        facility_id: Facility the arrivals belong to.
        dispatch_promise_hours: Promise policy applied to a new order.
        rules: Work-unit ruleset; defaults to the loaded catalog.

    Returns:
        Pending orders with `PROJ-` ids, deterministic for a given seed.
    """
    rules = rules or load_rules()
    horizon_hours = projection.horizon_minutes / 60.0

    rng = random.Random(projection.seed)
    steady_work_units = (
        observed_work_units_per_hour * horizon_hours * projection.arrival_multiplier
    )
    # Steady arrivals spread evenly across the horizon; a burst lands together
    # at its own minute, which is what makes a flash sale look like a flash sale.
    steady = _draw_arrivals(steady_work_units, rules, rng)
    burst = (
        _draw_arrivals(projection.burst.work_units, rules, rng)
        if projection.burst is not None
        else []
    )

    spacing = projection.horizon_minutes / max(len(steady), 1)
    arrivals = [(index * spacing, arrival) for index, arrival in enumerate(steady)]
    if projection.burst is not None:
        at = min(projection.burst.at_minute, projection.horizon_minutes)
        arrivals.extend((float(at), arrival) for arrival in burst)

    built: list[Order] = []
    for index, (minute, (work_units, item_count, segment)) in enumerate(arrivals):
        created_at = now + timedelta(minutes=minute)
        built.append(
            Order(
                order_id=f"{SYNTHETIC_PREFIX}{index:05d}",
                facility_id=facility_id,
                created_at=created_at,
                promised_dispatch_at=created_at + timedelta(hours=dispatch_promise_hours),
                item_count=item_count,
                work_units=work_units,
                order_value=Decimal(0),
                status=OrderStatus.PENDING,
                segment=segment,
            )
        )
    return built


def _draw_arrivals(
    target_work_units: float, rules: WorkUnitRules, rng: random.Random
) -> list[tuple[float, int, str | None]]:
    """Draw (work_units, item_count, segment) triples until the target is reached.

    Each arrival is drawn as the shape a real order would carry
    (`arrival_shapes`), then classified through the same `classify()` a real
    order goes through -- so a PROJ- order's size comes from the same
    function real orders are priced by, not a separate arrival_weight-only
    distribution. Segment is drawn independently from `segment_mix`, mirroring
    simulator/commerce.py's SEGMENTS, so a segment-targeted lever
    (PROTECT_SEGMENT, or a segment-scoped DEFER_LOW_STAKES) has a cohort to
    act on among projected arrivals, not just real ones.
    """
    if target_work_units <= 0:
        return []

    shapes = rules.arrival_shapes
    shape_weights = [shape.weight for shape in shapes]
    segments = rules.segment_mix
    segment_weights = [sample.weight for sample in segments]

    drawn: list[tuple[float, int, str | None]] = []
    accumulated = 0.0
    while accumulated < target_work_units:
        shape = rng.choices(shapes, weights=shape_weights, k=1)[0]
        work_units = classify(shape.line_count, shape.unit_count, shape.special_handling, rules)
        segment = rng.choices(segments, weights=segment_weights, k=1)[0].segment
        drawn.append((work_units, shape.unit_count, segment))
        accumulated += work_units
    return drawn


def synthetic_ids(orders: list[Order]) -> frozenset[str]:
    """Return the ids that were projected rather than placed."""
    return frozenset(o.order_id for o in orders if o.order_id.startswith(SYNTHETIC_PREFIX))
