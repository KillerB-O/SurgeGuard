"""A capacity claim must be checked against measured throughput, not trusted.

`capacity_commitments` records what a capacity_delta effect claimed;
`verify_capacity_commitments` is what later compares it against derived
throughput and writes a verdict. These tests seed both the commitment and the
transition history directly, the same pattern test_derived_throughput.py
uses, since the verdict depends on precise timing this codebase's own clock
would make awkward to control from an HTTP flow.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest

from app.config import settings
from app.db import engine
from app.verification import verify_capacity_commitments

FACILITY = "WH-01"


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    yield
    asyncio.run(engine.dispose())


def _dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def _run(coro):
    return asyncio.run(coro)


async def _clean_up(*order_ids: str) -> None:
    """Clear ALL of WH-01's orders, transitions, and commitment/action state.

    Every order for the facility, not just this test's own named ones:
    get_demand_work_units_per_hour (repository.py) sums every order created
    in the last hour for the whole facility, so a stray order left by an
    earlier test in this same file -- or a different one sharing WH-01 --
    would silently inflate demand and change which verdict a commitment
    resolves to. `order_ids` is accepted for readability at call sites but no
    longer narrows the delete; kept so each test still documents which ids it
    is about to create.
    """
    del order_ids  # documentation only; the delete below is facility-wide
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "DELETE FROM order_status_transitions WHERE facility_id = $1", FACILITY
        )
        await conn.execute("DELETE FROM capacity_commitments WHERE facility_id = $1", FACILITY)
        await conn.execute("DELETE FROM recovery_actions WHERE facility_id = $1", FACILITY)
        await conn.execute("DELETE FROM orders WHERE facility_id = $1", FACILITY)
    finally:
        await conn.close()


async def _seed_order(order_id: str, *, work_units: float, created_at: datetime) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO orders (order_id, facility_id, created_at, promised_dispatch_at,
                                item_count, work_units, order_value, status)
            VALUES ($1, $2, $3::timestamptz, $3::timestamptz + INTERVAL '24 hours',
                    1, $4, $5, 'PENDING')
            ON CONFLICT (order_id) DO UPDATE SET work_units = EXCLUDED.work_units
            """,
            order_id,
            FACILITY,
            created_at,
            work_units,
            Decimal(0),
        )
    finally:
        await conn.close()


