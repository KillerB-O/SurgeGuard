"""`facility_id` must be an explicit, required parameter -- not a silent
default to WH-01.

Every facility-scoped route used to declare `facility_id: str = "WH-01"`.
That is the same shape of problem P1 already fixed for `EventSource` being
named after one demo simulator: an operator who forgets the parameter, or a
URL that drops it through a typo or a stale integration, silently reads or
writes WH-01's data instead of getting a clear error -- exactly the wrong
failure mode in a multi-facility deployment, where WH-01 might not even be
the caller's own facility.

The frontend (`frontend/src/api.ts`) never omits `facility_id` on the wire --
every call builds its query string through `facilityQuery()`, which always
appends it. n8n's executor already calls `GET /recovery-actions?facility_id=
WH-01` explicitly. So making the parameter required costs nothing for either
real caller; it only removes an unsafe implicit fallback.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import sign_in

pytestmark = pytest.mark.usefixtures("require_database")

FACILITY_ID_TEST_ACCOUNT = "facility-id-required-tests@example.com"


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Each `with TestClient(app) as client:` block below runs on its own
    fresh event loop; dispose the app's shared engine after each so the next
    test's loop never reuses a pooled connection tied to a closed one.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def test_dashboard_rejects_a_missing_facility_id():
    """Omitting facility_id must 422, not silently read WH-01."""
    with TestClient(app) as client:
        sign_in(client, FACILITY_ID_TEST_ACCOUNT)
        response = client.get("/api/dashboard")

    assert response.status_code == 422


def test_simulations_rejects_a_missing_facility_id():
    """Omitting facility_id must 422, not silently simulate WH-01."""
    with TestClient(app) as client:
        sign_in(client, FACILITY_ID_TEST_ACCOUNT)
        response = client.post("/api/simulations", json={"capacity_per_hour": 60})

    assert response.status_code == 422


def test_orders_rejects_a_missing_facility_id():
    """Omitting facility_id must 422, not silently list WH-01's orders."""
    with TestClient(app) as client:
        sign_in(client, FACILITY_ID_TEST_ACCOUNT)
        response = client.get("/api/orders")

    assert response.status_code == 422
