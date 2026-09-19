import { useRef } from "react";

import { formatNumber } from "../lib/format";

/**
 * The one loud element in the product: incoming workload against observed
 * throughput on a shared scale. Overshoot is hatched, and the hairline marks
 * where current throughput runs out.
 */
export function GapBar({
  demand,
  throughput,
}: {
  demand: number;
  throughput: number;
}) {
  // Scaling to the current values alone draws only their ratio, so the bars
  // sat still while both numbers moved together. A high-water mark keeps the
  // magnitude visible too.
  const highWater = useRef(0);
  highWater.current = Math.max(highWater.current, demand, throughput);
  const scale = highWater.current * 1.12 || 1;
  const pct = (value: number) => `${(value / scale) * 100}%`;
  const gap = demand - throughput;
  const overshoot = Math.max(0, gap);
  const covered = Math.min(demand, throughput);

  return (
    <section className="panel gap-figure" aria-label="Demand against throughput">
      <div className="gap-headline">
        <span className="big num">{formatNumber(Math.abs(gap))}</span>
        <span className="unit">
          work units per hour {gap > 0 ? "more than" : "below"} what the floor is
          clearing
        </span>
      </div>

      <div className="bar-row">
        <span className="bar-label">Incoming demand</span>
        <div className="bar-track">
          <div className="bar-split">
            <div className="bar-fill demand" style={{ width: pct(covered) }} />
            {overshoot > 0 && (
              <div className="bar-fill overshoot" style={{ width: pct(overshoot) }} />
            )}
          </div>
          <div className="capacity-line" style={{ left: pct(throughput) }} data-label="throughput" />
        </div>
        <span className="bar-value num">{formatNumber(demand)}</span>
      </div>

      <div className="bar-row">
        <span className="bar-label">Observed throughput</span>
        <div className="bar-track">
          <div className="bar-fill" style={{ width: pct(throughput) }} />
        </div>
        <span className="bar-value num">{formatNumber(throughput)}</span>
      </div>
    </section>
  );
}
