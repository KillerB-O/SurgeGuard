"""Alert rule matching and the fire/don't-fire decision.

No database and no clock: `should_fire` takes the metrics, the last alert and
the instant as arguments precisely so the behaviour that matters -- cooldown,
escalation, and the None-metric guard -- can be pinned without one.
"""

from datetime import UTC, datetime, timedelta

from app.alerts.rules import (
    AlertMetrics,
    AlertRule,
    LastAlert,
    level_rank,
    load_rules,
    should_fire,
)
from app.models import SurgeRiskLevel

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _metrics(**overrides) -> AlertMetrics:
    """Build metrics for a facility in trouble, overridable per test."""
    base = {
        "facility_id": "WH-01",
        "risk_level": SurgeRiskLevel.HIGH,
        "pending_orders": 40,
        "at_risk_count": 12,
        "breached_count": 0,
        "backlog_growth_wu_per_hour": 25.0,
        "minutes_to_first_breach": 30.0,
        "snapshot_age_minutes": 1.0,
    }
    return AlertMetrics(**{**base, **overrides})


def _rule(**overrides) -> AlertRule:
    """Build a rule watching at_risk_count, overridable per test."""
    base = {
        "id": "test-rule",
        "name": "Test rule",
        "metric": "at_risk_count",
        "operator": "gte",
        "threshold": 10,
        "level": SurgeRiskLevel.HIGH,
        "cooldown_minutes": 30,
        "subject": "s",
        "body": "b",
    }
    return AlertRule(**{**base, **overrides})


def test_level_order_is_severity_not_alphabetical():
    """SurgeRiskLevel is a StrEnum, so `<` compares text and puts CRITICAL
    below LOW. Everything ordinal here depends on that NOT being used."""
    assert level_rank(SurgeRiskLevel.CRITICAL) > level_rank(SurgeRiskLevel.LOW)
    assert SurgeRiskLevel.CRITICAL < SurgeRiskLevel.LOW  # the trap itself


def test_a_missing_metric_never_matches():
    """`minutes_to_first_breach` is None when nothing is predicted to breach.
    Coerced to zero it would fire the decision-window rule exactly when the
    queue is healthiest."""
    rule = _rule(metric="minutes_to_first_breach", operator="lte", threshold=45)

    assert not rule.matches(_metrics(minutes_to_first_breach=None))


def test_at_or_above_compares_severity():
    rule = _rule(metric="risk_level", operator="at_or_above", threshold="HIGH")

    assert rule.matches(_metrics(risk_level=SurgeRiskLevel.CRITICAL))
    assert rule.matches(_metrics(risk_level=SurgeRiskLevel.HIGH))
    assert not rule.matches(_metrics(risk_level=SurgeRiskLevel.MEDIUM))


def test_a_rule_that_does_not_match_never_fires():
    assert not should_fire(_rule(), _metrics(at_risk_count=0), None, NOW)


def test_a_rule_fires_the_first_time_it_matches():
    assert should_fire(_rule(), _metrics(), None, NOW)


def test_a_matching_rule_stays_quiet_inside_its_cooldown():
    """The spam guard, and the budget guard: each firing costs one email per
    recipient against a 300/day allowance shared with signup mail."""
    last = LastAlert(level=SurgeRiskLevel.HIGH, decided_at=NOW - timedelta(minutes=10))

    assert not should_fire(_rule(cooldown_minutes=30), _metrics(), last, NOW)


def test_a_matching_rule_fires_again_once_the_cooldown_elapses():
    last = LastAlert(level=SurgeRiskLevel.HIGH, decided_at=NOW - timedelta(minutes=31))

    assert should_fire(_rule(cooldown_minutes=30), _metrics(), last, NOW)


def test_escalation_bypasses_the_cooldown():
    """Sitting out a silence while MEDIUM becomes CRITICAL is the one quiet
    period that would actually cost an operator something."""
    last = LastAlert(level=SurgeRiskLevel.MEDIUM, decided_at=NOW - timedelta(minutes=1))
    rule = _rule(level=SurgeRiskLevel.CRITICAL, cooldown_minutes=60)

    assert should_fire(rule, _metrics(), last, NOW)


def test_oscillation_does_not_re_fire_within_the_cooldown():
    """A queue near a ratio boundary flips HIGH/CRITICAL/HIGH. Dropping back
    to an already-alerted level is not an escalation."""
    last = LastAlert(
        level=SurgeRiskLevel.CRITICAL, decided_at=NOW - timedelta(minutes=5)
    )
    rule = _rule(level=SurgeRiskLevel.HIGH, cooldown_minutes=30)

    assert not should_fire(rule, _metrics(), last, NOW)


def test_shipped_catalog_loads_and_every_template_renders():
    """A KeyError in a subject would surface as a crashed evaluator tick, with
    the alert never sent and nothing to say why."""
    metrics = _metrics(breached_count=3, risk_level=SurgeRiskLevel.CRITICAL)

    for rule in load_rules().rules:
        subject, body = rule.render(metrics)

        assert subject and body


def test_a_facility_that_never_reported_does_not_look_stale():
    """None means "has never reported", which is not the same as "went
    quiet". Firing on it would alert on every fresh database and every demo
    reset, training an operator to ignore the rule that matters most."""
    rule = _rule(metric="snapshot_age_minutes", operator="gte", threshold=20)

    assert not rule.matches(_metrics(snapshot_age_minutes=None))


def test_staleness_fires_once_telemetry_stops():
    """The one condition a request-path evaluator could never detect: it
    measures the ABSENCE of events, so nothing arriving means nothing to run
    the check."""
    rule = _rule(metric="snapshot_age_minutes", operator="gte", threshold=20)

    assert rule.matches(_metrics(snapshot_age_minutes=35.0))
    assert not rule.matches(_metrics(snapshot_age_minutes=5.0))


def test_the_staleness_rule_has_a_long_cooldown():
    """A simulator left off overnight would otherwise email the whole list
    every few minutes, and the second reminder says nothing the first did
    not."""
    stale = next(r for r in load_rules().rules if r.id == "telemetry-gone-quiet")
    others = [r for r in load_rules().rules if r.id != "telemetry-gone-quiet"]

    assert stale.cooldown_minutes >= max(r.cooldown_minutes for r in others) * 2
