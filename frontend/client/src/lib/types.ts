/**
 * Mirrors backend/app/schemas.py and backend/app/models.py exactly.
 *
 * Any change here must correspond to a change the backend already shipped.
 * The frontend never invents a field the backend does not return.
 */

// --- Enums (backend StrEnum values) ---------------------------------------

export type OrderStatus =
  | "RECEIVED"
  | "PENDING"
  | "PICKING"
  | "PACKED"
  | "READY"
  | "DISPATCHED"
  | "CANCELLED"
  | "DELAYED";

export type SLAStatus =
  | "SAFE"
  | "WATCH"
  | "AT_RISK"
  | "BREACHED"
  /**
   * A breach the customer has been told about and compensated for. Only ever
   * appears in a recovery-plan projection, never in a live read. Still a
   * breach: an apology does not make an order arrive sooner.
   */
  | "BREACHED_MANAGED";

export type LeverFamily = "QUEUE" | "THROUGHPUT" | "INFLOW" | "PROMISE";

export type CostBand = "NONE" | "LOW" | "MEDIUM" | "HIGH";

export type SurgeRiskLevel = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

/**
 * AWAITING_ENACTMENT and ENACTED only appear on a manual/webhook facility's
 * throughput-family action (P6) -- everything the demo's simulator adapter
 * approves still goes PENDING -> SUCCESS/FAILED exactly as before. ENACTED is
 * deliberately not SUCCESS: it means a human confirmed the physical change
 * happened, not that n8n verified anything, and the two must not be shown as
 * equivalent (see VerificationStatus below for what actually happened next).
 */
export type ActionStatus =
  | "PENDING"
  | "AWAITING_ENACTMENT"
  | "ENACTED"
  | "SUCCESS"
  | "FAILED";

/**
 * Whether a capacity claim actually showed up in measured throughput (P6).
 * UNVERIFIED and NOT_OBSERVED must render prominently -- never as a quiet
 * grey badge -- because "you approved overtime 90 minutes ago and the floor
 * never moved" is the most valuable sentence this product can say.
 */
export type VerificationStatus = "UNVERIFIED" | "ACHIEVED" | "PARTIAL" | "NOT_OBSERVED";

export interface CapacityCommitment {
  lever_id: string;
  target_work_units_per_hour: number;
  verification_status: VerificationStatus;
  verified_at: string | null;
  observed_work_units_per_hour: number | null;
}

/**
 * Plans are composed from the backend's lever catalog, so the set of ids is
 * open: an operator can add a posture by editing catalog data, with no
 * frontend change. Never enumerate them here.
 */
export type PlanId = string;

// --- Orders ---------------------------------------------------------------

export interface OrderResponse {
  order_id: string;
  facility_id: string;
  /** ISO 8601, UTC. Backend uses datetime.now(UTC) throughout. */
  created_at: string;
  promised_dispatch_at: string;
  /** Null for every non-PENDING order: only PENDING orders are scheduled. */
  predicted_dispatch_at: string | null;
  item_count: number;
  work_units: number;
  /** Decimal on the backend, so it arrives as a JSON string, e.g. "2199.00". */
  order_value: string;
  status: OrderStatus;
  segment: string | null;
  original_promised_dispatch_at: string | null;
  sla_status: SLAStatus | null;
  priority_score: number | null;
  queue_position: number | null;
  /**
   * Together these reproduce the predicted dispatch time, so a status can be
   * explained in numbers rather than asserted. Null for orders the scheduler
   * does not reorder.
   */
  priority_breakdown: PriorityBreakdown | null;
  work_units_ahead: number | null;
  throughput_assumed: number | null;
  /** When the floor finishes. Earlier than dispatch when a cutoff binds. */
  work_complete_at: string | null;
}

export interface PriorityBreakdown {
  sla_urgency: number;
  aging_bonus: number;
  workload_penalty: number;
  /** Contributed by a recovery lever, e.g. pinning a segment to the front. */
  adjustment: number;
}

export interface OrdersResponse {
  items: OrderResponse[];
  /** Total matching orders, not the length of `items`. */
  total: number;
}

// --- Dashboard ------------------------------------------------------------

export interface OrderStateCounts {
  received: number;
  pending: number;
  picking: number;
  packed: number;
  ready: number;
  dispatched: number;
}

export interface SLACounts {
  safe: number;
  watch: number;
  at_risk: number;
  breached: number;
}

