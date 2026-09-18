/**
 * Shared React Query hooks.
 *
 * Polling matches doc 06 section 9 (every 2-3 seconds). Query keys are shared
 * so navigating between pages reuses cached data instead of blanking panels.
 */

import { useMutation, useQuery } from "@tanstack/react-query";

import type { ActionStatus, OrderStatus } from "@/lib/types";

import {
  FACILITY_ID,
  MAX_PAGE_SIZE,
  askRecoveryChat,
  getAtRiskOrders,
  getDashboard,
  getDemoStatus,
  getOrder,
  getOrders,
  getRecoveryAction,
  getRecoveryPlans,
  listAlertHistory,
  listAlertRecipients,
  listRecoveryActions,
} from "@/lib/api";

export const POLL_MS = 2500;

export const keys = {
  dashboard: (facilityId: string) => ["dashboard", facilityId] as const,
  orders: (facilityId: string) => ["orders", facilityId] as const,
  order: (facilityId: string, orderId: string) =>
    ["orders", facilityId, "detail", orderId] as const,
  atRisk: (facilityId: string) => ["orders", facilityId, "at-risk"] as const,
  plans: (facilityId: string) => ["recovery-plans", facilityId] as const,
  action: (actionId: string) => ["recovery-action", actionId] as const,
  actions: (facilityId: string, status: string) =>
    ["recovery-actions", facilityId, status] as const,
};

export function useDashboard(facilityId: string = FACILITY_ID) {
  return useQuery({
    queryKey: keys.dashboard(facilityId),
    queryFn: () => getDashboard(facilityId),
    refetchInterval: POLL_MS,
  });
}

export function useOrders(
  facilityId: string = FACILITY_ID,
  statuses: OrderStatus[] = [],
) {
  return useQuery({
    queryKey: [...keys.orders(facilityId), ...statuses],
    queryFn: () => getOrders(facilityId, MAX_PAGE_SIZE, 0, statuses),
    refetchInterval: POLL_MS,
  });
}

/**
 * Reads one order directly. Filtering the orders list client-side could not see
 * past its page, so any order beyond the first 500 looked like it did not exist.
 */
export function useOrder(orderId: string | undefined, facilityId: string = FACILITY_ID) {
  return useQuery({
    queryKey: keys.order(facilityId, orderId ?? ""),
    queryFn: () => getOrder(orderId as string, facilityId),
    enabled: Boolean(orderId),
    refetchInterval: POLL_MS,
  });
}

export function useAtRiskOrders(facilityId: string = FACILITY_ID) {
  return useQuery({
    queryKey: keys.atRisk(facilityId),
    queryFn: () => getAtRiskOrders(facilityId),
    refetchInterval: POLL_MS,
  });
}

/** Recovery plans are read on demand; they do not need a 2.5s poll. */
export function useRecoveryPlans(facilityId: string = FACILITY_ID) {
  return useQuery({
    queryKey: keys.plans(facilityId),
    queryFn: () => getRecoveryPlans(facilityId),
    staleTime: 10_000,
  });
}

/**
 * Grounded Q&A against one recovery plan. Stateless on the backend, so this
 * is a bare mutation with no query key: there is nothing to cache or
 * invalidate, and the visible transcript is owned entirely by the caller.
 */
export function useRecoveryChat() {
  return useMutation({
    mutationFn: ({
      planId,
      question,
      facilityId,
    }: {
      planId: string;
      question: string;
      facilityId?: string;
    }) => askRecoveryChat(planId, question, facilityId),
  });
}

// Statuses still waiting on something outside this tab: n8n's poll (PENDING),
// or another tab/operator confirming enactment (AWAITING_ENACTMENT). ENACTED
// is deliberately excluded -- its execution process is finished, only
// verification is outstanding, and that resolves on the alert-worker tick,
// not by this page polling faster (P6).
const OPEN_STATUSES: ActionStatus[] = ["PENDING", "AWAITING_ENACTMENT"];

/**
 * Server-side action history, so approvals survive a page refresh. The
 * in-memory log only ever knew about this tab.
 */
export function useRecoveryActions(
  status: ActionStatus,
  facilityId: string = FACILITY_ID,
) {
  return useQuery({
    queryKey: keys.actions(facilityId, status),
    queryFn: () => listRecoveryActions(status, facilityId),
    refetchInterval: OPEN_STATUSES.includes(status) ? POLL_MS : false,
  });
}

/** Stops polling once n8n has reported a terminal state, or once ENACTED. */
export function useRecoveryAction(actionId: string | null) {
  return useQuery({
    queryKey: keys.action(actionId ?? ""),
    queryFn: () => getRecoveryAction(actionId as string),
    enabled: Boolean(actionId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && OPEN_STATUSES.includes(status) ? POLL_MS : false;
    },
  });
}

/**
 * Polls only while a surge or the live clock is running, so an idle dashboard
 * is not asking a question whose answer cannot change.
 */
export function useDemoStatus() {
  return useQuery({
    queryKey: ["demo-status"],
    queryFn: getDemoStatus,
    refetchInterval: (query) =>
      query.state.data?.running || query.state.data?.live ? POLL_MS : false,
  });
}

/** The alert list. Changes only when an operator edits it, so it does not poll. */
export function useAlertRecipients() {
  return useQuery({
    queryKey: ["alert-recipients"],
    queryFn: listAlertRecipients,
  });
}

/**
 * Recent alerts and their delivery state.
 *
 * Polls, unlike the recipient list: the worker queues and sends on its own
 * schedule, so this changes without anybody touching the page. A PENDING row
 * that never became SENT is exactly what an operator needs to notice.
 */
export function useAlertHistory() {
  return useQuery({
    queryKey: ["alert-history"],
    queryFn: listAlertHistory,
    refetchInterval: POLL_MS,
  });
}
