"""Verify simulator payloads remain accepted by the backend's canonical models."""

from datetime import UTC, datetime

from backend.app.models import (
    FulfillmentSnapshotEvent as BackendFulfillmentSnapshotEvent,
)
from backend.app.models import OrderCreatedEvent as BackendOrderCreatedEvent
from backend.app.models import OrderStatusUpdatedEvent as BackendOrderStatusUpdatedEvent
from simulator.commerce import CommerceSimulator
from simulator.fulfillment import FulfillmentSimulator
from simulator.models import OrderStatus

ANCHOR = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def test_simulator_events_validate_against_backend_contracts() -> None:
    """Keep independently owned simulator models compatible with backend ingestion."""
    commerce = CommerceSimulator(run_id="run-contract")
    fulfillment = FulfillmentSimulator(run_id="run-contract")
    order = commerce.generate_orders(target_work_units=1, anchor=ANCHOR)[0]
    snapshot = fulfillment.snapshot(occurred_at=ANCHOR, open_orders=1)
    status = fulfillment.status_event(
        order_id=order.order_id,
        occurred_at=ANCHOR,
        status=OrderStatus.PICKING,
    )

    assert BackendOrderCreatedEvent.model_validate(order.model_dump(mode="json"))
    assert BackendFulfillmentSnapshotEvent.model_validate(snapshot.model_dump(mode="json"))
    assert BackendOrderStatusUpdatedEvent.model_validate(status.model_dump(mode="json"))
