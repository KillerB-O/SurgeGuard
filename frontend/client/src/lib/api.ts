/**
 * The only module in the frontend that knows a URL.
 *
 * Route paths match the running backend, not doc 06: `main.py` mounts the read
 * and simulation routers under `settings.api_prefix` (default `/api`), so
 * VITE_API_BASE must already include the prefix. Only `/health` sits outside it.
 *
 * Switching from fixtures to the real backend is one env var, not a code change.
 */

import {
  HEALTHY_DASHBOARD,
  HEALTHY_ORDERS,
  MOCK_PLANS,
  SURGE_DASHBOARD,
  SURGE_ORDERS,
  mockAction,
  mockApprove,
  mockConfirmEnactment,
  mockAddAlertRecipient,
  mockAlertHistory,
  mockAlertRecipients,
  mockOrders,
  mockRemoveAlertRecipient,
  mockSimulation,
} from "@/dashboard/mocks";
import type {
  ActionStatus,
  MeResponse,
  PasswordPolicy,
  StatusResponse,
  UserResponse,
  DashboardResponse,
  DemoStatus,
  OrderStatus,
  OrderResponse,
  OrdersResponse,
  RecoveryActionResponse,
  RecoveryApprovalResponse,
  RecoveryPlansResponse,
  SimulationRequest,
  SimulationResponse,
  AlertHistoryResponse,
  AlertRecipient,
  AlertRecipientsResponse,
  SurgeRiskLevel,
  SignupProfile,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000/api";

export const FACILITY_ID = import.meta.env.VITE_FACILITY_ID ?? "WH-01";

export const USING_MOCKS =
  (import.meta.env.VITE_DATA_SOURCE ?? "live") === "mock";

/** Which fixture the mock source serves. Toggled from the status strip. */
let mockScenario: "surge" | "healthy" = "surge";

export function setMockScenario(scenario: "surge" | "healthy") {
  mockScenario = scenario;
}

export function getMockScenario() {
  return mockScenario;
}

/** The backend caps `limit` at 500; ask for the ceiling on order reads. */
export const MAX_PAGE_SIZE = 500;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      // The session is an httpOnly cookie, which the browser will not attach to
      // a cross-origin request (:5173 -> :8000) unless the fetch opts in. Without
      // this, signing in appears to work and every request after it is a 401.
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    // Network-level failure: the backend is down or CORS rejected the origin.
    throw new ApiError(0, "Cannot reach the backend.");
  }

  if (!response.ok) {
    // The backend returns 503 with a retryable detail for database outages.
    const detail = await response
      .json()
      .then((body) => {
        if (typeof body?.detail === "string") return body.detail;
        if (Array.isArray(body?.detail)) {
          return body.detail
            .map((issue: { loc?: unknown[]; msg?: unknown }) => {
              const field = Array.isArray(issue.loc) ? issue.loc.at(-1) : null;
              return `${field ? `${field}: ` : ""}${String(issue.msg ?? "Invalid value")}`;
            })
            .join("; ");
        }
        return null;
      })
      .catch(() => null);
    notifyUnauthenticated(response.status);
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : `Request failed (${response.status})`,
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const SESSION_EXPIRED_EVENT = "surgeguard:session-expired";

function notifyUnauthenticated(status: number) {
  if (status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }
}

async function requestText(path: string, init?: RequestInit): Promise<string> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the backend.");
  }
  if (!response.ok) {
    notifyUnauthenticated(response.status);
    throw new ApiError(response.status, (await response.text()) || `Request failed (${response.status})`);
  }
  return response.text();
}

function facilityQuery(facilityId: string) {
  return `facility_id=${encodeURIComponent(facilityId)}`;
}

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), 180));
}

function scenarioOrders(): OrderResponse[] {
  return mockScenario === "surge" ? SURGE_ORDERS : HEALTHY_ORDERS;
}

// --- Reads ----------------------------------------------------------------

export function getDashboard(
  facilityId: string = FACILITY_ID,
): Promise<DashboardResponse> {
  if (USING_MOCKS) {
    return delay(mockScenario === "surge" ? SURGE_DASHBOARD : HEALTHY_DASHBOARD);
  }
  return request<DashboardResponse>(`/dashboard?${facilityQuery(facilityId)}`);
}

