from enum import StrEnum

from simulator.config import (
    NORMAL_DEMAND_WU_PER_HOUR,
    SURGE_1_DEMAND_WU_PER_HOUR,
    SURGE_2_DEMAND_WU_PER_HOUR,
    SURGE_3_DEMAND_WU_PER_HOUR,
)


class ScenarioStage(StrEnum):
    NORMAL = "NORMAL"
    SURGE_1 = "SURGE_1"
    SURGE_2 = "SURGE_2"
    SURGE_3 = "SURGE_3"


DEMAND_TARGETS = {
    ScenarioStage.NORMAL: NORMAL_DEMAND_WU_PER_HOUR,
    ScenarioStage.SURGE_1: SURGE_1_DEMAND_WU_PER_HOUR,
    ScenarioStage.SURGE_2: SURGE_2_DEMAND_WU_PER_HOUR,
    ScenarioStage.SURGE_3: SURGE_3_DEMAND_WU_PER_HOUR,
}


class Scenario:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.stage = ScenarioStage.NORMAL

    def start(self) -> None:
        self.stage = ScenarioStage.NORMAL

    def next_stage(self) -> ScenarioStage:
        stages = list(ScenarioStage)
        current_index = stages.index(self.stage)

        if current_index < len(stages) - 1:
            self.stage = stages[current_index + 1]

        return self.stage

    def demand_target(self) -> int:
        return DEMAND_TARGETS[self.stage]

    def demand_at(self, sim_hours_elapsed: float) -> float:
        """Work units per hour the shop is generating at this point in the wave.

        The old stage machine only ratchets up; this traces a wave instead --
        quiet, rise, peak, decay -- so the live loop can walk it by simulated
        hour without the demo needing a way back down. It never touches
        `demand_target()` or `self.stage`: those still drive the ratcheted
        surge path exactly as before.

        The shape is set by what it has to prove, not by what looks tidy. A
        24-hour promise only comes under threat once the queue takes longer
        than a day to clear, and at a 52 wu/hour floor that means roughly 1250
        work units of backlog. A short peak cannot build it: an earlier version
        of this curve topped out at 16.6 hours of clearance, so only the 12-hour
        cohort could ever breach and the surge looked survivable when it was
        not. Hence the long hold.

        The slump afterwards is not decoration either. At normal demand the
        floor recovers at only 20 work units an hour against a peak deficit of
        75, so a surge takes about four times its own length to drain. Demand
        pulled forward by a promotion leaves a real hole behind it, and that
        hole is what lets the backlog actually clear.

        Landmarks (hours from the start of the run):
            0-2    quiet at NORMAL
            2-4    sharp rise to SURGE_3 (peak)
            4-20   hold at peak -- the floor falls behind by ~75 wu/hour
            20-23  decay to the post-surge slump
            23-34  slump at half NORMAL, where the backlog drains
            34-38  recovery back to NORMAL
            38+    quiet at NORMAL

        Args:
            sim_hours_elapsed: Simulated hours since the run started. Negative
                values are treated as pre-run and clamped to NORMAL.

        Returns:
            Work units per hour, never exceeding SURGE_3.
        """
        normal = float(NORMAL_DEMAND_WU_PER_HOUR)
        peak = float(SURGE_3_DEMAND_WU_PER_HOUR)
        slump = normal / 2

        if sim_hours_elapsed <= 2:
            return normal
        if sim_hours_elapsed <= 4:
            progress = (sim_hours_elapsed - 2) / (4 - 2)
            return normal + progress * (peak - normal)
        if sim_hours_elapsed <= 20:
            return peak
        if sim_hours_elapsed <= 23:
            progress = (sim_hours_elapsed - 20) / (23 - 20)
            return peak - progress * (peak - slump)
        if sim_hours_elapsed <= 34:
            return slump
        if sim_hours_elapsed <= 38:
            progress = (sim_hours_elapsed - 34) / (38 - 34)
            return slump + progress * (normal - slump)
        return normal