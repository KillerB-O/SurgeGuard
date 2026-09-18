import { Link } from "react-router-dom";

import { RiskBadge } from "../components/Badges";
import { Empty, Failed, Loading } from "../components/States";
import { useDashboard, useRecoveryPlans } from "../lib/queries";
import { formatDateTime, formatHours, formatNumber, signed } from "../lib/format";

export function Recovery() {
  const { data, isLoading, isError, error } = useRecoveryPlans();
  const dashboard = useDashboard();

  if (isLoading) return <Loading label="Projecting recovery plans" />;
  if (isError) return <Failed error={error} />;
  if (!data) return null;

  if (data.plans.length === 0) {
    return <Empty title="No recovery plans available yet" />;
  }

  const liveCapacity = dashboard.data?.fulfillment_work_units_per_hour ?? null;
  const worst = Math.max(
    ...data.plans.map((p) => p.projected.at_risk_count + p.projected.breached_count),
  );

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Recovery plans</h1>
        <p>
          Each projection is the real scheduler run under that plan's
          assumptions. A recommendation is a suggestion, not an approval.
        </p>
      </div>

      <div className="plan-grid">
        {data.plans.map((plan) => {
          const exposed =
            plan.projected.at_risk_count + plan.projected.breached_count;
          const noChange =
            liveCapacity !== null &&
            plan.actions.capacity_per_hour === liveCapacity &&
            exposed === worst;

          // A queue lever changes who misses, never how many. When the backend
          // says a plan only moved exposure, the card must not read as a save.
          const redistributes = plan.outcome?.is_redistribution ?? false;

          return (
            <section
              className={`panel plan-card${plan.recommended ? " is-recommended" : ""}`}
              key={plan.plan_id}
            >
              <div className="panel-title plan-card-head">
                <h2>{plan.title}</h2>
                {plan.recommended && <span className="badge safe">Suggested</span>}
              </div>

              <div className="plan-card-copy">
                <p className="plan-card-description">{plan.description}</p>

                {plan.families.length > 0 && (
                  <div className="plan-card-families">
                    {plan.families.map((family) => (
                      <span key={family} className="badge">{family}</span>
                    ))}
                  </div>
                )}

                {plan.trades && (
                  <p className="plan-card-note">
                    <strong>Trades:</strong> {plan.trades}
                  </p>
                )}

                {redistributes && (
                  <p className="plan-card-warning">
                    Redistribution, not recovery. This moves which orders miss
                    rather than reducing how many.
                  </p>
                )}
              </div>

              {/*
                Plans are compared on what they can still change. Orders already
                past their promise are lost under every plan, so leading with the
                combined total made every card read the same once they piled up.
              */}
              <div className="metric">
                <span className="label">Saveable orders still exposed</span>
                <span
                  className="value num"
                  style={{ color: plan.projected.saveable_count === 0 ? "var(--safe)" : "var(--at-risk)" }}
                >
                  {formatNumber(plan.projected.saveable_count)}
                </span>
                <span className="sub">
                  {plan.projected.breached_count} breached ·{" "}
                  {plan.projected.at_risk_count} at risk
                </span>
                {plan.projected.already_late_count > 0 && (
                  <span className="plan-card-lost">
                    +{formatNumber(plan.projected.already_late_count)} already past their
                    promise; no plan can save these
                  </span>
                )}
              </div>

              <table className="plan-card-table">
                <tbody>
                  <tr>
                    <td>Capacity</td>
                    <td className="right num">
                      {formatNumber(plan.actions.capacity_per_hour, 1)} wu/hr
                      {liveCapacity !== null && (
                        <span style={{ color: "var(--muted)" }}>
                          {" "}
                          ({signed(plan.actions.capacity_per_hour - liveCapacity, 1)})
                        </span>
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>Queue clears in</td>
                    <td className="right num">
                      {formatHours(plan.projected.projected_recovery_hours)}
                    </td>
                  </tr>
                  {/*
                    Two plans that both clear the exposure show an identical 0
                    above, because exposure cannot go below none. What separates
                    them is headroom: how many orders still land close to their
                    promise, and would fall back into risk on the next arrival.
                  */}
                  <tr>
                    <td>Cutting it fine</td>
                    <td className="right num">
                      {formatNumber(plan.projected.watch_count)}
                      <span style={{ color: "var(--muted)" }}> orders</span>
                    </td>
                  </tr>
                  {plan.outcome && (
                    <tr>
                      <td>Breaches prevented</td>
                      <td className="right num">
                        <span
                          style={{
                            color:
                              plan.outcome.net_breach_change > 0
                                ? "var(--safe)"
                                : undefined,
                          }}
                        >
                          {formatNumber(plan.outcome.net_breach_change)}
                        </span>
                        {plan.outcome.breaches_relocated > 0 && (
                          <span style={{ color: "var(--muted)" }}>
                            {" "}
                            ({plan.outcome.breaches_relocated} moved)
                          </span>
                        )}
                      </td>
                    </tr>
                  )}
                  {plan.effective_at && (
                    <tr>
                      <td>In effect from</td>
                      <td className="right num">
                        {formatDateTime(plan.effective_at)}
                      </td>
                    </tr>
                  )}
                  <tr>
                    <td>Facility risk</td>
                    <td className="right">
                      <RiskBadge level={plan.projected.risk_level} />
                    </td>
                  </tr>
                  <tr>
                    <td>Relative cost</td>
                    <td className="right">{plan.relative_cost}</td>
                  </tr>
                </tbody>
              </table>

              <div className="plan-card-foot">
                {noChange && (
                  <p className="plan-card-caution">
                    Adds no capacity, so it leaves the queue as it is.
                  </p>
                )}
                <Link className="btn plan-card-cta" to={`/dashboard/recovery/${plan.plan_id}`}>
                  Review this plan
                </Link>
              </div>
            </section>
          );
        })}
      </div>

      {data.unavailable_levers.length > 0 && (
        <section className="panel">
          <div className="panel-title">
            <h2>Not available right now</h2>
          </div>
          <p style={{ marginTop: 0, color: "var(--muted)", fontSize: "0.875rem" }}>
            Shown rather than hidden, so the decision window is visible. A lever
            that lands after the carrier collection cannot rescue today's
            dispatch.
          </p>
          <table>
            <tbody>
              {data.unavailable_levers.map((lever) => (
                <tr key={lever.id} style={{ color: "var(--muted)" }}>
                  <td>
                    {lever.name}{" "}
                    <span className="badge">{lever.family}</span>
                  </td>
                  <td className="right">
                    {lever.unavailable_reason}
                    <span style={{ marginLeft: 8 }}>
                      ({lever.lead_time_minutes}m lead)
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
