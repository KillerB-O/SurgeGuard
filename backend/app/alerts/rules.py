"""Alert rules as data, and the pure decision about whether one fires.

Mirrors `app/levers.py`: the catalog lives in `catalog/alert_rules.json` and
an operator retunes a threshold there without a deploy.

Nothing in this module touches the database or the clock. `should_fire` takes
the metrics, the last alert, and the current instant as arguments, which is
what makes the interesting behaviour -- cooldown, escalation -- testable
without a database, the same way `classify_facility_risk` is.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models import SurgeRiskLevel

CATALOG_PATH = Path(__file__).parent.parent / "catalog" / "alert_rules.json"

# Severity order, so "at_or_above" and "has it escalated?" are answerable.
# SurgeRiskLevel is a StrEnum, and comparing StrEnum members compares their
# text -- "CRITICAL" < "LOW" alphabetically, which is exactly backwards. This
# tuple is the only ordering that may be used.
LEVEL_ORDER: tuple[SurgeRiskLevel, ...] = (
    SurgeRiskLevel.LOW,
    SurgeRiskLevel.MEDIUM,
    SurgeRiskLevel.HIGH,
    SurgeRiskLevel.CRITICAL,
)


def level_rank(level: SurgeRiskLevel) -> int:
    """Return a level's severity rank, low to high."""
    return LEVEL_ORDER.index(level)


@dataclass(frozen=True)
class AlertMetrics:
    """The live numbers a rule is evaluated against.

    Read off the same `compute_schedule` the dashboard renders, so an alert
    can never claim something the operator's screen disagrees with.
    """

    facility_id: str
    risk_level: SurgeRiskLevel
    pending_orders: int
    at_risk_count: int
    breached_count: int
    backlog_growth_wu_per_hour: float
    # None when nothing in the queue is predicted to breach. A rule watching
    # this metric must not fire on "no value" -- see `matches`.
    minutes_to_first_breach: float | None = None
    # How long since the floor last reported, in SIMULATED minutes. None when
    # the facility has never reported at all, which is a different condition
    # from having gone quiet and deliberately does not alert -- otherwise
    # every fresh database and every demo reset would fire immediately.
    #
    # This is the metric a request-path evaluator could never produce: it
    # measures the ABSENCE of events, and nothing arriving means nothing to
    # evaluate on. It matters because `get_throughput_signal` falls back to
    # the facility's configured capacity once telemetry goes stale, so the
    # risk level actively UNDERSTATES a stalled floor -- see that function's
    # docstring. Staleness is the only signal that contradicts it.
    snapshot_age_minutes: float | None = None

    def as_template_values(self) -> dict:
        """Return the values a rule's subject and body may interpolate."""
        return {
            "facility_id": self.facility_id,
            "risk_level": self.risk_level.value,
            "pending_orders": self.pending_orders,
            "at_risk_count": self.at_risk_count,
            "breached_count": self.breached_count,
            "backlog_growth_wu_per_hour": self.backlog_growth_wu_per_hour,
            "minutes_to_first_breach": self.minutes_to_first_breach,
            "snapshot_age_minutes": self.snapshot_age_minutes,
        }


@dataclass(frozen=True)
class LastAlert:
    """The most recent alert already decided for one rule at one facility."""

    level: SurgeRiskLevel
    decided_at: datetime


class AlertRule(BaseModel):
    """One condition worth emailing somebody about."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    metric: Literal[
        "risk_level",
        "pending_orders",
        "at_risk_count",
        "breached_count",
        "backlog_growth_wu_per_hour",
        "minutes_to_first_breach",
        "snapshot_age_minutes",
    ]
    operator: Literal["at_or_above", "gte", "gt", "lte", "lt"]
    threshold: float | str
    level: SurgeRiskLevel
    cooldown_minutes: float
    subject: str
    body: str

    def matches(self, metrics: AlertMetrics) -> bool:
        """Return whether the live numbers satisfy this rule.

        A `None` metric never matches. `minutes_to_first_breach` is None when
        nothing is predicted to breach at all, and a naive `None <= 45` would
        either raise or -- worse, if it were coerced to zero -- fire the
        "decision window closing" rule precisely when the queue is healthiest.
        """
        value = getattr(metrics, self.metric)
        if value is None:
            return False

        if self.operator == "at_or_above":
            return level_rank(value) >= level_rank(SurgeRiskLevel(self.threshold))

        threshold = float(self.threshold)
        if self.operator == "gte":
            return value >= threshold
        if self.operator == "gt":
            return value > threshold
        if self.operator == "lte":
            return value <= threshold
        return value < threshold

    def render(self, metrics: AlertMetrics) -> tuple[str, str]:
        """Return this rule's subject and body, filled in from the metrics."""
        values = metrics.as_template_values()
        return self.subject.format(**values), self.body.format(**values)


class RuleCatalog(BaseModel):
    """Every configured alert rule."""

    model_config = ConfigDict(frozen=True)

    rules: tuple[AlertRule, ...]


@lru_cache(maxsize=1)
def load_rules() -> RuleCatalog:
    """Load and validate the alert rule catalog.

    Cached like `load_catalog`, because a request path must not read a file
    per call. The worker loop calls `load_rules.cache_clear()` on each tick
    instead: a loop can afford one file read per tick, and without it "an
    operator retunes a threshold without a deploy" would still quietly mean
    "and then restarts the process".
    """
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return RuleCatalog.model_validate(raw)


def should_fire(
    rule: AlertRule, metrics: AlertMetrics, last: LastAlert | None, now: datetime
) -> bool:
    """Decide whether `rule` should raise an alert right now.

    Three conditions, in order:

    1. The rule must match the live numbers. Nothing else matters if it does
       not.
    2. If it has never fired here, it fires.
    3. Otherwise it fires only when the cooldown has elapsed -- OR when the
       situation has escalated above the level last alerted on.

    The cooldown is what stops a rule that stays true for hours from emailing
    every recipient on every tick; it is the spam guard and the budget guard
    at once. The escalation bypass is what stops the cooldown becoming a gag:
    sitting out a 30-minute silence while MEDIUM turns into CRITICAL is the
    one quiet period that would actually cost an operator something.

    No separate "edge" test is needed beyond this. A rule that oscillates --
    HIGH, CRITICAL, HIGH -- stays continuously matched, so the cooldown alone
    holds it to one alert per window, and only a genuine escalation gets
    through early.

    Args:
        rule: Rule under evaluation.
        metrics: Live numbers from the current schedule.
        last: Most recent alert for this rule at this facility, if any.
        now: Simulated time, on the same clock as `last.decided_at`.

    Returns:
        True if an alert should be raised.
    """
    if not rule.matches(metrics):
        return False
    if last is None:
        return True
    if level_rank(rule.level) > level_rank(last.level):
        return True
    return now - last.decided_at >= timedelta(minutes=rule.cooldown_minutes)
