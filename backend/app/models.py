"""Canonical domain, scheduler, event, and action-state models.

These types define the vocabulary shared by routes, persistence, and simulators.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PENDING = "PENDING"
    PICKING = "PICKING"
    PACKED = "PACKED"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    CANCELLED = "CANCELLED"
    DELAYED = "DELAYED"


class SLAStatus(StrEnum):
    SAFE = "SAFE"
    WATCH = "WATCH"
    AT_RISK = "AT_RISK"
    BREACHED = "BREACHED"
    # A breach the customer has been told about and compensated for. Never
    # produced by the scheduler -- it cannot be derived from slack -- and never
    # returned by a live read. It exists only in a plan projection, where a
    # promise lever converts an unavoidable miss into a managed one. The order
    # is still late; this is not a rescue.
    BREACHED_MANAGED = "BREACHED_MANAGED"


class EventType(StrEnum):
    ORDER_CREATED = "order_created"
    FULFILLMENT_SNAPSHOT = "fulfillment_snapshot"
    ORDER_STATUS_UPDATED = "order_status_updated"
    # A pre-existing order, imported once at onboarding rather than lived
    # through as a stream of events (P7). See OrderBackfillEvent.
    ORDER_BACKFILLED = "order_backfilled"
    # A known order's content or promise changed after creation -- Shopify's
    # orders/updated, not covered by park-and-replay (that handles an
    # *unknown* order; this is a *known* order, new revision) (P8). See
    # OrderRevisedEvent.
    ORDER_REVISED = "order_revised"


class SurgeRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    # A throughput-family action on a manual/webhook facility: born here
    # instead of PENDING, so n8n's poll (which defaults to status=PENDING)
    # never sees it (P6). Blocks a second approval, same as PENDING.
    AWAITING_ENACTMENT = "AWAITING_ENACTMENT"
    # A human confirmed the physical change happened. Terminal, parallel to
    # SUCCESS -- the execution *process* is complete either way -- and
    # deliberately not collapsed into SUCCESS: that would recreate the same
    # "SUCCESS is not proof" conflation already warned against, just for a
    # human's confirm click instead of n8n's callback. Does NOT block a new
    # approval: only observation (verification) is outstanding, tracked
    # separately on capacity_commitments (P6).
    ENACTED = "ENACTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class IngestionStatus(StrEnum):
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    # A status event for an order that has not arrived yet. Parked rather
    # than dropped; replayed in occurred_at order once the order exists.
    BUFFERED = "BUFFERED"


# Canonical MVP order lifecycle. Exception states are in the
# enum but have no defined transition semantics, so they stay out of this chain
# until they are explicitly implemented and tested.
ORDER_LIFECYCLE: tuple[OrderStatus, ...] = (
    OrderStatus.PENDING,
    OrderStatus.PICKING,
    OrderStatus.PACKED,
    OrderStatus.READY,
    OrderStatus.DISPATCHED,
)


def lifecycle_position(status: OrderStatus) -> int | None:
    """Return a status's index in the canonical lifecycle.

    Args:
        status: Operational status to locate.

    Returns:
        The zero-based position, or `None` for a status outside the lifecycle.
    """
    try:
        return ORDER_LIFECYCLE.index(status)
    except ValueError:
        return None


def _require_timezone_aware(value: datetime) -> datetime:
    """Reject naive timestamps at the boundary.

    A naive value is not wrong-looking, it is ambiguous: PostgreSQL would
    silently interpret it in the session timezone, so an order's deadline could
    shift by hours with nothing in the response to show it.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _require_promise_after_creation(created_at: datetime, promised_dispatch_at: datetime) -> None:
    """Require the promised dispatch time to follow creation time.

    This mirrors the database constraint and also protects in-memory simulations.
    """
    if promised_dispatch_at <= created_at:
        raise ValueError("promised_dispatch_at must be after created_at")


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    facility_id: str
    created_at: datetime
    promised_dispatch_at: datetime
    item_count: int = Field(gt=0)
    work_units: float = Field(gt=0)
    order_value: Decimal = Field(ge=0)
    status: OrderStatus = OrderStatus.PENDING
    # What a queue lever targets when it protects or defers a cohort.
    segment: str | None = None
    # The deadline this order was first given, kept when a promise lever moves
    # it. Re-promising a placed order is only defensible if the original
    # survives, so this is a fact about the order rather than a prediction.
    original_promised_dispatch_at: datetime | None = None
    # When the order actually left, on the same clock as its promise. Null
    # until it is dispatched. See migration 0009 for why `updated_at` cannot
    # stand in for this.
    dispatched_at: datetime | None = None

    _aware_timestamps = field_validator("created_at", "promised_dispatch_at")(
        _require_timezone_aware
    )

    @model_validator(mode="after")
    def _check_promise_after_creation(self) -> "Order":
        """Validate the order's promise against its creation timestamp."""
        _require_promise_after_creation(self.created_at, self.promised_dispatch_at)
        return self


