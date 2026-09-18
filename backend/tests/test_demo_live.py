"""The live loop: a floor that keeps running rather than a state that freezes.

The surge builds a picture and stops. These cover the continuous mode, where
simulated time advances faster than the wall clock and the simulator is driven
in small slices of it.

Most of what can go wrong here is arithmetic, and it fails silently: a tick that
sends a whole hour of demand instead of the two minutes it represents floods the
queue, and a tick that hands the floor a whole hour of capacity means the
backlog can never build at all. Both leave a dashboard that looks plausible and
tells a lie, so the slice maths is unit-tested directly rather than only through
the endpoints.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers.demo import LIVE_TICK_SECONDS, plan_live_tick
from tests.helpers import sign_in

ANCHOR = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def dispose_shared_engine_between_tests():
    """Dispose the shared async engine's pool after each test.

    Newly required now that `sign_in` (ACCESS-01 precondition, added by
    phase 3) makes the two HTTP-backed tests below touch the database: each
    `with TestClient(app) as client:` block runs on its own fresh event
    loop, and without disposing the pool in between, the next test's loop
    tries to reuse a connection tied to this one's already-closed loop --
    the same failure mode this fixture prevents elsewhere in the suite (see
    test_events_integration.py's identically named fixture). Harmless
    no-op for this file's many DB-free `plan_live_tick` unit tests.
    """
    yield
    from app.db import engine

    asyncio.run(engine.dispose())


def test_a_tick_covers_the_slice_of_simulated_time_it_represents():
    """A two-second tick at 60x is two simulated minutes, not an hour.

    This is the requirement most easily got wrong. An hourly tick at 60x drops
    sixty orders in a lump once a minute and sits still in between: a
    slideshow, not a floor.
    """
    plan = plan_live_tick(speed=60.0, tick_seconds=2.0, sim_anchor=ANCHOR, wall_elapsed=0.0)

    assert plan.duration_hours == pytest.approx(2.0 / 60.0)
    assert plan.duration_hours < 0.05, "a tick must not send a whole hour of demand"


def test_simulated_time_advances_at_the_chosen_speed():
    """One real minute at 60x is one simulated hour."""
    plan = plan_live_tick(speed=60.0, tick_seconds=2.0, sim_anchor=ANCHOR, wall_elapsed=60.0)

    assert plan.sim_hours_elapsed == pytest.approx(1.0)
    assert plan.at == ANCHOR + timedelta(hours=1)


def test_speed_scales_both_the_clock_and_the_slice():
    """Tripling the speed triples the demand a tick carries.

    Otherwise the world would age three times faster while the shop kept
    sending the same orders, and demand per simulated hour would silently fall
    to a third of the scenario.
    """
    slow = plan_live_tick(speed=60.0, tick_seconds=2.0, sim_anchor=ANCHOR, wall_elapsed=10.0)
    fast = plan_live_tick(speed=180.0, tick_seconds=2.0, sim_anchor=ANCHOR, wall_elapsed=10.0)

    assert fast.duration_hours == pytest.approx(slow.duration_hours * 3)
    assert fast.sim_hours_elapsed == pytest.approx(slow.sim_hours_elapsed * 3)


def test_a_tick_at_real_speed_is_still_proportional():
    """Speed 1 is a legitimate setting, not a special case."""
    plan = plan_live_tick(speed=1.0, tick_seconds=2.0, sim_anchor=ANCHOR, wall_elapsed=0.0)

    assert plan.duration_hours == pytest.approx(2.0 / 3600.0)


def test_the_default_tick_interval_is_seconds_not_minutes():
    """Continuous means continuous: orders a few at a time, not once a minute."""
    assert 0 < LIVE_TICK_SECONDS <= 5


@pytest.mark.usefixtures("require_database")
def test_live_controls_are_absent_without_a_simulator(monkeypatch):
    """Controls that mutate run state must not exist where unconfigured.

    Requires a real database now: these routes are gated behind a session
    (ACCESS-01, phase 3), and `sign_in` needs one to sign up and log in.
    Previously these two tests needed no database at all -- see this
    module's history -- but that was only ever true because the endpoints
    themselves were open.
    """
    monkeypatch.setattr(settings, "simulator_url", None)

    with TestClient(app) as client:
        sign_in(client, "demo-live-tests@example.com")
        started = client.post("/api/demo/live/start", json={"speed": 60})
        stopped = client.post("/api/demo/live/stop")

        assert started.status_code == 404
        assert stopped.status_code == 404
        assert "disabled" in started.json()["detail"]


@pytest.mark.usefixtures("require_database")
def test_status_reports_that_no_run_is_live():
    """The dashboard needs to know whether the world is moving on its own."""
    with TestClient(app) as client:
        sign_in(client, "demo-live-tests@example.com")
        status = client.get("/api/demo/status").json()

        assert status["live"] is False
        assert status["speed"] is None


def test_consecutive_ticks_tile_simulated_time_without_gaps():
    """Demand delivered must match the wave even when ticks run late.

    A tick sleeps for a fixed interval and then does real HTTP work, so it
    fires later than its nominal period -- measured at 3.3s against a nominal
    2.0s. A loop that always sends its nominal slice therefore delivers only
    the ratio of the two: 61% of the specified demand, observed live as 76.5
    work units an hour where the wave calls for 127.

    The fix is to bill each tick for the time that actually elapsed, so the
    slices tile the timeline exactly however irregularly they fire.
    """
    speed = 300.0
    # Deliberately irregular, as a loop competing with HTTP latency is.
    wall_stamps = [0.0, 3.3, 6.1, 9.9, 13.0, 17.2]

    total_slice = sum(
        plan_live_tick(
            speed=speed,
            tick_seconds=wall_stamps[i] - wall_stamps[i - 1],
            sim_anchor=ANCHOR,
            wall_elapsed=wall_stamps[i],
        ).duration_hours
        for i in range(1, len(wall_stamps))
    )
    simulated_span = (wall_stamps[-1] - wall_stamps[0]) * speed / 3600.0

    assert total_slice == pytest.approx(simulated_span), (
        "ticks must account for every simulated hour they span"
    )


def test_a_late_tick_carries_the_time_it_actually_took():
    """A tick that took twice as long must carry twice the demand."""
    on_time = plan_live_tick(speed=60.0, tick_seconds=2.0, sim_anchor=ANCHOR, wall_elapsed=10.0)
    late = plan_live_tick(speed=60.0, tick_seconds=4.0, sim_anchor=ANCHOR, wall_elapsed=10.0)

    assert late.duration_hours == pytest.approx(on_time.duration_hours * 2)
