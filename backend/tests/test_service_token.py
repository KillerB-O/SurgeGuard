"""DB-free proof that n8n's seven endpoints demand a service token (or, for
the one dual-caller endpoint, a session), plus a standing structural guard
that auth can never spread beyond those seven routes (D-10) -- the single
mistake that would take the live demo down.

`POST /api/events/order-revised` (P8) joined this list as the sixth
token-only endpoint: unlike `POST /api/events/backfill` (a one-time
onboarding import n8n never calls), the plan names order revision as
needing a fifth n8n workflow (additive, does not disturb the four locked-in
workflow ids), so it belongs on this list the same way the original four
event/action endpoints do.

Most of this file runs with NO database reachable at all.
`require_service_token` takes no `Depends(get_connection)`, and FastAPI puts
path-operation `dependencies=` at the front of the dependency list, so a bad
or absent token 401s before any transaction opens for the six
`require_service_token`-only endpoints. Docker being down on this machine is
therefore not a reason these tests can't run -- it is part of what they
prove: a 401 here, and specifically NOT a 503 (this repo's
database-unavailable response, see `app/main.py`'s `OperationalError`
handler), is the signal that the auth dependency resolved and rejected
BEFORE `Depends(get_connection)` ever touched Postgres.

`GET /api/recovery-actions` (`require_session_or_service_token`) is the one
exception to that 401-not-503 guarantee: its dependency signature also takes
`conn: AsyncConnection = Depends(get_connection)` (the same shape
`require_session` already uses, and the same connection the handler itself
needs), so with Postgres unreachable a token-only request to that endpoint
surfaces the database's own 503 rather than a 401. That is a known,
structural difference from the other six, not a bug -- see plan 03's
SUMMARY.

The acceptance tests at the bottom of this file DO need real rows and are
marked `require_database` individually, not via a module-level `pytestmark`,
because the DB-free tests above them must keep working even when Postgres is
unreachable -- that is the entire point of this file.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.conftest import TEST_SERVICE_TOKEN
from tests.helpers import mark_verified

# The seven endpoints n8n calls, gated in plan 03 task 3 (six) plus P8's
# order-revised endpoint (its fifth workflow). Driven from this list (rather
# than duplicated per test) so a later endpoint addition or removal cannot
# silently skip a case -- per the plan's own instruction.
GATED_ENDPOINTS: list[tuple[str, str]] = [
    ("POST", "/api/events/orders"),
    ("POST", "/api/events/fulfillment"),
    ("POST", "/api/events/order-status"),
    ("POST", "/api/events/order-revised"),
    ("POST", "/api/recovery-actions/some-action-id/status"),
    ("POST", "/api/recovery-actions/some-action-id/apply"),
    ("GET", "/api/recovery-actions?facility_id=WH-01"),
]

# SUPERSEDED by Phase 3 (ACCESS-01): a fixed `OPEN_ENDPOINTS` list and its
# parametrized "stays open" test used to live here -- the negative case from
# Phase 1's <endpoint_map>, back when dashboard gating was deliberately
# deferred. All thirteen of those endpoints (/dashboard, /orders*,
# /simulations, /recovery-plans*, /demo/*, and the singular
# /recovery-actions/{id}) are now session-gated (see reads.py, simulation.py,
# demo.py), which would leave the list -- and an `@pytest.mark.parametrize`
# built from it -- empty. An empty parametrize set is collected as a SKIP by
# pytest, which this suite's own verification gate treats as a failure
# (INTEGRATION-NOTES.md section 18), so the list and its test were removed
# outright rather than left empty. Their 401-without-a-session /
# 200-with-one coverage now lives in tests/test_dashboard_gating.py.


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared async engine's pool after each test.

    Each `with TestClient(app) as client:` block below runs on its own fresh
    event loop, and this file opens many such blocks in sequence (unlike the
    other integration suites, most of these calls resolve without ever
    reaching a route that opens a connection -- but before task 3's gates
    exist, several of them do). Without disposing the pool between tests,
    test N+1's loop tries to reuse a connection tied to test N's
    already-closed loop and blows up -- the same failure mode
    test_events_integration.py's identically named fixture exists to
    prevent. Applied autouse, module-wide, rather than only on the
    `require_database`-marked tests, because pre-gate behaviour (still
    reachable while task 1 writes this file) can hit the database from any
    of the parametrized cases above.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _request(client: TestClient, method: str, path: str, *, headers: dict | None = None):
    """Issue one request, sending an (ignorable if unused) empty JSON body on
    writes so a route with a required body model fails validation rather than
    erroring on a missing body -- irrelevant to the auth assertions below,
    which only care about whether the response is 401.
    """
    body = {} if method == "POST" else None
    return client.request(method, path, json=body, headers=headers)


# ---------------------------------------------------------------------------
# D-10 structural guards -- these must pass immediately and keep passing
# forever. They are the standing insurance for the live demo.
# ---------------------------------------------------------------------------


def test_no_global_dependency_on_the_app_router():
    """D-10: auth is applied per-endpoint only, never as a global dependency.

    A global dependency on `app.router` is the single mistake that would
    silently gate every endpoint, including the ones the frontend and the
    demo depend on.
    """
    assert app.router.dependencies == []


def test_middleware_stack_is_only_what_main_declares():
    """No `BaseHTTPMiddleware` inspecting credentials has snuck into the stack.

    Checked against `app.user_middleware` (what `main.py` actually declared
    via `add_middleware`), not `app.middleware_stack` (the built ASGI stack):
    the built stack always wraps user middleware in Starlette's own
    `ServerErrorMiddleware` / `ExceptionMiddleware`, and that wrapping shape
    varies by Starlette version -- asserting on it would be asserting on
    Starlette internals, not on anything this plan controls.
    """
    names = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert names == ["CORSMiddleware"], (
        f"unexpected middleware stack {names} -- a credential-inspecting "
        "middleware would bypass D-10's per-endpoint discipline entirely"
    )


def test_health_is_open_with_no_credentials():
    """ACCESS-04: /health answers with no credentials of any kind."""
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Gate rejection -- expected to FAIL (RED) until task 3 applies the gates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", GATED_ENDPOINTS)
def test_gated_endpoint_rejects_missing_token(method, path):
    with TestClient(app) as client:
        r = _request(client, method, path)
        assert r.status_code == 401, (
            f"{method} {path} returned {r.status_code} with no credential at all, "
            "expected 401 -- a 503 here would mean the database was reached "
            "before the auth dependency resolved"
        )


@pytest.mark.parametrize("method,path", GATED_ENDPOINTS)
def test_gated_endpoint_rejects_wrong_token(method, path):
    with TestClient(app) as client:
        r = _request(client, method, path, headers={"X-Service-Token": "wrong-token"})
        assert r.status_code == 401, (
            f"{method} {path} returned {r.status_code} with a wrong token, expected 401"
        )


# ---------------------------------------------------------------------------
# Acceptance -- these need real rows, marked require_database individually so
# the DB-free tests above are unaffected by database reachability.
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_service_token_tables():
    """Reset the tables these acceptance tests touch, and dispose the shared
    async engine afterward.

    Deliberately NOT autouse: the DB-free tests above must never attempt a
    connection, which is the entire reason this file can run with Docker
    down. Only the tests below request this fixture explicitly. Uses its own
    throwaway asyncpg connection/event loop, not the app's shared async
    engine, matching test_events_integration.py's `clean_tables` idiom for
    the same reason (see that file's docstring on mixing event loops).
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM recovery_actions")
            await conn.execute("DELETE FROM processed_events")
            await conn.execute("DELETE FROM orders")
            await conn.execute("DELETE FROM fulfillment_snapshots")
            # P4's derived throughput reads order_status_transitions for
            # WH-01 with no per-test scoping, so a leftover row pollutes a
            # later test's dashboard figures.
            await conn.execute("DELETE FROM order_status_transitions")
            await conn.execute("DELETE FROM pending_status_events")
            await conn.execute("DELETE FROM sessions")
            await conn.execute("DELETE FROM users")
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _order_event(event_id: str, order_id: str) -> dict:
    now = datetime.now(UTC)
    return {
        "event_id": event_id,
        "event_type": "order_created",
        "source": "commerce_sim",
        "order_id": order_id,
        "facility_id": "WH-01",
        "created_at": now.isoformat(),
        "promised_dispatch_at": (now + timedelta(hours=24)).isoformat(),
        "item_count": 1,
        "work_units": 1.5,
        "order_value": 199,
    }


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_recovery_actions_accepts_a_valid_service_token_with_no_session():
    with TestClient(app) as client:
        r = client.get(
            "/api/recovery-actions",
            params={"facility_id": "WH-01"},
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_recovery_actions_accepts_a_valid_session_with_no_token():
    """D-11: the frontend's caller path, with no X-Service-Token at all.

    Signs up and logs in for a real cookie rather than fabricating one, so
    this exercises `require_session_or_service_token`'s cookie branch via
    `lookup_session` exactly as `require_session` does.
    """
    with TestClient(app) as client:
        credentials = {"email": "svc-token-dual-check@example.com", "password": "Correct-Horse-Battery9"}
        assert client.post("/api/auth/signup", json=credentials).status_code == 201
        # Login is gated on verification; this test is about the dual-caller
        # dependency, not about email.
        mark_verified(credentials["email"])
        assert client.post("/api/auth/login", json=credentials).status_code == 200

        # This client now holds a real session cookie and no service-token
        # header at all -- confirming neither is required by any earlier
        # `TestClient(app, headers=...)` default elsewhere in the suite.
        r = client.get("/api/recovery-actions", params={"facility_id": "WH-01"})
        assert r.status_code == 200


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_recovery_actions_rejects_neither_credential():
    with TestClient(app) as client:
        r = client.get("/api/recovery-actions", params={"facility_id": "WH-01"})
        assert r.status_code == 401


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_orders_event_with_valid_token_behaves_exactly_as_before_the_gate():
    """POST /api/events/orders with a valid token processes the order exactly
    as it did before this plan's gate existed -- the gate changes who may
    call it, not what happens when the right caller does.
    """
    with TestClient(app) as client:
        r = client.post(
            "/api/events/orders",
            json=_order_event("evt-service-token-check", "ORD-SERVICE-TOKEN-CHECK"),
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"


# ---------------------------------------------------------------------------
# Token-only acceptance for the five n8n endpoints not already covered above.
#
# Phase 3 gave every `TestClient` in test_events_integration.py and
# test_interventions.py a real session (see `sign_in`, ACCESS-01), because
# those files also exercise the now session-gated reads/simulations/plans.
# That is correct for THIS suite's purpose, but it means those files no
# longer prove "a service token alone is sufficient" for the endpoints
# below -- every client there now carries a session AND a token. Without the
# tests in this section, a regression that silently widened
# `/recovery-actions/{id}/status` or `/apply` to `require_session_or_service_
# token` (or narrowed it to `require_session`, locking n8n out) would leave
# this suite green. `/status` and `/apply` are the executor's completion
# path (INTEGRATION-NOTES.md section 10), so this is not a cosmetic gap.
# order-revised (P8) is included here for the same reason: no other file's
# clients would otherwise prove token-only access still works for it.
# ---------------------------------------------------------------------------


async def _insert_pending_recovery_action(action_id: str, facility_id: str = "WH-01") -> None:
    """Seed one PENDING recovery action directly, bypassing the (now
    session-gated) approval endpoint entirely.

    Using `POST /recovery-plans/{plan_id}/approve` to set this up would force
    every client in this section to also sign in, defeating the point of a
    token-only acceptance test. Direct insertion keeps these clients
    header-only, matching `clean_service_token_tables`' own idiom for
    reaching past the app.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO recovery_actions (
                action_id, plan_id, facility_id,
                capacity_per_hour, dispatch_promise_hours, demand_multiplier
            ) VALUES ($1, 'buy-the-hour', $2, 60, 24, 1)
            """,
            action_id,
            facility_id,
        )
    finally:
        await conn.close()


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_fulfillment_event_with_valid_token_behaves_exactly_as_before_the_gate():
    """POST /api/events/fulfillment accepts a token with no session."""
    with TestClient(app) as client:
        r = client.post(
            "/api/events/fulfillment",
            json={
                "event_id": "evt-svc-token-fulfillment",
                "event_type": "fulfillment_snapshot",
                "source": "fulfillment_sim",
                "facility_id": "WH-01",
                "occurred_at": datetime.now(UTC).isoformat(),
                "open_orders": 1,
                "work_units_completed_last_hour": 40,
            },
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_order_status_event_with_valid_token_behaves_exactly_as_before_the_gate():
    """POST /api/events/order-status accepts a token with no session."""
    with TestClient(app) as client:
        client.post(
            "/api/events/orders",
            json=_order_event("evt-svc-token-status-order", "ORD-SVC-TOKEN-STATUS"),
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        r = client.post(
            "/api/events/order-status",
            json={
                "event_id": "evt-svc-token-status",
                "event_type": "order_status_updated",
                "source": "fulfillment_sim",
                "order_id": "ORD-SVC-TOKEN-STATUS",
                "facility_id": "WH-01",
                "occurred_at": datetime.now(UTC).isoformat(),
                "status": "PICKING",
            },
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_order_revised_event_with_valid_token_behaves_exactly_as_before_the_gate():
    """POST /api/events/order-revised accepts a token with no session."""
    with TestClient(app) as client:
        client.post(
            "/api/events/orders",
            json=_order_event("evt-svc-token-revision-order", "ORD-SVC-TOKEN-REVISION"),
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        r = client.post(
            "/api/events/order-revised",
            json={
                "event_id": "evt-svc-token-revision",
                "event_type": "order_revised",
                "source": "commerce_sim",
                "order_id": "ORD-SVC-TOKEN-REVISION",
                "facility_id": "WH-01",
                "promised_dispatch_at": (datetime.now(UTC) + timedelta(hours=48)).isoformat(),
                "item_count": 2,
                "work_units": 3.0,
                "order_value": 250,
            },
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PROCESSED"


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_recovery_action_status_update_with_valid_token_and_no_session():
    """POST /api/recovery-actions/{id}/status accepts a token with no session.

    This is one of n8n's own completion nodes ("Mark Action SUCCESS" /
    "Mark Action FAILED") -- if this ever silently required a session
    instead of (or in addition to) a token, every recovery action n8n
    executes would stall in PENDING.
    """
    action_id = "action-svc-token-status-check"
    asyncio.run(_insert_pending_recovery_action(action_id))

    with TestClient(app) as client:
        r = client.post(
            f"/api/recovery-actions/{action_id}/status",
            json={"status": "SUCCESS"},
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "SUCCESS"


@pytest.mark.usefixtures("require_database", "clean_service_token_tables")
def test_recovery_action_apply_with_valid_token_and_no_session():
    """POST /api/recovery-actions/{id}/apply accepts a token with no session.

    n8n's executor node "Apply Internal Effects" -- the other half of the
    completion path alongside `/status`, on the same router prefix as two
    endpoints with different auth rules (dual-caller GET, session-only
    single-item GET). A regression narrowing or widening this one is easy
    to make and would not show up in test_events_integration.py or
    test_interventions.py now that those clients also carry a session.
    """
    action_id = "action-svc-token-apply-check"
    asyncio.run(_insert_pending_recovery_action(action_id))

    with TestClient(app) as client:
        r = client.post(
            f"/api/recovery-actions/{action_id}/apply",
            headers={"X-Service-Token": TEST_SERVICE_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["action_id"] == action_id
