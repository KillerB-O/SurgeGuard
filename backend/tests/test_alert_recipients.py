"""The operator-managed alert recipient list.

Covers the CRUD surface and the two properties that are easy to regress: the
list is session-gated like the rest of the dashboard (ACCESS-01), and removal
is a soft delete so alert history survives it.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import sign_in

pytestmark = pytest.mark.usefixtures("require_database")


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared async engine's pool after each test.

    Each `with TestClient(app) as client:` block below runs on its own fresh
    event loop. The app's shared async engine (app.db.engine) pools
    connections tied to whichever loop last used it, so without disposing it
    between tests, test N+1's loop would try to reuse a pooled connection
    that belongs to test N's already-closed loop and blow up. Same fixture,
    same reasoning, as test_auth.py.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())

ACCOUNT = "alert-recipient-tests@example.com"

# Recipients persist between runs, like `users` does (see `sign_in`), and
# removal is a SOFT delete -- so a fixed address would 409 on the second run
# against the row the first run left behind. A per-run token keeps each run
# isolated without deleting rows, which matters because `alert_outbox`
# references recipients with ON DELETE RESTRICT: a cleanup fixture that
# DELETEs would start failing the moment the dispatcher writes its first row.
RUN = uuid4().hex[:8]


def _email(local: str) -> str:
    """Return an address unique to this test run."""
    return f"{local}-{RUN}@example.com"


def test_recipients_require_a_session():
    """ACCESS-01: the alert list is dashboard data, not a public read."""
    with TestClient(app) as client:
        assert client.get("/api/alerts/recipients").status_code == 401
        assert (
            client.post(
                "/api/alerts/recipients",
                json={"name": "Nobody", "email": "nobody@example.com"},
            ).status_code
            == 401
        )


def test_added_recipient_appears_in_the_list():
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)
        created = client.post(
            "/api/alerts/recipients",
            json={
                "name": "Ops Head",
                "email": _email("ops-head"),
                "min_level": "CRITICAL",
            },
        )

        assert created.status_code == 201
        body = created.json()
        assert body["min_level"] == "CRITICAL"
        assert body["id"].startswith("recipient-")

        listed = client.get("/api/alerts/recipients").json()["recipients"]
        assert body["id"] in {r["id"] for r in listed}


def test_min_level_defaults_to_high():
    """Adding somebody without a considered choice must not sign them up for
    every MEDIUM blip."""
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)
        created = client.post(
            "/api/alerts/recipients",
            json={"name": "Floor Lead", "email": _email("floor-lead")},
        )

        assert created.status_code == 201
        assert created.json()["min_level"] == "HIGH"


def test_duplicate_active_address_is_rejected():
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)
        payload = {"name": "Dupe", "email": _email("dupe")}
        assert client.post("/api/alerts/recipients", json=payload).status_code == 201

        again = client.post("/api/alerts/recipients", json=payload)

        assert again.status_code == 409


def test_address_with_embedded_crlf_is_rejected():
    """The dispatcher writes this straight into an EmailMessage "To" header,
    so it is the same header-injection vector signup closes (T-02-03)."""
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)
        response = client.post(
            "/api/alerts/recipients",
            json={"name": "Injector", "email": "a@b.com\r\nBcc: evil@example.com"},
        )

        assert response.status_code == 422


def test_removed_recipient_leaves_the_list_and_frees_the_address():
    """Soft delete must not block re-adding the same person later -- the
    unique index is partial on `active` precisely so it does not."""
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)
        payload = {"name": "Temp", "email": _email("temp")}
        created = client.post("/api/alerts/recipients", json=payload).json()

        removed = client.delete(f"/api/alerts/recipients/{created['id']}")

        assert removed.status_code == 204
        listed = client.get("/api/alerts/recipients").json()["recipients"]
        assert created["id"] not in {r["id"] for r in listed}

        readded = client.post("/api/alerts/recipients", json=payload)
        assert readded.status_code == 201
        assert readded.json()["id"] != created["id"]


def test_removing_an_unknown_recipient_is_not_an_error():
    """The caller asked for this person not to be on the list; after either
    outcome they are not. A 404 would also confirm whether an id existed."""
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)

        response = client.delete("/api/alerts/recipients/recipient-does-not-exist")

        assert response.status_code == 204


def test_alert_history_requires_a_session():
    with TestClient(app) as client:
        assert client.get("/api/alerts/history").status_code == 401


def test_alert_history_is_readable():
    """What makes the outbox worth having from an operator's seat: delivery
    state is a row they can see, not a line in a container log."""
    with TestClient(app) as client:
        sign_in(client, ACCOUNT)

        response = client.get("/api/alerts/history")

        assert response.status_code == 200
        alerts = response.json()["alerts"]
        assert isinstance(alerts, list)
        for alert in alerts:
            assert alert["status"] in {"PENDING", "SENT", "FAILED"}
            assert "last_error" in alert
