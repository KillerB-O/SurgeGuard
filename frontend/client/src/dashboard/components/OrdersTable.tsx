import { Link } from "react-router-dom";

import type { OrderResponse } from "@/lib/types";
import {
  formatCurrency,
  formatDateTime,
  formatHours,
  hoursBetween,
} from "../lib/format";
import { SlaBadge, StatusBadge } from "./Badges";

/**
 * Queue position is a genuine sequence, so numbering it is information rather
 * than decoration. Orders without a position are not in the pending queue.
 */
export function OrdersTable({ orders }: { orders: OrderResponse[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th className="right">#</th>
          <th>Order</th>
          <th>State</th>
          <th>Dispatch risk</th>
          <th>Promised by</th>
          <th>Predicted</th>
          <th className="right">Slack</th>
          <th className="right">Work units</th>
          <th className="right">Value</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((order) => {
          const slack = order.predicted_dispatch_at
            ? hoursBetween(order.predicted_dispatch_at, order.promised_dispatch_at)
            : null;

          return (
            <tr key={order.order_id}>
              <td className="right num" style={{ color: "var(--muted)" }}>
                {order.queue_position ?? "—"}
              </td>
              <td>
                <Link className="row-link" to={`/dashboard/orders/${order.order_id}`}>
                  {order.order_id}
                </Link>
              </td>
              <td>
                <StatusBadge status={order.status} />
              </td>
              <td>
                <SlaBadge status={order.sla_status} />
              </td>
              <td>{formatDateTime(order.promised_dispatch_at)}</td>
              <td>{formatDateTime(order.predicted_dispatch_at)}</td>
              <td
                className="right num"
                style={{ color: slack !== null && slack < 0 ? "var(--breached)" : undefined }}
              >
                {formatHours(slack)}
              </td>
              <td className="right num">{order.work_units.toFixed(1)}</td>
              <td className="right num">{formatCurrency(order.order_value)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
