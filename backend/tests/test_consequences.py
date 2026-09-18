"""What it costs to do nothing.

The product could price every intervention and not the alternative: every lever
carries a cost band, and `do-nothing` carried NONE. An operator asking whether
overtime is worth it got a blank on one side of the comparison.

The fix is not an invented rupee figure per breach. Marketplaces enforce a
*rate*: Amazon's Late Shipment Rate ceiling for seller-fulfilled orders is 4%,
and breaching it costs listings rather than money directly. Ops teams do not
think "this order cost me 500 rupees", they think "I am at 3.1% and the ceiling
is 4%". That is a budget being burned, and it is computable from data we already
hold.

The other half is irreversibility. `BREACHED` means *predicted* to miss. Once
the clock passes the promise, the miss is a fact no recovery can undo, and the
two must not be counted together -- one is a warning, the other is a receipt.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.consequences import (
    LateDispatchPolicy,
    count_preventable,
    late_dispatch_rate,
)
from app.models import Order, OrderStatus, SLAStatus

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _order(order_id: str, *, promised_in: float, status=OrderStatus.PENDING) -> Order:
    return Order(
        order_id=order_id,
        facility_id="WH-01",
        created_at=NOW - timedelta(hours=6),
        promised_dispatch_at=NOW + timedelta(hours=promised_in),
        item_count=1,
        work_units=1.5,
        order_value=100,
        status=status,
    )


class _Scheduled:
    """Minimal stand-in for a scheduled order's SLA verdict."""

    def __init__(self, order_id: str, sla_status: SLAStatus):
        self.order_id = order_id
        self.sla_status = sla_status


def test_an_order_past_its_promise_is_realised_not_preventable():
    """A deadline that has passed cannot be saved by any recovery.

    Counting it as preventable would offer the operator a rescue that does not
    exist, and would let a plan claim credit for orders already lost.
    """
    late = _order("ORD-LATE", promised_in=-2)
    scheduled = [_Scheduled("ORD-LATE", SLAStatus.BREACHED)]

    preventable = count_preventable([late], scheduled, NOW)

    assert preventable == 0


def test_an_order_predicted_to_miss_with_time_left_is_preventable():
    """Still exposed, still saveable -- this is what a plan can act on."""
    exposed = _order("ORD-SOON", promised_in=3)
    scheduled = [_Scheduled("ORD-SOON", SLAStatus.AT_RISK)]

    assert count_preventable([exposed], scheduled, NOW) == 1


def test_an_order_expected_to_make_its_promise_is_not_counted():
    """Only exposure counts. A safe order is not a pending liability."""
    safe = _order("ORD-FINE", promised_in=20)
    scheduled = [_Scheduled("ORD-FINE", SLAStatus.SAFE)]

    assert count_preventable([safe], scheduled, NOW) == 0


def test_the_rate_is_misses_over_everything_placed():
    """The denominator is every order, matching how a marketplace measures it."""
    policy = LateDispatchPolicy(threshold=0.04, label="test")

    assert late_dispatch_rate(misses=4, placed=100, policy=policy).rate == pytest.approx(0.04)


def test_the_rate_is_zero_before_anything_is_placed():
    """A fresh demo must not divide by zero, and must not read as perfect."""
    policy = LateDispatchPolicy(threshold=0.04, label="test")

    result = late_dispatch_rate(misses=0, placed=0, policy=policy)

    assert result.rate == 0.0
    assert result.breaching is False


def test_the_rate_reports_whether_the_ceiling_is_breached():
    """The threshold is the point of the metric, not decoration."""
    policy = LateDispatchPolicy(threshold=0.04, label="test")

    assert late_dispatch_rate(misses=3, placed=100, policy=policy).breaching is False
    assert late_dispatch_rate(misses=5, placed=100, policy=policy).breaching is True


def test_the_default_policy_is_the_published_marketplace_ceiling():
    """4% is Amazon's Late Shipment Rate limit for seller-fulfilled orders.

    It is loaded from the catalog rather than hardcoded so it can be changed
    per facility, and so nobody mistakes it for a number we invented.
    """
    policy = LateDispatchPolicy.load()

    assert policy.threshold == pytest.approx(0.04)
    assert policy.label


def test_the_default_policy_carries_a_rolling_window():
    """An all-time denominator makes the rate insensitive to recent history.

    Amazon's own Late Shipment Rate is measured over a trailing window (about
    10 days), not since the seller's account began. Without a window, a
    facility that has processed years of orders can have a catastrophic week
    and see the reported rate barely move -- exactly when an operator most
    needs the number to react.
    """
    policy = LateDispatchPolicy.load()

    assert policy.window_days == 10


def test_a_dispatched_order_is_judged_on_when_it_actually_left():
    """Lateness is measured against dispatch, not against the clock now.

    An order dispatched an hour late stays late forever; one dispatched on time
    never becomes late no matter how much time passes afterwards.
    """
    on_time = _order("ORD-OK", promised_in=-5, status=OrderStatus.DISPATCHED).model_copy(
        update={"dispatched_at": NOW - timedelta(hours=6)}
    )
    missed = _order("ORD-MISS", promised_in=-5, status=OrderStatus.DISPATCHED).model_copy(
        update={"dispatched_at": NOW - timedelta(hours=4)}
    )

    from app.consequences import dispatched_late

    assert dispatched_late(on_time) is False
    assert dispatched_late(missed) is True