class PriorityBreakdown(BaseModel):
    """The terms behind one order's priority score.

    Carried so an operator asking "why is this order here?" gets the arithmetic
    rather than a single opaque number (spec section 12).
    """

    model_config = ConfigDict(frozen=True)

    sla_urgency: float
    aging_bonus: float
    workload_penalty: float
    # Contributed by a recovery lever, e.g. pinning a segment to the front.
    adjustment: float = 0.0

    @property
    def total(self) -> float:
        """Return the score the queue is actually sorted by."""
        return self.sla_urgency + self.aging_bonus - self.workload_penalty + self.adjustment


class ScheduledOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    priority_score: float
    priority_breakdown: PriorityBreakdown
    # When the floor finishes the work. Dispatch can be later: an order that
    # misses the carrier collection waits for the next one, and the gap between
    # these two is exactly what extending a cutoff recovers.
    work_complete_at: datetime
    predicted_dispatch_at: datetime
    promised_dispatch_at: datetime
    sla_status: SLAStatus
    queue_position: int
    # Work the floor must clear before reaching this order, committed work
    # included. With queue position and throughput this fully explains the
    # predicted dispatch time.
    work_units_ahead: float


class ScheduleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    capacity_per_hour: float
    scheduled_orders: tuple[ScheduledOrder, ...]
    pending_orders: int
    pending_work_units: float
    safe_count: int
    watch_count: int
    at_risk_count: int
    breached_count: int
    # How long before the first promise actually fails. This is the operator's
    # decision window: a recovery lever that lands after it rescues nobody.
    # None when nothing in the queue is predicted to breach.
    minutes_to_first_breach: float | None = None
    # Only ever non-zero in a plan projection; see SLAStatus.BREACHED_MANAGED.
    breached_managed_count: int = 0


class OrderCreatedEvent(BaseModel):
    event_id: str
    # Pinned so a mislabelled event cannot be recorded under the wrong identity
    # in processed_events.
    event_type: Literal[EventType.ORDER_CREATED] = EventType.ORDER_CREATED
    source: str = Field(
        default="commerce_sim",
        min_length=1,
        max_length=64,
        description=(
            "Registered provider id. Widened from a two-value enum "
            "(commerce_sim/fulfillment_sim) so a real commerce feed has a "
            "legal value to send; validated against event_providers at "
            "the router, not the type, so an unregistered id is still "
            "refused."
        ),
    )
    order_id: str
    facility_id: str
    created_at: datetime
    promised_dispatch_at: datetime
    item_count: int = Field(gt=0)
    # Advisory. When the attributes below are present the backend classifies
    # instead, because how much work an order costs is our judgement, not the
    # provider's.
    work_units: float = Field(gt=0)
    order_value: Decimal = Field(ge=0)

    # Classification inputs. Optional so a producer can adopt them without a
    # flag day; absent, the declared work_units stands.
    line_count: int | None = Field(default=None, gt=0)
    unit_count: int | None = Field(default=None, gt=0)
    special_handling: bool = False

    # Commercial segment, e.g. FIRST_TIME, SUBSCRIBER, HIGH_LTV, MARKETPLACE.
    # Free-form on purpose: what counts as a protected cohort is a merchant's
    # decision, and recovery levers target it by name.
    segment: str | None = Field(default=None, max_length=64)

    _aware_timestamps = field_validator("created_at", "promised_dispatch_at")(
        _require_timezone_aware
    )

    @model_validator(mode="after")
    def _check_promise_after_creation(self) -> "OrderCreatedEvent":
        """Validate the event's promise against its creation timestamp."""
        _require_promise_after_creation(self.created_at, self.promised_dispatch_at)
        return self


class FulfillmentSnapshotEvent(BaseModel):
    event_id: str
    event_type: Literal[EventType.FULFILLMENT_SNAPSHOT] = EventType.FULFILLMENT_SNAPSHOT
    source: str = Field(
        default="fulfillment_sim",
        min_length=1,
        max_length=64,
        description="Registered provider id; see OrderCreatedEvent.source.",
    )
    facility_id: str
    occurred_at: datetime
    open_orders: int = Field(ge=0)
    work_units_completed_last_hour: float = Field(ge=0)

    _aware_timestamps = field_validator("occurred_at")(_require_timezone_aware)


