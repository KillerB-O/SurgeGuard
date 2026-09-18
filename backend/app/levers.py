"""Recovery levers, modelled as data rather than branches.

The product's honest core is that the four lever families are not equivalent:

    QUEUE       changes who misses, never how many. Free, instant, zero-sum.
    THROUGHPUT  reduces misses. Costs money, and lands after a lead time.
    INFLOW      reduces misses among future arrivals only. Costs revenue.
    PROMISE     reduces breaches without touching the work. Costs goodwill.

Against a 127 vs 52 work-unit gap no amount of resequencing rescues anyone in
aggregate -- it only decides whether the orders that miss are the cheap ones or
the loyal ones. Encoding that distinction, rather than presenting every lever as
a "fix", is what keeps the product honest.

Each lever declares its effect as a typed modification to scheduler inputs, so
applying one is data, not an `if`. The catalog lives in `catalog/levers.json`;
an operator can add or retune a lever there without a deploy.
"""

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CATALOG_PATH = Path(__file__).parent / "catalog" / "levers.json"


class LeverFamily(StrEnum):
    QUEUE = "QUEUE"
    THROUGHPUT = "THROUGHPUT"
    INFLOW = "INFLOW"
    PROMISE = "PROMISE"


class CostBand(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --- Effects: typed modifications to scheduler inputs ----------------------


class CapacityDelta(BaseModel):
    """Add throughput. The only effect that creates capacity."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["capacity_delta"]
    work_units_per_hour: float


class WorkUnitMultiplier(BaseModel):
    """Scale the work each pending order carries, e.g. by dropping gift wrap."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["work_unit_multiplier"]
    factor: float = Field(gt=0)


class PromiseShift(BaseModel):
    """Move the commitment rather than the work."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["promise_shift"]
    hours: float
    segment: str | None = None


class PriorityAdjustment(BaseModel):
    """Reorder the queue. Zero-sum by construction: it moves work, not clears it."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["priority_adjustment"]
    points: float
    segment: str | None = None
    # Applies only to orders with at least this much slack, which is how
    # "defer what can afford to wait" is expressed without naming orders.
    min_slack_hours: float | None = None


class InflowReduction(BaseModel):
    """Reduce what arrives next. Cannot help anything already queued."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["inflow_reduction"]
    factor: float = Field(gt=0, le=1)


class CutoffShift(BaseModel):
    """Move the carrier collection later."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["cutoff_shift"]
    minutes: float


class ManageBreach(BaseModel):
    """Convert an unavoidable breach into an acknowledged one.

    Explicitly not a rescue: the order is still late.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["manage_breach"]
    segment: str | None = None


Effect = Annotated[
    CapacityDelta
    | WorkUnitMultiplier
    | PromiseShift
    | PriorityAdjustment
    | InflowReduction
    | CutoffShift
    | ManageBreach,
    Field(discriminator="kind"),
]


class Preconditions(BaseModel):
    """What must be true for a lever to be applicable at all."""

    model_config = ConfigDict(frozen=True)

    # Needs a configured carrier collection to move.
    requires_cutoff: bool = False
    # Needs at least one pending order in the named cohort.
    requires_segment: str | None = None
    # Acts on future arrivals, so needs a projection horizon to act on.
    requires_projection: bool = False


class Lever(BaseModel):
    """One intervention an operator could take."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    family: LeverFamily
    description: str
    effect: Effect
    # Minutes before the intervention actually changes anything on the floor. A
    # lever that lands after the collection rescues nobody, and the engine has
    # to say so rather than quietly offering it.
    lead_time_minutes: int = Field(ge=0)
    cost: CostBand
    reversible: bool
    side_effects: tuple[str, ...] = ()
    n8n_action: str | None = None
    preconditions: Preconditions = Preconditions()


class Posture(BaseModel):
    """A named combination of levers, described by what it trades away."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    trades: str
    lever_ids: tuple[str, ...] = Field(alias="levers")


class Catalog(BaseModel):
    """The full lever catalog and the postures composed from it."""

    model_config = ConfigDict(frozen=True)

    levers: tuple[Lever, ...]
    postures: tuple[Posture, ...]

    def lever(self, lever_id: str) -> Lever:
        """Return one lever by id."""
        for lever in self.levers:
            if lever.id == lever_id:
                return lever
        raise KeyError(lever_id)


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    """Load and validate the lever catalog once per process."""
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return Catalog.model_validate(raw)
