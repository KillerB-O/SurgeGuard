"""Operator controls for driving the demo from the Command Centre.

These exist so a reset and a surge are one click rather than a terminal, and
they are deliberately fenced off from the rest of the product:

- the whole router is disabled unless `SIMULATOR_URL` is configured, so it
  cannot exist in a deployment that has no simulator to drive;
- reset truncates operational tables, which is destructive and is exactly
  what's needed to return a facility to a known baseline before a demo;
- nothing here touches the scheduler, the contracts, or any recovery decision.

The simulator has no clock: orders arrive and statuses progress only when
something posts to it. That something is normally `scripts/demo_driver.py`;
this router is the same thing behind a button.
"""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import clock
from app.auth.dependencies import require_admin, require_session
from app.config import settings
from app.db import engine, get_connection

logger = logging.getLogger(__name__)

# 18:00 IST. Kept in step with migration 0023.
DEMO_DISPATCH_CUTOFF_UTC = time(12, 30)

router = APIRouter(prefix="/demo", tags=["demo"])

# Tables holding run state. facilities is restored rather than emptied, because
# the facility itself is configuration, not run data.
RUN_STATE_TABLES = (
    "recovery_actions",
    "orders",
    # No foreign key to orders, and the simulator reuses order ids after a
    # reset, so leftover transitions attach to the new run's orders and inflate
    # derived throughput several times over.
    "order_status_transitions",
    "pending_status_events",
    "processed_events",
    "fulfillment_snapshots",
)

# A surge tick is one simulated hour: one hour of arrivals against one hour of
# capacity, which is what makes demand-versus-throughput legible.
# Long enough for the promise horizon to bite. A surge shorter than about
# eighteen hours never breaches anything: express orders are a fifth of arrivals
# -- around 25 work units an hour against a floor of 52 -- so the queue always
# serves them in time, and the backlog piles up in the 24- and 48-hour bands
# where there is slack to spare. Breaches start when the run is long enough that
# the earliest orders have been waiting past their own promise, which is the
# real thing being modelled: a facility that has been behind since yesterday.
# Sixteen stops while most predicted misses are still ahead of their promise, so
# recovery plans have orders left to save; the live run then realises the rest.
DEFAULT_SURGE_TICKS = 16
# Ticks are fast now that events are posted over a pooled connection, so this is
# just enough spacing for the dashboard's poll to show the backlog building.
SECONDS_BETWEEN_TICKS = 0.5
# Where the clock sits relative to a surge tick's anchor. Every event in a tick
# is stamped exactly at the anchor and the rate windows are `>= now - 1h`, so a
# pin exactly on the anchor would also count the previous tick, an hour back.
SURGE_CLOCK_LEAD = timedelta(minutes=1)

# How often the live loop drives the simulator, in REAL seconds. Small on
# purpose: each tick carries only the slice of simulated time that just
# elapsed, so orders arrive a few at a time. Ticking once per simulated hour
# instead would drop sixty orders in a lump once a minute and sit still
# between -- a slideshow, not a floor.
LIVE_TICK_SECONDS = 2.0
DEFAULT_LIVE_SPEED = 60.0
# Where the live demand wave reaches its peak (simulator Scenario.demand_at).
SURGE_PEAK_WAVE_HOUR = 4.0


class SurgeRequest(BaseModel):
    """How much surge to drive."""

    ticks: int = Field(default=DEFAULT_SURGE_TICKS, ge=1, le=60)
    stage: str = Field(default="SURGE_3")


class LiveRequest(BaseModel):
    """How fast simulated time should run during a live run."""

    # 60 means one real second is one simulated minute, so a day plays in 24
    # minutes -- slow enough to narrate, fast enough that the risk picture
    # visibly moves while somebody watches.
    speed: float = Field(default=DEFAULT_LIVE_SPEED, gt=0, le=600)