class OrderStatusUpdatedEvent(BaseModel):
    event_id: str
    event_type: Literal[EventType.ORDER_STATUS_UPDATED] = EventType.ORDER_STATUS_UPDATED
    source: str = Field(
        default="fulfillment_sim",
        min_length=1,
        max_length=64,
        description="Registered provider id; see OrderCreatedEvent.source.",
    )
    order_id: str
    facility_id: str
    occurred_at: datetime
    status: OrderStatus

    _aware_timestamps = field_validator("occurred_at")(_require_timezone_aware)


class OrderBackfillEvent(BaseModel):
    """A pre-existing order, imported once at onboarding.

    Same shape as `OrderCreatedEvent` plus the order's real current status
    and when it reached that status -- `status` is required (there is no
    PENDING default to fall back to, since the whole point is that this
    order is not new). Validated at the router by the same
    `_require_valid_transition` `OrderStatusUpdatedEvent`'s live path uses,
    so DELAYED is rejected and CANCELLED is accepted (P8), matching the live
    ingest contract exactly rather than a backfill-specific rule.
    """

    event_id: str
    event_type: Literal[EventType.ORDER_BACKFILLED] = EventType.ORDER_BACKFILLED
    source: str = Field(
        default="commerce_sim",
        min_length=1,
        max_length=64,
        description="Registered provider id; see OrderCreatedEvent.source.",
    )
    order_id: str
    facility_id: str
    created_at: datetime
    promised_dispatch_at: datetime
    item_count: int = Field(gt=0)
    work_units: float = Field(gt=0)
    order_value: Decimal = Field(ge=0)

    line_count: int | None = Field(default=None, gt=0)
    unit_count: int | None = Field(default=None, gt=0)
    special_handling: bool = False
    segment: str | None = Field(default=None, max_length=64)

    status: OrderStatus
    # When the order reached `status`, on the producer's own clock. Stamps
    # both the backfilled transition's occurred_at and, if status is
    # DISPATCHED, orders.dispatched_at -- see migration 0009 for why NOW()
    # (the wall clock) cannot stand in for this.
    occurred_at: datetime

    _aware_timestamps = field_validator("created_at", "promised_dispatch_at", "occurred_at")(
        _require_timezone_aware
    )

    @model_validator(mode="after")
    def _check_promise_after_creation(self) -> "OrderBackfillEvent":
        """Validate the event's promise against its creation timestamp."""
        _require_promise_after_creation(self.created_at, self.promised_dispatch_at)
        return self


class OrderRevisedEvent(BaseModel):
    """A known order's content or promise changed after creation (P8).

    Shopify's `orders/updated`: the order already exists, and this carries
    its new values -- not covered by park-and-replay, which exists for the
    opposite case (an *unknown* order). No `created_at`: creation time is a
    fact about when the order was first placed and does not change on
    revision. No `status`: a revision never changes lifecycle position,
    only content and promise -- that stays `POST /events/order-status`'s job.

    Does not carry `original_promised_dispatch_at`: that field survives a
    revision untouched, by the same `COALESCE(original_promised_dispatch_at,
    promised_dispatch_at)` an internal RE_PROMISE lever already relies on
    (`app/interventions.py`) -- a revised order gets a new `promised_
    dispatch_at`, and whichever lever first shifts it (before or after this
    revision) is still the one whose provenance is recorded, exactly as
    today.
    """

    event_id: str
    event_type: Literal[EventType.ORDER_REVISED] = EventType.ORDER_REVISED
    source: str = Field(
        default="commerce_sim",
        min_length=1,
        max_length=64,
        description="Registered provider id; see OrderCreatedEvent.source.",
    )
    order_id: str
    facility_id: str
    promised_dispatch_at: datetime
    item_count: int = Field(gt=0)
    work_units: float = Field(gt=0)
    order_value: Decimal = Field(ge=0)

    line_count: int | None = Field(default=None, gt=0)
    unit_count: int | None = Field(default=None, gt=0)
    special_handling: bool = False
    segment: str | None = Field(default=None, max_length=64)

    _aware_timestamps = field_validator("promised_dispatch_at")(_require_timezone_aware)


class EventIngestionResponse(BaseModel):
    event_id: str
    status: IngestionStatus


def require_at_sign(value: str) -> str:
    """Reject a value with no `@` at all -- not full RFC validation.

    Module-level rather than a method so every model taking an operator- or
    user-supplied address enforces the same rule. `AlertRecipientCreate`
    reuses it: an alert address is typed by an operator and goes straight
    into an `EmailMessage` header, exactly like a signup address does.
    """
    if "@" not in value.strip():
        raise ValueError("email must contain '@'")
    return value


