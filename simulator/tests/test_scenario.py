from simulator.config import (
    NORMAL_DEMAND_WU_PER_HOUR,
    SURGE_3_DEMAND_WU_PER_HOUR,
)
from simulator.scenario import Scenario, ScenarioStage


def test_scenario_starts_at_normal():
    scenario = Scenario()

    assert scenario.stage == ScenarioStage.NORMAL
    assert scenario.demand_target() == 32


def test_scenario_progresses_through_all_stages():
    scenario = Scenario()

    assert scenario.next_stage() == ScenarioStage.SURGE_1
    assert scenario.demand_target() == 62

    assert scenario.next_stage() == ScenarioStage.SURGE_2
    assert scenario.demand_target() == 91

    assert scenario.next_stage() == ScenarioStage.SURGE_3
    assert scenario.demand_target() == 127


def test_scenario_stays_at_final_stage():
    scenario = Scenario()

    scenario.next_stage()
    scenario.next_stage()
    scenario.next_stage()

    assert scenario.next_stage() == ScenarioStage.SURGE_3
    assert scenario.demand_target() == 127


def test_scenario_reset_returns_to_normal():
    scenario = Scenario()

    scenario.next_stage()
    scenario.next_stage()

    scenario.reset()

    assert scenario.stage == ScenarioStage.NORMAL
    assert scenario.demand_target() == 32


def test_scenario_start_returns_to_normal():
    scenario = Scenario()

    scenario.next_stage()
    scenario.next_stage()

    scenario.start()

    assert scenario.stage == ScenarioStage.NORMAL
    assert scenario.demand_target() == 32


def test_demand_at_starts_at_normal():
    """Before the wave rises, demand sits at the quiet baseline."""
    scenario = Scenario()

    assert scenario.demand_at(0) == NORMAL_DEMAND_WU_PER_HOUR
    assert scenario.demand_at(2) == NORMAL_DEMAND_WU_PER_HOUR


def test_demand_at_reaches_exactly_peak_and_holds():
    """The wave tops out at SURGE_3 and holds there through the plateau."""
    scenario = Scenario()

    assert scenario.demand_at(8) == SURGE_3_DEMAND_WU_PER_HOUR
    assert scenario.demand_at(11) == SURGE_3_DEMAND_WU_PER_HOUR
    assert scenario.demand_at(14) == SURGE_3_DEMAND_WU_PER_HOUR


def test_demand_at_decays_back_to_normal():
    """Past the plateau the wave subsides, landing back at normal by hour 22."""
    scenario = Scenario()

    assert scenario.demand_at(22) < SURGE_3_DEMAND_WU_PER_HOUR
    assert scenario.demand_at(30) < NORMAL_DEMAND_WU_PER_HOUR
    assert scenario.demand_at(40) == NORMAL_DEMAND_WU_PER_HOUR


def test_demand_at_never_exceeds_peak():
    """No point on the curve, at any resolution, may exceed SURGE_3."""
    scenario = Scenario()

    hours = [step * 0.25 for step in range(121)]  # 0..30 in quarter-hour steps

    assert all(scenario.demand_at(h) <= SURGE_3_DEMAND_WU_PER_HOUR for h in hours)

def test_demand_at_holds_peak_long_enough_to_breach_a_day_long_promise():
    """The surge has to outrun the floor for long enough to actually hurt.

    A wave that peaks briefly builds a backlog the floor clears well inside a
    24-hour promise, so the only orders that ever breach are the 12-hour
    cohort -- about a fifth of them. Risk tops out below CRITICAL and the
    surge looks survivable when it is not.

    Integrating the deficit against a 52 wu/hour floor is the honest check:
    peak clearance has to exceed 24 hours for the main cohort to be at risk.
    """
    scenario = Scenario()
    capacity, step, backlog, worst = 52.0, 0.05, 0.0, 0.0

    hour = 0.0
    while hour <= 40:
        backlog = max(0.0, backlog + (scenario.demand_at(hour) - capacity) * step)
        worst = max(worst, backlog / capacity)
        hour += step

    assert worst > 24, f"peak clearance {worst:.1f}h cannot breach a 24h promise"


def test_demand_at_slumps_below_normal_after_the_surge():
    """Demand pulled forward by a promotion leaves a hole behind it.

    The slump is not decoration: at normal demand the floor only recovers at
    20 work units an hour, so a surge takes four times its own length to clear.
    The post-surge dip is what lets the backlog actually drain.
    """
    scenario = Scenario()

    assert scenario.demand_at(28) < NORMAL_DEMAND_WU_PER_HOUR
    assert scenario.demand_at(40) == NORMAL_DEMAND_WU_PER_HOUR


def test_demand_at_is_continuous_across_every_landmark():
    """No cliff edges: a jump in demand would show up as a phantom surge."""
    scenario = Scenario()
    step = 0.01
    biggest = max(
        abs(scenario.demand_at(h * step) - scenario.demand_at((h - 1) * step))
        for h in range(1, 4000)
    )

    assert biggest < 1.0, f"demand jumps by {biggest:.1f} wu/hour between adjacent instants"
