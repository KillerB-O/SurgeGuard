import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { createSimulation } from "@/lib/api";
import { SummaryDelta } from "../components/SummaryDelta";
import { Failed, Loading } from "../components/States";
import { useDashboard } from "../lib/queries";
import { formatNumber } from "../lib/format";
import type { SimulationRequest } from "@/lib/types";

/**
 * The demand multiplier is live: the backend projects future arrivals at the
 * observed rate and runs them through the same scheduler (doc 12 section 1).
 *
 * It only means anything alongside a projection horizon, since it scales
 * arrivals that have not happened yet -- the backend rejects it on its own.
 */


export function WhatIf() {
  const dashboard = useDashboard();
  const current = dashboard.data;

  const [capacity, setCapacity] = useState<string>("");
  const [promiseHours, setPromiseHours] = useState<string>("");
  const [horizon, setHorizon] = useState<string>("");
  const [demand, setDemand] = useState<string>("1");

  const simulation = useMutation({
    mutationFn: (body: SimulationRequest) => createSimulation(body),
  });

  if (dashboard.isLoading) return <Loading label="Reading current settings" />;
  if (dashboard.isError) return <Failed error={dashboard.error} />;
  if (!current) return null;

  const liveCapacity = current.fulfillment_work_units_per_hour;
  const capacityValue = capacity === "" ? liveCapacity : Number(capacity);
  // Null means "no projection": only orders already placed are simulated.
  const horizonValue =
    horizon === "" || Number(horizon) <= 0 ? null : Number(horizon);
  const demandValue = demand === "" ? 1 : Number(demand);
  const promiseValue =
    promiseHours === "" ? current.dispatch_promise_hours : Number(promiseHours);
  const changed =
    capacityValue !== liveCapacity ||
    promiseValue !== current.dispatch_promise_hours ||
    horizonValue !== null ||
    (horizonValue !== null && demandValue !== 1);

  function run() {
    // Send only what the operator actually changed.
    const body: SimulationRequest = {};
    if (capacityValue !== liveCapacity) body.capacity_per_hour = capacityValue;
    if (promiseValue !== current!.dispatch_promise_hours) {
      body.dispatch_promise_hours = promiseValue;
    }
    // A demand assumption needs a future to apply to, so the horizon travels
    // with it rather than being sent on its own.
    if (horizonValue !== null) {
      body.projection_horizon_minutes = horizonValue;
      if (demandValue !== 1) body.demand_multiplier = demandValue;
    }
    simulation.mutate(body);
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>What-if</h1>
        <p>
          Ask the same scheduler what would happen under different assumptions.
          Nothing here changes live orders or saves anything.
        </p>
      </div>

      <div className="grid-2">
        <section className="panel">
          <div className="panel-title">
            <h2>Assumptions</h2>
            <span className="hint">now: {formatNumber(liveCapacity)} wu/hr</span>
          </div>

          <div className="field">
            <label htmlFor="capacity">Fulfillment capacity, work units per hour</label>
            <input
              id="capacity"
              type="range"
              min={Math.max(1, Math.round(liveCapacity * 0.5))}
              max={Math.round(liveCapacity * 3)}
              step={1}
              value={capacityValue}
              onChange={(event) => setCapacity(event.target.value)}
            />
            <input
              type="number"
              min={1}
              value={capacityValue}
              onChange={(event) => setCapacity(event.target.value)}
            />
            <span className="help">
              What the floor would clear per hour after the intervention.
            </span>
          </div>

          <div className="field">
            <label htmlFor="promise">Dispatch promise for new orders, hours</label>
            <input
              id="promise"
              type="number"
              min={1}
              value={promiseValue}
              onChange={(event) => setPromiseHours(event.target.value)}
            />
            <span className="help">
              Existing orders keep the deadline they were given. This only shows
              what a different policy would have meant.
            </span>
          </div>

          <div className="field">
            <label htmlFor="horizon">Project future arrivals (minutes)</label>
            <input
              id="horizon"
              type="number"
              step={30}
              min={0}
              value={horizon}
              placeholder="0 = only orders already placed"
              onChange={(event) => setHorizon(event.target.value)}
            />
            <span className="sub">
              Generates arrivals at the rate the facility is observed to be
              receiving, then runs them through the same scheduler. Counted
              separately from orders customers have placed.
            </span>
          </div>

          {horizonValue !== null && (
            <div className="field">
              <label htmlFor="demand">Future arrival rate</label>
              <input
                id="demand"
                type="number"
                step={0.05}
                min={0.1}
                value={demand}
                onChange={(event) => setDemand(event.target.value)}
              />
              <span className="sub">
                1.0 keeps the observed rate. 0.7 is roughly what pausing a
                promotion buys.
              </span>
            </div>
          )}

          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn" onClick={run} disabled={!changed || simulation.isPending}>
              {simulation.isPending ? "Running" : "Run simulation"}
            </button>
            <button
              className="btn secondary"
              onClick={() => {
                setCapacity("");
                setPromiseHours("");
                setHorizon("");
                setDemand("1");
                simulation.reset();
              }}
            >
              Reset
            </button>
          </div>

          {!changed && (
            <p className="help" style={{ marginTop: 12, marginBottom: 0 }}>
              Change an assumption to run a simulation.
            </p>
          )}
        </section>

        <section className="panel">
          <div className="panel-title">
            <h2>Result</h2>
            {simulation.data && (
              <span className="hint">
                at {formatNumber(simulation.data.applied_capacity_per_hour)} wu/hr
              </span>
            )}
          </div>

          {simulation.isError && <Failed error={simulation.error} />}

          {!simulation.data && !simulation.isError && (
            <p style={{ color: "var(--muted)", margin: 0 }}>
              Results appear here. The comparison comes from the backend, run
              through the same scheduler that produces the live queue.
            </p>
          )}

          {simulation.data && (
            <>
              <SummaryDelta
                baseline={simulation.data.baseline}
                simulated={simulation.data.simulated}
              />
              <p style={{ marginBottom: 0, marginTop: 16 }}>
                <Link className="row-link" to="/dashboard/recovery">
                  Compare recovery plans that reach this
                </Link>
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
