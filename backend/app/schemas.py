"""Request and response schemas for backend APIs."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models import (
    ActionStatus,
    OrderStatus,
    SLAStatus,
    SurgeRiskLevel,
    reject_control_characters,
    require_at_sign,
)


class PriorityBreakdownResponse(BaseModel):
    """The arithmetic behind one order's place in the queue."""

    sla_urgency: float
    aging_bonus: float
    workload_penalty: float
    adjustment: float


class OrderResponse(BaseModel):
    order_id: str
    facility_id: str
    created_at: datetime
    promised_dispatch_at: datetime
    predicted_dispatch_at: datetime | None
    item_count: int
    work_units: float
    order_value: Decimal
    status: OrderStatus
    segment: str | None = None
    # Set only where a promise lever has moved this order's deadline; the value
    # is what the customer was originally told.
    original_promised_dispatch_at: datetime | None = None
    sla_status: SLAStatus | None
    priority_score: float | None
    queue_position: int | None
    # Everything below explains the status above, so an operator asking "why is
    # this order AT_RISK?" gets numbers rather than a verdict. Null for orders
    # the scheduler does not reorder.
    priority_breakdown: PriorityBreakdownResponse | None = None
    work_units_ahead: float | None = None
    throughput_assumed: float | None = None
    # When the floor finishes the work, which is earlier than dispatch whenever
    # the order misses a carrier collection and waits for the next one.
    work_complete_at: datetime | None = None


class OrdersResponse(BaseModel):
    items: list[OrderResponse]
    total: int


class OrderStateCounts(BaseModel):
    received: int
    pending: int
    picking: int
    packed: int
    ready: int
    dispatched: int


class SLACounts(BaseModel):
    safe: int
    watch: int
    at_risk: int
    breached: int


class DashboardResponse(BaseModel):
    facility_id: str
    generated_at: datetime
    demand_work_units_per_hour: float
    fulfillment_work_units_per_hour: float
    # Where the throughput figure came from: "derived" from status history,
    # "reported" by a producer, or "configured" when nothing was measured.
    throughput_source: str = "configured"
    # Work is waiting and the open floor is not moving it. Throughput then
    # reads zero and risk is at least HIGH.
    throughput_stalled: bool = False
    backlog_orders: int
    backlog_work_units: float
    dispatch_promise_hours: float
    risk_level: SurgeRiskLevel
    order_states: OrderStateCounts
    sla_counts: SLACounts
    # Demand minus throughput: positive means the backlog is still growing.
    # Names the gap the dashboard already displays rather than deriving it twice.
    backlog_growth_wu_per_hour: float
    # When the first promise actually fails, which bounds how long the operator
    # has to act. Null when nothing is predicted to breach.
    minutes_to_first_breach: float | None = None
    # What doing nothing has already cost, and what is still saveable. A
    # realised miss is a fact no recovery undoes; a preventable one is a
    # warning. Reporting them together would offer a rescue that does not
    # exist. See app/consequences.py.
    realised_breaches: int = 0
    preventable_breaches: int = 0
    # Misses over everything placed, against the published marketplace ceiling
    # the facility is measured on. This is what makes inaction costed at all.
    late_dispatch_rate: float = 0.0
    late_dispatch_threshold: float = 0.04
    late_dispatch_breaching: bool = False
    # Time to the next carrier collection. This is the operator's real decision
    # window: an intervention landing after it cannot help today's dispatch.
    # Null when the facility dispatches continuously.
    minutes_to_cutoff: float | None = None


class ArrivalBurstRequest(BaseModel):
    """A flash-sale spike to drop into the projection."""

    at_minute: int = Field(ge=0)
    work_units: float = Field(gt=0)


class SimulationRequest(BaseModel):
    capacity_per_hour: float | None = Field(default=None, gt=0)
    dispatch_promise_hours: float | None = Field(default=None, gt=0)

    # Scales projected future arrivals against the observed rate. Applied only
    # when a projection horizon is given, since without one there are no future
    # arrivals to scale.
    demand_multiplier: float | None = Field(default=None, gt=0)

    # Minutes of future demand to project at the observed arrival rate. Omit for
    # a queue-only What-If against orders already placed.
    projection_horizon_minutes: int | None = Field(default=None, gt=0, le=24 * 60)
    burst: ArrivalBurstRequest | None = None
    seed: int | None = None


