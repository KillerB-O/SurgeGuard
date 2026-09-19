import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError, resetDemo, startLive, startSurge, stopLive } from "@/lib/api";
import { useDemoStatus } from "../lib/queries";
import { formatClock, formatNumber } from "../lib/format";

/**
 * Simulated seconds per real second, offered as a dropdown.
 *
 * 1x is real time: the floor moves at the pace a real warehouse does, which is
 * the honest setting for checking that nothing in the product depends on time
 * being compressed. 25x plays a working day in about an hour. 60x is kept
 * because it is `DEFAULT_LIVE_SPEED` on the backend, so dropping it would
 * leave a default nobody can pick.
 *
 * Anything in `0 < speed <= 600` is accepted by `LiveRequest.speed`, so adding
 * a rate here needs no backend change.
 */
const LIVE_SPEEDS = [1, 25, 60, 180, 600];

/**
 * Reset, surge, and the continuous live clock, so driving the demo does not
 * mean a terminal.
 *
 * Renders nothing unless the backend is configured with a simulator to drive.
 * A deployment without one has no demo to control, and the controls clear
 * operational data, so they should not merely be disabled -- they should not
 * be there at all.
 */
export function DemoControls() {
  const status = useDemoStatus();
  const queryClient = useQueryClient();
  const [speed, setSpeed] = useState(600);

  // Every panel reads from the same backend state, so a reset, a surge, or a
  // live start/stop invalidates all of it rather than guessing which views
  // changed.
  const refreshEverything = () => queryClient.invalidateQueries();

  const reset = useMutation({
    mutationFn: () => resetDemo(),
    onSuccess: () => {
      status.refetch();
      refreshEverything();
    },
  });

  const surge = useMutation({
    mutationFn: startSurge,
    onSuccess: () => {
      status.refetch();
      refreshEverything();
    },
  });

  const live = useMutation({
    mutationFn: () => startLive(speed),
    onSuccess: () => {
      status.refetch();
      refreshEverything();
    },
  });

  const stop = useMutation({
    mutationFn: stopLive,
    onSuccess: () => {
      status.refetch();
      refreshEverything();
    },
  });

  if (!status.data?.enabled) return null;

  const running = status.data.running;
  const isLive = status.data.live;
  const busy =
    running || isLive || reset.isPending || surge.isPending || live.isPending;
  const error = reset.error ?? surge.error ?? live.error ?? stop.error;

  return (
    <section className="panel">
      <div className="panel-title">
        <h2>Demo controls</h2>
        {running && (
          <span className="badge watch">
            Surging {status.data.ticks_done}/{status.data.ticks_total}
          </span>
        )}
        {isLive && (
          <span className="badge watch">
            Live {status.data.speed}x · {formatClock(status.data.simulated_time)}
          </span>
        )}
      </div>

      <p style={{ marginTop: 0, color: "var(--muted)", fontSize: "0.875rem" }}>
        The simulator has no clock, so orders only arrive when something drives
        it. Each surge tick is an hour of arrivals against an hour of floor
        capacity, which is what makes the demand-versus-throughput gap real.
        Live mode runs that same clock continuously at the chosen speed.
      </p>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <button
          className="btn"
          disabled={busy}
          onClick={() => surge.mutate()}
        >
          {running ? "Surge running…" : "Start surge"}
        </button>

        {isLive ? (
          <button
            className="btn secondary"
            disabled={stop.isPending}
            onClick={() => stop.mutate()}
          >
            {stop.isPending ? "Stopping…" : "Stop"}
          </button>
        ) : (
          <>
            <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span style={{ color: "var(--muted)", fontSize: "0.875rem" }}>
                Speed
              </span>
              <select
                className="btn secondary"
                disabled={busy}
                value={speed}
                onChange={(event) => setSpeed(Number(event.target.value))}
              >
                {LIVE_SPEEDS.map((option) => (
                  <option key={option} value={option}>
                    {option === 1 ? "1x (real time)" : `${option}x`}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn"
              disabled={busy}
              onClick={() => live.mutate()}
            >
              {live.isPending ? "Starting…" : "Start live"}
            </button>
          </>
        )}

        <button
          className="btn secondary"
          disabled={busy}
          onClick={() => reset.mutate()}
        >
          {reset.isPending ? "Resetting…" : "Reset demo"}
        </button>
      </div>

      {isLive && (
        <p
          style={{
            marginBottom: 0,
            marginTop: 12,
            fontSize: "0.8125rem",
            color: "var(--muted)",
          }}
        >
          Simulated time {formatClock(status.data.simulated_time)} ·{" "}
          {formatNumber(status.data.sim_hours_elapsed ?? 0, 1)}h elapsed at{" "}
          {status.data.speed}x.
        </p>
      )}

      <p style={{ marginBottom: 0, marginTop: 12, fontSize: "0.8125rem", color: "var(--muted)" }}>
        {error
          ? // Only a network failure means "unreachable"; a 4xx is the backend
            // refusing the request, and saying otherwise hides the real reason.
            error instanceof ApiError && error.status > 0
            ? `Demo control refused: ${error.message}`
            : `Could not reach the simulator: ${(error as Error).message}`
          : running
            ? "Building; the surge keeps running if you navigate away. The dashboard clock follows each simulated hour as it is written."
            : isLive
              ? "The live clock keeps running if you navigate away."
              : status.data.detail ??
                "Reset clears orders and returns capacity, promise and the simulator to baseline."}
      </p>
    </section>
  );
}
