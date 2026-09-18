# SurgeGuard Simulator

The simulator generates canonical commerce and fulfillment events. It never
writes to PostgreSQL; events are sent through the configured n8n gateway.

## Run locally

```powershell
uvicorn simulator.app:app --reload --port 8010
```

The client defaults to `http://localhost:5678/webhook`. Set
`N8N_WEBHOOK_BASE_URL` when the gateway runs at a different URL.

## Control endpoints

```text
POST /scenario/reset
POST /scenario/start
POST /scenario/next-stage
POST /scenario/run-stage
POST /scenario/set-capacity
POST /scenario/set-dispatch-promise
POST /scenario/advance-statuses
GET  /scenario/status
```

`/scenario/run-stage` generates the current target workload as pending orders
and publishes a matching fulfillment snapshot. `/scenario/advance-statuses`
moves unfinished orders one operational state at a time and publishes updated
telemetry. `/scenario/set-capacity` immediately publishes fresh observed
throughput, while `/scenario/set-dispatch-promise` applies only to future
orders.

`/scenario/reset` resets simulator memory only. For a fully clean repeatable
demo, reset the backend's demo data as well; otherwise canonical idempotency
correctly treats replayed events as duplicates.

## Ownership boundary

Keep backend scheduling and frontend presentation outside this directory.
The simulator must emit canonical events, remain deterministic after reset,
and stay blind to SLA/risk results.
