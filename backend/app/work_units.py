"""Derive an order's fulfillment work units from its attributes.

Not every order costs the same effort to pick and pack, and the scheduler
reasons in work units per hour rather than orders per hour. This module is where
an order's attributes become that number.

The rules live in `catalog/work_units.json` rather than in this file: they are
configurable demo defaults today and calibrated from historical pick/pack times
later (docs/10 section 2), so retuning them must not require touching code.

Classification happens backend-side by design. A provider tells us what is in
the order; it does not get to tell us how much work that is. Keeping that
judgement here is what makes the core provider-agnostic rather than hostage to
whatever a Shopify app or a 3PL feed happens to put in a field.
"""

import json
from functools import lru_cache
from math import ceil, isclose
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import ORDER_LIFECYCLE

CATALOG_PATH = Path(__file__).parent / "catalog" / "work_units.json"


class WorkUnitBand(BaseModel):
    """One complexity band and the work units it costs."""

    model_config = ConfigDict(frozen=True)

    name: str
    work_units: float = Field(gt=0)
    # None marks the open-ended top band.
    max_effective_lines: int | None = None


class ArrivalShape(BaseModel):
    """One order shape a projected arrival can be drawn as.

    Mirrors simulator/commerce.py's ORDER_SHAPES: a projected arrival is
    drawn as the attributes a real order would carry (line_count, unit_count,
    special_handling), then classified through the same `classify()` a real
    order goes through, rather than a work-units figure invented separately.
    """

    model_config = ConfigDict(frozen=True)

    line_count: int = Field(gt=0)
    unit_count: int = Field(gt=0)
    special_handling: bool = False
    # Share of arrivals drawn as this shape. Classification itself never
    # consults it; only projection does.
    weight: float = Field(gt=0)


class SegmentSample(BaseModel):
    """One customer cohort a projected arrival can be drawn as.

    Mirrors simulator/commerce.py's SEGMENTS, `None` bucket included: a
    projection where every arrival has a segment is its own fidelity bug,
    since real orders are also frequently segmentless.
    """

    model_config = ConfigDict(frozen=True)

    segment: str | None = None
    weight: float = Field(gt=0)


class WorkUnitRules(BaseModel):
    """The full classification ruleset, loaded from the catalog."""

    model_config = ConfigDict(frozen=True)

    units_per_line_equivalent: int = Field(gt=0)
    special_handling_surcharge: float = Field(ge=0)
    max_work_units: float = Field(gt=0)
    bands: tuple[WorkUnitBand, ...] = Field(min_length=1)
    # Share of an order's total work each lifecycle step costs, in
    # ORDER_LIFECYCLE order (PENDING->PICKING, PICKING->PACKED, ...). Backs
    # both the per-step charge recorded in order_status_transitions and
    # OrderTotals.committed_work_units's remaining-fraction charge, so the
    # two stay consistent with each other by construction.
    stage_weights: tuple[float, ...] = Field(min_length=1)
    # What app/projection.py draws PROJ- arrivals from, so a projected order
    # carries the same shape of attributes a real order would (docs/07).
    arrival_shapes: tuple[ArrivalShape, ...] = Field(min_length=1)
    segment_mix: tuple[SegmentSample, ...] = Field(min_length=1)

    def band_for(self, effective_lines: int) -> WorkUnitBand:
        """Return the first band wide enough to hold this order."""
        for band in self.bands:
            if band.max_effective_lines is None or effective_lines <= band.max_effective_lines:
                return band
        return self.bands[-1]

    @model_validator(mode="after")
    def _check_stage_weights(self) -> "WorkUnitRules":
        """Require one weight per lifecycle transition, accounting for the whole order."""
        expected = len(ORDER_LIFECYCLE) - 1
        if len(self.stage_weights) != expected:
            raise ValueError(
                f"stage_weights must have exactly {expected} entries "
                f"(one per ORDER_LIFECYCLE transition), got {len(self.stage_weights)}"
            )
        total = sum(self.stage_weights)
        if not isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"stage_weights must sum to 1.0, got {total}")
        return self


@lru_cache(maxsize=1)
def load_rules() -> WorkUnitRules:
    """Load and validate the classification rules once per process."""
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return WorkUnitRules.model_validate(raw)


def effective_lines(line_count: int, unit_count: int, rules: WorkUnitRules) -> int:
    """Return the line count this order behaves like on the floor.

    A single line of forty units is not a one-touch pick, so bulk quantity is
    folded back into an equivalent number of lines rather than ignored.
    """
    from_units = ceil(unit_count / rules.units_per_line_equivalent)
    return max(line_count, from_units, 1)


def classify(
    line_count: int,
    unit_count: int,
    special_handling: bool = False,
    rules: WorkUnitRules | None = None,
) -> float:
    """Return the work units one order costs to fulfil.

    Args:
        line_count: Distinct order lines.
        unit_count: Total units across those lines.
        special_handling: Fragile, gift-wrapped, personalised or similar.
        rules: Override ruleset; defaults to the loaded catalog.

    Returns:
        Work units, capped at the ruleset's maximum.

    Raises:
        ValueError: If the counts are not positive.
    """
    if line_count <= 0 or unit_count <= 0:
        raise ValueError("line_count and unit_count must both be > 0")

    rules = rules or load_rules()
    band = rules.band_for(effective_lines(line_count, unit_count, rules))

    work_units = band.work_units
    if special_handling:
        work_units += rules.special_handling_surcharge

    return min(work_units, rules.max_work_units)


def classify_order_event(
    line_count: int | None,
    unit_count: int | None,
    special_handling: bool | None,
    declared_work_units: float,
) -> float:
    """Choose the work units to persist for an inbound order event.

    Classification wins whenever the producer sent enough to classify from.
    Without those attributes there is nothing to derive from, so the declared
    value stands -- that fallback is what lets a producer adopt the attributes
    later without a flag day.

    Args:
        line_count: Distinct order lines, if the producer sent them.
        unit_count: Total units, if the producer sent them.
        special_handling: Special-handling flag, if the producer sent it.
        declared_work_units: The producer's own figure.

    Returns:
        Work units to persist.
    """
    if line_count is None or unit_count is None:
        return declared_work_units
    return classify(line_count, unit_count, bool(special_handling))


def work_units_for_step(
    order_work_units: float,
    from_position: int,
    to_position: int,
    rules: WorkUnitRules | None = None,
) -> float:
    """Return the work one status transition, or a run of skipped ones, accounts for.

    Real effort is not evenly spread across pick, pack, stage and dispatch, so
    this charges each covered step its catalog weight rather than an equal
    split. A forward jump (a retried or reordered delivery skipping a status)
    is charged the sum of every step it covers, matching the caller's existing
    "steps_covered" semantics.

    Args:
        order_work_units: The order's total cost.
        from_position: Lifecycle index the order started this update at.
        to_position: Lifecycle index the order ends this update at.
        rules: Override ruleset; defaults to the loaded catalog.

    Returns:
        Work units this transition (or run of transitions) accounts for.
    """
    rules = rules or load_rules()
    return order_work_units * sum(rules.stage_weights[from_position:to_position])


def remaining_work_fraction(position: int, rules: WorkUnitRules | None = None) -> float:
    """Return the fraction of an order's total work not yet done at this lifecycle position.

    Args:
        position: The order's current index in ORDER_LIFECYCLE (0 for PENDING).
        rules: Override ruleset; defaults to the loaded catalog.

    Returns:
        1.0 at PENDING, shrinking to 0.0 once every step is done.
    """
    rules = rules or load_rules()
    return sum(rules.stage_weights[position:])
