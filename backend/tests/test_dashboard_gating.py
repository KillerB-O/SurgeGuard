"""ACCESS-01 (dashboard gating) and ACCESS-03 (seeded demo account).

Phase 3 moves the dashboard reads, simulations, recovery plans/actions, and
demo controls from open to session-gated -- the one point in this milestone
where existing, currently-open endpoints change behaviour. This file is the
standing proof of that change, the same role `tests/test_service_token.py`
plays for the machine-token gates Phase 1 introduced.

Unlike `require_service_token` (no database dependency at all -- see its
docstring), `require_session`'s OWN signature carries
`conn: AsyncConnection = Depends(get_connection)`, and FastAPI resolves a
dependency's sub-dependencies before that dependency's body ever runs. So
every test below, including the 401-with-no-cookie ones, opens a real
database connection to get its answer, and every test in this file is marked
`require_database` (module-wide, not per test) for that reason. This is the
same asymmetry `app/auth/dependencies.py` documents for
`require_session_or_service_token`: a token-only call to the one dual-caller
endpoint surfaces a 503 rather than a 401 when Postgres is unreachable,
because its cookie branch's `Depends(get_connection)` resolves unconditionally
too.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.helpers import read_is_admin, read_verified, sign_in

pytestmark = pytest.mark.usefixtures("require_database")

# Every endpoint Phase 3 moves from open to session-gated. Driven from one
# list, like test_service_token.py's GATED_ENDPOINTS, so a later addition or
# removal of a dashboard-data endpoint cannot silently skip a case.
SESSION_GATED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/api/dashboard"),
    ("GET", "/api/orders"),
    ("GET", "/api/orders/at-risk"),
    ("GET", "/api/orders/some-order-id"),
    ("POST", "/api/simulations"),
    ("GET", "/api/recovery-plans"),
    ("POST", "/api/recovery-plans/some-plan-id/approve"),
    ("GET", "/api/recovery-actions/some-action-id"),
    ("GET", "/api/demo/status"),
    ("POST", "/api/demo/reset"),
    ("POST", "/api/demo/live/start"),
    ("POST", "/api/demo/live/stop"),
    ("POST", "/api/demo/surge"),
]


def _request(client: TestClient, method: str, path: str):
    """Issue one request, sending an empty JSON body on writes so a route
    with a required body model fails validation rather than erroring on a
    missing body -- irrelevant to the auth assertions below, which only care
    whether the response is 401.
    """
    body = {} if method == "POST" else None
    return client.request(method, path, json=body)


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared async engine's pool after each test.

    Each `with TestClient(app) as client:` block below runs on its own fresh
    event loop; without disposing the pool between tests, the next test's
    loop would try to reuse a connection tied to this test's already-closed
    loop. Same fixture, same reasoning, as the rest of this suite.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# ACCESS-01: rejection with no session.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", SESSION_GATED_ENDPOINTS)
def test_gated_endpoint_rejects_no_session(method, path):
    """Every dashboard-data endpoint 401s with no session cookie at all."""
    with TestClient(app) as client:
        r = _request(client, method, path)
        assert r.status_code == 401, (
            f"{method} {path} returned {r.status_code} with no session, expected 401"
        )


def test_health_stays_open_after_dashboard_gating():
    """ACCESS-04: gating the dashboard must never touch /health."""
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# ACCESS-01: acceptance with a valid session -- needs a real account.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", SESSION_GATED_ENDPOINTS)
def test_gated_endpoint_accepts_a_valid_session(method, path, monkeypatch):
    """A signed-in operator reaches the endpoint's own logic, not the gate.

    Asserts `!= 401`, not `== 200`, the same convention the superseded
    `test_open_endpoints_do_not_demand_a_credential` used: several of these
    paths carry a made-up id (`some-order-id`, `some-plan-id`,
    `some-action-id`) that legitimately 404. The claim under test is "a
    valid session is accepted", not "this specific made-up id resolves to
    something".

    `settings.simulator_url` is pinned to `None` explicitly (not relied on
    as an ambient default) for the four `/api/demo/*` cases: those routes
    404 on "disabled" only when it is unset, and if it ever fell through to
    a real, configured simulator, `POST /api/demo/reset` truncates
    `orders`/`recovery_actions`/`processed_events`/`fulfillment_snapshots`
    (see `app/routers/demo.py`) -- a `!= 401` assertion would still pass
    while silently taking that destructive path instead of the intended
    "disabled" 404.
    """
    monkeypatch.setattr(settings, "simulator_url", None)

    with TestClient(app) as client:
        sign_in(client, "dashboard-gating-tests@example.com")
        r = _request(client, method, path)
        assert r.status_code != 401, f"{method} {path} rejected a valid session"


# ---------------------------------------------------------------------------
# ACCESS-03: the seeded, pre-verified demo account.
# ---------------------------------------------------------------------------


def test_seeded_demo_account_exists_verified_and_can_log_in(monkeypatch):
    """The configured demo account needs no signup or email verification.

    Configures `settings.demo_user_email`/`demo_user_password` and then
    opens a `TestClient`, whose `__enter__` runs the app's lifespan -- the
    same startup hook that seeds this account in a real deployment (see
    `app.main.lifespan` / `app.auth.seed.seed_demo_user`). No `POST
    /auth/signup` and no `mark_verified` call appear anywhere in this test;
    that is the entire point of ACCESS-03.
    """
    email = "demo-access-03-tests@example.com"
    password = "Correct-Horse-Battery9"
    monkeypatch.setattr(settings, "demo_user_email", email)
    monkeypatch.setattr(settings, "demo_user_password", password)

    with TestClient(app) as client:
        assert read_verified(email) is True, "seeded demo account must start verified"
        assert read_is_admin(email) is True, "seeded demo account must be an administrator"

        login = client.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text

        me = client.get("/api/me")
        assert me.status_code == 200
        # seed_demo_user's INSERT never lists full_name/company/role, so
        # these fall back to migration 0013's column default of "".
        assert me.json() == {
            "email": email,
            "verified": True,
            "full_name": "",
            "company": "",
            "role": "",
            "is_admin": True,
        }


def test_seeding_the_demo_account_is_idempotent_across_restarts(monkeypatch):
    """A second application start with the same demo credentials must not crash.

    `docker compose restart backend` re-runs the startup hook against an
    account that already exists. `seed_demo_user`'s `ON CONFLICT ((lower(
    email))) DO UPDATE SET verified = TRUE` must make that a harmless
    re-verify, not an unhandled `IntegrityError` that takes the whole
    application down.
    """
    email = "demo-access-03-idempotent@example.com"
    password = "Correct-Horse-Battery9"
    monkeypatch.setattr(settings, "demo_user_email", email)
    monkeypatch.setattr(settings, "demo_user_password", password)

    with TestClient(app):
        pass  # first "start": seeds the account

    # See the identically-reasoned dispose call in
    # test_seeding_verifies_an_existing_unverified_account_at_the_same_email.
    from app.db import engine as _engine

    asyncio.run(_engine.dispose())

    with TestClient(app) as client:  # second "start": must not raise
        login = client.post("/api/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text


def test_seeding_verifies_an_existing_unverified_account_at_the_same_email(monkeypatch):
    """A prior signup at the demo email must not survive as unverified.

    Covers the collision `ON CONFLICT DO NOTHING` would have handled wrong:
    if the configured `DEMO_USER_EMAIL` already has a row -- here, from an
    ordinary signup that never clicked its verification link -- seeding must
    still leave it verified, or the account is exactly as unable to sign in
    as ACCESS-03 exists to prevent. See `seed_demo_user`'s docstring on why
    this targets the conflict with `DO UPDATE SET verified = TRUE` rather
    than reusing signup's bare `DO NOTHING`.
    """
    email = "demo-access-03-collision@example.com"
    demo_password = "Correct-Horse-Battery9"
    original_password = "Some-Other-Password9"

    with TestClient(app) as setup_client:
        signup = setup_client.post(
            "/api/auth/signup", json={"email": email, "password": original_password}
        )
        assert signup.status_code == 201
        assert signup.json()["verified"] is False

    # Dispose the shared engine's pool before the next `with TestClient(app)`
    # block below: each such block runs on its own fresh event loop, and
    # without this, the second block's lifespan-triggered seed would try to
    # reuse a pooled connection tied to this block's already-closed loop --
    # `seed_demo_user`'s own broad `except Exception:` would then swallow
    # that failure silently, and this test would see an unverified row with
    # no visible error at all. Same fixture, same reasoning, as
    # `dispose_shared_engine_between_tests` (which only runs between separate
    # tests, not between two blocks inside one test).
    from app.db import engine as _engine

    asyncio.run(_engine.dispose())

    monkeypatch.setattr(settings, "demo_user_email", email)
    monkeypatch.setattr(settings, "demo_user_password", demo_password)

    with TestClient(app):
        pass  # lifespan runs seed_demo_user against the existing, unverified row

    assert read_verified(email) is True, "an existing row at the demo email must end up verified"
    assert read_is_admin(email) is True, "an existing row at the demo email must become admin"

    asyncio.run(_engine.dispose())
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login", json={"email": email, "password": demo_password}
        )
        assert login.status_code == 200, login.text


def test_demo_seed_failure_is_swallowed_and_never_blocks_startup(monkeypatch):
    """A seeding failure must never be the reason the application fails to start.

    `/health` performs no database access at all (ACCESS-04) and must keep
    answering even when the startup seed itself blows up -- simulated here
    with a hashing failure so no real database outage is required to prove
    the swallow.
    """

    def _boom(_plaintext: str) -> str:
        raise RuntimeError("simulated hashing failure")

    monkeypatch.setattr(settings, "demo_user_email", "seed-failure-tests@example.com")
    monkeypatch.setattr(settings, "demo_user_password", "Correct-Horse-Battery9")
    monkeypatch.setattr("app.auth.seed.hash_password", _boom)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_demo_account_is_never_seeded_when_unconfigured(monkeypatch):
    """No credentials configured means no account -- the fail-closed default.

    `seed_demo_user` itself is DB-free in this case -- it returns before ever
    opening a connection when either setting is unset (see its docstring) --
    but `POST /auth/login` always queries `users` regardless of whether a
    matching row exists (see login's own docstring on paying one Argon2
    verification either way), so this test still needs a reachable database.
    """
    monkeypatch.setattr(settings, "demo_user_email", None)
    monkeypatch.setattr(settings, "demo_user_password", None)

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"email": "nobody-configured-this@example.com", "password": "irrelevant"},
        )
        assert login.status_code == 401
