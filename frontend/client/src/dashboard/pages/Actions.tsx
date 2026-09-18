import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { confirmRecoveryActionEnactment } from "@/lib/api";
import { ActionBadge, VerificationBadge } from "../components/Badges";
import { Empty, Failed } from "../components/States";
import { listActions } from "../lib/actionLog";
import { keys, useRecoveryAction, useRecoveryActions } from "../lib/queries";
import { formatDateTime, formatNumber } from "../lib/format";

/**
 * Execution log. The frontend only reads action state for the simulator
 * adapter's path; n8n owns the write that moves that kind of action to
 * SUCCESS or FAILED. A manual/webhook facility's action is different: the
 * operator's own confirm-enactment click is what moves AWAITING_ENACTMENT to
 * ENACTED or FAILED (P6), and that write belongs here.
 */
export function Actions() {
  const [params] = useSearchParams();
  const highlight = params.get("highlight");

  // Server-side history, so approvals survive a refresh. The in-memory log is
  // kept only as an ordering hint for actions this tab submitted; the backend
  // remains the source of truth for every status.
  const pending = useRecoveryActions("PENDING");
  const awaitingEnactment = useRecoveryActions("AWAITING_ENACTMENT");
  const enacted = useRecoveryActions("ENACTED");
  const succeeded = useRecoveryActions("SUCCESS");
  const failed = useRecoveryActions("FAILED");

  const server = [
    ...(pending.data ?? []),
    ...(awaitingEnactment.data ?? []),
    ...(enacted.data ?? []),
    ...(succeeded.data ?? []),
    ...(failed.data ?? []),
  ].sort((a, b) => b.created_at.localeCompare(a.created_at));

  const seen = new Set(server.map((action) => action.action_id));
  const submitted = [
    ...server.map((action) => ({
      actionId: action.action_id,
      planId: action.plan_id,
    })),
    ...listActions().filter((entry) => !seen.has(entry.actionId)),
  ];

  if (submitted.length === 0) {
    return (
      <div className="stack">
        <div className="page-head">
          <h1>Actions</h1>
        </div>
        <Empty title="Nothing has been approved yet">
          <p>Approved recovery plans and their execution state appear here.</p>
        </Empty>
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Actions</h1>
        <p>
          What the operator approved, and what n8n reported back. An action stays
          pending until the external system confirms it.
        </p>
      </div>

      {submitted.map((entry) => (
        <ActionRow
          key={entry.actionId}
          actionId={entry.actionId}
          planId={entry.planId}
          highlighted={entry.actionId === highlight}
        />
      ))}
    </div>
  );
}

function ActionRow({
  actionId,
  planId,
  highlighted,
}: {
  actionId: string;
  planId: string;
  highlighted: boolean;
}) {
  const { data, isError, error } = useRecoveryAction(actionId);

  return (
    <section
      className="panel"
      style={highlighted ? { borderColor: "var(--rule-strong)", borderWidth: 2 } : undefined}
    >
      <div className="panel-title">
        <h2>{planId}</h2>
        {data ? <ActionBadge status={data.status} /> : <span className="hint">reading…</span>}
      </div>

      {isError && <Failed error={error} />}

      {data && (
        <>
          <div className="metric-row">
            <div className="metric">
              <span className="label">Capacity target</span>
              <span className="value num">
                {formatNumber(data.capacity_per_hour, 1)} wu/hr
              </span>
            </div>
            <div className="metric">
              <span className="label">Approved</span>
              <span className="value">{formatDateTime(data.created_at)}</span>
            </div>
            <div className="metric">
              <span className="label">Last update</span>
              <span className="value">{formatDateTime(data.updated_at)}</span>
            </div>
          </div>

          {data.status === "PENDING" && (
            <p className="help" style={{ marginBottom: 0 }}>
              Waiting for n8n to confirm the change on the operational side.
            </p>
          )}

          {data.status === "AWAITING_ENACTMENT" && (
            <ConfirmEnactment actionId={actionId} />
          )}

          {data.status === "ENACTED" && (
            <p className="help" style={{ marginBottom: 0 }}>
              Confirmed by an operator. This is not proof the change took
              effect -- see verification below once it resolves.
            </p>
          )}

          {data.status === "FAILED" && (
            <div className="notice error" style={{ marginTop: 14 }}>
              Execution failed. {data.error_detail}
            </div>
          )}

          {data.status === "SUCCESS" && (
            <p className="help" style={{ marginBottom: 0 }}>
              Executed. The dashboard changes once the next fulfillment snapshot
              reports the new throughput.
            </p>
          )}

          {data.capacity_commitments.length > 0 && (
            <div style={{ marginTop: 14 }}>
              {data.capacity_commitments.map((commitment) => (
                <div
                  key={commitment.lever_id}
                  style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}
                >
                  <VerificationBadge status={commitment.verification_status} />
                  <span className="help" style={{ margin: 0 }}>
                    {commitment.lever_id}: targeted{" "}
                    {formatNumber(commitment.target_work_units_per_hour, 1)} wu/hr
                    {commitment.observed_work_units_per_hour !== null
                      ? `, floor measured at ${formatNumber(commitment.observed_work_units_per_hour, 1)} wu/hr`
                      : ", not yet measured"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <p className="help" style={{ marginBottom: 0, marginTop: 10 }}>
        {actionId}
      </p>
    </section>
  );
}

/**
 * An operator affirming a real-world change happened, for a manual/webhook
 * facility's AWAITING_ENACTMENT action (P6). Mirrors PlanDetail.tsx's
 * two-step approve confirmation deliberately: this is the same class of
 * decision -- a click that cannot be taken back -- and should read the same
 * way to an operator who has seen that pattern once already.
 */
function ConfirmEnactment({ actionId }: { actionId: string }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const onSettled = () => {
    queryClient.invalidateQueries({ queryKey: keys.action(actionId) });
    queryClient.invalidateQueries({ queryKey: ["recovery-actions"] });
  };

  const succeed = useMutation({
    mutationFn: () => confirmRecoveryActionEnactment(actionId, true),
    onSuccess: onSettled,
  });
  const fail = useMutation({
    mutationFn: () =>
      confirmRecoveryActionEnactment(actionId, false, "operator reported it did not happen"),
    onSuccess: onSettled,
  });

  return (
    <div>
      <p style={{ marginTop: 0 }}>
        This facility executes manually. Confirm once the change has actually
        happened on the floor -- confirming does not mean it worked, only that
        it was done; verification against measured throughput follows
        separately.
      </p>
      {succeed.isError && <Failed error={succeed.error} />}
      {fail.isError && <Failed error={fail.error} />}
      {confirming ? (
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <button
            className="btn"
            onClick={() => succeed.mutate()}
            disabled={succeed.isPending || fail.isPending}
          >
            {succeed.isPending ? "Confirming" : "Yes, it happened"}
          </button>
          <button
            className="btn danger"
            onClick={() => fail.mutate()}
            disabled={succeed.isPending || fail.isPending}
          >
            {fail.isPending ? "Reporting" : "No, it didn't"}
          </button>
          <button className="btn secondary" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      ) : (
        <button className="btn" onClick={() => setConfirming(true)}>
          Confirm enactment
        </button>
      )}
    </div>
  );
}
