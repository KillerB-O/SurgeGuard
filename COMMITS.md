# SurgeGuard Backend Commit Strategy

## Overview
Minimal, modular commits to build the backend incrementally. Each commit unblocks the next component without external dependencies.

---

## ✅ Completed Commits

### **Commit 1: Project Scaffold & Core Models**
**Status:** Done  
**Branch:** backend

**Files:**
- `backend/pyproject.toml` — FastAPI, SQLAlchemy, Pydantic, Alembic, pytest
- `backend/app/__init__.py`
- `backend/app/config.py` — Settings, SMTP config, session lifetime
- `backend/app/db.py` — Async SQLAlchemy engine, connection factory
- `backend/app/models.py` — Frozen Pydantic models (Order, Events, Actions)
- `backend/app/schemas.py` — Read-side response shapes (Dashboard, Orders, etc.)
- `backend/migration/__init__.py`
- `backend/tests/__init__.py`
- `backend/README.md` — Setup, Docker, migrations, email config

**Commit Message:**
```
feat(backend): scaffold FastAPI project structure with core models

Add backend package scaffold:
- pyproject.toml with FastAPI, SQLAlchemy, Pydantic, Alembic dependencies
- app/ package: config (Settings), async db engine, Pydantic models/schemas
- migration/ package for Alembic migrations
- tests/ package placeholder
- __init__.py files for all packages

Core models frozen per architectural specs: Order, OrderEvent, FulfillmentSnapshot,
and read-side response contracts. DB engine setup in app/db.py for async 
SQLAlchemy with asyncpg driver.
```

---

### **Commit 2: FastAPI Bootstrap & Migration 0001**
**Status:** Done  
**Branch:** backend

**Files:**
- `backend/app/main.py` — FastAPI init, lifespan, /health endpoint, 503 handler
- `backend/migration/versions/0001_initial.py` — Alembic migration

**Schema (0001_initial):**
- `facilities` — facility_id, name, capacity_per_hour, dispatch_promise_hours
- `orders` — order_id, facility_id, timestamps, metrics, status
- `fulfillment_snapshots` — facility metrics history
- `processed_events` — event idempotency tracking
- `recovery_actions` — approval/execution state

**Commit Message:**
```
feat(backend): add FastAPI app bootstrap and initial database schema

Add app/main.py:
- FastAPI app initialization with lifespan management
- CORS middleware configured from Settings
- /health endpoint with database connectivity check
- 503 handler for database unavailability

Add migration 0001_initial.py:
- Alembic migration for initial schema
- Tables: facilities, orders, fulfillment_snapshots, processed_events, recovery_actions
- Constraints, indexes, and demo facility seed (WH-01)
- Matches schema in backend/migration/0001_initial.sql (reference SQL)
```

---

### **Commit 3: Read-Side APIs & Scheduling Foundation**
**Status:** Done  
**Branch:** backend

**Files:**
- `backend/app/repository.py` — SQL queries (orders, throughput, demand)
- `backend/app/scheduler.py` — SLA/risk classification, priority scoring
- `backend/app/work_units.py` — Order classification from attributes
- `backend/app/routers/health.py` — /health (no DB dependency)
- `backend/app/routers/reads.py` — Dashboard, orders, at-risk orders
- `backend/app/catalog/work_units.json` — Work unit bands (config)

**Endpoints (0001 compatible):**
- `GET /health` — liveness check
- `GET /api/dashboard` — facility metrics, risk level, SLA counts
- `GET /api/orders` — paginated order listing with scheduling
- `GET /api/orders/at-risk` — at-risk/breached orders only
- `GET /api/orders/{order_id}` — single order with scheduler fields

**Blockers Removed:**
- ❌ No auth requirement (require_session)
- ❌ No clock dependency (datetime.now(timezone.utc))
- ❌ No consequences/interventions (priority_adjustments=None)
- ❌ No late-dispatch-rate calculation

