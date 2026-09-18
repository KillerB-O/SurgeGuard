"""Tests for the demo clock: compressed simulated time layered over the wall clock.

Before this, every `now()` on a request path was `datetime.now(UTC)` directly, so
a compressed demo run had no way to make risk numbers drift -- the wall clock
ticks at one second per second no matter how fast the simulator is told to run.
These tests pin the pure projection arithmetic, the persistence round trip
through `start`/`stop`/`state`, the module-level cache that keeps `now()` off
the database on every call, and the fact that with no live run the API still
behaves exactly as it did before the clock existed (Global Constraint 1).

Connection-based tests use a dedicated `NullPool` engine rather than the app's
shared `app.db.engine`: a `NullPool` connection is opened and closed fresh on
every checkout, so it is never reused across the separate event loops that
`asyncio.run()` creates per test on this platform. The shared, pooled engine
(created with `pool_pre_ping=True`) does get reused across loops by design
(see conftest.py and `dispose_shared_engine_between_tests` elsewhere in this
suite), which is fine when it is only ever driven from inside one
`with TestClient(app)` block per test, but not when a test opens it directly
across more than one `asyncio.run` call -- pre-ping then pings a pooled
connection whose underlying socket belongs to an already-closed loop and
crashes. The one test here that goes through the real HTTP path uses
`TestClient`, exactly like the rest of the suite, and so uses the shared
engine like everything else does.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app import clock
from app.config import settings
from app.db import engine as shared_engine
from app.main import app
from tests.helpers import sign_in

pytestmark = pytest.mark.usefixtures("require_database")

# A dedicated engine for tests that open a connection directly, so the shared
# app engine's pooled connections are never touched outside a TestClient block.
_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)


@pytest.fixture(autouse=True)
def clean_clock_state():
    """Guarantee no clock survives between tests, in the table or the cache.

    Uses a throwaway asyncpg connection, not either SQLAlchemy engine, matching
    the rest of this suite's cleanup fixtures (see conftest.py). The cache is
    module-level and outlives any one test's connection, so a row-only cleanup
    would leave a later test reading a stale cached instant even after the
    table itself is empty.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _clean_table() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DELETE FROM demo_clock")
        finally:
            await conn.close()

    def _clean_cache() -> None:
        clock._cache = None
        clock._loaded = False

    asyncio.run(_clean_table())
    _clean_cache()
    yield
    asyncio.run(_clean_table())
    _clean_cache()


@pytest.fixture(autouse=True)
def dispose_engines_between_tests():
    yield
    asyncio.run(_test_engine.dispose())
    asyncio.run(shared_engine.dispose())


def test_project_with_no_state_returns_the_wall_instant_unchanged():
    """With no live run, projection is the identity function."""
    wall = datetime(2026, 1, 1, tzinfo=UTC)

    assert clock.project(None, wall) == wall


def test_a_rate_of_sixty_advances_simulated_time_sixty_times_wall_elapsed():
    """The whole point of the clock: compressed time must actually compress."""
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    state = clock.ClockState(sim_anchor=anchor, wall_anchor=anchor, rate=60.0)
    wall = anchor + timedelta(seconds=10)

    projected = clock.project(state, wall)

    assert projected == anchor + timedelta(seconds=600)


def test_now_returns_wall_clock_time_when_no_run_is_live():
    """An empty table means `now()` behaves exactly as `datetime.now(UTC)` did."""

    async def _run() -> datetime:
        async with _test_engine.begin() as conn:
            return await clock.now(conn)

    before = datetime.now(UTC)
    result = asyncio.run(_run())
    after = datetime.now(UTC)

    assert before <= result <= after