async def _seed_transition(order_id: str, *, occurred_at: datetime, work_units: float) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO order_status_transitions
                (order_id, facility_id, from_status, to_status, occurred_at,
                 event_id, work_units, origin)
            VALUES ($1, $2, 'PENDING', 'PICKING', $3, $4, $5, 'event')
            """,
            order_id,
            FACILITY,
            occurred_at,
            f"txn-verify-{order_id}",
            work_units,
        )
    finally:
        await conn.close()


async def _seed_action_and_commitment(
    action_id: str,
    *,
    target: float,
    lead_time_minutes: int,
    committed_at: datetime,
) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            """
            INSERT INTO recovery_actions
                (action_id, plan_id, facility_id, capacity_per_hour,
                 dispatch_promise_hours, demand_multiplier, status)
            VALUES ($1, 'buy-the-hour', $2, $3, 24, 1, 'SUCCESS')
            ON CONFLICT (action_id) DO NOTHING
            """,
            action_id,
            FACILITY,
            target,
        )
        await conn.execute(
            """
            INSERT INTO capacity_commitments
                (facility_id, action_id, lever_id, target_work_units_per_hour,
                 lead_time_minutes, committed_at)
            VALUES ($1, $2, 'EXTEND_SHIFT', $3, $4, $5)
            """,
            FACILITY,
            action_id,
            target,
            lead_time_minutes,
            committed_at,
        )
    finally:
        await conn.close()


async def _verification_status(action_id: str) -> str | None:
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetchval(
            "SELECT verification_status FROM capacity_commitments WHERE action_id = $1",
            action_id,
        )
    finally:
        await conn.close()


@pytest.mark.usefixtures("require_database")
def test_a_commitment_before_its_lead_time_stays_unverified():
    """Judging a claim before its lead time has elapsed is no more honest than trusting it."""
    _run(_clean_up("VRF-EARLY"))
    now = datetime.now(UTC)
    _run(
        _seed_action_and_commitment(
            "action-early", target=72.0, lead_time_minutes=90, committed_at=now
        )
    )

    async def _scenario():
        async with engine.begin() as conn:
            resolved = await verify_capacity_commitments(conn, FACILITY, now + timedelta(minutes=10))
            return resolved

    resolved = _run(_scenario())

    assert resolved == 0
    assert _run(_verification_status("action-early")) == "UNVERIFIED"


@pytest.mark.usefixtures("require_database")
def test_a_commitment_that_met_its_target_is_achieved():
    """A floor that measurably cleared the target must be reported as having done so."""
    _run(_clean_up("VRF-MET-0", "VRF-MET-1", "VRF-MET-2"))
    committed_at = datetime.now(UTC) - timedelta(hours=2)
    now = datetime.now(UTC)
    # MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT (repository.py) requires 3
    # transitions in the window before a derived signal exists at all.
    for i in range(3):
        order_id = f"VRF-MET-{i}"
        _run(_seed_order(order_id, work_units=100.0, created_at=now - timedelta(minutes=30)))
        _run(_seed_transition(order_id, occurred_at=now - timedelta(minutes=10), work_units=100.0))
    _run(
        _seed_action_and_commitment(
            "action-met", target=10.0, lead_time_minutes=0, committed_at=committed_at
        )
    )

    async def _scenario():
        async with engine.begin() as conn:
            return await verify_capacity_commitments(conn, FACILITY, now)

    resolved = _run(_scenario())

    assert resolved == 1
    assert _run(_verification_status("action-met")) == "ACHIEVED"


@pytest.mark.usefixtures("require_database")
def test_a_commitment_far_below_target_is_partial():
    """A measurable but insufficient floor must be reported as partial, not silently accepted."""
    _run(_clean_up("VRF-LOW"))
    committed_at = datetime.now(UTC) - timedelta(hours=2)
    now = datetime.now(UTC)
    # A thin transition history, but MIN_TRANSITIONS_FOR_DERIVED_THROUGHPUT (3)
    # worth of it, and plenty of fresh demand to test the claim against.
    for i in range(3):
        order_id = f"VRF-LOW-{i}"
        _run(_seed_order(order_id, work_units=1.0, created_at=now - timedelta(minutes=30)))
        _run(_seed_transition(order_id, occurred_at=now - timedelta(minutes=10), work_units=1.0))
    _run(_seed_order("VRF-LOW-DEMAND", work_units=500.0, created_at=now - timedelta(minutes=5)))
    _run(
        _seed_action_and_commitment(
            "action-low", target=100.0, lead_time_minutes=0, committed_at=committed_at
        )
    )

    async def _scenario():
        async with engine.begin() as conn:
            return await verify_capacity_commitments(conn, FACILITY, now)

    resolved = _run(_scenario())

    assert resolved == 1
    assert _run(_verification_status("action-low")) == "PARTIAL"


@pytest.mark.usefixtures("require_database")
def test_a_silent_floor_past_the_wait_ceiling_is_not_observed():
    """A floor with no measurement at all must eventually be reported, not left quiet forever."""
    _run(_clean_up())
    committed_at = datetime.now(UTC) - timedelta(hours=3)
    now = datetime.now(UTC)
    _run(
        _seed_action_and_commitment(
            "action-silent", target=72.0, lead_time_minutes=0, committed_at=committed_at
        )
    )

    async def _scenario():
        async with engine.begin() as conn:
            return await verify_capacity_commitments(conn, FACILITY, now)

    resolved = _run(_scenario())

    assert resolved == 1
    assert _run(_verification_status("action-silent")) == "NOT_OBSERVED"


@pytest.mark.usefixtures("require_database")
def test_a_commitment_is_verified_against_available_demand_not_raw_target():
    """A floor must not be blamed for not exceeding demand that never arrived.

    Demand this thin (well under the 72 wu/hr target) would fail a raw
    target/derived comparison even though the floor met every bit of work
    that actually showed up -- the false negative this module exists to
    avoid.
    """
    _run(_clean_up("VRF-DEMAND-0", "VRF-DEMAND-1", "VRF-DEMAND-2"))
    committed_at = datetime.now(UTC) - timedelta(hours=2)
    now = datetime.now(UTC)
    # 3 transitions (the derived-signal floor) of 2.0 work units each: 6.0
    # total in the 1-hour window on both sides, so demand and derived agree
    # exactly -- the floor did all of the thin demand that arrived.
    for i in range(3):
        order_id = f"VRF-DEMAND-{i}"
        _run(_seed_order(order_id, work_units=2.0, created_at=now - timedelta(minutes=30)))
        _run(_seed_transition(order_id, occurred_at=now - timedelta(minutes=10), work_units=2.0))
    _run(
        _seed_action_and_commitment(
            "action-demand-capped", target=72.0, lead_time_minutes=0, committed_at=committed_at
        )
    )

    async def _scenario():
        async with engine.begin() as conn:
            return await verify_capacity_commitments(conn, FACILITY, now)

    resolved = _run(_scenario())

    assert resolved == 1
    assert _run(_verification_status("action-demand-capped")) == "ACHIEVED"
