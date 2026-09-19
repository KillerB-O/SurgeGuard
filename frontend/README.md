# SurgeGuard Frontend

One React + Vite app: the public landing page, sign-in and account pages, and
the operator command centre under `/dashboard`.

## Run it

```bash
cp .env.example .env.local
npm install
npm run dev          # http://localhost:5173 (strict: fails rather than switching port)
```

The dev server must stay on 5173: that is the origin the backend's CORS and
session cookie are configured for (`FRONTEND_ORIGIN`), and where emailed links
point (`FRONTEND_BASE_URL`).

Start the backend first (from the repo root):

```bash
docker compose up -d
```

Sanity-check the contract at `http://localhost:8000/docs` before debugging the
UI. FastAPI's generated schema is the real contract; if it disagrees with
`client/src/lib/types.ts`, the schema wins.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE` | `http://localhost:8000/api` | Must already include the `/api` prefix. |
| `VITE_FACILITY_ID` | `WH-01` | Facility shown by default. |
| `VITE_DATA_SOURCE` | `live` | `mock` renders fixtures with no backend and skips the dashboard's sign-in guard. Auth pages always need the backend. |

Vite reads `.env.local` once at startup; restart the dev server after editing it.

Production build: `npm run check && npm run build`, then `npm start` serves
`dist/` with client-side routing (`PORT`, default 3000).

## Pages

Public:

| Route | Purpose | Backend |
|---|---|---|
| `/` | Landing page; shows **Open dashboard** when signed in | `GET /api/me` |
| `/auth`, `/login`, `/signup` | Sign in / create account, live password checklist | `POST /api/auth/login`, `POST /api/auth/signup`, `GET /api/auth/password-policy` |
| `/reset-password` | Request a reset email | `POST /api/auth/reset/request` |
| `/set-new-password?token=` | Opened from the reset email | `GET` (peek) then `POST /api/auth/reset/confirm` |
| `/verify-email?token=` | Opened from the verification email | `GET /api/auth/verify`, `POST /api/auth/verify/resend` |

Behind a session (`ProtectedDashboard`; a signed-out visit goes to `/auth` and
returns to the requested page after sign-in):

| Route | Endpoint |
|---|---|
| `/dashboard` | `GET /api/dashboard` |
| `/dashboard/orders` | `GET /api/orders` |
| `/dashboard/orders/:orderId` | filtered client-side from `GET /api/orders` |
| `/dashboard/at-risk` | `GET /api/orders/at-risk` |
| `/dashboard/what-if` | `POST /api/simulations` |
| `/dashboard/recovery` | `GET /api/recovery-plans` |
| `/dashboard/recovery/:planId` | `POST /api/recovery-plans/{plan_id}/approve` |
| `/dashboard/actions` | `GET /api/recovery-actions/{action_id}` |
| `/dashboard/alerts` | `/api/alerts/recipients`, `/api/alerts/history` |

Sign out is in the dashboard rail (`POST /api/auth/logout`).

## Where things live

```text
client/src/App.tsx                    route table; the dashboard is lazy-loaded
client/src/contexts/AuthContext.tsx   the one `/me` query every page reads the session from
client/src/components/                ProtectedDashboard, PasswordChecklist, RecoveryShell, brand helpers
client/src/pages/                     landing, auth, reset, set-new-password, verify-email
client/src/lib/api.ts                 the only module containing a URL; mock/live switch lives here
client/src/lib/types.ts               mirrors backend/app/schemas.py
client/src/index.css                  landing and auth visual system (Tailwind + custom)
client/src/dashboard/                 the command centre: pages, components, queries, fixtures
client/src/dashboard/dashboard.css    every rule scoped under .sg-dashboard so it cannot restyle the landing page
server/index.ts                       static production server
```

## What this frontend must never do

Calculate SLA status, queue positions, or risk thresholds; fake recovery
outcomes; write to the database; persist hypothetical state; or call
`POST /recovery-actions/{id}/status` — that write belongs to n8n, and calling it
here would mean the UI claiming an execution result it never observed.
See doc 06 section 9 and doc 09 section 1.

## Known gaps, and what this code does about them

**The demand multiplier does nothing.** The backend accepts
`demand_multiplier`, echoes it back as `applied_demand_multiplier`, and never
applies it to the schedule. The What-If control is therefore hidden behind
`DEMAND_MULTIPLIER_ENABLED` in `client/src/dashboard/pages/WhatIf.tsx`. Turn it on only once a
real future-arrival projection exists (doc 08 section 4, doc 12 section 1).

**Cost Saver is currently a no-op.** Because the multiplier is inert and the
plan changes neither capacity nor promise hours, its projection comes back
identical to the baseline. `SummaryDelta` says so plainly rather than showing an
empty delta column, and the recovery card flags a plan that adds no capacity.
The real fix is Member B's: give Cost Saver a lever that works.

**Recommendation can pick the no-op plan.** `get_recovery_plans` sorts by
`(at_risk + breached, capacity)`, so under a light surge where every plan clears
the risk, the tie-break selects the lowest capacity — Cost Saver. Worth raising
before demo day.

**SLA counts and backlog have different denominators.** `compute_schedule`
classifies `PENDING` orders only, while `backlog_orders` counts PENDING,
PICKING, PACKED and READY. The counts will not sum to the backlog, so every SLA
panel is labelled "of N pending orders".

**No `GET /orders/{order_id}`.** `getOrder` reads the full queue and filters.
React Query dedupes it against the orders list, so the detail page normally
costs no extra request. Swap the body of `getOrder` when the route lands.

**CORS is an allow-list.** `FRONTEND_ORIGIN` accepts a comma-separated list.
Demoing from a phone or a deployed URL means adding that origin there, and
the session cookie needs `SESSION_COOKIE_SECURE=true` once served over https.

**Timestamps are UTC.** The backend uses `datetime.now(UTC)` throughout. All
rendering goes through `client/src/dashboard/lib/format.ts`, which pins display to IST and labels
it, so promised and predicted times are never compared across clocks.

## Fixture numbers are not demo numbers

`client/src/dashboard/mocks.ts` exists for layout work. Demo-day figures must come from the
running scheduler (doc 10 section 11). If the real system produces 76 → 19, show
76 → 19.
