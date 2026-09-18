# `app/alerts/`

Operator alerting: watch the same numbers the dashboard renders, decide when
they're worth emailing somebody about, and actually get the mail out. For the
wire-level API contract and the operational runbook (how to run this, how it
fails, how to add a rule), see [`backend/ALERTS.md`](../../ALERTS.md). This
file is the map of the code itself.

## Files, in the order data flows through them

| File | Role |
|---|---|
| `rules.py` | The rule catalog loader and the pure decision logic (`should_fire`). No database, no clock side effects — just data in, bool out. |
| `evaluator.py` | Pulls live metrics (via the same `compute_schedule` the dashboard uses), runs every rule against them, and writes PENDING rows to `alert_outbox` for whoever matches. |
| `repository.py` | All SQL for this feature: the recipient list and the outbox. Deliberately separate from `app/repository.py`, which is read-only. |
| `dispatcher.py` | Drains PENDING outbox rows: sends each one via `app/auth/mailer.py`, marks it SENT or retries/retires it on failure. |
| `worker.py` | The process entrypoint (`python -m app.alerts.worker`) that ticks the evaluator and dispatcher forever, on their own schedule, independent of any HTTP request. |
| `router.py` | The operator-facing HTTP surface: manage the recipient list, read delivery history. Session-gated, like the rest of the dashboard. |

Rule *data* lives outside this package, at
[`backend/app/catalog/alert_rules.json`](../catalog/alert_rules.json) — an
operator retunes a threshold there without a deploy (mirrors
`app/levers.json`'s pattern). Schema lives in
[`backend/migrations/versions/0012_alert_recipients_and_outbox.py`](../../migrations/versions/0012_alert_recipients_and_outbox.py).

## The one thing that's easy to forget

**`worker.py` is a separate OS process, not something `backend`'s FastAPI app
runs on its own.** Nothing evaluates rules or sends mail unless it's actually
running:

```
docker compose up -d worker
```

It's a normal service in `docker-compose.yml` (`docker compose ps` should show
`surgeguard-worker` as `Up`), but because it depends on nothing else being
touched to add or change, it's the one piece that's easy to leave stopped
after a fresh `docker compose up` if that command was run before `worker`
existed in the compose file, or was scoped to specific services. If nobody is
receiving alerts and the dashboard otherwise looks fine, check this first —
see the "Nothing is arriving" entry in `ALERTS.md`'s troubleshooting section.

## Why evaluation and delivery are two separate passes

`worker.run_once` runs the evaluator in one transaction (commit), then the
dispatcher in a second, separate transaction. If they shared one:

- An SMTP failure would roll back the *decision* to alert at all, and the
  system would look like it never noticed the problem.
- A crash between "decided" and "sent" would lose the alert outright instead
  of leaving a PENDING row the next tick picks up.

The outbox (`alert_outbox`) is what makes this safe — it's the durable record
of "this was decided," independent of whether the mail has gone out yet, and
it doubles as the cooldown/dedup state (see `repository.last_alert_per_rule`).
There is no separate "last notified" column anywhere to drift out of
agreement with it.

## Adding a new rule

1. Add an entry to `backend/app/catalog/alert_rules.json` — pick a `metric`
   from `AlertMetrics` (`rules.py`), an `operator`, `threshold`, `level`, and
   `cooldown_minutes`. `subject`/`body` are Python `.format()` templates
   filled from `AlertMetrics.as_template_values()`.
2. No code change, no migration, no restart required in production — the
   worker reloads the catalog every tick (`load_rules.cache_clear()` in
   `worker.run_once`). The request-facing password-policy-style "serve it
   from the same source" pattern doesn't apply here since nothing renders
   this catalog to a client; it's purely server-side.
3. Add a case to `backend/tests/test_alert_rules.py` if the new rule has
   interesting edge behavior (a `None`-valued metric, an unusual cooldown
   interaction).

## Testing

- `tests/test_alert_rules.py` — pure `should_fire`/`AlertRule.matches` logic,
  no database.
- `tests/test_alert_evaluator.py` — `evaluate_facility` against a real
  database, including the "matched but nobody subscribed" no-op case.
- `tests/test_alert_dispatcher.py` — `drain_outbox`, including the
  retry/retire-at-`max_attempts` path.
- `tests/test_alert_worker.py` — `run_once`/`run_forever`, including that a
  single tick's exception never kills the loop.
- `tests/test_alert_recipients.py` — the CRUD router surface. Includes an
  autouse `dispose_shared_engine_between_tests` fixture (added after a
  cross-event-loop `RuntimeError` surfaced in CI) — copy this fixture into
  any new test file in this suite that opens more than one `TestClient(app)`
  block, or tests after the first will intermittently fail with
  `RuntimeError: ... attached to a different loop`.

All of the above need a real Postgres — see `ALERTS.md`'s troubleshooting
section if `pytest` is silently skipping them instead of running.
