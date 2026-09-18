import type { SimulationSummary } from "@/lib/types";
import { formatHours, formatNumber, signed } from "../lib/format";
import { RiskBadge } from "./Badges";

/**
 * Renders two scheduler summaries side by side. The deltas shown are simple
 * subtraction of numbers the backend already computed — no re-classification.
 */
export function SummaryDelta({
  baseline,
  simulated,
  baselineLabel = "Now",
  simulatedLabel = "Simulated",
}: {
  baseline: SimulationSummary;
  simulated: SimulationSummary;
  baselineLabel?: string;
  simulatedLabel?: string;
}) {
  const rows: Array<[string, number, number]> = [
    ["Breached", baseline.breached_count, simulated.breached_count],
    ["At risk", baseline.at_risk_count, simulated.at_risk_count],
    ["Watch", baseline.watch_count, simulated.watch_count],
    ["Safe", baseline.safe_count, simulated.safe_count],
    ["Backlog orders", baseline.backlog_orders, simulated.backlog_orders],
  ];

  const identical = rows.every(([, a, b]) => a === b);

  return (
    <div>
      {identical && (
        <div className="notice warn" style={{ marginBottom: 16 }}>
          This produces the same result as the current queue. Nothing in it
          changes what the scheduler predicts.
        </div>
      )}

      <table>
        <thead>
          <tr>
            <th>Outcome</th>
            <th className="right">{baselineLabel}</th>
            <th className="right">{simulatedLabel}</th>
            <th className="right">Change</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, before, after]) => (
            <tr key={label}>
              <td>{label}</td>
              <td className="right num">{formatNumber(before)}</td>
              <td className="right num">{formatNumber(after)}</td>
              <td
                className="right num"
                style={{
                  color:
                    after === before
                      ? "var(--muted)"
                      : after < before
                        ? "var(--safe)"
                        : "var(--breached)",
                }}
              >
                {signed(after - before)}
              </td>
            </tr>
          ))}
          <tr>
            <td>Time to clear the queue</td>
            <td className="right num">
              {formatHours(baseline.projected_recovery_hours)}
            </td>
            <td className="right num">
              {formatHours(simulated.projected_recovery_hours)}
            </td>
            <td className="right" style={{ color: "var(--muted)" }}>
              —
            </td>
          </tr>
          <tr>
            <td>Facility risk</td>
            <td className="right">
              <RiskBadge level={baseline.risk_level} />
            </td>
            <td className="right">
              <RiskBadge level={simulated.risk_level} />
            </td>
            <td className="right" style={{ color: "var(--muted)" }}>
              —
            </td>
          </tr>
        </tbody>
      </table>

      {(baseline.projected_arrivals || simulated.projected_arrivals) && (
        <p style={{ marginTop: 10, fontSize: "0.8125rem", color: "var(--muted)" }}>
          Plus projected arrivals nobody has placed yet:{" "}
          <strong>{baseline.projected_arrivals?.orders ?? 0}</strong> before,{" "}
          <strong>{simulated.projected_arrivals?.orders ?? 0}</strong> after
          {simulated.projected_arrivals
            ? ` (${simulated.projected_arrivals.breached_count} of them breaching)`
            : ""}
          . Counted separately, because a plan cannot rescue an order that does
          not exist.
        </p>
      )}
    </div>
  );
}
