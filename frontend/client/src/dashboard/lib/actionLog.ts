/**
 * Approved action ids for this session.
 *
 * There is no "list actions" endpoint, only
 * `GET /recovery-actions/{action_id}`, so the frontend remembers which ids it
 * submitted and reads each one back. This is a navigation aid, not operational
 * state: the backend remains the source of truth for every status.
 */

const entries: Array<{ actionId: string; planId: string }> = [];

export function rememberAction(actionId: string, planId: string) {
  if (!entries.some((entry) => entry.actionId === actionId)) {
    entries.unshift({ actionId, planId });
  }
}

export function listActions() {
  return entries;
}