class ProjectedArrivalCounts(BaseModel):
    """Exposure among orders nobody has placed yet.

    Reported apart from real orders on purpose: a plan must never be able to
    claim it rescued an order that does not exist.
    """

    orders: int
    work_units: float
    safe_count: int
    watch_count: int
    at_risk_count: int
    breached_count: int


class SimulationSummary(BaseModel):
    backlog_orders: int
    backlog_work_units: float
    # Counts below describe orders customers have actually placed.
    safe_count: int
    watch_count: int
    at_risk_count: int
    breached_count: int
    # Breaches the customer has been told about. Still breaches: counted apart
    # so a plan cannot present an apology as a rescue.
    breached_managed_count: int = 0
    # The at-risk + breached orders above, split by whether their promise has
    # already passed. Already late is lost to every plan alike; only saveable
    # exposure can differ between plans, so a card compares plans on that.
    already_late_count: int = 0
    saveable_count: int = 0
    projected_recovery_hours: float | None
    risk_level: SurgeRiskLevel
    # Present only when the caller asked for a projection horizon.
    projected_arrivals: ProjectedArrivalCounts | None = None


class SimulationResponse(BaseModel):
    generated_at: datetime
    baseline: SimulationSummary
    simulated: SimulationSummary
    applied_capacity_per_hour: float
    applied_dispatch_promise_hours: float
    applied_demand_multiplier: float
    applied_projection_horizon_minutes: int | None = None


class RecoveryPlanActions(BaseModel):
    capacity_per_hour: float
    dispatch_promise_hours: float
    demand_multiplier: float


class LeverResponse(BaseModel):
    """One intervention inside a plan, or one the operator cannot use yet."""

    id: str
    name: str
    family: str
    description: str
    cost: str
    lead_time_minutes: int
    reversible: bool
    side_effects: list[str]
    # Set when the lever exists but cannot help this decision, e.g. it arrives
    # after the carrier collection. Surfaced rather than hidden so the operator
    # learns where the decision window closes.
    unavailable_reason: str | None = None


class PlanOutcomeResponse(BaseModel):
    """What a plan prevents, versus what it merely moves.

    A queue lever cannot reduce the number of missed promises; it decides which
    orders miss. Reporting both numbers is what stops a plan claiming a rescue
    it did not perform.
    """

    breaches_avoided: int
    breaches_relocated: int
    net_breach_change: int
    orders_improved: int
    orders_worsened: int
    # True when exposure moved but did not fall. Such a plan must never be
    # described as having saved anything.
    is_redistribution: bool
    # Where this plan would leave the facility's late-dispatch rate. It is what
    # gives do-nothing a real cost to compare a HIGH-cost lever against.
    projected_late_dispatch_rate: float = 0.0


class RecoveryPlanResponse(BaseModel):
    # A catalog posture id, e.g. "buy-the-hour". An open string: postures are
    # data, so clients must not assume a fixed set.
    plan_id: str
    title: str
    description: str
    relative_cost: str
    actions: RecoveryPlanActions
    projected: SimulationSummary
    recommended: bool = False
    # What the plan trades away, in the operator's terms.
    trades: str | None = None
    families: list[str] = []
    levers: list[LeverResponse] = []
    outcome: PlanOutcomeResponse | None = None
    effective_at: datetime | None = None


class RecoveryPlansResponse(BaseModel):
    plans: list[RecoveryPlanResponse]
    # Levers filtered out of every plan, with the reason. Shown greyed rather
    # than omitted so the decision window is visible.
    unavailable_levers: list[LeverResponse] = []


class RecoveryApprovalResponse(BaseModel):
    plan_id: str
    action_id: str
    status: ActionStatus


