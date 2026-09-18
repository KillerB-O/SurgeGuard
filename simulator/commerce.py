import random
from datetime import datetime, timedelta
from decimal import Decimal

from simulator.config import FACILITY_ID, RANDOM_SEED
from simulator.models import OrderCreatedEvent

WORK_UNIT_DISTRIBUTION = (
    (1.0, 0.45),
    (1.5, 0.30),
    (2.0, 0.20),
    (2.5, 0.05),
)

# Order shapes the backend classifies work units from. The bands above are kept
# so a stage still hits its target workload, but each order now carries the
# attributes a real commerce feed would supply, and the backend derives the work
# from those rather than trusting this number.
ORDER_SHAPES = (
    # (line_count, unit_count, special_handling, weight)
    (1, 1, False, 0.40),
    (2, 3, False, 0.25),
    (4, 6, False, 0.18),
    (3, 4, True, 0.09),
    (8, 20, False, 0.06),
    (12, 60, True, 0.02),
)

# Customer cohorts a queue recovery lever can protect or defer. Without these
# the whole QUEUE family is inert, because there is no cohort to move.
SEGMENTS = (
    ("FIRST_TIME", 0.30),
    ("SUBSCRIBER", 0.20),
    ("HIGH_LTV", 0.15),
    (None, 0.35),
)

# One (low, high) dollar range per ORDER_SHAPES entry, same index. A cart's
# value tracks what's in it -- pairing this to shape, not drawing it
# independently, is what makes "$412.99 for a 12-line bulk order" and
# "$18.99 for a single item" both plausible instead of arbitrary. This has no
# effect on scheduling, levers, or verification -- nothing downstream reads
# order_value -- it exists only so the simulator's data looks like it came
# from a real storefront rather than a fixture.
PRICE_RANGE_BY_SHAPE = (
    (12.00, 45.00),
    (25.00, 90.00),
    (55.00, 180.00),
    (45.00, 150.00),
    (140.00, 450.00),
    (300.00, 900.00),
)

# A repeat/high-value customer's cart skews larger. Cosmetic, same reasoning
# as PRICE_RANGE_BY_SHAPE above -- it just keeps segment and price from being
# decoupled facts about the same order.
HIGH_LTV_PRICE_MULTIPLIER = 1.4

# Promise windows in hours. A single fixed promise makes SLA urgency and order
# age perfectly collinear, so nothing can be deferred relative to anything else
# and queue levers have no spread to work with.
PROMISE_WINDOWS = (
    # Express sits near the clearance time a surged queue actually achieves, so
    # a capacity or cutoff lever can still rescue it. Set it far below that and
    # express orders breach irrecoverably, which makes every plan look useless.
    (12.0, 0.20),  # express
    (24.0, 0.55),  # standard
    (48.0, 0.25),  # economy
)


class CommerceSimulator:
    def __init__(self, run_id: str = "run-001") -> None:
        self.run_id = run_id
        self.rng = random.Random(RANDOM_SEED)
        self.order_number = 0
        # Demand asked for but not yet large enough to place an order, carried
        # to the next tick. See `generate_orders`.
        self._demand_credit = 0.0

    def reset(self) -> None:
        self.rng = random.Random(RANDOM_SEED)
        self.order_number = 0
        self._demand_credit = 0.0

    def _next_work_units(self) -> float:
        values = [item[0] for item in WORK_UNIT_DISTRIBUTION]
        weights = [item[1] for item in WORK_UNIT_DISTRIBUTION]
        return self.rng.choices(values, weights=weights, k=1)[0]

    def _weighted(self, table):
        """Draw one entry from a (value, weight) table using the seeded RNG."""
        values = [row[0] for row in table]
        weights = [row[1] for row in table]
        return self.rng.choices(values, weights=weights, k=1)[0]

    def _next_shape(self) -> tuple[int, int, int, bool]:
        """Draw the line/unit/handling shape of one order.

        Returns the shape's own index alongside its attributes so a caller
        can look up the matching price range without a second, independent
        draw -- a cart's value should track what's actually in it.
        """
        shape_index = self.rng.choices(
            range(len(ORDER_SHAPES)), weights=[row[3] for row in ORDER_SHAPES], k=1
        )[0]
        lines, units, special, _ = ORDER_SHAPES[shape_index]
        return shape_index, lines, units, special

    def _next_order_value(self, shape_index: int, segment: str | None) -> Decimal:
        """Draw a realistic order value correlated with the order's shape.

        Bigger/bulkier orders draw from a higher price range than a single
        item, and a HIGH_LTV customer's cart skews larger on top of that --
        the same shape of correlation `_next_shape`/`SEGMENTS` already model
        for work units, applied to price. Rounded to a `.99` ending, the way
        real storefront pricing reads, rather than an arbitrary float.
        """
        low, high = PRICE_RANGE_BY_SHAPE[shape_index]
        value = self.rng.uniform(low, high)
        if segment == "HIGH_LTV":
            value *= HIGH_LTV_PRICE_MULTIPLIER
        return Decimal(f"{int(value)}.99")

    def generate_orders(
        self,
        target_work_units: float,
        anchor: datetime,
        dispatch_promise_hours: float = 24,
    ) -> list[OrderCreatedEvent]:
        """Create canonical orders for one demand stage.

        A tick asks for a slice of an hour, and at a slow clock that slice is
        worth a fraction of one order: at 1x it is about 0.07 work units against
        a smallest order of 1.0. Generating "until the target is reached" rounds
        every such tick up to a whole order, which had the shop sending roughly
        twenty times the demand the wave specifies and left the surge invisible
        at the very speed meant to expose it.

        So the shortfall is carried rather than rounded away. Each call adds its
        target to a running credit and places orders while that credit lasts; a
        tick too small to afford one places nothing and leaves the credit to grow.
        Over any stretch of ticks the work generated is the wave's rate times the
        time covered, whatever cadence the caller ticks at.

        The credit needs no ceiling. Placing an order always drives it to zero or
        below, so after any call it sits in `(-max order, 0]` and cannot
        accumulate.

        Args:
            target_work_units: Workload to generate for the stage.
            anchor: Current scenario time used as each order's creation time.
            dispatch_promise_hours: Promise policy applied to newly created orders.
        """
        # Guarded before the credit is touched: a negative target is not demand
        # owed, and banking it as debt would suppress real orders later.
        if target_work_units <= 0:
            return []

        events: list[OrderCreatedEvent] = []
        self._demand_credit += target_work_units

        while self._demand_credit > 0:
            self.order_number += 1

            work_units = self._next_work_units()
            shape_index, line_count, unit_count, special_handling = self._next_shape()
            segment = self._weighted(SEGMENTS)
            order_value = self._next_order_value(shape_index, segment)

            created_at = anchor
            # Mixed promise windows, scaled by the stage's policy. A single
            # window leaves every order with identical slack, which makes queue
            # levers provably inert.
            window = self._weighted(PROMISE_WINDOWS) * (dispatch_promise_hours / 24.0)
            promised_dispatch_at = created_at + timedelta(hours=window)

            event = OrderCreatedEvent(
                event_id=f"{self.run_id}-order-{self.order_number:04d}",
                order_id=f"ORD-{self.order_number:04d}",
                facility_id=FACILITY_ID,
                created_at=created_at,
                promised_dispatch_at=promised_dispatch_at,
                item_count=unit_count,
                work_units=work_units,
                order_value=order_value,
                line_count=line_count,
                unit_count=unit_count,
                special_handling=special_handling,
                segment=segment,
            )

            events.append(event)
            self._demand_credit -= work_units

        return events