**Commit Message:**
```
feat(backend): add minimal read-side APIs (reads, health)

Minimal read-side endpoints for frontend dashboard:
- GET /health: liveness check
- GET /api/dashboard: facility metrics, risk level, SLA counts
- GET /api/orders: paginated order listing with scheduling
- GET /api/orders/at-risk: at-risk/breached orders
- GET /api/orders/{order_id}: single order details

Core logic (migration 0001 compatible):
- repository.py: facility/order queries using 0001 columns only
- scheduler.py: SLA/risk classification with hardcoded defaults (until 0021)
- work_units.py: order classification from attributes
- Thresholds use defaults, no facility-level overrides yet

No blockers: no auth, no clock, no consequences, priority_adjustments=None
```

---

### **Commit 4: Event Ingestion Endpoints**
**Status:** Done  
**Branch:** backend

**Files:**
- `backend/app/routers/events.py` — 5 event handlers

**Endpoints:**
- `POST /events/orders` — OrderCreatedEvent → orders table
- `POST /events/fulfillment` — FulfillmentSnapshotEvent → fulfillment_snapshots
- `POST /events/order-status` — OrderStatusUpdatedEvent → update orders.status
- `POST /events/backfill` — OrderBackfillEvent → pre-existing orders
- `POST /events/order-revised` — OrderRevisedEvent → update order attributes

**Features:**
- Idempotent via `processed_events` table (ON CONFLICT DO NOTHING)
- Automatic DUPLICATE/PROCESSED responses
- Logging on failures

**Blockers Removed:**
- ❌ No auth (require_service_token)
- ❌ No event provider validation (event_providers table N/A)
- ❌ No work unit classification (uses event values directly)

**Commit Message:**
```
feat(backend): add event ingestion endpoints (minimal)

Add 5 event ingestion endpoints for migration 0001:
- POST /events/orders: ingest OrderCreatedEvent
- POST /events/fulfillment: ingest FulfillmentSnapshotEvent
- POST /events/order-status: ingest OrderStatusUpdatedEvent
- POST /events/backfill: ingest pre-existing orders
- POST /events/order-revised: ingest order revisions

Minimal implementation:
- No auth requirement (require_service_token removed)
- No event provider validation (event_providers table not yet)
- No work unit classification (uses event values directly)
- Idempotent via processed_events table
- Works with migration 0001 schema
```

---

## 📋 Planned Commits

### **Commit 5: Simulation & What-If**
**Files:**
- `backend/app/routers/simulation.py` — What-If scenarios, plan generation
- `backend/app/plans.py` — Recovery plan logic
- `backend/app/interventions.py` — Lever definitions & effects

**Endpoints:**
- `POST /api/simulation` — What-If with capacity/demand changes
- `POST /api/recovery-plans` — Generate recovery plans from What-If
- `GET /api/recovery-plans/{plan_id}` — Plan details

**Blockers:** Minimal (uses scheduler, repository)

---

### **Commit 6: Recovery Actions & Execution**
**Files:**
- `backend/app/routers/simulation.py` (continued) — action approval/execution
- `backend/app/executor.py` — Execute recovery actions
- `backend/app/levers.py` — Lever implementation
- `backend/app/catalog/levers.json` — Lever definitions (config)

**Endpoints:**
- `POST /api/recovery-actions/{action_id}/approve` — Approve an action
- `PATCH /api/recovery-actions/{action_id}/status` — n8n sends status updates

---

### **Commit 7: Authentication System**
**Files:**
- `backend/app/auth/router.py` — signup, login, password reset
- `backend/app/auth/dependencies.py` — session validation, require_session
- `backend/app/auth/sessions.py` — session management
- `backend/app/auth/tokens.py` — email verification/reset tokens
- `backend/app/auth/passwords.py` — hashing, validation
- `backend/app/auth/mailer.py` — email sending
- `backend/app/auth/seed.py` — demo account seeding
- Migrations 0010-0013 (users, sessions, emails, etc.)

