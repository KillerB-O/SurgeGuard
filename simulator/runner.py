from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from simulator.client import SurgeGuardClient
from simulator.commerce import CommerceSimulator
from simulator.config import (
    EVENT_CONCURRENCY,
    FULFILLMENT_BASELINE_WU_PER_HOUR,
    MAX_ORDER_WORK_UNITS,
)
from simulator.fulfillment import FulfillmentSimulator
from simulator.models import OrderStatus

# What each station hands the next one. Module level so a station's carried
# capacity can be keyed by it before any tick has run.
STATION_PROGRESSION = {
    OrderStatus.PENDING: OrderStatus.PICKING,
    OrderStatus.PICKING: OrderStatus.PACKED,
    OrderStatus.PACKED: OrderStatus.READY,
    OrderStatus.READY: OrderStatus.DISPATCHED,
}

# An order costs its work units to take all the way through, so one step costs a
# quarter of that, and the priciest single step is this.
MAX_STEP_WORK_UNITS = MAX_ORDER_WORK_UNITS / len(STATION_PROGRESSION)


class SimulationRunner:
    """Orchestrates simulator events through n8n."""

    def __init__(
        self,
        *,
        run_id: str = "run-001",
        client: SurgeGuardClient | None = None,
    ) -> None:
        self.client = client or SurgeGuardClient()
        self.commerce = CommerceSimulator(run_id=run_id)
        self.fulfillment = FulfillmentSimulator(run_id=run_id)
        self.order_statuses: dict[str, OrderStatus] = {}
        # Work units per order, so advancing one status can be charged against
        # the floor's throughput rather than being free.
        self.order_work_units: dict[str, float] = {}
        # Capacity allocated to each station and not yet spent, carried between
        # ticks. See `advance_statuses`.
        self._station_credit: dict[OrderStatus, float] = dict.fromkeys(
            STATION_PROGRESSION, 0.0
        )
        # (simulated hours covered, work units actually performed) per call,
        # trimmed to the last simulated hour. This is what telemetry reports.
        self._completed: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self.commerce.reset()
        self.fulfillment.reset()
        self.order_statuses.clear()
        self.order_work_units.clear()
        self._station_credit = dict.fromkeys(STATION_PROGRESSION, 0.0)
        self._completed.clear()

    def _record_completed(self, duration_hours: float, work_units: float) -> None:
        """Add one tick of measured work, keeping a simulated hour of history.

        The window is measured in the durations the caller reports rather than
        in event timestamps, because a caller may legitimately stamp several
        ticks at the same simulated instant.
        """
        self._completed.append((duration_hours, work_units))
        total = sum(entry[0] for entry in self._completed)
        while len(self._completed) > 1 and total - self._completed[0][0] >= 1.0:
            total -= self._completed.popleft()[0]

    def measured_work_units_per_hour(self) -> float | None:
        """Work units an hour the floor has actually achieved, or None.

        None means nothing has been measured yet, which is a different claim
        from "the floor did no work" and is left for the caller to resolve.
        """
        covered = sum(entry[0] for entry in self._completed)
        if covered <= 0:
            return None
        return sum(entry[1] for entry in self._completed) / min(covered, 1.0)

    def run_stage(
        self,
        *,
        demand_work_units: float,
        anchor: datetime | None = None,
        capacity_per_hour: float = FULFILLMENT_BASELINE_WU_PER_HOUR,
        dispatch_promise_hours: float = 24,
    ) -> dict[str, int]:
        """Generate and send one stage through the configured event gateway.

        Orders are emitted at the current wall clock so the backend's one-hour
        demand window reflects the selected stage. New orders remain pending;
        status progression is controlled separately.
        """

        if anchor is None:
            anchor = datetime.now(UTC)

        orders = self.commerce.generate_orders(
            target_work_units=demand_work_units,
            anchor=anchor,
            dispatch_promise_hours=dispatch_promise_hours,
        )

        status_events_sent = 0

        for order in orders:
            self.order_statuses[order.order_id] = OrderStatus.PENDING
            self.order_work_units[order.order_id] = order.work_units
        orders_sent = self._send_all(self.client.send_order, orders)

        self.emit_snapshot(
            capacity_per_hour=capacity_per_hour,
            occurred_at=anchor,
        )

        return {
            "orders_sent": orders_sent,
            "status_events_sent": status_events_sent,
            "fulfillment_snapshots_sent": 1,
        }

    def advance_statuses(
        self,
        *,
        work_units_budget: float | None = None,
        duration_hours: float = 1.0,
        occurred_at: datetime | None = None,
    ) -> int:
        """Advance unfinished orders as far as the floor's throughput allows.

        A warehouse doing 52 work units an hour cannot move five hundred orders
        in one step. Advancing every order at once emptied the pending queue in
        a single call, which is both unrealistic and destroys the SLA picture
        the queue produces.

        Each call represents one hour of floor time, matching `run_stage`, which
        injects one hour of demand. So a stage of 127 work units against a
        budget of 52 grows the backlog by 75 an hour -- the scenario the product
        is built around.

        The budget is split evenly across the four stations rather than spent
        most-advanced-first out of one pot. A single pot let an empty station
        hand its share to the one below it, so every stage drained completely on
        every call and the floor marched in lockstep -- picking, packing and
        staging sat empty three ticks in four, which is not what a warehouse
        looks like. Per-station budgets fill the pipeline instead: about a
        quarter of an hour's work moves at each station, so every stage holds
        work and dispatches come out at the floor's actual rate.

        End-to-end throughput is unchanged and still equals `capacity_per_hour`
        -- a quarter of the budget at a quarter of an order's cost per step is
        the same number of completed orders an hour. That equality matters: the
        backend's SLA model schedules against exactly this capacity.

        Args:
            work_units_budget: Work units the floor can process this call. None
                means unbounded, which is the old all-at-once behaviour.
            duration_hours: Simulated hours this call represents. Used to turn
                the work performed into the hourly rate telemetry reports.
            occurred_at: Timestamp applied to emitted status events.

        Returns:
            Number of canonical status events sent.
        """
        occurred_at = occurred_at or datetime.now(UTC)
        progression = STATION_PROGRESSION
        # One order costs its work units to take all the way through, so a
        # single step costs a quarter of that.
        steps = len(progression)

        # Most advanced first: finish what the floor already started.
        order_of_work = list(progression)
        unfinished = [
            (order_id, status)
            for order_id, status in self.order_statuses.items()
            if status in progression
        ]
        unfinished.sort(key=lambda item: order_of_work.index(item[1]), reverse=True)

        # A station cannot lend its share to another station, but it can carry
        # its own leftover forward. Spending only what a tick allocated meant a
        # budget smaller than one step still bought one, because the guard
        # tested spend before spending -- so at 1x the floor ran about fifty
        # times its rated capacity and no backlog could ever form. Carrying the
        # remainder instead makes throughput match capacity at any tick size.
        station_budget = (
            None if work_units_budget is None else work_units_budget / steps
        )
        if station_budget is not None:
            # Carried capacity is bounded. A station holding nothing in its
            # status never spends, and without a ceiling it would bank every
            # tick it sat out and discharge the lot when work arrived -- a floor
            # that clears a backlog instantly because it was idle yesterday. The
            # bound is two steps on top of the current allocation, and carries no
            # term that varies with tick size, so the same floor behaves the same
            # way at 1x and 60x.
            ceiling = station_budget + 2 * MAX_STEP_WORK_UNITS
            for station in progression:
                self._station_credit[station] = min(
                    self._station_credit[station] + station_budget, ceiling
                )

        events = []
        performed = 0.0
        for order_id, current_status in unfinished:
            step_cost = self.order_work_units.get(order_id, 1.0) / steps
            if station_budget is not None:
                if self._station_credit[current_status] <= 0:
                    # This station is out for now; later ones may not be.
                    continue
                self._station_credit[current_status] -= step_cost

            events.append(
                self.fulfillment.status_event(
                    order_id=order_id,
                    occurred_at=occurred_at,
                    status=progression[current_status],
                )
            )
            self.order_statuses[order_id] = progression[current_status]
            performed += step_cost

        # What the floor actually did, which is what telemetry now reports. A
        # tick that moved nothing records a zero rather than nothing at all: a
        # stalled floor is a measurement, not an absence of one.
        self._record_completed(duration_hours, performed)
        return self._send_all(self.client.send_order_status, events)

    def _send_all(self, send, events: list) -> int:
        """Post one tick's independent events, several in flight at a time.

        A tick's events do not depend on each other: the orders are distinct,
        and a status step touches one order once. Each carries its own event id
        and the backend claims that id before doing any work, so a retry or a
        reordering is already handled. What is *not* independent is the two
        phases -- every order exists before any status event refers to it --
        and that ordering is preserved because run_stage returns before
        advance_statuses is called.

        Sequential posting made a day-long surge take minutes of handshakes
        rather than of scenario. Returns the number of events sent.
        """
        if not events:
            return 0
        if EVENT_CONCURRENCY <= 1:
            for event in events:
                send(event)
            return len(events)

        with ThreadPoolExecutor(max_workers=EVENT_CONCURRENCY) as pool:
            # list() rather than a bare map so a failed post raises here
            # instead of being silently dropped.
            list(pool.map(send, events))
        return len(events)

    def emit_snapshot(
        self,
        *,
        capacity_per_hour: float,
        occurred_at: datetime | None = None,
    ) -> None:
        """Publish measured throughput and unfinished-order telemetry.

        Every caller publishes `open_orders`, which is what a snapshot is for
        and which is genuinely counted here. What changed is
        the throughput field: it used to be `capacity_per_hour` handed straight
        back, so the backend's observed-versus-configured distinction was
        decorative and a floor falling behind its rating could never be seen.
        Worse, a capacity change published a snapshot asserting the new rate was
        already achieved, which let a plan's projected `breaches_avoided` come
        true by construction rather than by the floor speeding up.

        It now reports what `advance_statuses` measured. A capacity change still
        publishes a snapshot, so the approval loop still closes; the new rate
        simply shows up as the floor actually earns it.

        Args:
            capacity_per_hour: Configured rating, used only as the fallback
                before the floor has done anything measurable.
            occurred_at: Snapshot timestamp. Defaults to current UTC time.
        """
        occurred_at = occurred_at or datetime.now(UTC)
        open_orders = sum(
            status != OrderStatus.DISPATCHED for status in self.order_statuses.values()
        )
        measured = self.measured_work_units_per_hour()
        snapshot = self.fulfillment.snapshot(
            occurred_at=occurred_at,
            open_orders=open_orders,
            # No measurement yet means the run has not ticked the floor at all,
            # which is the one moment the configured rating is the honest answer.
            work_units_completed_last_hour=(
                capacity_per_hour if measured is None else measured
            ),
        )
        self.client.send_fulfillment_snapshot(snapshot)