class CapacityCommitmentResponse(BaseModel):
    """What one capacity_delta effect claimed, and whether it held up.

    Surfaced so the UI can render the sentence the Architecture section
    requires: "you approved overtime 90 minutes ago; the floor is still at
    51 wu/hr, not the 72 this plan assumed" -- UNVERIFIED and NOT_OBSERVED
    must render prominently, not as a quiet grey badge.
    """

    lever_id: str
    target_work_units_per_hour: float
    verification_status: str
    verified_at: datetime | None
    observed_work_units_per_hour: float | None


class RecoveryActionResponse(BaseModel):
    action_id: str
    plan_id: str
    facility_id: str
    status: ActionStatus
    capacity_per_hour: float
    dispatch_promise_hours: float
    demand_multiplier: float
    # The posture's typed effects. The three numbers above can express a
    # capacity change and nothing else, so an executor that only reads them
    # cannot carry out a queue reorder or a re-promise.
    effects: list[dict] = Field(default_factory=list)
    error_detail: str | None = None
    # When a human confirmed the physical change happened (P6). None until
    # the action reaches ENACTED; irrelevant, and always None, on the
    # simulator adapter path.
    enacted_at: datetime | None = None
    # One entry per capacity_delta effect this action carried (P6). Empty for
    # a QUEUE/PROMISE-only action, or before execution has completed --
    # UNVERIFIED is the meaningful un-resolved state, not an absent entry.
    capacity_commitments: list[CapacityCommitmentResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RecoveryActionApplied(BaseModel):
    """What applying an approved action actually changed in this service."""

    action_id: str
    # Empty when the action had nothing internal to do, which is the normal
    # case for a capacity-only plan.
    applied_levers: list[str] = Field(default_factory=list)


class RecoveryActionStatusRequest(BaseModel):
    status: ActionStatus
    error_detail: str | None = Field(default=None, max_length=1000)


class ConfirmEnactmentRequest(BaseModel):
    """An operator affirming whether the real-world change actually happened.

    Session-gated, not token-gated (schemas.py mirrors the router): this is
    the operator's own act, not n8n's, and mirrors RecoveryActionStatusRequest's
    shape deliberately so the two endpoints read the same to a caller.
    """

    succeeded: bool
    error_detail: str | None = Field(default=None, max_length=1000)


class AlertRecipientCreate(BaseModel):
    """Untrusted payload adding somebody to the alert list.

    `email` reuses the same two validators `SignupRequest` applies. That is
    not tidiness: this address is typed by an operator and is written
    straight into an `EmailMessage` "To" header by the dispatcher, so it is
    the identical header-injection vector, and a second, looser check here
    would quietly reopen what `reject_control_characters` exists to close.
    """

    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    # The lowest severity worth emailing this person about. Defaults to HIGH
    # so adding somebody without a considered choice does not sign them up
    # for every MEDIUM blip.
    min_level: SurgeRiskLevel = SurgeRiskLevel.HIGH

    _at_sign = field_validator("email")(require_at_sign)
    _no_control_characters = field_validator("email")(reject_control_characters)


class AlertRecipientResponse(BaseModel):
    """One person on the alert list, as the operator UI sees them."""

    id: str
    name: str
    email: str
    min_level: SurgeRiskLevel


class AlertRecipientsResponse(BaseModel):
    recipients: list[AlertRecipientResponse]


class AlertHistoryEntry(BaseModel):
    """One decided alert and what became of it.

    `status` and `last_error` are surfaced rather than left in the logs on
    purpose: an alert nobody knows failed to send is the exact failure the
    outbox exists to prevent, and "check the backend logs" does not prevent
    it.
    """

    id: str
    rule_id: str
    facility_id: str
    level: SurgeRiskLevel
    subject: str
    status: str
    attempts: int
    last_error: str | None
    decided_at: datetime
    sent_at: datetime | None
    # Null once the recipient has been removed from the list. The history is
    # kept anyway -- that is what the soft delete is for.
    recipient_name: str | None
    recipient_email: str | None


class AlertHistoryResponse(BaseModel):
    alerts: list[AlertHistoryEntry]
