"""Single source of "now": wall-clock time, or simulated time during a live demo run.

Every request-path `now()` used to be `datetime.now(UTC)` directly, so a
compressed demo run (an hour of simulator arrivals posted in seconds) could
never make the backend's own notion of time -- order age, slack, minutes to
breach -- move faster than real time. `demo_clock` is a one-row table that
pins two instants together: `wall_anchor`, the real time a run was (re)started,
and `sim_anchor`, the simulated instant that moment stands for. `now()`
advances from `sim_anchor` by the wall-clock time elapsed since `wall_anchor`,
scaled by `rate`:

    simulated_now = sim_anchor + (wall_now - wall_anchor) * rate

An empty table means no live run, so `now()` returns `datetime.now(UTC)`
unchanged -- Global Constraint 1, and the reason wiring this in is safe.

Caching: `now()` sits on every request path, so it must not cost a query per
call. The clock row is cached in module state (`_cache`, `_loaded`) and kept
current by `start()`/`stop()`. The cache also self-populates on first use --
`_loaded` starts False at import and flips true on the first `now()`/`state()`
call -- so a process that restarts mid-run picks the clock back up from the
database instead of silently snapping back to wall time, which is the whole
reason the clock is persisted rather than kept in memory only. The cached
value is a small immutable dataclass, not a connection, so it is safe to share
across requests, event loops, and (in tests) across a disposed and recreated
engine.

`conn` is required, not optional, on every function here. Every call site in
this codebase already holds a request-scoped connection, and a default of
`None` that silently falls back to wall time would hide a call site that was
wired up wrong instead of failing it loudly.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class ClockState:
    """The anchor pair and rate persisted while a demo clock is running."""

    sim_anchor: datetime
    wall_anchor: datetime
    rate: float


# Cached clock row. `_cache` is the running clock, or None once "checked, and
# no run is live" -- that is distinct from `_loaded`, which tracks whether that
# check has happened yet in this process. Without the distinction, a fresh
# process could not tell "no run" from "haven't looked", and would either query
# on every call (defeating the cache) or misreport wall time after a restart.
_cache: ClockState | None = None
_loaded: bool = False


async def _ensure_loaded(conn: AsyncConnection) -> None:
    """Populate the cache from the database, once per process.

    Every later call returns immediately without a query; `start()` and
    `stop()` keep the cache current after that point themselves.
    """
    global _cache, _loaded
    if _loaded:
        return
    _cache = await _read(conn)
    _loaded = True


async def _read(conn: AsyncConnection) -> ClockState | None:
    """Read the single clock row directly, bypassing the cache."""
    result = await conn.execute(text("SELECT sim_anchor, wall_anchor, rate FROM demo_clock"))
    row = result.mappings().first()
    if row is None:
        return None
    return ClockState(sim_anchor=row["sim_anchor"], wall_anchor=row["wall_anchor"], rate=row["rate"])


async def start(rate: float, conn: AsyncConnection) -> ClockState:
    """Start (or restart) the demo clock, anchored to the current instant.

    Simulated time begins equal to wall time and then runs at `rate`. Restarting
    mid-run re-anchors from now rather than stacking a second anchor, which is
    why this is an upsert against the single-row table rather than an insert.

    Args:
        rate: Simulated seconds elapsed per wall-clock second.
        conn: Connection the write happens through.

    Returns:
        The clock state just persisted and cached.
    """
    global _cache, _loaded
    wall_anchor = datetime.now(UTC)
    state = ClockState(sim_anchor=wall_anchor, wall_anchor=wall_anchor, rate=rate)
    await conn.execute(
        text(
            """
            INSERT INTO demo_clock (id, sim_anchor, wall_anchor, rate)
            VALUES (TRUE, :sim_anchor, :wall_anchor, :rate)
            ON CONFLICT (id) DO UPDATE
            SET sim_anchor = EXCLUDED.sim_anchor,
                wall_anchor = EXCLUDED.wall_anchor,
                rate = EXCLUDED.rate
            """
        ),
        {"sim_anchor": wall_anchor, "wall_anchor": wall_anchor, "rate": rate},
    )
    _cache = state
    _loaded = True
    return state


async def stop(conn: AsyncConnection) -> None:
    """Delete the clock row; the world returns to wall-clock time immediately.

    Args:
        conn: Connection the delete happens through.
    """
    global _cache, _loaded
    await conn.execute(text("DELETE FROM demo_clock"))
    _cache = None
    _loaded = True


def invalidate() -> None:
    """Drop the cached clock row so the next read comes from the database.

    The cache is kept current by `start()` and `stop()`, which only ever run
    in the API process. A SEPARATE process -- the alert worker -- would
    otherwise load the anchors once on its first tick and never notice a demo
    restart or a speed change, so its `now()` would drift away from the one
    the dashboard renders. Cooldowns would then be measured against a
    different clock than the schedule they guard, which mid-demo looks like
    alerts firing at random.

    Safe to call every tick: it costs one query per tick, not per `now()`
    call, so the caching rationale in this module's docstring still holds for
    the request path.
    """
    global _loaded
    _loaded = False


async def state(conn: AsyncConnection) -> ClockState | None:
    """Return the running clock, or None when wall-clock time applies.

    Args:
        conn: Connection used only if the cache has not been loaded yet.

    Returns:
        The cached clock state, or None.
    """
    await _ensure_loaded(conn)
    return _cache


def project(clock_state: ClockState | None, wall: datetime) -> datetime:
    """Compute the simulated instant that corresponds to a wall-clock instant.

    Pure arithmetic and no database access, so the compression math is testable
    without a connection.

    Args:
        clock_state: The cached clock, or None when no live run is active.
        wall: The real-world instant to project from.

    Returns:
        `wall` unchanged when no clock is running; otherwise `sim_anchor`
        advanced by the wall-elapsed time since `wall_anchor`, scaled by rate.
    """
    if clock_state is None:
        return wall
    elapsed_seconds = (wall - clock_state.wall_anchor).total_seconds()
    return clock_state.sim_anchor + timedelta(seconds=elapsed_seconds * clock_state.rate)


async def now(conn: AsyncConnection) -> datetime:
    """Return the instant request handlers and the scheduler should treat as "now".

    Costs a database query only the first time this (or `state()`) is called in
    a process; every later call is pure computation against the cached row.

    Args:
        conn: Connection used only if the cache has not been loaded yet.

    Returns:
        Wall-clock time when no clock is running; otherwise the projected
        simulated instant.
    """
    await _ensure_loaded(conn)
    return project(_cache, datetime.now(UTC))