def test_now_advances_sixty_times_faster_once_a_run_is_started():
    """`start` with rate 60 makes `now()` run 60x wall time from that instant."""

    async def _run() -> tuple[clock.ClockState, datetime]:
        async with _test_engine.begin() as conn:
            started = await clock.start(rate=60.0, conn=conn)
            await asyncio.sleep(0.2)
            result = await clock.now(conn)
            return started, result

    started, result = asyncio.run(_run())

    elapsed_wall = (datetime.now(UTC) - started.wall_anchor).total_seconds()
    expected_minimum = started.sim_anchor + timedelta(seconds=0.2 * 60.0)

    assert result >= expected_minimum
    # Sanity bound: even with scheduler jitter this must not run away to
    # multiples of the expected elapsed simulated time.
    assert result <= started.sim_anchor + timedelta(seconds=elapsed_wall * 60.0 + 5)


def test_stop_restores_wall_clock_time():
    """`stop` deletes the run, and `now()` falls back to real time immediately."""

    async def _run() -> datetime:
        async with _test_engine.begin() as conn:
            await clock.start(rate=60.0, conn=conn)
            await clock.stop(conn)
            return await clock.now(conn)

    before = datetime.now(UTC)
    result = asyncio.run(_run())
    after = datetime.now(UTC)

    assert before <= result <= after


def test_state_round_trips_through_a_real_connection():
    """`state` reports the running clock, or None once it is stopped."""

    async def _run() -> None:
        async with _test_engine.begin() as conn:
            assert await clock.state(conn) is None

            anchor_before = datetime.now(UTC)
            started = await clock.start(rate=10.0, conn=conn)
            assert started.rate == 10.0
            assert started.sim_anchor >= anchor_before

            current = await clock.state(conn)
            assert current == started

            await clock.stop(conn)
            assert await clock.state(conn) is None

    asyncio.run(_run())


def test_now_does_not_query_the_database_once_the_cache_is_loaded():
    """The whole point of caching: a live request path must not pay a query per call.

    The cache is populated once by `start()`, then the row is deleted directly
    (bypassing `stop()`, so the cache is never told). A query-per-call `now()`
    would see the empty table and fall back to wall time; a correctly cached
    `now()` keeps projecting off the row it already has. At rate 60 the two
    are easy to tell apart: after a short sleep, the cached projection must be
    strictly ahead of wall-clock `now()`, which a wall-time fallback can never be.
    """

    async def _run() -> datetime:
        async with _test_engine.begin() as conn:
            await clock.start(rate=60.0, conn=conn)
            await conn.execute(text("DELETE FROM demo_clock"))
            await asyncio.sleep(0.1)
            return await clock.now(conn)

    result = asyncio.run(_run())

    assert result > datetime.now(UTC)


def test_now_lazily_loads_a_running_clock_left_by_a_prior_process():
    """A fresh process (empty cache) must pick up a clock another process started.

    This is the restart-survival guarantee the clock exists for: an anchor
    written directly to the table, with no `start()` call in this process, must
    still be picked up on the first `now()` -- otherwise a backend restart
    mid-demo silently snaps back to wall time.
    """
    anchor = datetime.now(UTC) - timedelta(seconds=5)

    async def _run() -> datetime:
        async with _test_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO demo_clock (id, sim_anchor, wall_anchor, rate)
                    VALUES (TRUE, :sim_anchor, :wall_anchor, :rate)
                    """
                ),
                {"sim_anchor": anchor, "wall_anchor": anchor, "rate": 60.0},
            )
            # No start() call in this process: the cache is cold.
            assert clock._loaded is False
            return await clock.now(conn)

    result = asyncio.run(_run())

    assert result >= anchor + timedelta(seconds=5 * 60.0)


def test_dashboard_behaves_exactly_as_before_with_no_live_run():
    """Wiring the clock into the read path must not change the no-run behaviour."""
    before = datetime.now(UTC)
    with TestClient(app) as client:
        # ACCESS-01, phase 3: /api/dashboard now requires a session.
        sign_in(client, "clock-tests@example.com")
        response = client.get("/api/dashboard?facility_id=WH-01")
    after = datetime.now(UTC)

    assert response.status_code == 200
    generated_at = datetime.fromisoformat(response.json()["generated_at"])

    # No clock is running, so generated_at must be real wall time bracketed by
    # the request -- not a fixed demo instant, and not drifting ahead of it.
    assert before <= generated_at <= after
