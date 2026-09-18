"""Test order-total aggregation that needs no database, only the catalog."""

import pytest

from app.models import OrderStatus
from app.repository import OrderTotals
from app.work_units import load_rules


def _totals(**work_units_by_status: float) -> OrderTotals:
    """Build order totals with one order per named status."""
    return OrderTotals(
        counts={OrderStatus(name.upper()): 1 for name in work_units_by_status},
        work_units={OrderStatus(name.upper()): wu for name, wu in work_units_by_status.items()},
    )


def test_committed_work_units_charges_only_the_remaining_fraction():
    """A PACKED order has fewer steps left than a PICKING order, and must cost less.

    Full-charging (the old behaviour) would report 10.0 + 10.0 = 20.0 here,
    which is not a conservative estimate of remaining floor work -- it is the
    total work of both orders, as if neither had been touched.
    """
    rules = load_rules()
    totals = _totals(picking=10.0, packed=10.0)

    picking_remaining = 10.0 * sum(rules.stage_weights[1:])
    packed_remaining = 10.0 * sum(rules.stage_weights[2:])

    assert totals.committed_work_units == pytest.approx(picking_remaining + packed_remaining)
    assert totals.committed_work_units < 20.0


def test_committed_work_units_for_packed_is_less_than_for_picking():
    """Further along the lifecycle means less work left, order for order."""
    picking_only = _totals(picking=10.0)
    packed_only = _totals(packed=10.0)

    assert packed_only.committed_work_units < picking_only.committed_work_units


def test_committed_work_units_ignores_uncommitted_statuses():
    """PENDING and READY are not COMMITTED_STATUSES, so they contribute nothing."""
    totals = _totals(pending=50.0, ready=50.0)

    assert totals.committed_work_units == 0.0
