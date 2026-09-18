from datetime import UTC, datetime
from decimal import Decimal

import pytest
from simulator.commerce import (
    HIGH_LTV_PRICE_MULTIPLIER,
    PRICE_RANGE_BY_SHAPE,
    CommerceSimulator,
)

ANCHOR = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

# The live loop ticks every 2 real seconds, so a tick covers this many simulated
# hours at a given clock speed. 1x is the setting that exposes anything secretly
# depending on compressed time.
LIVE_TICK_SECONDS = 2.0

# Largest order the distribution can draw, which bounds how far a single tick
# may legitimately overshoot the target.
MAX_ORDER_WORK_UNITS = 2.5


def test_generate_orders_reaches_target_workload() -> None:
    simulator = CommerceSimulator()

    events = simulator.generate_orders(
        target_work_units=32,
        anchor=ANCHOR,
    )

    total_work_units = sum(event.work_units for event in events)

    assert events
    assert total_work_units >= 32


def test_generate_orders_is_deterministic_after_reset() -> None:
    simulator = CommerceSimulator()

    first = simulator.generate_orders(
        target_work_units=32,
        anchor=ANCHOR,
    )

    simulator.reset()

    second = simulator.generate_orders(
        target_work_units=32,
        anchor=ANCHOR,
    )

    assert [event.model_dump() for event in first] == [
        event.model_dump() for event in second
    ]


def test_generated_events_use_canonical_order_identity() -> None:
    simulator = CommerceSimulator(run_id="run-test")

    events = simulator.generate_orders(
        target_work_units=5,
        anchor=ANCHOR,
    )

    assert events[0].event_id == "run-test-order-0001"
    assert events[0].order_id == "ORD-0001"
    assert events[0].facility_id == "WH-01"


def test_non_positive_target_generates_no_events() -> None:
    simulator = CommerceSimulator()

    assert simulator.generate_orders(0, ANCHOR) == []
    assert simulator.generate_orders(-1, ANCHOR) == []


def test_generated_orders_use_the_selected_promise_policy() -> None:
    """Future orders reflect an approved dispatch-promise policy."""
    event = CommerceSimulator().generate_orders(1, ANCHOR, dispatch_promise_hours=30)[0]

    assert (event.promised_dispatch_at - event.created_at).total_seconds() == 30 * 3600


@pytest.mark.parametrize("speed", [1.0, 25.0, 60.0])
def test_demand_matches_the_wave_rate_at_any_clock_speed(speed: float) -> None:
    """A tick shorter than one order must not be rounded up to a whole order.

    A live tick asks for a fraction of a simulated hour. Generating "until the
    target is reached" always emits at least one order, so at 1x -- where a tick
    is worth 0.07 work units against a smallest order of 1.0 -- the shop sends
    roughly twenty times the demand the wave specifies, and the surge the whole
    product is built around never appears. The shortfall has to be carried
    between ticks instead of rounded away.
    """
    rate = 127.0  # peak of the demand wave
    duration_hours = LIVE_TICK_SECONDS * speed / 3600.0
    ticks = round(4.0 / duration_hours)  # about four simulated hours either way

    simulator = CommerceSimulator()
    generated = 0.0
    for _ in range(ticks):
        generated += sum(
            event.work_units
            for event in simulator.generate_orders(rate * duration_hours, ANCHOR)
        )

    expected = rate * duration_hours * ticks
    assert generated == pytest.approx(expected, abs=MAX_ORDER_WORK_UNITS), (
        f"at {speed}x the shop generated {generated:.1f} work units where the "
        f"wave specifies {expected:.1f}"
    )


def test_a_partial_tick_leaves_no_remainder_after_reset() -> None:
    """Reset must clear the carried remainder, not just the counter and RNG.

    A remainder that survives reset would make the first tick of a new run
    depend on how the previous one happened to end.
    """
    simulator = CommerceSimulator()
    # A tick far too small to afford an order: this is pure carried debt.
    simulator.generate_orders(0.07, ANCHOR)
    simulator.reset()

    after_reset = simulator.generate_orders(32, ANCHOR)
    fresh = CommerceSimulator().generate_orders(32, ANCHOR)

    assert [event.model_dump() for event in after_reset] == [
        event.model_dump() for event in fresh
    ]


# --- realistic order pricing -------------------------------------------------


def test_order_value_varies_across_a_batch() -> None:
    """Every order used to be a flat $100.00; a realistic feed's prices vary."""
    simulator = CommerceSimulator()

    events = simulator.generate_orders(target_work_units=64, anchor=ANCHOR)

    values = {event.order_value for event in events}
    assert len(values) > 1, "order_value must vary, not repeat a single constant"
    assert all(value != Decimal("100.00") for value in values)


def test_order_value_stays_within_the_widest_possible_range() -> None:
    simulator = CommerceSimulator()

    events = simulator.generate_orders(target_work_units=64, anchor=ANCHOR)

    lowest = Decimal(str(min(low for low, _ in PRICE_RANGE_BY_SHAPE)))
    highest = Decimal(str(max(high for _, high in PRICE_RANGE_BY_SHAPE))) * Decimal(
        str(HIGH_LTV_PRICE_MULTIPLIER)
    )
    for event in events:
        assert lowest <= event.order_value <= highest
        assert event.order_value >= 0


def test_order_value_ends_in_ninety_nine_cents() -> None:
    """Reads as real storefront pricing rather than an arbitrary float."""
    simulator = CommerceSimulator()

    events = simulator.generate_orders(target_work_units=32, anchor=ANCHOR)

    assert all(event.order_value % 1 == Decimal("0.99") for event in events)


def test_order_value_correlates_with_the_drawn_shape() -> None:
    """A bulkier order draws from a higher price range than a small one.

    The two shapes' price ranges don't overlap (widest low shape tops out at
    $45, narrowest high shape starts at $140), so this holds regardless of
    the specific random draw.
    """
    smallest_shape_value = CommerceSimulator()._next_order_value(0, None)
    largest_shape_value = CommerceSimulator()._next_order_value(
        len(PRICE_RANGE_BY_SHAPE) - 1, None
    )

    assert largest_shape_value > smallest_shape_value


def test_high_ltv_segment_multiplies_order_value() -> None:
    """Same seed, same first draw -- only the segment differs."""
    shape_index = 2

    without_segment = CommerceSimulator()._next_order_value(shape_index, None)
    with_high_ltv = CommerceSimulator()._next_order_value(shape_index, "HIGH_LTV")

    assert with_high_ltv > without_segment


def test_order_value_is_deterministic_after_reset() -> None:
    simulator = CommerceSimulator()

    first = [event.order_value for event in simulator.generate_orders(32, ANCHOR)]
    simulator.reset()
    second = [event.order_value for event in simulator.generate_orders(32, ANCHOR)]

    assert first == second
