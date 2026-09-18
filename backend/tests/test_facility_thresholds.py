"""SLA and surge-risk thresholds must be per-facility, not global constants.

`app/scheduler.py` used to classify every facility against the same
`WATCH_THRESHOLD_HOURS`/`AT_RISK_THRESHOLD_HOURS` and the same three exposure
ratios, regardless of what that facility's own policy should be -- the same
shape of problem P1 already fixed for `EventSource`
being named after one demo simulator. Migration 0021 gives `facilities` its
own columns for these, defaulted to the frozen values so WH-01's
classification does not move.

These tests seed a second facility with deliberately different thresholds
and prove the dashboard classifies against *that* facility's own configured
values, not the global constants.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.helpers import sign_in

pytestmark = pytest.mark.usefixtures("require_database")

THRESHOLDS_TEST_ACCOUNT = "facility-thresholds-tests@example.com"
TIGHT_FACILITY = "WH-TIGHT-01"
LOOSE_EXPOSURE_FACILITY = "WH-LOOSE-EXPOSURE-01"


@pytest.fixture(autouse=True)
def clean_tables():
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM recovery_actions")
            await conn.execute(
                "DELETE FROM orders WHERE facility_id IN ($1, $2, 'WH-01')",
                TIGHT_FACILITY,
                LOOSE_EXPOSURE_FACILITY,
            )
            await conn.execute(
                "DELETE FROM facilities WHERE facility_id IN ($1, $2)",
                TIGHT_FACILITY,
                LOOSE_EXPOSURE_FACILITY,
            )
        finally:
            await conn.close()

    asyncio.run(_clean())
    yield


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


async def _seed_facility(facility_id: str, **thresholds: float) -> None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        columns = ", ".join(thresholds)
        placeholders = ", ".join(f"${i + 3}" for i in range(len(thresholds)))
        await conn.execute(
            f"""
            INSERT INTO facilities (facility_id, name, capacity_per_hour,
                                    dispatch_promise_hours, {columns})
            VALUES ($1, $2, 52, 24, {placeholders})
            """,
            facility_id,
            facility_id,
            *thresholds.values(),
        )
    finally:
        await conn.close()


async def _seed_order(facility_id: str, order_id: str, hours_until_due: float) -> None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        now = datetime.now(UTC)
        await conn.execute(
            """
            INSERT INTO orders (order_id, facility_id, created_at, promised_dispatch_at,
                                item_count, work_units, order_value, status)
            VALUES ($1, $2, $3, $4, 1, 1.0, 0, 'PENDING')
            """,
            order_id,
            facility_id,
            now,
            now + timedelta(hours=hours_until_due),
        )
    finally:
        await conn.close()


def test_sla_classification_uses_the_facilitys_own_thresholds():
    """The same slack must classify differently under different facility policy."""
    asyncio.run(
        _seed_facility(
            TIGHT_FACILITY,
            watch_threshold_hours=3.0,
            at_risk_threshold_hours=1.0,
        )
    )
    # ~6 hours of slack: WATCH under the global default (<=12h, >4h), but
    # SAFE under this facility's own tighter 3h/1h policy.
    asyncio.run(_seed_order(TIGHT_FACILITY, "TIGHT-ORD-1", hours_until_due=6.0))
    asyncio.run(_seed_order("WH-01", "DEFAULT-ORD-1", hours_until_due=6.0))

    with TestClient(app) as client:
        sign_in(client, THRESHOLDS_TEST_ACCOUNT)
        default_dashboard = client.get("/api/dashboard?facility_id=WH-01").json()
        tight_dashboard = client.get(f"/api/dashboard?facility_id={TIGHT_FACILITY}").json()

    assert default_dashboard["sla_counts"]["watch"] == 1
    assert tight_dashboard["sla_counts"]["safe"] == 1
    assert tight_dashboard["sla_counts"]["watch"] == 0


def test_facility_risk_uses_the_facilitys_own_exposure_ratio():
    """The same exposure ratio must classify differently under different appetite."""
    asyncio.run(
        _seed_facility(LOOSE_EXPOSURE_FACILITY, exposure_high_ratio=0.05)
    )
    # 1 at-risk order out of 10 pending = 10% exposure: MEDIUM under the
    # global default (10% < 15% high threshold), HIGH under this facility's
    # own 5% high threshold.
    for i in range(9):
        asyncio.run(_seed_order(LOOSE_EXPOSURE_FACILITY, f"LOOSE-SAFE-{i}", hours_until_due=48.0))
        asyncio.run(_seed_order("WH-01", f"DEFAULT-SAFE-{i}", hours_until_due=48.0))
    asyncio.run(_seed_order(LOOSE_EXPOSURE_FACILITY, "LOOSE-AT-RISK", hours_until_due=2.0))
    asyncio.run(_seed_order("WH-01", "DEFAULT-AT-RISK", hours_until_due=2.0))

    with TestClient(app) as client:
        sign_in(client, THRESHOLDS_TEST_ACCOUNT)
        default_dashboard = client.get("/api/dashboard?facility_id=WH-01").json()
        loose_dashboard = client.get(f"/api/dashboard?facility_id={LOOSE_EXPOSURE_FACILITY}").json()

    assert default_dashboard["risk_level"] == "MEDIUM"
    assert loose_dashboard["risk_level"] == "HIGH"
