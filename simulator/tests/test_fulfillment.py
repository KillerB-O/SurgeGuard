from datetime import UTC, datetime

from simulator.fulfillment import FulfillmentSimulator
from simulator.models import EventSource, EventType

ANCHOR = datetime(2026, 8, 15, 10, 20, tzinfo=UTC)


def test_snapshot_uses_canonical_fulfillment_event():
    sim = FulfillmentSimulator()

    event = sim.snapshot(
        occurred_at=ANCHOR,
        open_orders=84,
    )

    assert event.event_type == EventType.FULFILLMENT_SNAPSHOT
    assert event.source == EventSource.FULFILLMENT_SIM
    assert event.facility_id == "WH-01"
    assert event.occurred_at == ANCHOR
    assert event.open_orders == 84
    assert event.work_units_completed_last_hour == 52.0


def test_snapshot_accepts_explicit_completion_rate():
    sim = FulfillmentSimulator()

    event = sim.snapshot(
        occurred_at=ANCHOR,
        open_orders=84,
        work_units_completed_last_hour=61.5,
    )

    assert event.work_units_completed_last_hour == 61.5


def test_snapshot_event_id_is_deterministic():
    sim = FulfillmentSimulator()

    event = sim.snapshot(
        occurred_at=ANCHOR,
        open_orders=84,
    )

    assert event.event_id == "run-001-fulfillment-20260815102000-0001"


def test_snapshot_event_id_uses_run_id():
    """Event IDs remain unique when multiple simulator runs are active."""
    sim = FulfillmentSimulator(run_id="run-test")

    event = sim.snapshot(occurred_at=ANCHOR, open_orders=1)

    assert event.event_id == "run-test-fulfillment-20260815102000-0001"


def test_reset_clears_internal_state():
    sim = FulfillmentSimulator()

    sim.snapshot(
        occurred_at=ANCHOR,
        open_orders=84,
        work_units_completed_last_hour=61.5,
    )

    sim.reset()

    assert sim.open_orders == 0
    assert sim.work_units_completed_last_hour == 0.0
