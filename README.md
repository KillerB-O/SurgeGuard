# SurgeGuard

SurgeGuard detects when e-commerce demand is growing faster than warehouse fulfillment capacity, predicts which orders are likely to miss their dispatch promises, lets operators simulate recovery strategies, and executes only human-approved responses through n8n.

Built for DSU DevHack 3.0.

## The loop

SurgeGuard is one continuous loop, not a one-shot alert:

```
OBSERVE  →  PREDICT  →  SIMULATE  →  CHOOSE  →  ACT  →  RECALCULATE
```

| Step | What happens | Where |
|---|---|---|
| OBSERVE | Order and fulfillment telemetry arrives through n8n webhooks into the backend's ingestion routes. | `backend/app/routers/events.py` |
| PREDICT | A deterministic scheduler re-simulates the pending queue on every read and classifies each order `SAFE` / `WATCH` / `AT_RISK` / `BREACHED`. | `backend/app/scheduler.py :: compute_schedule` |
| SIMULATE | The same scheduler is re-run under each candidate "posture" (a bundle of levers) to project its outcome before anything is committed. | `backend/app/plans.py` |
| CHOOSE | The operator reviews plans side by side and picks one. | frontend What-If / Recovery pages |
| ACT | Approving a plan writes a `recovery_actions` row; n8n polls for it every 5 seconds and carries it out. | `backend/app/executor.py`, `n8n/workflows/recovery-action-executor.json` |
| RECALCULATE | Fresh telemetry flows back through the same ingestion path; a background worker verifies whether the promised capacity actually showed up. | `backend/app/verification.py`, `backend/app/alerts/worker.py` |

There is no machine-learning model anywhere in the core engine. `compute_schedule` is a pure, deterministic function - the same inputs always produce the same schedule - which is what lets a recovery plan's projected numbers be trusted: the live dashboard and every What-If preview call the exact same function.

Nothing acts on its own. Every recovery action requires an explicit human "Approve," and once executed, the system doesn't trust its own success message - it waits for measured real throughput and verifies the claim, resolving it to `ACHIEVED`, `PARTIAL`, or `NOT_OBSERVED`.

## Architecture

Five containerized services, plus a frontend that intentionally runs outside Docker during development.

```
                         +-------------+
                         |  PostgreSQL |  facilities, orders, recovery_actions,
                         |    (16)     |  capacity_commitments, users, sessions,
                         +------+------+  alert_outbox, event_providers ...
                                |
              +-----------------+-----------------+
              |                                    |
       +------v------+                       +------v------+
       |   backend   |<----- same DB ---------|    worker   |
       |  (FastAPI)  |      own connection     |  (alerts,   |
       |    :8000    |                         |  15s tick)  |
       +------+------+                         +-------------+
              | session cookie                        | SMTP
              | or X-Service-Token                     v
       +------v------+                         operator inbox
       |  frontend   |
       | (React/Vite)|
       +-------------+

       +-------------+   webhooks    +-------------+
       |  simulator  +-------------->|     n8n     +--X-Service-Token--> backend
       |    :8010    |<--------------+    :5678    |
       +-------------+  poll + apply +-------------+
```

A few deliberate design choices worth knowing before you read the code:

- **The simulator only talks to n8n webhooks** - never to Postgres or the backend directly. That boundary is what makes it a swappable adapter: a real Shopify/WMS integration can replace it later without touching the intelligence engine. It stands in for a real commerce platform and warehouse system, and never queries the backend's own risk calculations, hard-codes results, or writes to the database.
- **n8n is the only machine caller the backend trusts**, authenticated with a shared `X-Service-Token`. Operators authenticate separately with an httpOnly session cookie - two distinct auth paths, never one global middleware.
- **Every compose port is bound to `127.0.0.1`**, not `0.0.0.0`. Nothing in the stack is reachable from outside the host machine; this is a deliberate demo-security choice.
- **Postgres is the single source of truth.** The scheduler is stateless and reads current rows fresh on every request - there is no cached or precomputed schedule anywhere.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI (async), SQLAlchemy Core + asyncpg, Alembic (24 migrations) |
| Frontend | React + Vite + TypeScript, TanStack Query, Tailwind + shadcn/Radix UI |
| Database | PostgreSQL 16 |
| Orchestration | n8n (webhook + polling workflows) - the only executor of approved actions |
| Simulation | Custom commerce + fulfillment simulator (deterministic seed), standing in for a real WMS/OMS |
| Auth | Argon2id password hashing (`pwdlib[argon2]`), httpOnly session cookies |
| AI explanation layer | Gemini, read-only - explains an already-computed recovery plan; never calculates, chooses, or approves one |