export function getOrders(
  facilityId: string = FACILITY_ID,
  limit = MAX_PAGE_SIZE,
  offset = 0,
  statuses: OrderStatus[] = [],
): Promise<OrdersResponse> {
  if (USING_MOCKS) {
    const rows = statuses.length
      ? scenarioOrders().filter((o) => statuses.includes(o.status))
      : scenarioOrders();
    return delay(mockOrders(rows, limit, offset));
  }
  // Filtering server-side is not an optimisation, it is correctness: scheduled
  // orders sort first, so filtering one page client-side would only ever see
  // PENDING orders and report every other state as empty.
  const filter = statuses.map((s) => `&status=${s}`).join("");
  return request<OrdersResponse>(
    `/orders?${facilityQuery(facilityId)}&limit=${limit}&offset=${offset}${filter}`,
  );
}

export function getAtRiskOrders(
  facilityId: string = FACILITY_ID,
  limit = MAX_PAGE_SIZE,
  offset = 0,
): Promise<OrdersResponse> {
  if (USING_MOCKS) {
    const atRisk = scenarioOrders().filter(
      (o) => o.sla_status === "AT_RISK" || o.sla_status === "BREACHED",
    );
    return delay(mockOrders(atRisk, limit, offset));
  }
  return request<OrdersResponse>(
    `/orders/at-risk?${facilityQuery(facilityId)}&limit=${limit}&offset=${offset}`,
  );
}

/**
 * Looks the order up directly. Filtering the first page client-side could not
 * find an order beyond `MAX_PAGE_SIZE`, so any order past the first 500 read as
 * "dispatched" even while it sat pending in the queue.
 */
