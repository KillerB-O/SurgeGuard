"""What it costs to do nothing.

Every lever in the catalog carries a cost band. `do-nothing` carried `NONE`,
which made the product's central decision unanswerable: an operator asking
whether overtime is worth it got a blank on one side of the comparison. It
could price every action except inaction.

The fix is not a rupee figure per breach -- that number would be invented, and
a dispatch miss is not a lost order anyway. The work still ships, late. What
marketplaces actually enforce is a *rate*: Amazon's Late Shipment Rate ceiling
for seller-fulfilled orders is 4%, and crossing it costs listings and Buy Box
eligibility. Ops teams do not think "this order cost me 500 rupees", they think
"I am at 3.1% and the ceiling is 4%" -- a budget being burned rather than a bill
arriving. That is computable from data already held, against a published number
nobody has to defend as a guess.

The other half is irreversibility. `BREACHED` means *predicted* to miss. Once
the clock passes the promise, the miss is a fact no recovery can undo. Counting
the two together would offer the operator a rescue that does not exist, so they
are reported separately: one is a warning, the other is a receipt.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from app.models import Order, OrderStatus, SLAStatus

CATALOG_PATH = Path(__file__).parent / "catalog" / "consequences.json"

# Exposure a plan can still act on. A realised miss is deliberately not here.
EXPOSED = (SLAStatus.AT_RISK, SLAStatus.BREACHED)


@dataclass(frozen=True)
class LateDispatchPolicy:
    """The ceiling a facility is measured against, and where it comes from."""

    threshold: float
    label: str
    # Trailing window the rate is measured over, matching how Amazon itself
    # computes Late Shipment Rate. An all-time denominator would make the
    # rate insensitive to recent history: a facility with years of orders can
    # have a catastrophic week and see the reported number barely move.
    window_days: int = 10

    @classmethod
    def load(cls) -> "LateDispatchPolicy":
        """Read the policy from the catalog.

        Kept in a file beside the lever catalog rather than hardcoded so a
        facility selling on a different channel can carry a different ceiling,
        and so the number is visibly sourced rather than mistaken for one we
        made up.
        """
        return _load_policy()


@lru_cache(maxsize=1)
def _load_policy() -> LateDispatchPolicy:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return LateDispatchPolicy(
        threshold=raw["threshold"],
        label=raw["label"],
        window_days=raw.get("window_days", 10),
    )


@dataclass(frozen=True)
class LateDispatch:
    """A facility's standing against its late-dispatch ceiling."""

    rate: float
    threshold: float
    label: str
    misses: int
    placed: int
    breaching: bool


def late_dispatch_rate(*, misses: int, placed: int, policy: LateDispatchPolicy) -> LateDispatch:
    """Misses over everything placed, which is how a marketplace measures it.

    Args:
        misses: Orders that missed their promise, dispatched late or still
            undispatched past it.
        placed: Every order the facility has taken.
        policy: The ceiling being measured against.

    Returns:
        The rate and whether it breaches the ceiling. A facility with no orders
        reads 0.0 rather than dividing by zero -- and is not breaching, because
        an empty shop has not missed anything.
    """
    rate = misses / placed if placed else 0.0
    return LateDispatch(
        rate=rate,
        threshold=policy.threshold,
        label=policy.label,
        misses=misses,
        placed=placed,
        breaching=placed > 0 and rate > policy.threshold,
    )


def dispatched_late(order: Order) -> bool:
    """Whether an order that has left did so after its promise.

    Judged on when it actually went, not on the clock now: an order dispatched
    on time never becomes late however long ago that was, and one dispatched an
    hour late stays late forever.
    """
    if order.status != OrderStatus.DISPATCHED or order.dispatched_at is None:
        return False
    return order.dispatched_at > order.promised_dispatch_at


def count_preventable(orders: list[Order], scheduled, now: datetime) -> int:
    """Count exposed orders whose deadline has not yet passed.

    An order predicted to miss with time still on the clock is what a recovery
    plan can act on. One already past its promise is not: no lever makes it
    arrive on time, and counting it here would let a plan take credit for
    orders that are already lost.

    Args:
        orders: Candidate orders, keyed by id against the schedule.
        scheduled: Scheduled orders carrying an `order_id` and `sla_status`.
        now: Evaluation instant, from the shared clock.

    Returns:
        How many exposed orders are still saveable.
    """
    promise_by_id = {order.order_id: order.promised_dispatch_at for order in orders}

    return sum(
        1
        for item in scheduled
        if item.sla_status in EXPOSED
        and promise_by_id.get(item.order_id) is not None
        and promise_by_id[item.order_id] > now
    )
