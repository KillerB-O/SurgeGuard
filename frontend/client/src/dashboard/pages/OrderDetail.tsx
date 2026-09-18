import { Link, useParams } from "react-router-dom";

import { RiskBadge, SlaBadge, StatusBadge } from "../components/Badges";
import { Empty, Failed, Loading } from "../components/States";
import { useDashboard, useOrder } from "../lib/queries";
import {
  formatCurrency,
  formatDateTime,
  formatHours,
  hoursBetween,
} from "../lib/format";

/**
 * The "why" panel. Every number here is read from the backend response; the
 * only arithmetic is subtracting two timestamps the scheduler already produced.
 */
export function OrderDetail() {
  const { orderId } = useParams();
  // Read the order directly: filtering the orders list could not see past its
  // page, so anything beyond the first 500 looked like it had been dispatched.
  const { data: order, isLoading, isError, error } = useOrder(orderId);
  const dashboard = useDashboard();

  if (isLoading) return <Loading label="Looking up the order" />;
  if (isError) return <Failed error={error} />;

  if (!order) {
    return (
      <Empty title={`No order called ${orderId}`}>
        <p>
          No order with that id exists at this facility.{" "}
          <Link className="row-link" to="/dashboard/orders">
            Back to orders
          </Link>
        </p>
      </Empty>
    );
  }

  const slack = order.predicted_dispatch_at
    ? hoursBetween(order.predicted_dispatch_at, order.promised_dispatch_at)
    : null;

  // The backend totals this from the whole queue, including work already on
  // the floor. Deriving it from a loaded page could only ever understate it.
  const ahead = order.work_units_ahead;

  // Finishing the work is not dispatching it: an order that misses the carrier
  // collection waits for the next one, and that gap is worth naming.
  const missesCollection =
    order.work_complete_at !== null &&
    order.predicted_dispatch_at !== null &&
    order.predicted_dispatch_at !== order.work_complete_at;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>{order.order_id}</h1>
        <p>
          <Link className="row-link" to="/dashboard/orders">
            Orders
          </Link>{" "}
          · {order.item_count} items · {formatCurrency(order.order_value)} ·{" "}
          {order.work_units.toFixed(1)} work units
        </p>
      </div>

      <section className="panel">
        <div className="panel-title">
          <h2>Dispatch outlook</h2>
          <SlaBadge status={order.sla_status} />
        </div>

        <div className="metric-row">
          <div className="metric">
            <span className="label">Promised by</span>
            <span className="value">{formatDateTime(order.promised_dispatch_at)}</span>
            <span className="sub">frozen when the order was created</span>
          </div>
          <div className="metric">
            <span className="label">Predicted dispatch</span>
            <span className="value">{formatDateTime(order.predicted_dispatch_at)}</span>
            <span className="sub">
              {missesCollection
                ? `work finishes ${formatDateTime(order.work_complete_at)}, then waits for the carrier`
                : "at the throughput observed right now"}
            </span>
          </div>
          <div className="metric">
            <span className="label">Slack</span>
            <span
              className="value num"
              style={{ color: slack !== null && slack < 0 ? "var(--breached)" : undefined }}
            >
              {formatHours(slack)}
            </span>
            <span className="sub">
              {slack === null
                ? "not in the pending queue"
                : slack < 0
                  ? "past the promise"
                  : "before the promise"}
            </span>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>Why it sits here</h2>
          <StatusBadge status={order.status} />
        </div>

        {order.queue_position === null ? (
          <p style={{ margin: 0, color: "var(--muted)" }}>
            This order has left the pending queue, so the scheduler no longer
            reorders it or predicts a dispatch time. Work already started stays
            committed on the floor.
          </p>
        ) : (
          <div className="metric-row">
            <div className="metric">
              <span className="label">Queue position</span>
              <span className="value num">{order.queue_position}</span>
              <span className="sub">of the pending queue</span>
            </div>
            <div className="metric">
              <span className="label">Work ahead of it</span>
              <span className="value num">{ahead?.toFixed(1) ?? "—"}</span>
              <span className="sub">
                {order.throughput_assumed
                  ? `work units to clear, at ${order.throughput_assumed.toFixed(0)} wu/hr`
                  : "work units to clear first"}
              </span>
            </div>
            <div className="metric">
              <span className="label">Priority score</span>
              <span className="value num">
                {order.priority_score?.toFixed(2) ?? "—"}
              </span>
              <span className="sub">
                {order.priority_breakdown
                  ? `${order.priority_breakdown.sla_urgency.toFixed(2)} urgency ` +
                    `+ ${order.priority_breakdown.aging_bonus.toFixed(2)} age ` +
                    `− ${order.priority_breakdown.workload_penalty.toFixed(2)} workload` +
                    (order.priority_breakdown.adjustment !== 0
                      ? ` ${order.priority_breakdown.adjustment > 0 ? "+" : "−"} ${Math.abs(
                          order.priority_breakdown.adjustment,
                        ).toFixed(2)} from a recovery lever`
                      : "")
                  : "urgency, plus age, less workload"}
              </span>
            </div>
            <div className="metric">
              <span className="label">Facility risk</span>
              <span className="value">
                {dashboard.data ? <RiskBadge level={dashboard.data.risk_level} /> : "—"}
              </span>
              <span className="sub">{order.facility_id}</span>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
