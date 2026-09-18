import { useState } from "react";

import { OrdersTable } from "../components/OrdersTable";
import { Empty, Failed, Loading } from "../components/States";
import { useOrders } from "../lib/queries";
import { formatNumber } from "../lib/format";
import type { OrderStatus } from "@/lib/types";

/**
 * Each filter names the statuses the backend should return. An empty list means
 * every status. The page deliberately does not filter rows itself: scheduled
 * orders sort first, so filtering one page would show nothing but PENDING.
 */
const FILTERS: Array<{ id: string; label: string; statuses: OrderStatus[] }> = [
  { id: "all", label: "All", statuses: [] },
  { id: "queue", label: "In the queue", statuses: ["PENDING"] },
  { id: "active", label: "Being worked", statuses: ["PICKING", "PACKED", "READY"] },
  { id: "done", label: "Dispatched", statuses: ["DISPATCHED"] },
];

export function Orders() {
  const [filter, setFilter] = useState("all");
  const active = FILTERS.find((f) => f.id === filter) ?? FILTERS[0];
  const { data, isLoading, isError, error } = useOrders(undefined, active.statuses);

  if (isLoading) return <Loading label="Reading the order queue" />;
  if (isError) return <Failed error={error} />;
  if (!data) return null;

  const rows = data.items;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Orders</h1>
        <p>
          Ordered by the scheduler's queue position. Orders already being worked
          keep their place on the floor and are listed after the queue.
        </p>
      </div>

      <section className="panel">
        <div className="panel-title">
          <div style={{ display: "flex", gap: 8 }}>
            {FILTERS.map((option) => (
              <button
                key={option.id}
                className={`btn ${filter === option.id ? "" : "secondary"}`}
                onClick={() => setFilter(option.id)}
              >
                {option.label}
              </button>
            ))}
          </div>
          <span className="hint">
            {formatNumber(rows.length)} of {formatNumber(data.total)} orders
          </span>
        </div>

        {rows.length === 0 ? (
          <Empty title="No orders here yet">
            <p>
              Orders appear once the commerce simulator starts sending them
              through n8n.
            </p>
          </Empty>
        ) : (
          <OrdersTable orders={rows} />
        )}
      </section>
    </div>
  );
}
