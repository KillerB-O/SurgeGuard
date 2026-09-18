"""Test Pydantic validation for canonical model and event constraints."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models import (
    FulfillmentSnapshotEvent,
    Order,
    OrderCreatedEvent,
    OrderStatusUpdatedEvent,
)

# Deliberately zone-less: the boundary must refuse it.
NAIVE_TIMESTAMP = datetime(2026, 8, 15, 10, 0)  # noqa: DTZ001


def _valid_order_kwargs(**overrides) -> dict:
    now = datetime.now(UTC)
    kwargs = {
        "order_id": "ORD-1",
        "facility_id": "WH-01",
        "created_at": now,
        "promised_dispatch_at": now + timedelta(hours=24),
        "item_count": 1,
        "work_units": 1.0,
        "order_value": Decimal("100.00"),
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("work_units", 0.0),
        ("work_units", -1.0),
        ("item_count", 0),
        ("item_count", -1),
        ("order_value", Decimal("-0.01")),
    ],
)
def test_order_rejects_invalid_field_values(field, bad_value):
    with pytest.raises(ValidationError):
        Order(**_valid_order_kwargs(**{field: bad_value}))


def test_order_allows_boundary_zero_order_value():
    """order_value >= 0 -- exactly 0 (e.g. a free/promotional order) is valid."""
    Order(**_valid_order_kwargs(order_value=Decimal(0)))


def test_order_rejects_promise_not_after_creation():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        Order(**_valid_order_kwargs(created_at=now, promised_dispatch_at=now))

    with pytest.raises(ValidationError):
        Order(
            **_valid_order_kwargs(
                created_at=now, promised_dispatch_at=now - timedelta(minutes=1)
            )
        )


def _valid_order_created_event_kwargs(**overrides) -> dict:
    now = datetime.now(UTC)
    kwargs = {
        "event_id": "evt-1",
        "source": "commerce_sim",
        "order_id": "ORD-1",
        "facility_id": "WH-01",
        "created_at": now,
        "promised_dispatch_at": now + timedelta(hours=24),
        "item_count": 1,
        "work_units": 1.0,
        "order_value": Decimal("100.00"),
    }
    kwargs.update(overrides)
    return kwargs


def test_order_created_event_rejects_same_invalid_values_as_order():
    """The inbound event should fail fast with a 422 at the API boundary
    rather than reaching the DB's CHECK constraint as an unhandled 500.
    """
    with pytest.raises(ValidationError):
        OrderCreatedEvent(**_valid_order_created_event_kwargs(work_units=-5.0))

    with pytest.raises(ValidationError):
        OrderCreatedEvent(**_valid_order_created_event_kwargs(item_count=0))

    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        OrderCreatedEvent(
            **_valid_order_created_event_kwargs(
                created_at=now, promised_dispatch_at=now - timedelta(hours=1)
            )
        )


def test_fulfillment_snapshot_rejects_negative_values():
    now = datetime.now(UTC)
    base = {
        "event_id": "evt-2",
        "source": "fulfillment_sim",
        "facility_id": "WH-01",
        "occurred_at": now,
        "open_orders": 10,
        "work_units_completed_last_hour": 52.0,
    }

    FulfillmentSnapshotEvent(**base)  # valid baseline should not raise

    with pytest.raises(ValidationError):
        FulfillmentSnapshotEvent(**{**base, "open_orders": -1})

    with pytest.raises(ValidationError):
        FulfillmentSnapshotEvent(**{**base, "work_units_completed_last_hour": -0.01})


@pytest.mark.parametrize("field", ["created_at", "promised_dispatch_at"])
def test_order_rejects_naive_timestamps(field):
    """Refuse ambiguous timestamps rather than let PostgreSQL guess a zone."""
    with pytest.raises(ValidationError):
        Order(**_valid_order_kwargs(**{field: NAIVE_TIMESTAMP}))


@pytest.mark.parametrize("field", ["created_at", "promised_dispatch_at"])
def test_order_created_event_rejects_naive_timestamps(field):
    """Apply the same timezone rule at the ingestion boundary."""
    with pytest.raises(ValidationError):
        OrderCreatedEvent(
            **_valid_order_created_event_kwargs(**{field: NAIVE_TIMESTAMP})
        )


def test_fulfillment_snapshot_rejects_naive_timestamps():
    """Apply the timezone rule to telemetry as well."""
    with pytest.raises(ValidationError):
        FulfillmentSnapshotEvent(
            event_id="e1",
            source="fulfillment_sim",
            facility_id="WH-01",
            occurred_at=NAIVE_TIMESTAMP,
            open_orders=1,
            work_units_completed_last_hour=10,
        )


def test_order_status_event_rejects_naive_timestamps():
    """Apply the timezone rule to status events as well."""
    with pytest.raises(ValidationError):
        OrderStatusUpdatedEvent(
            event_id="e1",
            source="fulfillment_sim",
            order_id="ORD-1",
            facility_id="WH-01",
            occurred_at=NAIVE_TIMESTAMP,
            status="PICKING",
        )


def test_order_created_event_rejects_a_foreign_event_type():
    """Keep processed_events from recording an event under the wrong identity."""
    with pytest.raises(ValidationError):
        OrderCreatedEvent(
            **_valid_order_created_event_kwargs(event_type="fulfillment_snapshot")
        )


# There used to be a test here asserting that OrderCreatedEvent rejects
# source="fulfillment_sim" via Pydantic's Literal pin. That guarantee moved
# deliberately: `source` is now a provider id validated against a live
# registry (kind must match commerce/fulfillment), which needs a database
# lookup a pure model cannot make. The equivalent check now lives in
# tests/test_event_providers.py, at the layer that can actually enforce it.
