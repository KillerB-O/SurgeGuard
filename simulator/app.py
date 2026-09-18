from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from pydantic import BaseModel, Field

from simulator.config import FULFILLMENT_BASELINE_WU_PER_HOUR
from simulator.runner import SimulationRunner
from simulator.scenario import Scenario

app = FastAPI(title="SurgeGuard Simulator")


class CapacityRequest(BaseModel):
    capacity_per_hour: float = Field(gt=0)


class DispatchPromiseRequest(BaseModel):
    dispatch_promise_hours: float = Field(gt=0)


class ArrivalMultiplierRequest(BaseModel):
    """How much of normal demand the shop should still be sending.

    An inflow lever -- pausing a promotion, hiding express at checkout --
    reduces future arrivals. It cannot touch an order already placed, so this
    scales what the next stage generates and nothing else.
    """

    # An inflow lever only ever reduces, but the bound is looser than that:
    # a 422 here would fail an execution and mark a sound action FAILED.
    arrival_multiplier: float = Field(gt=0, le=10)


class TickRequest(BaseModel):
    """Which simulated hour a tick represents, counted back from now.

    Zero -- the default -- keeps the old behaviour of stamping the tick at the
    wall clock. A caller playing a multi-hour surge in seconds passes a
    decreasing offset so the ticks land an hour apart.

    `at` is a second, independent way to pin the tick: an absolute simulated
    instant rather than an offset from wherever `now()` happens to land when
    the request arrives. It wins over `hours_ago` when given. The backend is
    the one that knows the simulated instant (see `docs/07 section 13`); the
    simulator itself still never reads a clock -- it is only ever told.

    `sim_hours_elapsed` and `duration_hours` are the other half of that: the
    backend also knows where on the demand wave this tick sits and how much
    of an hour it covers, and has to tell the simulator both, over HTTP --
    the live loop runs in a separate process and cannot call
    `Scenario.demand_at()` directly. `sim_hours_elapsed` is None by default,
    which keeps the ratcheted stage ladder (`_current_demand()`) as the
    demand source, exactly as before `demand_at` existed. `duration_hours`
    defaults to a full hour so an omitted value reproduces today's behaviour;
    a live loop ticking every couple of real seconds at high speed passes a
    duration well under an hour so a tick generates only its fair share of
    demand and floor throughput, not a whole hour's worth in one lump.
    """

    hours_ago: float = Field(default=0, ge=0, le=72)
    at: datetime | None = None
    sim_hours_elapsed: float | None = None
    duration_hours: float = Field(default=1.0, gt=0)


class ScenarioStatus(BaseModel):
    stage: str
    demand_target_wu_per_hour: int
    capacity_per_hour: float
    dispatch_promise_hours: float
    run_id: str
    started: bool


class StageRunResponse(ScenarioStatus):
    orders_sent: int
    status_events_sent: int
    fulfillment_snapshots_sent: int


class StatusAdvanceResponse(ScenarioStatus):
    status_events_sent: int
    fulfillment_snapshots_sent: int


scenario = Scenario()
runner = SimulationRunner(run_id="run-001")

capacity_per_hour = float(FULFILLMENT_BASELINE_WU_PER_HOUR)
dispatch_promise_hours = 24.0
started = False
# Scaled down by an approved inflow lever; 1.0 is a shop sending normal demand.
arrival_multiplier = 1.0


@app.post("/scenario/reset", response_model=ScenarioStatus)
def reset_scenario() -> ScenarioStatus:
    global capacity_per_hour, dispatch_promise_hours, started, arrival_multiplier

    scenario.reset()
    runner.reset()

    capacity_per_hour = float(FULFILLMENT_BASELINE_WU_PER_HOUR)
    dispatch_promise_hours = 24.0
    started = False
    arrival_multiplier = 1.0

    return _status()


@app.post("/scenario/start", response_model=ScenarioStatus)
def start_scenario() -> ScenarioStatus:
    global started

    scenario.start()
    started = True

    return _status()


@app.post("/scenario/next-stage", response_model=ScenarioStatus)
def next_stage() -> ScenarioStatus:
    global started

    started = True
    scenario.next_stage()

    return _status()


@app.post("/scenario/set-capacity", response_model=ScenarioStatus)
def set_capacity(request: CapacityRequest) -> ScenarioStatus:
    """Apply observed capacity and publish a fresh telemetry snapshot."""
    global capacity_per_hour

    capacity_per_hour = request.capacity_per_hour
    runner.emit_snapshot(capacity_per_hour=capacity_per_hour)

    return _status()


