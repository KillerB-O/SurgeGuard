import { Link } from "react-router-dom";

import { useAuth } from "@/contexts/AuthContext";
import { USING_MOCKS } from "@/lib/api";

import { DemoControls } from "../components/DemoControls";
import { GapBar } from "../components/GapBar";
import { Failed, Loading } from "../components/States";
import { useDashboard } from "../lib/queries";
import { formatDuration, formatNumber } from "../lib/format";

/**
 * When the earliest promise the scheduler expects to miss falls due.
 *
 * This is a deadline, not a forecast: it is the promised time of the soonest
 * order already predicted to breach. Ahead of now it is the window left to act
 * on that order; behind now the window has closed and the miss is real, which
 * is why the sentence changes rather than the sign on a number. Nothing is
 * shown when nothing is predicted to breach -- an absent deadline is not a
 * deadline of zero.
 */
function FirstBreachNote({ minutes }: { minutes: number | null }) {
  if (minutes === null) return null;

  const elapsed = minutes < 0;
  return (
    <p className={elapsed ? "note elapsed" : "note"}>
      {elapsed ? (
        <>
          The earliest promise now expected to be missed fell due{" "}
          <strong>{formatDuration(minutes)} ago</strong>. Capacity cannot
          recover that one; only the promise can change.
        </>
      ) : (
        <>
          The earliest promise now expected to be missed falls due in{" "}
          <strong>{formatDuration(minutes)}</strong> — the window left to act on
          it.
        </>
      )}
    </p>
  );
}

/** OBSERVE: the whole operational picture on one screen. */
export function CommandCenter() {
  const { data, isLoading, isError, error } = useDashboard();
  const { user } = useAuth();

  if (isLoading) return <Loading label="Reading facility state" />;
  if (isError) return <Failed error={error} />;
  if (!data) return null;

  const exposed = data.sla_counts.at_risk + data.sla_counts.breached;
  const pending = data.order_states.pending;

  const sla: Array<[string, number, string]> = [
    ["Breached", data.sla_counts.breached, "var(--breached)"],
    ["At risk", data.sla_counts.at_risk, "var(--at-risk)"],
    ["Watch", data.sla_counts.watch, "var(--watch)"],
    ["Safe", data.sla_counts.safe, "var(--safe)"],
  ];

  const states: Array<[string, number]> = [
    ["Pending", data.order_states.pending],
    ["Picking", data.order_states.picking],
    ["Packed", data.order_states.packed],
    ["Ready", data.order_states.ready],
    ["Dispatched", data.order_states.dispatched],
  ];

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Command centre</h1>
        <p>
          Everything below is computed by the backend scheduler from persisted
          order state. Figures refresh every few seconds.
        </p>
      </div>

      {/* Every demo POST is admin-only on the backend; nobody else sees buttons that would 403. */}
      {(USING_MOCKS || user?.is_admin) && <DemoControls />}

      {data.throughput_stalled && (
        <div className="notice warn" role="status">
          <strong>Floor stalled.</strong> Orders are waiting and nothing has
          moved through the floor in the last hour. Throughput shows 0 and risk
          is held at HIGH or above until work moves again.
        </div>
      )}

      <GapBar
        demand={data.demand_work_units_per_hour}
        throughput={data.fulfillment_work_units_per_hour}
      />

      <div className="grid-2">
        <section className="panel">
          <div className="panel-title">
            <h2>Dispatch risk</h2>
            <span className="hint">of {formatNumber(pending)} pending orders</span>
          </div>

          <div className="metric-row">
            {sla.map(([label, value, colour]) => (
              <div className="metric" key={label}>
                <span className="label">{label}</span>
                <span className="value num" style={{ color: colour }}>
                  {formatNumber(value)}
                </span>
              </div>
            ))}
          </div>

          <FirstBreachNote minutes={data.minutes_to_first_breach} />

          <p style={{ marginTop: 12, marginBottom: 0 }}>
            Late dispatch rate{" "}
            <strong>{formatNumber(data.late_dispatch_rate * 100, 1)}%</strong>{" "}
            against a {formatNumber(data.late_dispatch_threshold * 100, 0)}%
            ceiling (Amazon's Late Shipment Rate limit).{" "}
            {data.late_dispatch_breaching && (
              <span className="badge breached">Over the ceiling</span>
            )}
          </p>
          <p style={{ marginTop: 4, marginBottom: 0, color: "var(--muted)", fontSize: "0.875rem" }}>
            {formatNumber(data.realised_breaches)} realised{" "}
            {data.realised_breaches === 1 ? "miss is" : "misses are"} already
            past their promise -- dispatched late, or still waiting with the
            deadline gone. A receipt, not a warning; no recovery undoes them.
            {" "}
            {formatNumber(data.preventable_breaches)} more{" "}
            {data.preventable_breaches === 1 ? "is" : "are"} still predicted to
            miss but have not dispatched yet -- still saveable.
          </p>

          {exposed > 0 && (
            <p style={{ marginBottom: 0, marginTop: 18 }}>
              <Link className="row-link" to="/dashboard/at-risk">
                Open the {formatNumber(exposed)} orders that will miss their promise
              </Link>
            </p>
          )}
        </section>

        <section className="panel">
          <div className="panel-title">
            <h2>Backlog</h2>
            <span className="hint">unfinished work in the building</span>
          </div>

          <div className="metric-row">
            <div className="metric">
              <span className="label">Orders</span>
              <span className="value num">{formatNumber(data.backlog_orders)}</span>
              <span className="sub">pending, picking, packed, ready</span>
            </div>
            <div className="metric">
              <span className="label">Work units</span>
              <span className="value num">
                {formatNumber(data.backlog_work_units, 1)}
              </span>
              <span className="sub">
                {data.fulfillment_work_units_per_hour > 0 ? (
                  <>
                    about{" "}
                    {formatNumber(
                      data.backlog_work_units / data.fulfillment_work_units_per_hour,
                      1,
                    )}
                    h at current throughput
                  </>
                ) : (
                  "not clearing at current throughput"
                )}
              </span>
            </div>
            <div className="metric">
              <span className="label">Promise policy</span>
              <span className="value num">{data.dispatch_promise_hours}h</span>
              <span className="sub">applies to new orders</span>
            </div>
          </div>
        </section>
      </div>

      <section className="panel">
        <div className="panel-title">
          <h2>Where the work sits</h2>
          <span className="hint">
            only pending orders carry a dispatch-risk classification
          </span>
        </div>
        <div className="metric-row">
          {states.map(([label, value]) => (
            <div className="metric" key={label}>
              <span className="label">{label}</span>
              <span className="value num">{formatNumber(value)}</span>
            </div>
          ))}
        </div>
      </section>

      {exposed > 0 && (
        <div className="notice warn">
          Demand is outrunning the floor.{" "}
          <Link className="row-link" to="/dashboard/what-if">
            Try a capacity change
          </Link>{" "}
          before choosing a recovery plan.
        </div>
      )}
    </div>
  );
}