export interface DashboardResponse {
  facility_id: string;
  generated_at: string;
  demand_work_units_per_hour: number;
  fulfillment_work_units_per_hour: number;
  /**
   * Where throughput came from: derived from status history, reported by a
   * producer, or configured when nothing was measured.
   */
  throughput_source: "derived" | "reported" | "configured";
  /** Work is waiting and the open floor is not moving it. Throughput reads 0. */
  throughput_stalled: boolean;
  /** Counts PENDING + PICKING + PACKED + READY. */
  backlog_orders: number;
  backlog_work_units: number;
  dispatch_promise_hours: number;
  risk_level: SurgeRiskLevel;
  order_states: OrderStateCounts;
  /**
   * Counts PENDING orders only, so these will not sum to backlog_orders.
   * Always label this "of pending orders" in the UI.
   */
  sla_counts: SLACounts;
  /** Demand minus throughput. Positive means the backlog is still growing. */
  backlog_growth_wu_per_hour: number;
  /** When the first promise actually fails; bounds the time left to act. */
  minutes_to_first_breach: number | null;
  /** Time to the next carrier collection. Null if dispatch is continuous. */
  minutes_to_cutoff: number | null;
  /** Already missed and dispatched late. Permanent -- no recovery undoes these. */
  realised_breaches: number;
  /** Predicted to miss but not yet dispatched. Still saveable. */
  preventable_breaches: number;
  /** Share of dispatches that went out late, 0..1, e.g. 0.0108. */
  late_dispatch_rate: number;
  /** The published marketplace ceiling this rate is measured against, e.g. 0.04. */
  late_dispatch_threshold: number;
  /** True when late_dispatch_rate exceeds late_dispatch_threshold. */
  late_dispatch_breaching: boolean;
}

// --- What-If --------------------------------------------------------------

/** Send only the assumptions the operator actually changed. */
export interface SimulationRequest {
  capacity_per_hour?: number | null;
  dispatch_promise_hours?: number | null;
  /** Scales projected arrivals. Requires projection_horizon_minutes. */
  demand_multiplier?: number | null;
  projection_horizon_minutes?: number | null;
  burst?: { at_minute: number; work_units: number } | null;
  seed?: number | null;
}

/** Exposure among orders nobody has placed yet. Never mixed with real ones. */
export interface ProjectedArrivalCounts {
  orders: number;
  work_units: number;
  safe_count: number;
  watch_count: number;
  at_risk_count: number;
  breached_count: number;
}

export interface SimulationSummary {
  backlog_orders: number;
  backlog_work_units: number;
  /** Counts below describe orders customers have actually placed. */
  safe_count: number;
  watch_count: number;
  at_risk_count: number;
  breached_count: number;
  /** Breaches the customer was told about. Still breaches. */
  breached_managed_count: number;
  /**
   * At-risk + breached split by whether the promise has already passed.
   * Already late is lost under every plan; only saveable can differ.
   */
  already_late_count: number;
  saveable_count: number;
  /** Null when there is no pending work to clear. */
  projected_recovery_hours: number | null;
  risk_level: SurgeRiskLevel;
  projected_arrivals: ProjectedArrivalCounts | null;
}

export interface SimulationResponse {
  generated_at: string;
  baseline: SimulationSummary;
  simulated: SimulationSummary;
  applied_capacity_per_hour: number;
  applied_dispatch_promise_hours: number;
  /** Scales projected arrivals; 1.0 when no horizon was requested. */
  applied_demand_multiplier: number;
  applied_projection_horizon_minutes: number | null;
}

// --- Recovery -------------------------------------------------------------

export interface RecoveryPlanActions {
  capacity_per_hour: number;
  dispatch_promise_hours: number;
  demand_multiplier: number;
}

export interface LeverResponse {
  id: string;
  name: string;
  family: LeverFamily;
  description: string;
  cost: CostBand;
  lead_time_minutes: number;
  reversible: boolean;
  side_effects: string[];
  /** Set when the lever cannot help this decision, e.g. it lands too late. */
  unavailable_reason: string | null;
}

/**
 * What a plan prevents, versus what it merely moves.
 *
 * A queue lever cannot reduce the number of missed promises; it decides which
 * orders miss. When `is_redistribution` is true the UI must not describe the
 * plan as having saved anything.
 */