class TickPlan(BaseModel):
    """What one live tick should tell the simulator to generate."""

    # The simulated instant this tick lands on; every event it emits is
    # stamped here so deadlines and the demand window share one timeline.
    at: datetime
    # Where the run sits on the demand wave.
    sim_hours_elapsed: float
    # How much of a simulated hour this tick covers. The whole feature lives
    # or dies on this being a small fraction rather than 1.0.
    duration_hours: float


class DemoStatus(BaseModel):
    """Whether the controls are usable, and what the demo is doing."""

    enabled: bool
    running: bool
    stage: str | None = None
    ticks_done: int = 0
    ticks_total: int = 0
    detail: str | None = None
    # A live run advances simulated time on its own; a surge does not.
    live: bool = False
    speed: float | None = None
    simulated_time: datetime | None = None
    sim_hours_elapsed: float | None = None


def plan_live_tick(
    *, speed: float, tick_seconds: float, sim_anchor: datetime, wall_elapsed: float
) -> TickPlan:
    """Work out what one tick of a live run represents.

    Kept pure and separate from the loop because this is where the feature
    quietly dies: a tick that sends a whole hour of demand floods the queue,
    and one that hands the floor a whole hour of capacity means the backlog can
    never build. Both produce a dashboard that looks plausible and is wrong, so
    the arithmetic is worth testing on its own.

    Args:
        speed: Simulated seconds per real second.
        tick_seconds: Real seconds this tick covers.
        sim_anchor: Simulated instant the run started at.
        wall_elapsed: Real seconds since the run started.

    Returns:
        The instant to stamp, the point on the demand wave, and the fraction of
        a simulated hour to generate.
    """
    sim_hours_elapsed = wall_elapsed * speed / 3600.0
    return TickPlan(
        at=sim_anchor + timedelta(hours=sim_hours_elapsed),
        sim_hours_elapsed=sim_hours_elapsed,
        duration_hours=tick_seconds * speed / 3600.0,
    )


# One surge at a time per process. In-memory on purpose: it is a progress hint
# for the button, not operational state -- the database remains the truth.
_state = DemoStatus(enabled=False, running=False)


def _require_enabled() -> str:
    """Return the simulator URL, or refuse if demo controls are switched off.

    Two independent switches, both required. `simulator_url` used to be the
    only one: a shared environment template or a misconfigured deployment
    that happened to set it was enough to expose destructive controls in a
    real deployment. `demo_mode` is a second, explicit flag that must also be
    turned on.
    """
    if not settings.simulator_url or not settings.demo_mode:
        raise HTTPException(
            status_code=404,
            detail=(
                "demo controls are disabled: set SIMULATOR_URL and DEMO_MODE "
                "to enable them. They drive the simulator and reset "
                "operational data, so they are off unless both are "
                "deliberately configured."
            ),
        )
    return settings.simulator_url.rstrip("/")


async def _require_demo_facility(conn: AsyncConnection, facility_id: str) -> None:
    """Refuse to reset a facility that is not explicitly flagged as a demo.

    The deletes below used to carry no facility scoping at all, so resetting
    "the demo" emptied every facility's orders, actions and snapshots. This
    is the second half of the fix: even with demo controls switched on, only
    a facility an administrator has flagged `is_demo` may be reset.
    """
    result = await conn.execute(
        text("SELECT is_demo FROM facilities WHERE facility_id = :facility_id"),
        {"facility_id": facility_id},
    )
    row = result.first()
    if row is None or not row[0]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{facility_id} is not flagged as a demo facility; refusing to "
                "reset it"
            ),
        )


