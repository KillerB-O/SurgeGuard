"""A real provider feed must have a legal `source` value to send.

`EventSource` used to be a `StrEnum` of exactly `commerce_sim` and
`fulfillment_sim`, validated on every ingest by Pydantic's `Literal` pin. A
real commerce or WMS feed has no value it could legally send: the core
ingestion contract was named after the simulator, not defined independently
of it.

These cover the registry that replaces the enum: the two demo sources still
work exactly as before (so n8n's hardcoded `"source": "commerce_sim"` payload
keeps validating with zero workflow changes), a newly registered real provider
can send its own id, and an unregistered source is refused.
"""

import asyncio

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

ORDER_BODY = {
    "event_id": "provider-test-order-0001",
    "order_id": "PROV-0001",
    "facility_id": "WH-01",
    "created_at": "2026-09-15T08:00:00Z",
    "promised_dispatch_at": "2026-09-16T08:00:00Z",
    "item_count": 1,
    "work_units": 1.0,
    "order_value": "10.00",
}


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


async def _register_provider(provider_id: str, *, is_demo: bool = False) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO event_providers (provider_id, kind, is_demo)
            VALUES ($1, 'commerce', $2)
            ON CONFLICT (provider_id) DO NOTHING
            """,
            provider_id,
            is_demo,
        )
    finally:
        await conn.close()


async def _delete_order(order_id: str) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute("DELETE FROM orders WHERE order_id = $1", order_id)
    finally:
        await conn.close()


@pytest.mark.usefixtures("require_database")
def test_the_seeded_demo_sources_still_validate(monkeypatch):
    """n8n's hardcoded body must keep working with zero workflow changes."""
    monkeypatch.setattr(settings, "service_token", "test-token")
    _run(_delete_order(ORDER_BODY["order_id"]))

    body = {**ORDER_BODY, "source": "commerce_sim"}
    with TestClient(app) as client:
        response = client.post(
            "/api/events/orders", json=body, headers={"X-Service-Token": "test-token"}
        )

    assert response.status_code == 200, response.text


@pytest.mark.usefixtures("require_database")
def test_a_registered_real_provider_may_send_its_own_source(monkeypatch):
    """A source is not required to be a simulator to be legal.

    This is the fix for the core defect: the contract used to accept exactly
    two literal values, both named after the simulator.
    """
    monkeypatch.setattr(settings, "service_token", "test-token")
    _run(_register_provider("acme-commerce-v1"))
    _run(_delete_order("PROV-REAL-0001"))

    body = {**ORDER_BODY, "order_id": "PROV-REAL-0001", "source": "acme-commerce-v1"}
    with TestClient(app) as client:
        response = client.post(
            "/api/events/orders", json=body, headers={"X-Service-Token": "test-token"}
        )

    assert response.status_code == 200, response.text


@pytest.mark.usefixtures("require_database")
def test_a_fulfillment_provider_cannot_send_a_commerce_event(monkeypatch):
    """A provider registered for one kind cannot claim the other's identity.

    The old `Literal` enum guaranteed this for free: `OrderCreatedEvent.source`
    and `FulfillmentSnapshotEvent.source` were pinned to different literal
    values, so a commerce event could never claim to be `fulfillment_sim`.
    Widening `source` to a plain string would silently drop that guarantee
    unless the registry check also verifies `kind`.
    """
    monkeypatch.setattr(settings, "service_token", "test-token")

    body = {**ORDER_BODY, "order_id": "PROV-WRONGKIND-0001", "source": "fulfillment_sim"}
    with TestClient(app) as client:
        response = client.post(
            "/api/events/orders", json=body, headers={"X-Service-Token": "test-token"}
        )

    assert response.status_code == 422
    assert "commerce" in response.text.lower()


@pytest.mark.usefixtures("require_database")
def test_an_unregistered_source_is_refused(monkeypatch):
    """A source with no registry entry is not a silent success.

    Widening from a two-value enum to any string must not mean any string is
    accepted -- an unregistered source is still a mistake worth surfacing.
    """
    monkeypatch.setattr(settings, "service_token", "test-token")

    body = {**ORDER_BODY, "order_id": "PROV-UNKNOWN-0001", "source": "nobody-registered-this"}
    with TestClient(app) as client:
        response = client.post(
            "/api/events/orders", json=body, headers={"X-Service-Token": "test-token"}
        )

    assert response.status_code == 422
