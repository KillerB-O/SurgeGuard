"""Evaluate the alert rules against live facility state and queue what fires.

Reads the SAME `compute_schedule` the dashboard renders, rather than a
parallel calculation of its own. That is deliberate and load-bearing: an
alert that disagreed with the screen an operator is looking at would destroy
the credibility the alert exists to trade on.

Nothing here sends email. Rules that fire become PENDING rows in
`alert_outbox`, committed with the rest of the evaluation, and
`app/alerts/dispatcher.py` turns them into mail afterwards.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from app import repository as core_repository
from app.alerts import repository
from app.alerts.rules import AlertMetrics, level_rank, load_rules, should_fire
from app.clock import now as clock_now
from app.interventions import active_priority_adjustments
from app.models import SurgeRiskLevel
from app.repository import FacilityNotFoundError
from app.scheduler import (
    classify_facility_risk,
    compute_schedule,
    cutoff_policy,
    operating_calendar,
    risk_thresholds,
    sla_thresholds,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationResult:
    """What one evaluation pass decided."""

    rules_fired: tuple[str, ...] = ()
    alerts_queued: int = 0


async def collect_metrics(conn: AsyncConnection, facility_id: str) -> AlertMetrics:
    """Assemble the live numbers the rules are evaluated against.

    Mirrors `routers/reads.get_dashboard` step for step, using the same
    repository calls and the same scheduler, so the two cannot disagree.

    Args:
        conn: Request-scoped transaction.
        facility_id: Facility to evaluate.

    Returns:
        The metrics snapshot.

    Raises:
        FacilityNotFoundError: No such facility.
    """
    now = await clock_now(conn)
    facility = await core_repository.get_facility(conn, facility_id)
    throughput = await core_repository.get_throughput_signal(
        conn, facility_id, facility["capacity_per_hour"], now, operating_calendar(facility)
    )
    totals = await core_repository.get_order_totals(conn, facility_id)
    pending = await core_repository.list_pending_orders(conn, facility_id)
    schedule = compute_schedule(
        pending,
        throughput.scheduling_capacity_per_hour,
        now,
        committed_work_units=totals.committed_work_units,
        dispatch_cutoff=cutoff_policy(facility),
        operating_calendar=operating_calendar(facility),
        sla_thresholds=sla_thresholds(facility),
        priority_adjustments=await active_priority_adjustments(conn, facility_id),
    )
    demand = await core_repository.get_demand_work_units_per_hour(conn, facility_id, now)

    last_snapshot = await core_repository.get_latest_snapshot_at(conn, facility_id, now)
    snapshot_age_minutes = (
        None if last_snapshot is None else (now - last_snapshot).total_seconds() / 60.0
    )

    return AlertMetrics(
        facility_id=facility_id,
        risk_level=classify_facility_risk(
            schedule, risk_thresholds(facility), stalled=throughput.stalled
        ),
        pending_orders=schedule.pending_orders,
        at_risk_count=schedule.at_risk_count,
        breached_count=schedule.breached_count,
        backlog_growth_wu_per_hour=demand - throughput.reported_work_units_per_hour,
        minutes_to_first_breach=schedule.minutes_to_first_breach,
        snapshot_age_minutes=snapshot_age_minutes,
    )


async def evaluate_facility(conn: AsyncConnection, facility_id: str) -> EvaluationResult:
    """Run every rule against one facility and queue the alerts that fire.

    Recipients are filtered by their own `min_level`: the operational head can
    take CRITICAL only while a floor supervisor takes HIGH and up, so one
    firing does not email everybody indiscriminately.

    A rule that fires with NO matching recipient writes nothing at all. That
    is deliberate -- an outbox row with no addressee would sit PENDING
    forever, and, because the outbox is also the cooldown state, it would
    silently suppress the rule for the whole cooldown window afterwards.

    Args:
        conn: Transaction shared by the whole pass, so a facility queues all
            of its alerts or none.
        facility_id: Facility to evaluate.

    Returns:
        Which rules fired and how many alerts were queued.
    """
    try:
        metrics = await collect_metrics(conn, facility_id)
    except FacilityNotFoundError:
        logger.warning("alert evaluation skipped: unknown facility %s", facility_id)
        return EvaluationResult()

    last_by_rule = await repository.last_alert_per_rule(conn, facility_id)
    recipients = await repository.list_recipients(conn)
    now = await clock_now(conn)

    fired: list[str] = []
    queued = 0

    for rule in load_rules().rules:
        if not should_fire(rule, metrics, last_by_rule.get(rule.id), now):
            continue

        addressees = [
            recipient
            for recipient in recipients
            if level_rank(rule.level)
            >= level_rank(SurgeRiskLevel(recipient["min_level"]))
        ]
        if not addressees:
            logger.info(
                "rule %s matched at %s but nobody is signed up at %s or below",
                rule.id,
                facility_id,
                rule.level.value,
            )
            continue

        subject, body = rule.render(metrics)
        for recipient in addressees:
            await repository.queue_alert(
                conn,
                rule_id=rule.id,
                facility_id=facility_id,
                recipient_id=recipient["id"],
                level=rule.level,
                subject=subject,
                body=body,
                decided_at=now,
            )
            queued += 1
        fired.append(rule.id)

    if fired:
        logger.info(
            "queued %d alerts at %s for rules: %s",
            queued,
            facility_id,
            ", ".join(fired),
        )
    return EvaluationResult(rules_fired=tuple(fired), alerts_queued=queued)
