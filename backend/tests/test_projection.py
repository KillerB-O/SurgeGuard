"""Test projected future arrivals for What-If runs."""

from datetime import UTC, datetime, timedelta

import pytest

from app.levers import load_catalog
from app.plans import ScheduleInputs, apply_lever
from app.projection import (
    SYNTHETIC_PREFIX,
    ArrivalBurst,
    ArrivalProjection,
    project_arrivals,
    synthetic_ids,
)
from app.work_units import classify, load_rules

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _project(**kwargs):
    """Project two hours of arrivals at the surge rate."""
    projection = ArrivalProjection(**kwargs)
    return project_arrivals(127.0, projection, NOW, "WH-01", 24.0)


def test_projection_is_deterministic_under_a_seed():
    """A saved scenario must replay identically (spec section 6)."""
    first = _project(horizon_minutes=120)
    second = _project(horizon_minutes=120)

    assert [(o.order_id, o.work_units, o.created_at) for o in first] == [
        (o.order_id, o.work_units, o.created_at) for o in second
    ]


def test_a_different_seed_gives_a_different_draw():
    """Keep the seed meaningful rather than decorative."""
    assert [o.work_units for o in _project(horizon_minutes=120, seed=1)] != [
        o.work_units for o in _project(horizon_minutes=120, seed=2)
    ]


def test_volume_tracks_the_observed_rate_and_horizon():
    """Project the rate the facility is actually receiving, not a guess."""
    two_hours = sum(o.work_units for o in _project(horizon_minutes=120))
    one_hour = sum(o.work_units for o in _project(horizon_minutes=60))

    assert two_hours == pytest.approx(254.0, abs=3.0), "127 wu/hr for two hours"
    assert one_hour == pytest.approx(127.0, abs=3.0)


def test_multiplier_scales_arrivals():
    """Model what closing the tap upstream is actually worth."""
    full = sum(o.work_units for o in _project(horizon_minutes=120))
    halved = sum(o.work_units for o in _project(horizon_minutes=120, arrival_multiplier=0.5))

    assert halved == pytest.approx(full / 2, rel=0.05)


def test_a_burst_lands_together_rather_than_spread():
    """A flash sale should look like a flash sale, not a drizzle."""
    orders = _project(
        horizon_minutes=120, burst=ArrivalBurst(at_minute=30, work_units=200.0)
    )

    burst_time = NOW + timedelta(minutes=30)
    at_burst = [o for o in orders if o.created_at == burst_time]
    assert sum(o.work_units for o in at_burst) >= 200.0


def test_arrivals_are_marked_and_pending():
    """Projected orders must be identifiable and schedulable."""
    orders = _project(horizon_minutes=60)

    assert all(o.order_id.startswith(SYNTHETIC_PREFIX) for o in orders)
    assert synthetic_ids(orders) == {o.order_id for o in orders}
    assert all(o.status == "PENDING" for o in orders)


def test_arrivals_land_inside_the_horizon():
    """Never project an order beyond the window the operator asked about."""
    orders = _project(horizon_minutes=60)

    assert all(NOW <= o.created_at <= NOW + timedelta(minutes=60) for o in orders)


def test_no_observed_demand_projects_nothing():
    """A quiet facility has no future arrivals to invent."""
    assert project_arrivals(0.0, ArrivalProjection(), NOW, "WH-01", 24.0) == []


def test_item_count_is_not_hardcoded_to_one():
    """A projected arrival's item_count must come from its drawn shape.

    A real order's item_count varies with what was ordered; a projection
    that always reports 1 cannot stand in for real arrivals anywhere that
    reads item_count.
    """
    orders = _project(horizon_minutes=120)

    assert any(o.item_count != 1 for o in orders)


def test_segment_is_drawn_from_a_realistic_distribution():
    """Projected arrivals must carry a segment, like real orders do.

    Without this, PROTECT_SEGMENT and any segment-scoped DEFER_LOW_STAKES
    have no cohort to act on among projected arrivals, silently making the
    whole family inert for a What-If run.
    """
    orders = _project(horizon_minutes=120)

    segments = {o.segment for o in orders}
    assert segments - {None}, "at least one projected order must carry a real segment"
    # The None bucket must survive too -- a projection where every order
    # has a segment is its own fidelity bug.
    assert None in segments


def test_sizes_route_through_classify():
    """A projected order's work_units must be a value classify() can produce.

    Not a value drawn independently from a separate arrival_weight-only
    distribution: sizes come from the same shapes a real order would report,
    run through the same classification function real orders use.
    """
    rules = load_rules()
    possible_sizes = {
        classify(shape.line_count, shape.unit_count, shape.special_handling, rules)
        for shape in rules.arrival_shapes
    }

    orders = _project(horizon_minutes=120)

    assert orders, "need at least one projected order to assert anything about its size"
    assert all(o.work_units in possible_sizes for o in orders)


def test_a_segment_targeted_lever_can_act_on_a_projected_arrival():
    """PROTECT_SEGMENT must be able to pin a projected FIRST_TIME arrival.

    Proving `segment is not None` somewhere is not the same claim: this
    exercises the actual lever a What-If run would apply, the way
    test_plans.py does for real orders. It also confirms the pinned order is
    still a synthetic id -- a queue lever reordering projected arrivals must
    not make score_outcome start counting one as a real order improved,
    since it excludes synthetic_ids from prevention counts (app/plans.py).
    """
    orders = _project(horizon_minutes=120)
    lever = load_catalog().lever("PROTECT_SEGMENT")
    inputs = ScheduleInputs(
        orders=orders, capacity_per_hour=50.0, committed_work_units=0.0, dispatch_cutoff=None
    )

    after = apply_lever(lever, inputs, NOW)

    assert after.priority_adjustments, "no projected order was pinned by the segment lever"
    pinned_ids = set(after.priority_adjustments)
    assert pinned_ids <= synthetic_ids(orders), (
        "a pinned order must still be identifiable as synthetic, or score_outcome "
        "would start counting a projected arrival as a real order improved"
    )
