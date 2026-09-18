/**
 * Static fixtures so every page can be built and demoed before the simulator
 * exists. Shapes are generated to match types.ts exactly.
 *
 * These are placeholders for layout work only. Demo-day numbers must come from
 * the running scheduler (doc 10, section 11) — never copy a fixture number into
 * a slide.
 */

import type {
  AlertHistoryResponse,
  AlertRecipient,
  AlertRecipientsResponse,
  DashboardResponse,
  OrderResponse,
  OrdersResponse,
  RecoveryActionResponse,
  RecoveryPlansResponse,
  SLAStatus,
  SimulationRequest,
  SimulationResponse,
  SimulationSummary,
  SurgeRiskLevel,
} from "@/lib/types";

// --- Recovery chat fixture --------------------------------------------------

/**
 * Canned answers so the chat panel is usable in mock mode with no backend.
 * Keyed loosely off question content, not a real model -- good enough to
 * demo the UI, never a source of truth for plan numbers.
 */
export function mockRecoveryChatAnswer(planId: string, question: string): string {
  const plan = MOCK_PLANS.plans.find((p) => p.plan_id === planId);
  const lower = question.toLowerCase();

  if (!plan) {
    return `I don't have a plan called \`${planId}\` to answer against.`;
  }
  if (lower.includes("cost") || lower.includes("trade")) {
    return `**${plan.title}** trades: ${plan.trades ?? "no trade-offs recorded."}`;
  }
  return [
    `**${plan.title}** projects ${plan.projected.at_risk_count} at-risk and ${plan.projected.breached_count} breached orders,`,
    `moving capacity to ${plan.actions.capacity_per_hour} wu/hr.`,
    "",
    "This is a mock answer -- set `VITE_DATA_SOURCE=live` to ask the real assistant.",
  ].join("\n");
}

const FACILITY = "WH-01";
const ANCHOR = new Date("2026-08-15T10:20:00Z");

