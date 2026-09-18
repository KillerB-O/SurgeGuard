import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { approveRecoveryPlan } from "@/lib/api";
import { RiskBadge } from "../components/Badges";
import { SummaryDelta } from "../components/SummaryDelta";
import { Empty, Failed, Loading } from "../components/States";
import { rememberAction } from "../lib/actionLog";
import { useDashboard, useRecoveryPlans } from "../lib/queries";
import { formatNumber } from "../lib/format";

/**
 * Approval is deliberately two steps. The endpoint only creates a PENDING
 * action; n8n reports the real outcome later, so this page never claims
 * success (doc 08 section 7).
 */
export function PlanDetail() {
  const { planId } = useParams();
  const navigate = useNavigate();
  const plans = useRecoveryPlans();
  const dashboard = useDashboard();
  const [confirming, setConfirming] = useState(false);

  const approve = useMutation({
    mutationFn: () => approveRecoveryPlan(planId as string),
    onSuccess: (response) => {
      rememberAction(response.action_id, response.plan_id);
      navigate(`/dashboard/actions?highlight=${response.action_id}`);
    },
  });

  if (plans.isLoading) return <Loading label="Loading the plan" />;
  if (plans.isError) return <Failed error={plans.error} />;

  const plan = plans.data?.plans.find((item) => item.plan_id === planId);

  if (!plan) {
    return (
      <Empty title={`No plan called ${planId}`}>
        <p>
          <Link className="row-link" to="/dashboard/recovery">
            Back to recovery plans
          </Link>
        </p>
      </Empty>
    );
  }

  const liveCapacity = dashboard.data?.fulfillment_work_units_per_hour;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{plan.title}</h1>
        <p>
          <Link className="row-link" to="/dashboard/recovery">
            Recovery plans
          </Link>{" "}
          · {plan.description}
        </p>
      </div>

      <section className="panel">
        <div className="panel-title">
          <h2>What this plan changes</h2>
          <span className="hint">{plan.relative_cost} cost</span>
        </div>

        <div className="metric-row">
          <div className="metric">
            <span className="label">Capacity target</span>
            <span className="value num">
              {formatNumber(plan.actions.capacity_per_hour, 1)} wu/hr
            </span>
            <span className="sub">
              {liveCapacity !== undefined
                ? `from ${formatNumber(liveCapacity, 1)} today`
                : ""}
            </span>
          </div>
          <div className="metric">
            <span className="label">Promise for new orders</span>
            <span className="value num">{plan.actions.dispatch_promise_hours}h</span>
            <span className="sub">existing orders keep their deadline</span>
          </div>
          <div className="metric">
            <span className="label">Facility risk after</span>
            <span className="value">
              <RiskBadge level={plan.projected.risk_level} />
            </span>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>Projected outcome</h2>
          <span className="hint">same scheduler, changed assumptions</span>
        </div>
        {dashboard.data && (
          <SummaryDelta
            baseline={{
              backlog_orders: dashboard.data.backlog_orders,
              backlog_work_units: dashboard.data.backlog_work_units,
              safe_count: dashboard.data.sla_counts.safe,
              watch_count: dashboard.data.sla_counts.watch,
              at_risk_count: dashboard.data.sla_counts.at_risk,
              breached_count: dashboard.data.sla_counts.breached,
              breached_managed_count: 0,
              already_late_count: dashboard.data.realised_breaches,
              saveable_count: dashboard.data.preventable_breaches,
              projected_arrivals: null,
              projected_recovery_hours: null,
              risk_level: dashboard.data.risk_level,
            }}
            simulated={plan.projected}
            baselineLabel="Now"
            simulatedLabel="With this plan"
          />
        )}
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>Approve</h2>
        </div>

        {approve.isError && <Failed error={approve.error} />}

        <p style={{ marginTop: 0 }}>
          Approving sends this plan to n8n, which changes capacity on the
          operational side. The dashboard only moves once fresh telemetry
          arrives from the floor.
        </p>

        {confirming ? (
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <button
              className="btn danger"
              onClick={() => approve.mutate()}
              disabled={approve.isPending}
            >
              {approve.isPending ? "Sending" : `Approve ${plan.title}`}
            </button>
            <button className="btn secondary" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <button className="btn" onClick={() => setConfirming(true)}>
            Approve this plan
          </button>
        )}
      </section>
    </div>
  );
}