**Endpoints:**
- `POST /auth/signup` — Create account
- `POST /auth/login` — Start session
- `GET /auth/verify` — Email verification link
- `POST /auth/reset/request` — Password reset request
- `POST /auth/reset/confirm` — Password reset confirmation
- `GET /me` — Current user info

---

### **Commit 8: Alerts & Monitoring**
**Files:**
- `backend/app/alerts/router.py` — alert recipient management
- `backend/app/alerts/dispatcher.py` — send alerts
- `backend/app/alerts/evaluator.py` — evaluate alert rules
- `backend/app/alerts/worker.py` — background alert processing
- `backend/app/alerts/rules.py` — alert rule logic
- `backend/app/catalog/alert_rules.json` — rule definitions
- Migrations 0012, 0014 (alert recipients, outbox)

**Endpoints:**
- `POST /api/alerts/recipients` — Add alert recipient
- `GET /api/alerts/recipients` — List recipients
- `DELETE /api/alerts/recipients/{id}` — Remove recipient
- `GET /api/alerts/history` — Alert send history

---

## 🔧 Schema Evolution

### Migration 0001 (Current)
**Sufficient for:** Commits 1-4 (scaffold, reads, events)

- facilities (basic)
- orders (basic)
- fulfillment_snapshots
- processed_events
- recovery_actions

### Migrations 0002-0005 (Planned)
**For:** Commit 5 (simulation)
- Order attributes (line_count, unit_count, special_handling, segment)
- Facility cutoff policy
- Read-path indexes

### Migrations 0006-0009 (Planned)
**For:** Commit 5-6 (interventions, execution)
- Executable interventions
- Demo clock simulation
- Dispatched_at tracking

### Migrations 0010-0023 (Planned)
**For:** Commits 7-8 (auth, alerts)
- Users and sessions (0010)
- Email tokens (0011)
- Alert recipients and outbox (0012)
- User profile fields (0013)
- Demo containment (0014)
- Event providers (0015)
- Pending status events (0016)
- Order status transitions (0017)
- Execution adapters (0018)
- Capacity commitments (0019)
- Cancelled status (0020)
- Facility thresholds (0021)
- Operating calendar (0022)
- Demo carrier pickup (0023)

---

## 📊 Progress Checklist

### Core (MVP)
- [x] Commit 1: Scaffold + models
- [x] Commit 2: Main + migration 0001
- [x] Commit 3: Reads + scheduler
- [x] Commit 4: Event ingestion

### Demo (Interactive)
- [ ] Commit 5: Simulation & What-If
- [ ] Commit 6: Recovery actions

### Auth & Monitoring
- [ ] Commit 7: Auth (signup, login, reset)
- [ ] Commit 8: Alerts (recipient management, alerts)

---

## 🚀 Unblocking Order

| Component | Unblocked By | Status |
|-----------|--------------|--------|
| Frontend dashboard | Commit 3 | ✅ |
| Simulator/n8n data flow | Commit 4 | ✅ |
| What-If scenarios | Commit 5 | ⏳ |
| Recovery plan approval | Commit 6 | ⏳ |
| User login | Commit 7 | ⏳ |
| Alert notifications | Commit 8 | ⏳ |

---

## ⚠️ Known Constraints

### Migration 0001 Only
- No facility-level thresholds (uses hardcoded defaults until 0021)
- No dispatch cutoff time (uses continuous dispatch)
- No operating calendar (assumes 24/7 capacity)
- No event provider validation (accepts any source)
- No user/session tables (auth blocked until 0010)

### No External Dependencies
- No auth requirement on reads/events
- No clock simulation (uses real time)
- No consequences calculation (late-dispatch-rate skipped)
- No priority adjustments (scheduler uses empty map)

---

## 📝 Next Steps

1. **Complete Commits 5-6** if demoing What-If/recovery
2. **Complete Commits 7-8** if demoing full auth + alerting
3. **Copy remaining migrations (0002-0023)** from Downloads backend if comprehensive implementation needed
4. **Add comprehensive tests** for each commit before shipping

