import { Link } from "react-router-dom";

import { OrdersTable } from "../components/OrdersTable";
import { Empty, Failed, Loading } from "../components/States";
import { useAtRiskOrders } from "../lib/queries";
import { formatNumber } from "../lib/format";

export function AtRisk() {
  const { data, isLoading, isError, error } = useAtRiskOrders();

  if (isLoading) return <Loading label="Finding exposed orders" />;
  if (isError) return <Failed error={error} />;
  if (!data) return null;

  const breached = data.items.filter((o) => o.sla_status === "BREACHED").length;

  return (
    <div className="stack">
      <div className="page-head">
        <h1>At risk</h1>
        <p>
          Orders the scheduler predicts will dispatch at or past their promised
          time. {formatNumber(breached)} of these have already crossed the
          promise.
        </p>
      </div>

      <section className="panel">
        <div className="panel-title">
          <h2>{formatNumber(data.total)} exposed orders</h2>
          <Link className="row-link" to="/dashboard/what-if">
            Try a capacity change
          </Link>
        </div>

        {data.items.length === 0 ? (
          <Empty title="Every pending order is on track">
            <p>Nothing in the queue is predicted to miss its dispatch promise.</p>
          </Empty>
        ) : (
          <OrdersTable orders={data.items} />
        )}
      </section>
    </div>
  );
}
