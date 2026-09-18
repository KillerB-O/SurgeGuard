"""Demo controls must not be able to touch a real facility's data.

`/demo/reset` exists so a run can start from a known baseline, and it is
deliberately destructive. What it was not meant to be is reachable in a real
deployment, or capable of reaching past the demo facility.

Two things made it both. The deletes carried no WHERE clause at all, so a reset
emptied every facility's orders, actions and snapshots rather than the demo
one's. And the only gate was the presence of `SIMULATOR_URL` plus any signed-in
session, so a shared env template, a misconfiguration, or a facility that
happened to be called WH-01 was enough to lose real operational data to a single
POST by any operator.

These cover the containment: an explicit demo mode, a facility that has to be
flagged as a demo facility, an administrator to authorise it, and deletes that
stop at that facility's own rows.
"""

import asyncio
import json
from datetime import time

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.helpers import mark_verified, sign_in


class _StubSimulatorClient:
    """Stand-in for httpx.AsyncClient so reset does not need a live simulator.

    Mirrors the pattern in test_executor.py's _StubClient: this test is about
    the containment gates around reset, not about the simulator round-trip.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url: str, json: dict | None = None):
        if url.endswith("/scenario/reset"):
            body = {"stage": "NORMAL", "capacity_per_hour": 52.0}
        else:
            body = {}
        return httpx.Response(
            200, request=httpx.Request("POST", url), content=json_dumps(body)
        )


def json_dumps(body: dict) -> bytes:
    return json.dumps(body).encode()

DEMO_FACILITY = "WH-01"
REAL_FACILITY = "WH-REAL-01"


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Each TestClient block runs on its own loop; do not reuse its pool."""
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