## Repository layout

```
backend/            FastAPI app: scheduler, recovery engine, auth, alerts, migrations, tests
  app/scheduler.py     compute_schedule — the deterministic prediction engine
  app/plans.py         recovery postures: simulate and score candidate responses
  app/executor.py      writes/consumes recovery_actions for n8n to pick up
  app/verification.py  checks a capacity claim against measured throughput
  app/catalog/         levers, postures, alert rules, work-unit weights (data, not code)
  migrations/versions/ 24 sequential Alembic migrations
  tests/               110+ backend tests
frontend/           React + Vite operator dashboard and public site
  client/src/dashboard/  the command centre: live queue, What-If, recovery plans, alerts
simulator/          Deterministic commerce + fulfillment event generator (FastAPI, port 8010)
n8n/workflows/      Ingestion and recovery-execution workflows (JSON, importable into n8n)
scripts/            Helper scripts
docker-compose.yml  postgres, migrate, backend, worker, n8n, simulator
```

## Running it

Prerequisites: Docker and Docker Compose, and Node.js for the frontend dev server.

1. **Configure environment variables.**

   ```bash
   cp .env.example .env
   ```

   Set `SURGEGUARD_SERVICE_TOKEN` (a random secret shared between the backend and n8n - generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`), `GEMINI_API_KEY`, and `OPENROUTER_API_KEY`. `docker compose up` refuses to start if these are missing. SMTP and demo-user variables are optional — leaving SMTP blank writes verification/reset emails to the backend log instead of sending them, which is a fully supported way to run the project.

2. **Start the backend stack.**

   ```bash
   docker compose up -d
   ```

   This brings up Postgres, applies all migrations, and starts the backend (`:8000`), the alerts worker, n8n (`:5678`), and the simulator (`:8010`). Check the API contract at `http://localhost:8000/docs`.

3. **Import the n8n workflows.** Open `http://localhost:5678`, and import each JSON file in `n8n/workflows/` (ingestion workflows plus `recovery-action-executor.json`), then activate them.

4. **Start the frontend.**

   ```bash
   cd frontend
   cp .env.example .env.local
   npm install
   npm run dev
   ```

   Serves at `http://localhost:5173` - this must stay the port, since it's what the backend's CORS and session cookie are configured for.

5. **Run a demo surge.**

   ```bash
   curl -X POST http://localhost:8000/api/demo/reset
   curl -X POST http://localhost:8000/api/demo/surge
   ```

   This plays a realistic order surge in about a minute. Watch the dashboard move from calm to at-risk, open a recovery plan, approve it, and watch the at-risk count drop as n8n executes the plan and the system verifies the result.

### Running the tests

```bash
pip install -e backend[dev]   # or however your environment installs backend + dev deps
pytest
```

`pytest.ini` runs `backend/tests` and `simulator/tests` together - 110+ backend tests and 24+ simulator tests.

## Known limitations

Stated here deliberately rather than left for someone to find:

- **Order value is tracked but unused.** `order_value` is captured and displayed, but nothing in scheduling, lever, or verification logic reads it yet. Wiring it in (so the system can say "these late orders are worth ₹X") is the single highest-leverage feature left undone.
- **Single warehouse only.** Multi-facility support - including moving orders between warehouses - is future work.
- **No real store or WMS connection.** The simulator stands in for Shopify/WooCommerce and a real warehouse management system. The canonical event contract it emits is designed so a real adapter can be swapped in without touching the scheduler.
- **This is a working demo, not a production deployment.** No TLS/ingress, no secrets manager, no frontend container, no database backup or replication, and no horizontal scaling for the backend or worker.
- **The scheduler recomputes the full pending queue on every read.** Fine at hundreds of orders; a much larger warehouse would need incremental updates instead of recalculating from scratch each time.

See the technical defense document for the full list, including real bugs found and fixed during development (an inverted risk-alarm bug, predictions that ignored in-flight work, a missing execution trigger, and a compounding-capacity bug that made the demo only repeatable once).

## Documentation

- `backend/README.md`, `frontend/README.md`, `simulator/README.md`, `n8n/README.md` — service-level docs.
- Technical Defense Document and Simple Judging Guide (hackathon submission materials) - the full architecture rationale, algorithm walkthrough, data model, and anticipated Q&A.

## License

MIT - see [LICENSE](LICENSE).