@app.post("/scenario/set-arrival-multiplier", response_model=ScenarioStatus)
def set_arrival_multiplier(request: ArrivalMultiplierRequest) -> ScenarioStatus:
    """Scale the demand the next stage will generate.

    This is the executable half of an inflow lever: the backend can project what
    a quieter shop would look like, but only the thing generating orders can
    make the shop quieter.
    """
    global arrival_multiplier

    arrival_multiplier = request.arrival_multiplier
    return _status()


@app.post("/scenario/set-dispatch-promise", response_model=ScenarioStatus)
def set_dispatch_promise(request: DispatchPromiseRequest) -> ScenarioStatus:
    """Set the promise policy used for orders generated after this request."""
    global dispatch_promise_hours

    dispatch_promise_hours = request.dispatch_promise_hours
    return _status()


def _tick_anchor(request: TickRequest) -> datetime:
    """Timestamp for a tick that represents an hour some way in the past.

    A tick is a simulated hour, but a caller plays twelve of them in about
    half a minute. Stamped at the wall clock, twelve hours of demand land
    inside the backend's one-hour demand window and it reports a rate an order
    of magnitude above the scenario -- 2310 work units an hour for a scenario
    that describes 127. Spacing the ticks an hour apart puts each one in its
    own window, so the reported rate is the rate that was actually configured.

    The events are still real and still arrive now; only the hour they describe
    moves. Orders from earlier ticks are therefore genuinely older, which ages
    them in the queue and brings their promises nearer -- the surge builds a
    twelve-hour history rather than a twelve-hour spike in one instant.

    `request.at`, when given, wins outright: the caller is naming the exact
    simulated instant rather than an offset from now, which is what a live
    run driven by the backend's own clock needs. A caller that omits the
    offset is assumed to mean UTC, matching every other timestamp here.
    """
    if request.at is not None:
        return request.at if request.at.tzinfo is not None else request.at.replace(tzinfo=UTC)
    return datetime.now(UTC) - timedelta(hours=request.hours_ago)


def _tick_demand(request: TickRequest) -> float:
    """Work units to generate for this tick.

    Defaults to the ratcheted stage ladder (`_current_demand()`), exactly as
    before `demand_at` existed. When the caller names where on the wave this
    tick sits (`sim_hours_elapsed`), the wave is used instead, scaled by how
    much of an hour the tick actually covers -- a live loop ticking every
    couple of real seconds must not hand a two-minute slice a full hour's
    worth of demand. The inflow lever still applies either way.
    """
    if request.sim_hours_elapsed is None:
        return _current_demand()
    wave_demand = scenario.demand_at(request.sim_hours_elapsed)
    return wave_demand * request.duration_hours * arrival_multiplier


@app.post("/scenario/run-stage", response_model=StageRunResponse)
def run_stage(request: TickRequest | None = None) -> StageRunResponse:
    """Generate the current stage and send its events through n8n."""
    global started

    started = True
    tick = request or TickRequest()
    counts = runner.run_stage(
        demand_work_units=_tick_demand(tick),
        anchor=_tick_anchor(tick),
        capacity_per_hour=capacity_per_hour,
        dispatch_promise_hours=dispatch_promise_hours,
    )
    return StageRunResponse(**_status().model_dump(), **counts)


@app.post("/scenario/advance-statuses", response_model=StatusAdvanceResponse)
def advance_statuses(request: TickRequest | None = None) -> StatusAdvanceResponse:
    """Advance unfinished orders by one status and publish matching telemetry."""
    # One call represents duration_hours of floor time, matching run-stage's
    # tick of demand, so the two together reproduce the demand-versus-
    # throughput gap at whatever cadence the caller is actually ticking at.
    tick = request or TickRequest()
    occurred_at = _tick_anchor(tick)
    status_events_sent = runner.advance_statuses(
        work_units_budget=capacity_per_hour * tick.duration_hours,
        duration_hours=tick.duration_hours,
        occurred_at=occurred_at,
    )
    runner.emit_snapshot(capacity_per_hour=capacity_per_hour, occurred_at=occurred_at)
    return StatusAdvanceResponse(
        **_status().model_dump(),
        status_events_sent=status_events_sent,
        fulfillment_snapshots_sent=1,
    )


@app.get("/scenario/status", response_model=ScenarioStatus)
def get_status() -> ScenarioStatus:
    return _status()


def _current_demand() -> int:
    """Demand the shop is currently sending, after any inflow lever."""
    return int(scenario.demand_target() * arrival_multiplier)


def _status() -> ScenarioStatus:
    return ScenarioStatus(
        stage=scenario.stage.value,
        demand_target_wu_per_hour=_current_demand(),
        capacity_per_hour=capacity_per_hour,
        dispatch_promise_hours=dispatch_promise_hours,
        run_id=runner.commerce.run_id,
        started=started,
    )