async def _seed_real_facility_with_an_order() -> None:
    """Create a second, non-demo facility holding one real order."""
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO facilities (facility_id, name, capacity_per_hour,
                                    dispatch_promise_hours, is_demo)
            VALUES ($1, 'Real Warehouse', 40, 24, FALSE)
            ON CONFLICT (facility_id) DO UPDATE SET is_demo = FALSE
            """,
            REAL_FACILITY,
        )
        await conn.execute(
            """
            INSERT INTO orders (order_id, facility_id, created_at, promised_dispatch_at,
                                item_count, work_units, order_value, status)
            VALUES ('REAL-0001', $1, NOW(), NOW() + INTERVAL '24 hours',
                    1, 1.0, 0, 'PENDING')
            ON CONFLICT (order_id) DO NOTHING
            """,
            REAL_FACILITY,
        )
    finally:
        await conn.close()


async def _count_orders(facility_id: str) -> int:
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM orders WHERE facility_id = $1", facility_id
        )
    finally:
        await conn.close()


async def _set_admin(email: str, is_admin: bool) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "UPDATE users SET is_admin = $2 WHERE lower(email) = lower($1)",
            email,
            is_admin,
        )
    finally:
        await conn.close()


def _admin_client(client: TestClient, email: str) -> None:
    """Sign in and promote to administrator."""
    sign_in(client, email)
    mark_verified(email)
    _run(_set_admin(email, True))


@pytest.mark.usefixtures("require_database")
def test_reset_leaves_a_real_facilitys_orders_alone(monkeypatch):
    """The blast radius of a demo reset is the demo facility, nothing more.

    The deletes used to run unscoped, so resetting the demo emptied the orders
    of every facility in the database.
    """
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    monkeypatch.setattr(settings, "simulator_url", "http://simulator.test:8010", raising=False)
    monkeypatch.setattr(
        "app.routers.demo.httpx.AsyncClient", lambda **kwargs: _StubSimulatorClient()
    )
    _run(_seed_real_facility_with_an_order())

    with TestClient(app) as client:
        _admin_client(client, "demo-containment-scope@example.com")
        response = client.post(f"/api/demo/reset?facility_id={DEMO_FACILITY}")

    assert response.status_code == 200, response.text
    assert _run(_count_orders(REAL_FACILITY)) == 1, (
        "a demo reset deleted a real facility's orders"
    )


async def _read_cutoff(facility_id: str):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchval(
            "SELECT dispatch_cutoff_utc FROM facilities WHERE facility_id = $1", facility_id
        )
    finally:
        await conn.close()


@pytest.mark.usefixtures("require_database")
def test_reset_restores_the_demo_carrier_pickup(monkeypatch):
    """Reset returns to a known baseline, and that baseline has a pickup.

    It used to clear the cutoff, which kept "Book a late carrier pickup"
    permanently unavailable, and an approved cutoff shift was never undone.
    """
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    monkeypatch.setattr(settings, "simulator_url", "http://simulator.test:8010", raising=False)
    monkeypatch.setattr(
        "app.routers.demo.httpx.AsyncClient", lambda **kwargs: _StubSimulatorClient()
    )

    with TestClient(app) as client:
        _admin_client(client, "demo-containment-cutoff@example.com")
        response = client.post(f"/api/demo/reset?facility_id={DEMO_FACILITY}")

    assert response.status_code == 200, response.text
    assert _run(_read_cutoff(DEMO_FACILITY)) == time(12, 30)


@pytest.mark.usefixtures("require_database")
def test_reset_refuses_a_facility_that_is_not_a_demo_facility(monkeypatch):
    """Only a facility explicitly flagged as a demo may be reset."""
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    monkeypatch.setattr(settings, "simulator_url", "http://simulator.test:8010", raising=False)
    _run(_seed_real_facility_with_an_order())

    with TestClient(app) as client:
        _admin_client(client, "demo-containment-refuse@example.com")
        response = client.post(f"/api/demo/reset?facility_id={REAL_FACILITY}")

    assert response.status_code == 409
    assert "demo" in response.text.lower()


@pytest.mark.usefixtures("require_database")
def test_demo_controls_are_off_without_demo_mode(monkeypatch):
    """A configured simulator URL is not on its own permission to be reachable.

    Gating only on `SIMULATOR_URL` meant a shared environment template was
    enough to expose destructive controls in a real deployment.
    """
    monkeypatch.setattr(settings, "demo_mode", False, raising=False)

    with TestClient(app) as client:
        _admin_client(client, "demo-containment-off@example.com")
        response = client.post(f"/api/demo/reset?facility_id={DEMO_FACILITY}")

    assert response.status_code == 404


@pytest.mark.usefixtures("require_database")
def test_an_unconfigured_deployment_404s_for_a_non_admin_too(monkeypatch):
    """Absence must be checked before authorisation, not after.

    A non-admin hitting a genuinely unconfigured deployment must see "this
    does not exist" (404), not "you may not do this" (403) -- the latter
    would leak that the feature exists at all, and reverses the priority an
    unconfigured environment needs: nobody should be able to reset anything,
    regardless of role.
    """
    monkeypatch.setattr(settings, "demo_mode", False, raising=False)
    email = "demo-containment-unconfigured-nonadmin@example.com"

    with TestClient(app) as client:
        sign_in(client, email)
        mark_verified(email)
        _run(_set_admin(email, False))
        response = client.post(f"/api/demo/reset?facility_id={DEMO_FACILITY}")

    assert response.status_code == 404


@pytest.mark.usefixtures("require_database")
def test_reset_requires_an_administrator(monkeypatch):
    """A destructive control needs more than any signed-in operator.

    `users.role` cannot serve here: it is free text the user types at signup,
    so anyone could award themselves one.
    """
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    monkeypatch.setattr(settings, "simulator_url", "http://simulator.test:8010", raising=False)
    email = "demo-containment-nonadmin@example.com"

    with TestClient(app) as client:
        sign_in(client, email)
        mark_verified(email)
        _run(_set_admin(email, False))
        response = client.post(f"/api/demo/reset?facility_id={DEMO_FACILITY}")

    assert response.status_code == 403


@pytest.mark.usefixtures("require_database")
@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/demo/surge", {"stage": "SURGE_3"}),
        ("/api/demo/live/start", {"speed": 60}),
        ("/api/demo/live/stop", None),
    ],
)
def test_run_controls_require_an_administrator(path, body, monkeypatch):
    """Starting or stopping a run rewrites what every operator sees.

    Reset was admin-only while these were open to any signed-in account,
    including one anybody can create through signup.
    """
    monkeypatch.setattr(settings, "demo_mode", True, raising=False)
    monkeypatch.setattr(settings, "simulator_url", "http://simulator.test:8010", raising=False)
    email = "demo-containment-run-nonadmin@example.com"

    with TestClient(app) as client:
        sign_in(client, email)
        mark_verified(email)
        _run(_set_admin(email, False))
        response = client.post(path, json=body) if body is not None else client.post(path)

    assert response.status_code == 403


@pytest.mark.usefixtures("require_database")
def test_me_reports_whether_the_user_is_an_administrator(monkeypatch):
    """The dashboard hides demo controls from anyone who could not use them."""
    email = "demo-containment-me-admin@example.com"

    with TestClient(app) as client:
        sign_in(client, email)
        mark_verified(email)
        _run(_set_admin(email, False))
        assert client.get("/api/me").json()["is_admin"] is False
        _run(_set_admin(email, True))
        assert client.get("/api/me").json()["is_admin"] is True