export async function getOrder(
  orderId: string,
  facilityId: string = FACILITY_ID,
): Promise<OrderResponse | null> {
  if (USING_MOCKS) {
    const orders = await getOrders(facilityId, MAX_PAGE_SIZE, 0);
    return orders.items.find((o) => o.order_id === orderId) ?? null;
  }
  try {
    return await request<OrderResponse>(
      `/orders/${encodeURIComponent(orderId)}?${facilityQuery(facilityId)}`,
    );
  } catch (error) {
    // A genuinely unknown order is "not found", not a failed request.
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

// --- What-If and recovery -------------------------------------------------

export function createSimulation(
  body: SimulationRequest,
  facilityId: string = FACILITY_ID,
): Promise<SimulationResponse> {
  if (USING_MOCKS) return delay(mockSimulation(body));
  return request<SimulationResponse>(`/simulations?${facilityQuery(facilityId)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Minutes of future arrivals every recovery plan is built against.
 *
 * Inflow levers (pause the promotion, hide express) only change orders that
 * have not arrived yet, so without a horizon they can never be offered. The
 * same value goes to approval, which re-derives the plans: a mismatch would
 * refuse a plan the operator was just shown.
 */
export const RECOVERY_HORIZON_MINUTES = 240;

function horizonQuery() {
  return `projection_horizon_minutes=${RECOVERY_HORIZON_MINUTES}`;
}

export function getRecoveryPlans(
  facilityId: string = FACILITY_ID,
): Promise<RecoveryPlansResponse> {
  if (USING_MOCKS) return delay(MOCK_PLANS);
  return request<RecoveryPlansResponse>(
    `/recovery-plans?${facilityQuery(facilityId)}&${horizonQuery()}`,
  );
}

export function approveRecoveryPlan(
  planId: string,
  facilityId: string = FACILITY_ID,
): Promise<RecoveryApprovalResponse> {
  if (USING_MOCKS) {
    const action = mockApprove(planId);
    return delay({
      plan_id: action.plan_id,
      action_id: action.action_id,
      status: action.status,
    });
  }
  return request<RecoveryApprovalResponse>(
    `/recovery-plans/${encodeURIComponent(planId)}/approve?${facilityQuery(facilityId)}&${horizonQuery()}`,
    { method: "POST" },
  );
}

/**
 * Read-only for the frontend. n8n owns
 * `POST /recovery-actions/{id}/status` — never call it from here, or the UI
 * would be claiming an execution result it did not observe.
 */
/**
 * Action history from the backend rather than this tab's memory. The list
 * endpoint defaults to PENDING (the n8n executor polls it), so a status is
 * always passed explicitly here.
 */
export function listRecoveryActions(
  status: ActionStatus,
  facilityId: string = FACILITY_ID,
): Promise<RecoveryActionResponse[]> {
  if (USING_MOCKS) return delay([]);
  return request<RecoveryActionResponse[]>(
    `/recovery-actions?${facilityQuery(facilityId)}&status=${status}`,
  );
}

export function getRecoveryAction(
  actionId: string,
): Promise<RecoveryActionResponse> {
  if (USING_MOCKS) return delay(mockAction(actionId));
  return request<RecoveryActionResponse>(
    `/recovery-actions/${encodeURIComponent(actionId)}`,
  );
}

/**
 * The operator's own affirmation that a real-world change happened, for an
 * AWAITING_ENACTMENT action on a manual/webhook facility (P6). This is NOT
 * the forbidden n8n callback above -- `/status` claims an execution result
 * this frontend observed, which it never does; `/confirm-enactment` is a
 * human telling the system what they just did, session-gated for exactly
 * that reason.
 */
export function confirmRecoveryActionEnactment(
  actionId: string,
  succeeded: boolean,
  errorDetail?: string,
): Promise<RecoveryActionResponse> {
  if (USING_MOCKS) return delay(mockConfirmEnactment(actionId, succeeded, errorDetail));
  return request<RecoveryActionResponse>(
    `/recovery-actions/${encodeURIComponent(actionId)}/confirm-enactment`,
    {
      method: "POST",
      body: JSON.stringify({ succeeded, error_detail: errorDetail ?? null }),
    },
  );
}

// --- Demo controls --------------------------------------------------------

/**
 * Drives the simulator so a reset or a surge is one click rather than a
 * terminal. The backend disables these unless it is configured with a
 * simulator, so `enabled` false simply means "not a demo deployment".
 */
const DISABLED_DEMO_STATUS: DemoStatus = {
  enabled: false,
  running: false,
  stage: null,
  ticks_done: 0,
  ticks_total: 0,
  detail: null,
  live: false,
  speed: null,
  simulated_time: null,
  sim_hours_elapsed: null,
};

export async function getDemoStatus(): Promise<DemoStatus> {
  if (USING_MOCKS) {
    return delay(DISABLED_DEMO_STATUS);
  }
  try {
    return await request<DemoStatus>("/demo/status");
  } catch (error) {
    // A backend without demo controls answers 404. That is a configuration
    // fact, not a failure, so the controls simply do not render.
    if (error instanceof ApiError && error.status === 404) {
      return DISABLED_DEMO_STATUS;
    }
    throw error;
  }
}

/** Resets one demo facility; the backend requires naming it and refuses non-demo ones. */
export function resetDemo(facilityId: string = FACILITY_ID): Promise<DemoStatus> {
  return request<DemoStatus>(`/demo/reset?${facilityQuery(facilityId)}`, { method: "POST" });
}

/**
 * Starts a surge, deliberately without naming a tick count.
 *
 * How long a surge must run to be worth showing is a property of the promise
 * horizon, which is the backend's to know: too short and nothing is ever late,
 * because express orders are a fifth of arrivals against a floor that can serve
 * them. This asked for 12 while the backend had moved to 24, and quietly
 * overrode it -- the button produced a surge with zero breaches while the same
 * endpoint called without a body produced hundreds.
 */
export function startSurge(): Promise<DemoStatus> {
  return request<DemoStatus>("/demo/surge", {
    method: "POST",
    body: JSON.stringify({ stage: "SURGE_3" }),
  });
}

/** Starts the continuous live clock at the given simulated-time multiplier. */
export function startLive(speed: number): Promise<DemoStatus> {
  return request<DemoStatus>("/demo/live/start", {
    method: "POST",
    body: JSON.stringify({ speed }),
  });
}

export function stopLive(): Promise<DemoStatus> {
  return request<DemoStatus>("/demo/live/stop", { method: "POST" });
}

// --- Auth -----------------------------------------------------------------

/**
 * Auth deliberately ignores USING_MOCKS: there is no useful way to fake a
 * session, and a mocked sign-in would hide exactly the bug worth catching (the
 * cookie not being sent). Mock mode instead skips the guard entirely — see
 * `useSession` — so the dashboard still runs against fixtures with no backend.
 */

export function getPasswordPolicy(): Promise<PasswordPolicy> {
  return request<PasswordPolicy>("/auth/password-policy");
}

export function signup(
  email: string,
  password: string,
  profile: SignupProfile = {},
): Promise<UserResponse> {
  return request<UserResponse>("/auth/signup", {
    method: "POST",
    body: JSON.stringify({ email, password, ...profile }),
  });
}

export function login(
  email: string,
  password: string,
  rememberMe = false,
): Promise<StatusResponse> {
  return request<StatusResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password, remember_me: rememberMe }),
  });
}

export function logout(): Promise<StatusResponse> {
  return request<StatusResponse>("/auth/logout", { method: "POST" });
}

export function getMe(): Promise<MeResponse> {
  return request<MeResponse>("/me");
}

/**
 * Always resolves the same way whether or not the address is registered. The
 * backend returns an identical body by design, so the form must not claim "no
 * account with that email" — there is nothing here that would distinguish it.
 */
export function requestPasswordReset(email: string): Promise<StatusResponse> {
  return request<StatusResponse>("/auth/reset/request", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export function confirmPasswordReset(
  token: string,
  password: string,
): Promise<StatusResponse> {
  return request<StatusResponse>("/auth/reset/confirm", {
    method: "POST",
    body: JSON.stringify({ token, password }),
  });
}

/**
 * Consume an emailed verification token. Unlike the reset check below this is
 * single-use: call it once per token (see VerifyEmailPage), never on a loop.
 */
export function verifyEmail(token: string): Promise<string> {
  return requestText(`/auth/verify?token=${encodeURIComponent(token)}`);
}

/** The emailed reset link is plain text, and this read never consumes it. */
export function validatePasswordResetToken(token: string): Promise<string> {
  return requestText(`/auth/reset/confirm?token=${encodeURIComponent(token)}`);
}

/**
 * Mint a fresh verification link. Answers identically whether the address is
 * unknown, already verified, or genuinely waiting — so, like the reset
 * request, nothing here can be used to discover which addresses exist.
 */
export function resendVerification(email: string): Promise<StatusResponse> {
  return request<StatusResponse>("/auth/verify/resend", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}


// --- Alerting -------------------------------------------------------------

/** Everyone currently on the alert list. */
export function listAlertRecipients(): Promise<AlertRecipientsResponse> {
  if (USING_MOCKS) return Promise.resolve(mockAlertRecipients());
  return request<AlertRecipientsResponse>("/alerts/recipients");
}

/** Add somebody to the alert list. Rejects a duplicate address with 409. */
export function addAlertRecipient(
  name: string,
  email: string,
  minLevel: SurgeRiskLevel,
): Promise<AlertRecipient> {
  if (USING_MOCKS) return Promise.resolve(mockAddAlertRecipient(name, email, minLevel));
  return request<AlertRecipient>("/alerts/recipients", {
    method: "POST",
    body: JSON.stringify({ name, email, min_level: minLevel }),
  });
}

/**
 * Remove somebody from the alert list.
 *
 * A soft delete on the backend, so alerts they were already sent stay in the
 * history. Answers 204 for an unknown id too -- after either outcome they are
 * not on the list.
 */
export function removeAlertRecipient(recipientId: string): Promise<void> {
  if (USING_MOCKS) return Promise.resolve(mockRemoveAlertRecipient(recipientId));
  return request<void>(`/alerts/recipients/${encodeURIComponent(recipientId)}`, {
    method: "DELETE",
  });
}

/** Recently decided alerts, and whether each actually reached its recipient. */
export function listAlertHistory(): Promise<AlertHistoryResponse> {
  if (USING_MOCKS) return Promise.resolve(mockAlertHistory());
  return request<AlertHistoryResponse>("/alerts/history");
}