def reject_control_characters(value: str) -> str:
    """Reject embedded control characters, including CR/LF (phase 2, T-02-03).

    A JSON body can carry literal CR/LF bytes inside a string value
    (`"x@y.com
Bcc: evil@x.com"` is valid JSON; `json.loads` decodes the
    escape into real control characters), so an unvalidated address would
    reach the mailer un-sanitized. `email.message.EmailMessage` already
    raises `ValueError` on embedded CR/LF in a header value (verified
    empirically), which closes the header-injection vector -- but that
    exception would fire deep inside the handler, on the request path,
    before `background_tasks.add_task` is ever reached, turning a bad
    address into an unhandled 500 instead of a clean 422. This check moves
    that failure here, with a clear reason, at the boundary. Not a full RFC
    5322 validator -- deliberately narrow, matching `require_at_sign`'s
    style and Phase 1's choice to avoid the `email-validator` dependency.
    """
    if any(ord(char) < 32 for char in value):
        raise ValueError("email must not contain control characters")
    return value


class SignupRequest(BaseModel):
    """Untrusted account-creation payload.

    `email` is plain `str`, not Pydantic's `EmailStr`: `EmailStr` needs the
    optional `email-validator` package, which is not a dependency of this
    project (and is not pulled in transitively by `uvicorn[standard]`). Adding
    it would be a second new dependency in a plan that already adds
    `pwdlib[argon2]`. A minimal explicit shape check below is enough for
    AUTH-01 without that extra install.
    """

    email: str = Field(min_length=3, max_length=254)
    password: str
    full_name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)

    _at_sign = field_validator("email")(require_at_sign)
    _no_control_characters = field_validator("email")(reject_control_characters)


class PasswordRuleResponse(BaseModel):
    """One rule the signup form renders as a checklist item.

    `id` is the stable contract -- the form keys its live tick marks off this,
    never off `label`, so the wording can change without breaking the UI.
    """

    id: str
    label: str


class PasswordPolicyResponse(BaseModel):
    """Everything the signup form needs to evaluate a password as the user types.

    Served so the form and the server cannot disagree about what is acceptable:
    a checklist showing all ticks while signup returns 400 is worse than having
    no checklist at all.
    """

    min_length: int
    max_length: int
    rules: list[PasswordRuleResponse]


class UserResponse(BaseModel):
    """Public account shape. Deliberately carries nothing password-shaped."""

    id: str
    email: str
    verified: bool
    full_name: str
    company: str
    role: str


class LoginRequest(BaseModel):
    """Untrusted login payload -- attacker-controlled by definition (AUTH-04).

    Deliberately as permissive as `SignupRequest` on shape (min_length=3 for
    `email`, no `@` requirement enforced here): a login attempt with a
    malformed email must fail the SAME way as a login with a well-formed but
    unregistered one (AUTH-09/D-12). A stricter validator here would let a
    422 on the email field distinguish "clearly not an account" from "correct
    shape, wrong credentials" before the handler even runs, reopening the
    enumeration channel this whole slice exists to close.
    """

    email: str
    password: str
    remember_me: bool = False


class ResetRequestRequest(BaseModel):
    """Untrusted password-reset request payload (EMAIL-05).

    As permissive as `LoginRequest` on shape, and for the same reason: this
    endpoint must return an identical response for a registered and an
    unregistered address, so a stricter validator here that 422s a
    malformed-but-plausible address before the lookup would not itself leak
    anything about registration -- no malformed string is ever a stored
    email -- but keeping this model as loose as `LoginRequest` avoids
    introducing a second, divergent shape check on the same field type.
    """

    email: str


class ResetConfirmRequest(BaseModel):
    """Untrusted payload for completing a password reset.

    `token` is the raw value from the emailed link, never trusted until
    `consume_token` validates it. `password` goes through the same
    `password_policy_error` signup enforces, checked BEFORE the token is
    consumed (T-02-22).
    """

    token: str
    password: str


class MeResponse(BaseModel):
    """Identity `GET /me` reports back to an authenticated caller (AUTH-07).

    `verified` flips to `true` once the account's verification link has
    been visited (EMAIL-02, `GET /auth/verify`). It stays informational
    always -- never a login precondition. Do not "fix" this into a
    login/`/me` gate.
    """

    email: str
    verified: bool
    full_name: str
    company: str
    role: str
    # `users.is_admin`, not `role` (free text from signup). Lets the dashboard
    # hide controls the backend would refuse anyway; the backend still enforces.
    is_admin: bool = False
