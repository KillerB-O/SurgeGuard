"""Test work-unit classification from order attributes."""

import pytest

from app.work_units import (
    classify,
    classify_order_event,
    effective_lines,
    load_rules,
    remaining_work_fraction,
    work_units_for_step,
)


def test_catalog_loads_and_matches_the_documented_bands():
    """Keep the shipped ruleset aligned with the documented four bands."""
    rules = load_rules()

    assert [band.work_units for band in rules.bands] == [1.0, 1.5, 2.0, 2.5]
    assert rules.bands[-1].max_effective_lines is None, "top band must be open-ended"


@pytest.mark.parametrize(
    ("line_count", "expected"),
    [
        (1, 1.0),
        (2, 1.0),
        (3, 1.5),
        (5, 1.5),
        (6, 2.0),
        (10, 2.0),
        (11, 2.5),
        (500, 2.5),
    ],
)
def test_band_boundaries(line_count, expected):
    """Classify on each side of every boundary, not just the middles."""
    assert classify(line_count=line_count, unit_count=1) == expected


def test_bulk_quantity_is_not_a_one_touch_pick():
    """Fold units back into line-equivalents so bulk is not read as simple.

    A single line of forty units is not the same work as a single line of one,
    and counting only lines would price them identically. At five units per
    line-equivalent, forty units is eight lines of work.
    """
    assert classify(line_count=1, unit_count=1) == 1.0
    assert classify(line_count=1, unit_count=40) == 2.0
    assert classify(line_count=1, unit_count=60) == 2.5


def test_special_handling_adds_a_surcharge_within_the_cap():
    """Charge for fragile or personalised work, but never past the ceiling."""
    rules = load_rules()

    plain = classify(line_count=1, unit_count=1)
    special = classify(line_count=1, unit_count=1, special_handling=True)

    assert special == plain + rules.special_handling_surcharge
    assert classify(line_count=500, unit_count=500, special_handling=True) <= rules.max_work_units


def test_effective_lines_never_falls_below_one():
    """Keep a tiny order from classifying as zero work."""
    assert effective_lines(1, 1, load_rules()) == 1


def test_classification_is_deterministic():
    """Same attributes must always cost the same, run to run."""
    assert classify(4, 12, True) == classify(4, 12, True)


@pytest.mark.parametrize(("line_count", "unit_count"), [(0, 1), (1, 0), (-1, 5)])
def test_non_positive_counts_are_rejected(line_count, unit_count):
    """Refuse inputs that cannot describe a real order."""
    with pytest.raises(ValueError):
        classify(line_count=line_count, unit_count=unit_count)


def test_classification_overrides_a_producer_supplied_value():
    """The core decides what work an order costs, not the provider."""
    derived = classify_order_event(
        line_count=8, unit_count=8, special_handling=False, declared_work_units=999.0
    )

    assert derived == 2.0


def test_missing_attributes_fall_back_to_the_declared_value():
    """Let a producer adopt the attributes later without a flag day."""
    assert (
        classify_order_event(
            line_count=None, unit_count=None, special_handling=None, declared_work_units=1.5
        )
        == 1.5
    )


def test_stage_weights_sum_to_one():
    """The catalog's per-step weights must account for the whole order, not part of it."""
    rules = load_rules()

    assert sum(rules.stage_weights) == pytest.approx(1.0)


def test_stage_weights_cover_every_lifecycle_step():
    """One weight per transition in ORDER_LIFECYCLE (PENDING through DISPATCHED)."""
    rules = load_rules()

    assert len(rules.stage_weights) == 4


def test_work_units_for_step_charges_only_the_covered_steps():
    """A single step (PICKING -> PACKED, position 1 to 2) costs its own weight only."""
    rules = load_rules()

    assert work_units_for_step(4.0, 1, 2, rules) == pytest.approx(4.0 * rules.stage_weights[1])


def test_work_units_for_step_sums_a_forward_jump():
    """A skipped step (PENDING -> PACKED, position 0 to 2) is charged both steps it covers."""
    rules = load_rules()

    expected = 4.0 * (rules.stage_weights[0] + rules.stage_weights[1])
    assert work_units_for_step(4.0, 0, 2, rules) == pytest.approx(expected)


def test_remaining_work_fraction_shrinks_with_lifecycle_position():
    """An order further along the lifecycle has strictly less work left."""
    rules = load_rules()

    at_pending = remaining_work_fraction(0, rules)
    at_picking = remaining_work_fraction(1, rules)
    at_packed = remaining_work_fraction(2, rules)

    assert at_pending == pytest.approx(1.0)
    assert at_pending > at_picking > at_packed > 0.0


def test_remaining_work_fraction_is_zero_once_dispatched():
    """Nothing is left to charge once every step is done."""
    rules = load_rules()

    assert remaining_work_fraction(4, rules) == pytest.approx(0.0)