@router.get(
    "/status",
    response_model=DemoStatus,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def get_demo_status() -> DemoStatus:
    """Report whether the controls are available and what they are doing."""
    return _state.model_copy(update={"enabled": bool(settings.simulator_url)})


@router.post(
    "/reset",
    response_model=DemoStatus,
    # A session is required to reach the endpoint at all (401 with none), but
    # NOT require_admin here: whether the feature is enabled must be checked
    # first, inside the body, so an unconfigured deployment 404s ("this does
    # not exist") for anyone rather than 403ing ("you may not do this") for a
    # non-admin -- see _require_enabled, _require_demo_facility, and
    # require_admin (called directly, not via Depends) below.
    dependencies=[Depends(require_session)],
)
async def reset_demo(
    facility_id: str,
    user_id: str = Depends(require_session),
    conn: AsyncConnection = Depends(get_connection),
) -> DemoStatus:
    """Return one demo facility to a known baseline.

    Both halves matter and neither is sufficient alone. Clearing the database
    without resetting the simulator leaves its order-id counter ahead; resetting
    the simulator without clearing the database makes it replay ids that already
    exist, which the backend correctly rejects as duplicate identities while n8n
    still reports success.

    Args:
        facility_id: The demo facility to reset. Must be flagged `is_demo`;
            any other facility is refused rather than touched.
        conn: Request-scoped database transaction.
    """
    simulator = _require_enabled()
    await require_admin(user_id=user_id, conn=conn)
    await _require_demo_facility(conn, facility_id)
    if _state.running and not _state.live:
        raise HTTPException(status_code=409, detail="a surge is still running")
    # A live run is stopped rather than refused: reset is the way out of any
    # state, and leaving the clock running over an emptied database would show
    # predictions against a world that no longer exists.
    await _halt_live()

    for table in RUN_STATE_TABLES:
        if table == "processed_events":
            # No facility_id column exists on this table (idempotency keys are
            # global), so it is scoped by source instead. Today those are only
            # ever the two demo sources; this becomes a real restriction once
            # a provider registry (P1) introduces real, non-demo sources.
            await conn.execute(
                text(
                    "DELETE FROM processed_events "
                    "WHERE source IN ('commerce_sim', 'fulfillment_sim')"
                )
            )
        else:
            await conn.execute(
                text(f"DELETE FROM {table} WHERE facility_id = :facility_id"),
                {"facility_id": facility_id},
            )
    await conn.execute(
        text(
            """
            UPDATE facilities
            SET capacity_per_hour = 52,
                dispatch_promise_hours = 24,
                dispatch_cutoff_utc = :cutoff
            WHERE facility_id = :facility_id
            """
        ),
        # The baseline pickup from migration 0023. Restoring it (not clearing
        # it) keeps "Book a late carrier pickup" available and undoes any
        # cutoff shift a previous run approved.
        {"facility_id": facility_id, "cutoff": DEMO_DISPATCH_CUTOFF_UTC},
    )
    await conn.commit()

    async with httpx.AsyncClient(timeout=settings.simulator_timeout_seconds) as client:
        status = (await client.post(f"{simulator}/scenario/reset")).json()
        # Resetting only restores the simulator's own number; the backend
        # schedules against the latest snapshot, so the baseline has to be
        # republished as telemetry or the last recovery keeps applying.
        await client.post(
            f"{simulator}/scenario/set-capacity",
            json={"capacity_per_hour": status["capacity_per_hour"]},
        )

    return DemoStatus(
        enabled=True,
        running=False,
        stage=status["stage"],
        detail="reset to baseline",
    )


@router.post(
    "/live/start",
    response_model=DemoStatus,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def start_live(
    request: LiveRequest,
    user_id: str = Depends(require_session),
    conn: AsyncConnection = Depends(get_connection),
) -> DemoStatus:
    """Start a continuous run: simulated time advances and orders keep arriving.

    The surge writes a fixed stretch of history and stops. This keeps going, so
    the queue ages, deadlines fall due, and the risk picture moves on its own
    rather than sitting where the last button left it.

    Returns immediately; the run advances in the background.
    """
    simulator = _require_enabled()
    # After the enabled check, like reset: unconfigured is a 404 for everyone.
    await require_admin(user_id=user_id, conn=conn)
    if _state.running or _state.live:
        raise HTTPException(status_code=409, detail="a run is already in progress")

    # Straight after a surge the floor is already at peak demand; starting the
    # wave from its quiet phase would read as the surge having stopped.
    wave_offset_hours = SURGE_PEAK_WAVE_HOUR if _state.detail == "surge complete" else 0.0

    async with engine.begin() as clock_conn:
        state = await clock.start(request.speed, clock_conn)

    asyncio.create_task(
        _run_live(simulator, request.speed, state.sim_anchor, wave_offset_hours)
    )
    return DemoStatus(
        enabled=True,
        running=True,
        live=True,
        speed=request.speed,
        simulated_time=state.sim_anchor,
        sim_hours_elapsed=0.0,
        detail="live run started",
    )


@router.post(
    "/live/stop",
    response_model=DemoStatus,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def stop_live(
    user_id: str = Depends(require_session),
    conn: AsyncConnection = Depends(get_connection),
) -> DemoStatus:
    """End a live run and return the world to wall-clock time."""
    _require_enabled()
    await require_admin(user_id=user_id, conn=conn)
    await _halt_live()
    return DemoStatus(enabled=True, running=False, live=False, detail="live run stopped")


async def _halt_live() -> None:
    """Stop the loop and clear the clock, whether or not a run is going.

    Safe to call unconditionally: reset uses it to guarantee that whatever the
    demo was doing, the world is back on the wall clock afterwards.
    """
    global _state

    _state = _state.model_copy(update={"live": False, "running": False})
    async with engine.begin() as conn:
        await clock.stop(conn)


async def _run_live(
    simulator: str, speed: float, sim_anchor: datetime, wave_offset_hours: float = 0.0
) -> None:
    """Drive the simulator in small slices of simulated time until stopped.

    Each pass covers only the sliver of simulated time that just elapsed, so
    orders arrive a few at a time and the floor gets a matching sliver of
    capacity. Sending a whole hour of either would break the demand-versus-
    throughput relationship the whole product is about.
    """
    global _state

    _state = DemoStatus(
        enabled=True, running=True, live=True, speed=speed, simulated_time=sim_anchor
    )
    started_at = asyncio.get_running_loop().time()
    # Each tick is billed for the time that actually passed, not for the
    # interval it was scheduled at. A tick sleeps and then does real HTTP work,
    # so it fires late -- measured at 3.3s against a nominal 2.0s -- and a loop
    # that always sent its nominal slice delivered only 61% of the demand the
    # wave specifies. Billing the real elapsed time makes the slices tile the
    # timeline exactly, however irregularly they land.
    last_tick_at = started_at
    ticks = 0

    try:
        async with httpx.AsyncClient(timeout=settings.simulator_timeout_seconds) as client:
            current = (await client.get(f"{simulator}/scenario/status")).json()
            if not current.get("started"):
                await client.post(f"{simulator}/scenario/start")

            while _state.live:
                fired_at = asyncio.get_running_loop().time()
                plan = plan_live_tick(
                    speed=speed,
                    tick_seconds=fired_at - last_tick_at,
                    sim_anchor=sim_anchor,
                    wall_elapsed=fired_at - started_at,
                )
                last_tick_at = fired_at
                body = {
                    "at": plan.at.isoformat(),
                    "sim_hours_elapsed": plan.sim_hours_elapsed + wave_offset_hours,
                    "duration_hours": plan.duration_hours,
                }
                await client.post(f"{simulator}/scenario/run-stage", json=body)
                await client.post(f"{simulator}/scenario/advance-statuses", json=body)

                ticks += 1
                if _state.live:
                    _state = _state.model_copy(
                        update={
                            "ticks_done": ticks,
                            "simulated_time": plan.at,
                            "sim_hours_elapsed": plan.sim_hours_elapsed,
                        }
                    )
                await asyncio.sleep(LIVE_TICK_SECONDS)
    except Exception as exc:
        logger.exception("live run failed")
        _state = _state.model_copy(
            update={"live": False, "running": False, "detail": f"live run failed: {exc}"}
        )
        # A dead loop must not leave the world running on a simulated clock
        # nobody is advancing, which would freeze every prediction.
        async with engine.begin() as conn:
            await clock.stop(conn)


@router.post(
    "/surge",
    response_model=DemoStatus,
    # ACCESS-01, phase 3: dashboard data requires a signed-in operator.
    dependencies=[Depends(require_session)],
)
async def start_surge(
    request: SurgeRequest,
    user_id: str = Depends(require_session),
    conn: AsyncConnection = Depends(get_connection),
) -> DemoStatus:
    """Drive a surge in the background and return immediately.

    A tick posts an hour of arrivals and an hour of floor capacity, so a surge
    takes real seconds per tick. Returning immediately lets the dashboard's
    existing poll show it building rather than blocking on it.
    """
    simulator = _require_enabled()
    await require_admin(user_id=user_id, conn=conn)
    if _state.running:
        raise HTTPException(status_code=409, detail="a surge is already running")

    asyncio.create_task(_run_surge(simulator, request))
    return DemoStatus(
        enabled=True,
        running=True,
        stage=request.stage,
        ticks_total=request.ticks,
        detail="surge started",
    )


async def _run_surge(simulator: str, request: SurgeRequest) -> None:
    """Tick the simulator, updating progress as it goes."""
    global _state
    _state = DemoStatus(
        enabled=True, running=True, stage=request.stage, ticks_total=request.ticks
    )

    try:
        async with httpx.AsyncClient(timeout=settings.simulator_timeout_seconds) as client:
            current = (await client.get(f"{simulator}/scenario/status")).json()
            if not current.get("started"):
                await client.post(f"{simulator}/scenario/start")

            # next-stage only moves forward; going back needs a reset, which
            # would replay order ids the database already holds.
            stages = ["NORMAL", "SURGE_1", "SURGE_2", "SURGE_3"]
            target = stages.index(request.stage) if request.stage in stages else 0
            for _ in range(max(target - stages.index(current["stage"]), 0)):
                await client.post(f"{simulator}/scenario/next-stage")

            surge_end = datetime.now(UTC)
            for tick in range(1, request.ticks + 1):
                # Tick 1 describes the hour furthest back, the last tick
                # describes now. Without this the whole surge lands inside one
                # demand window and the backend reports a rate an order of
                # magnitude above the scenario -- 2310 work units an hour for a
                # surge that describes 127. It also means earlier orders are
                # genuinely older, so the queue ages and promises fall due:
                # twelve ticks build a twelve-hour history, not an instant
                # spike that nothing is yet late for.
                anchor = surge_end - timedelta(hours=request.ticks - tick)
                body = {"at": anchor.isoformat()}
                await client.post(f"{simulator}/scenario/run-stage", json=body)
                await client.post(f"{simulator}/scenario/advance-statuses", json=body)
                # Backdated ticks fall outside the one-hour rate windows of the
                # wall clock, so the dashboard's demand and throughput sat still
                # until the last tick. Pinning "now" to the hour just written
                # lets them move as the surge builds. Pinning only once the
                # tick is fully ingested keeps a poll from reading half a tick.
                async with engine.begin() as clock_conn:
                    await clock.pin(anchor + SURGE_CLOCK_LEAD, clock_conn)
                _state = _state.model_copy(update={"ticks_done": tick})
                if tick < request.ticks:
                    await asyncio.sleep(SECONDS_BETWEEN_TICKS)

        _state = _state.model_copy(update={"running": False, "detail": "surge complete"})
    except Exception as exc:
        logger.exception("surge failed")
        _state = _state.model_copy(
            update={"running": False, "detail": f"surge failed: {exc}"}
        )
    finally:
        async with engine.begin() as conn:
            await clock.stop(conn)