/** Deterministic generator so the mock scenario replays identically. */
function seeded(seed: number) {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

const WORK_UNITS = [1.0, 1.0, 1.0, 1.5, 1.5, 2.0, 2.5];

function iso(base: Date, hours: number): string {
  return new Date(base.getTime() + hours * 3600_000).toISOString();
}

function buildOrders(count: number, capacity: number): OrderResponse[] {
  const rng = seeded(42);
  const orders: OrderResponse[] = [];
  let cumulative = 0;

  for (let i = 0; i < count; i += 1) {
    const workUnits = WORK_UNITS[Math.floor(rng() * WORK_UNITS.length)];
    const ageHours = -(rng() * 9);
    const createdAt = iso(ANCHOR, ageHours);

    // Roughly a fifth of orders have already left the pending queue.
    const active = rng() < 0.2;
    const status = active
      ? (["PICKING", "PACKED", "READY", "DISPATCHED"] as const)[
          Math.floor(rng() * 4)
        ]
      : ("PENDING" as const);

    let predicted: string | null = null;
    let sla: SLAStatus | null = null;
    let position: number | null = null;
    let priority: number | null = null;

    if (status === "PENDING") {
      cumulative += workUnits;
      position = orders.filter((o) => o.status === "PENDING").length + 1;
      const hoursOut = cumulative / capacity;
      predicted = iso(ANCHOR, hoursOut);
      priority = Number((12 - hoursOut + ageHours * 0.1).toFixed(3));

      // Promise is 24h from creation; slack decides the class.
      const slack = 24 + ageHours - hoursOut;
      sla =
        slack < 0
          ? "BREACHED"
          : slack <= 4
            ? "AT_RISK"
            : slack <= 12
              ? "WATCH"
              : "SAFE";
    }

    orders.push({
      order_id: `ORD-${String(1000 + i)}`,
      facility_id: FACILITY,
      created_at: createdAt,
      promised_dispatch_at: iso(ANCHOR, ageHours + 24),
      original_promised_dispatch_at: null,
      predicted_dispatch_at: predicted,
      item_count: Math.max(1, Math.round(workUnits * 2)),
      work_units: workUnits,
      order_value: (400 + Math.floor(rng() * 4600)).toFixed(2),
      status,
      segment: null,
      sla_status: sla,
      priority_score: priority,
      queue_position: position,
      priority_breakdown:
        priority === null
          ? null
          : {
              sla_urgency: priority,
              aging_bonus: Number((ageHours * 0.1).toFixed(3)),
              workload_penalty: Number((workUnits * 0.1).toFixed(3)),
              adjustment: 0,
            },
      work_units_ahead: position === null ? null : Number((cumulative - workUnits).toFixed(1)),
      throughput_assumed: position === null ? null : capacity,
      work_complete_at: predicted,
    });
  }

  // Match the backend: scheduled orders first, active orders after.
  return orders.sort((a, b) => {
    const aNull = a.queue_position === null;
    const bNull = b.queue_position === null;
    if (aNull !== bNull) return aNull ? 1 : -1;
    return (a.queue_position ?? 0) - (b.queue_position ?? 0);
  });
}

export const SURGE_ORDERS = buildOrders(96, 52);
export const HEALTHY_ORDERS = buildOrders(22, 52);

function countSla(orders: OrderResponse[]) {
  const tally = { safe: 0, watch: 0, at_risk: 0, breached: 0 };
  for (const order of orders) {
    if (order.sla_status === "SAFE") tally.safe += 1;
    if (order.sla_status === "WATCH") tally.watch += 1;
    if (order.sla_status === "AT_RISK") tally.at_risk += 1;
    if (order.sla_status === "BREACHED") tally.breached += 1;
  }
  return tally;
}

function backlogOf(orders: OrderResponse[]) {
  return orders.filter((o) =>
    ["PENDING", "PICKING", "PACKED", "READY"].includes(o.status),
  );
}

function dashboardFor(
  orders: OrderResponse[],
  demand: number,
  throughput: number,
): DashboardResponse {
  const sla = countSla(orders);
  const backlog = backlogOf(orders);
  const pending = orders.filter((o) => o.status === "PENDING").length;
  const exposed = sla.at_risk + sla.breached;
  const ratio = pending === 0 ? 0 : exposed / pending;

  // Demo fixture only: dispatched orders carry no "went out late" flag, so a
  // small, deterministic share of them stands in for realised misses.
  const dispatched = orders.filter((o) => o.status === "DISPATCHED").length;
  const realisedBreaches = Math.round(dispatched * 0.08);
  const lateDispatchThreshold = 0.04;
  const lateDispatchRate =
    dispatched === 0 ? 0 : Number((realisedBreaches / dispatched).toFixed(4));

  return {
    facility_id: FACILITY,
    generated_at: ANCHOR.toISOString(),
    demand_work_units_per_hour: demand,
    fulfillment_work_units_per_hour: throughput,
    throughput_source: "derived",
    throughput_stalled: false,
    backlog_orders: backlog.length,
    backlog_work_units: Number(
      backlog.reduce((sum, o) => sum + o.work_units, 0).toFixed(1),
    ),
    dispatch_promise_hours: 24,
    backlog_growth_wu_per_hour: Number((demand - throughput).toFixed(1)),
    minutes_to_first_breach: null,
    minutes_to_cutoff: null,
    realised_breaches: realisedBreaches,
    preventable_breaches: sla.breached,
    late_dispatch_rate: lateDispatchRate,
    late_dispatch_threshold: lateDispatchThreshold,
    late_dispatch_breaching: lateDispatchRate > lateDispatchThreshold,
    risk_level:
      ratio === 0
        ? "LOW"
        : ratio < 0.15
          ? "MEDIUM"
          : ratio < 0.4
            ? "HIGH"
            : "CRITICAL",
    order_states: {
      received: 0,
      pending,
      picking: orders.filter((o) => o.status === "PICKING").length,
      packed: orders.filter((o) => o.status === "PACKED").length,
      ready: orders.filter((o) => o.status === "READY").length,
      dispatched: orders.filter((o) => o.status === "DISPATCHED").length,
    },
    sla_counts: sla,
  };
}

export const SURGE_DASHBOARD = dashboardFor(SURGE_ORDERS, 127, 52);
export const HEALTHY_DASHBOARD = dashboardFor(HEALTHY_ORDERS, 32, 52);

function summaryFor(
  orders: OrderResponse[],
  capacity: number,
): SimulationSummary {
  const pending = orders.filter((o) => o.status === "PENDING");
  const pendingWork = pending.reduce((sum, o) => sum + o.work_units, 0);
  const backlog = backlogOf(orders);

  // Re-derive SLA under the hypothetical capacity, mirroring the scheduler.
  let cumulative = 0;
  const tally = { safe: 0, watch: 0, at_risk: 0, breached: 0 };
  let alreadyLate = 0;
  for (const order of pending) {
    cumulative += order.work_units;
    const hoursOut = cumulative / capacity;
    const ageHours =
      (new Date(order.created_at).getTime() - ANCHOR.getTime()) / 3600_000;
    const slack = 24 + ageHours - hoursOut;
    // Promise is created + 24h; past the anchor means no plan can save it.
    if (slack <= 4 && 24 + ageHours <= 0) alreadyLate += 1;
    if (slack < 0) tally.breached += 1;
    else if (slack <= 4) tally.at_risk += 1;
    else if (slack <= 12) tally.watch += 1;
    else tally.safe += 1;
  }

  const exposed = tally.at_risk + tally.breached;
  const ratio = pending.length === 0 ? 0 : exposed / pending.length;

  return {
    backlog_orders: backlog.length,
    backlog_work_units: Number(
      backlog.reduce((sum, o) => sum + o.work_units, 0).toFixed(1),
    ),
    safe_count: tally.safe,
    watch_count: tally.watch,
    at_risk_count: tally.at_risk,
    breached_count: tally.breached,
    breached_managed_count: 0,
    already_late_count: alreadyLate,
    saveable_count: exposed - alreadyLate,
    projected_arrivals: null,
    projected_recovery_hours:
      pendingWork === 0 ? null : Number((pendingWork / capacity).toFixed(2)),
    risk_level:
      ratio === 0
        ? "LOW"
        : ratio < 0.15
          ? "MEDIUM"
          : ratio < 0.4
            ? "HIGH"
            : "CRITICAL",
  };
}

export function mockSimulation(request: SimulationRequest): SimulationResponse {
  const capacity = request.capacity_per_hour ?? 52;
  return {
    generated_at: new Date().toISOString(),
    baseline: summaryFor(SURGE_ORDERS, 52),
    simulated: summaryFor(SURGE_ORDERS, capacity),
    applied_capacity_per_hour: capacity,
    applied_dispatch_promise_hours: request.dispatch_promise_hours ?? 24,
    applied_demand_multiplier: request.demand_multiplier ?? 1,
  applied_projection_horizon_minutes: request.projection_horizon_minutes ?? null,
  };
}

/**
 * Demo postures mirroring `backend/app/catalog/levers.json`. Plans are composed
 * from that catalog, so these are a snapshot of it, not a second source of
 * truth -- the live set depends on which levers the facility state allows.
 */
export const MOCK_PLANS: RecoveryPlansResponse = {
  unavailable_levers: [],
  plans: [
    {
      plan_id: "do-nothing",
      title: "Do Nothing",
      description:
        "No intervention. Shown so the cost of inaction is a visible choice rather than an absence of one.",
      relative_cost: "NONE",
      actions: {
        capacity_per_hour: 52,
        dispatch_promise_hours: 24,
        demand_multiplier: 1,
      },
      projected: summaryFor(SURGE_ORDERS, 52),
      recommended: false,
      trades: "Nothing. This is what happens if no one acts.",
      families: [],
      levers: [],
      outcome: null,
      effective_at: null,
    },
    {
      plan_id: "buy-the-hour",
      title: "Buy the Hour",
      description:
        "Hold staff past the end of shift to add picking capacity. Pause gift wrap, personalisation and inserts for the surge window.",
      relative_cost: "HIGH",
      actions: {
        capacity_per_hour: 72,
        dispatch_promise_hours: 24,
        demand_multiplier: 1,
      },
      projected: summaryFor(SURGE_ORDERS, 72),
      recommended: true,
      trades: "Money for throughput: overtime plus dropping value-added work.",
      families: ["THROUGHPUT"],
      levers: [],
      outcome: null,
      effective_at: null,
    },
    {
      plan_id: "defer-the-comfortable",
      title: "Defer the Comfortable",
      description:
        "Push orders with more than half a day of room behind those running out of it. Changes the order of misses, not the number.",
      relative_cost: "NONE",
      actions: {
        capacity_per_hour: 52,
        dispatch_promise_hours: 24,
        demand_multiplier: 1,
      },
      projected: summaryFor(SURGE_ORDERS, 52),
      recommended: false,
      trades:
        "Orders with slack to spare wait, so tighter ones move up. Nobody is rescued; the misses move.",
      families: ["QUEUE"],
      levers: [],
      outcome: null,
      effective_at: null,
    },
    {
      plan_id: "manage-the-miss",
      title: "Manage the Miss",
      description:
        "Re-promise pending orders with proactive notification. For orders already beyond rescue, convert a silent breach into a managed one.",
      relative_cost: "MEDIUM",
      actions: {
        capacity_per_hour: 52,
        dispatch_promise_hours: 30,
        demand_multiplier: 1,
      },
      projected: summaryFor(SURGE_ORDERS, 52),
      recommended: false,
      trades: "Goodwill and credits, for breaches that cannot be prevented.",
      families: ["PROMISE"],
      levers: [],
      outcome: null,
      effective_at: null,
    },
  ],
};

/** In-memory action store so the approval flow is clickable without a backend. */
const mockActions = new Map<string, RecoveryActionResponse>();

export function mockApprove(planId: string): RecoveryActionResponse {
  const plan =
    MOCK_PLANS.plans.find((p) => p.plan_id === planId) ?? MOCK_PLANS.plans[1];
  const now = new Date().toISOString();
  const action: RecoveryActionResponse = {
    action_id: `action-${Math.random().toString(16).slice(2, 10)}`,
    plan_id: plan.plan_id,
    facility_id: FACILITY,
    status: "PENDING",
    capacity_per_hour: plan.actions.capacity_per_hour,
    dispatch_promise_hours: plan.actions.dispatch_promise_hours,
    demand_multiplier: plan.actions.demand_multiplier,
    error_detail: null,
    // Mock mode always mimics the simulator adapter's PENDING -> SUCCESS
    // path below, so these P6 fields never populate here -- a manual/webhook
    // facility's AWAITING_ENACTMENT/ENACTED flow has no mock story yet.
    enacted_at: null,
    capacity_commitments: [],
    effects: [],
    created_at: now,
    updated_at: now,
  };
  mockActions.set(action.action_id, action);

  // Stand in for n8n reporting back, so PENDING -> SUCCESS is observable.
  setTimeout(() => {
    const current = mockActions.get(action.action_id);
    if (current && current.status === "PENDING") {
      mockActions.set(action.action_id, {
        ...current,
        status: "SUCCESS",
        updated_at: new Date().toISOString(),
      });
    }
  }, 6000);

  return action;
}

export function mockAction(actionId: string): RecoveryActionResponse {
  const action = mockActions.get(actionId);
  if (!action) throw new Error(`unknown recovery action ${actionId}`);
  return action;
}

/**
 * Mock mode never creates an AWAITING_ENACTMENT action (mockApprove above
 * always goes PENDING -> SUCCESS, the simulator adapter's path), so this
 * exists only so the confirm-enactment control compiles and has somewhere to
 * call in mock mode -- it is not expected to be reachable from the demo UI.
 */
export function mockConfirmEnactment(
  actionId: string,
  succeeded: boolean,
  errorDetail?: string,
): RecoveryActionResponse {
  const action = mockActions.get(actionId);
  if (!action) throw new Error(`unknown recovery action ${actionId}`);
  const updated: RecoveryActionResponse = {
    ...action,
    status: succeeded ? "ENACTED" : "FAILED",
    error_detail: succeeded ? null : errorDetail ?? "mock enactment reported as failed",
    enacted_at: succeeded ? new Date().toISOString() : action.enacted_at,
    updated_at: new Date().toISOString(),
  };
  mockActions.set(actionId, updated);
  return updated;
}

export function mockOrders(
  orders: OrderResponse[],
  limit: number,
  offset: number,
): OrdersResponse {
  return { items: orders.slice(offset, offset + limit), total: orders.length };
}

// --- Alerting fixtures ----------------------------------------------------

/**
 * In-memory recipient list so the page is usable with no backend.
 *
 * Seeded with the two roles the severity routing exists for: an operational
 * head who only wants CRITICAL, and a floor lead who takes HIGH and up.
 */
const mockRecipients: AlertRecipient[] = [
  {
    id: "recipient-demo-head",
    name: "Priya Raghavan",
    email: "ops.head@example.com",
    min_level: "CRITICAL",
  },
  {
    id: "recipient-demo-lead",
    name: "Daniel Okonkwo",
    email: "floor.lead@example.com",
    min_level: "HIGH",
  },
];

export function mockAlertRecipients(): AlertRecipientsResponse {
  return { recipients: [...mockRecipients] };
}

export function mockAddAlertRecipient(
  name: string,
  email: string,
  minLevel: SurgeRiskLevel,
): AlertRecipient {
  const created: AlertRecipient = {
    id: `recipient-${Math.random().toString(16).slice(2, 10)}`,
    name,
    email,
    min_level: minLevel,
  };
  mockRecipients.push(created);
  return created;
}

export function mockRemoveAlertRecipient(recipientId: string): void {
  const index = mockRecipients.findIndex((r) => r.id === recipientId);
  if (index >= 0) mockRecipients.splice(index, 1);
}

/**
 * Fixture history covering all three delivery states, because the FAILED row
 * is the whole reason this table exists -- a fixture showing only successes
 * would hide the case the page is designed around.
 */
export function mockAlertHistory(): AlertHistoryResponse {
  const now = Date.now();
  const at = (minutesAgo: number) =>
    new Date(now - minutesAgo * 60_000).toISOString();

  return {
    alerts: [
      {
        id: "alert-demo-1",
        rule_id: "promises-already-missed",
        facility_id: "WH-01",
        level: "CRITICAL",
        subject: "SurgeGuard: 3 missed promises at WH-01",
        status: "SENT",
        attempts: 1,
        last_error: null,
        decided_at: at(12),
        sent_at: at(12),
        recipient_name: "Priya Raghavan",
        recipient_email: "ops.head@example.com",
      },
      {
        id: "alert-demo-2",
        rule_id: "promises-already-missed",
        facility_id: "WH-01",
        level: "CRITICAL",
        subject: "SurgeGuard: 3 missed promises at WH-01",
        status: "FAILED",
        attempts: 3,
        last_error: "SMTPRecipientsRefused: mailbox unavailable",
        decided_at: at(12),
        sent_at: null,
        recipient_name: "Daniel Okonkwo",
        recipient_email: "floor.lead@example.com",
      },
      {
        id: "alert-demo-3",
        rule_id: "backlog-outpacing-throughput",
        facility_id: "WH-01",
        level: "HIGH",
        subject: "SurgeGuard: backlog outpacing throughput at WH-01",
        status: "PENDING",
        attempts: 0,
        last_error: null,
        decided_at: at(2),
        sent_at: null,
        recipient_name: "Daniel Okonkwo",
        recipient_email: "floor.lead@example.com",
      },
    ],
  };
}