export interface PlanOutcomeResponse {
  breaches_avoided: number;
  breaches_relocated: number;
  net_breach_change: number;
  orders_improved: number;
  orders_worsened: number;
  is_redistribution: boolean;
  /** Share of dispatches projected to go out late under this plan, 0..1. */
  projected_late_dispatch_rate: number;
}

export interface RecoveryPlanResponse {
  plan_id: PlanId;
  title: string;
  description: string;
  relative_cost: string;
  actions: RecoveryPlanActions;
  projected: SimulationSummary;
  recommended: boolean;
  /** What this plan gives up, in the operator's terms. */
  trades: string | null;
  families: LeverFamily[];
  levers: LeverResponse[];
  outcome: PlanOutcomeResponse | null;
  /** When its slowest lever lands. */
  effective_at: string | null;
}

export interface RecoveryPlansResponse {
  plans: RecoveryPlanResponse[];
  /** Levers that cannot help right now. Show them greyed, not hidden. */
  unavailable_levers: LeverResponse[];
}

export interface RecoveryApprovalResponse {
  plan_id: string;
  action_id: string;
  status: ActionStatus;
}

export interface RecoveryActionResponse {
  action_id: string;
  plan_id: string;
  facility_id: string;
  status: ActionStatus;
  capacity_per_hour: number;
  dispatch_promise_hours: number;
  demand_multiplier: number;
  /** Typed backend-owned lever effects; their fields depend on `kind`. */
  effects: Array<Record<string, unknown>>;
  error_detail: string | null;
  /** When a human confirmed enactment (P6). Null until ENACTED. */
  enacted_at: string | null;
  /** One entry per capacity_delta effect this action carried. Empty for a
   * QUEUE/PROMISE-only action. */
  capacity_commitments: CapacityCommitment[];
  created_at: string;
  updated_at: string;
}

/** Operator controls for driving the demo. Absent in a deployment without a
 * simulator to drive, in which case `enabled` is false. */
export interface DemoStatus {
  enabled: boolean;
  running: boolean;
  stage: string | null;
  ticks_done: number;
  ticks_total: number;
  detail: string | null;
  /** True while the continuous live clock is driving the simulator. */
  live: boolean;
  /** The multiplier the live clock is running at, e.g. 60 for 60x. */
  speed: number | null;
  /** ISO instant. The simulator's current clock while live. */
  simulated_time: string | null;
  /** Simulated hours elapsed since the live clock started. */
  sim_hours_elapsed: number | null;
}

// --- Auth -----------------------------------------------------------------

/** One rule the sign-up form renders as a live checklist item. Key the UI off
 * `id`, never off `label`: ids are a stable contract asserted by a backend test,
 * labels are display copy. */
export interface PasswordRule {
  id: "length" | "uppercase" | "lowercase" | "digit" | "symbol";
  label: string;
}

/** Served by GET /auth/password-policy so the checklist and the server cannot
 * disagree about what is acceptable. */
export interface PasswordPolicy {
  min_length: number;
  max_length: number;
  rules: PasswordRule[];
}

export interface UserResponse {
  id: string;
  email: string;
  verified: boolean;
  full_name: string;
  company: string;
  role: string;
}

export interface SignupProfile {
  full_name?: string;
  company?: string;
  role?: string;
}

/** GET /me. `verified` is informational — never gate the UI on it. */
export interface MeResponse {
  email: string;
  verified: boolean;
  full_name: string;
  company: string;
  role: string;
  /** `users.is_admin`. Hides admin-only controls; the backend enforces it regardless. */
  is_admin: boolean;
}

export interface StatusResponse {
  status: string;
  detail?: string;
}


/** Delivery state of one queued alert. FAILED is terminal: it is not retried. */
export type AlertStatus = "PENDING" | "SENT" | "FAILED";

export interface AlertRecipient {
  id: string;
  name: string;
  email: string;
  /** Lowest severity worth emailing this person about. */
  min_level: SurgeRiskLevel;
}

export interface AlertRecipientsResponse {
  recipients: AlertRecipient[];
}

export interface AlertHistoryEntry {
  id: string;
  rule_id: string;
  facility_id: string;
  level: SurgeRiskLevel;
  subject: string;
  status: AlertStatus;
  attempts: number;
  last_error: string | null;
  decided_at: string;
  sent_at: string | null;
  /** Null once the recipient has been removed; the history outlives them. */
  recipient_name: string | null;
  recipient_email: string | null;
}

export interface AlertHistoryResponse {
  alerts: AlertHistoryEntry[];
}
