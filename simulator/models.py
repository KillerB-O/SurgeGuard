from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PICKING = "PICKING"
    PACKED = "PACKED"
    READY = "READY"
    DISPATCHED = "DISPATCHED"


class EventSource(StrEnum):
    COMMERCE_SIM = "commerce_sim"
    FULFILLMENT_SIM = "fulfillment_sim"


class EventType(StrEnum):
    ORDER_CREATED = "order_created"
    FULFILLMENT_SNAPSHOT = "fulfillment_snapshot"
    ORDER_STATUS_UPDATED = "order_status_updated"

class IngestionStatus(StrEnum):
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    # A status event for an order that has not arrived yet. Parked rather
    # than dropped; replayed in occurred_at order once the order exists.
    BUFFERED = "BUFFERED"


class EventIngestionResponse(BaseModel):
    event_id: str
    status: IngestionStatus

class OrderCreatedEvent(BaseModel):
    event_id: str
    event_type: EventType = EventType.ORDER_CREATED
    source: EventSource = EventSource.COMMERCE_SIM
    order_id: str
    facility_id: str
    created_at: datetime
    promised_dispatch_at: datetime
    item_count: int = Field(gt=0)
    work_units: float = Field(gt=0)
    order_value: Decimal = Field(ge=0)

    # Attributes the backend classifies work units from, and the cohort a
    # recovery lever targets. Optional to match the canonical event.
    line_count: int | None = Field(default=None, gt=0)
    unit_count: int | None = Field(default=None, gt=0)
    special_handling: bool = False
    segment: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def check_promise_after_creation(self) -> "OrderCreatedEvent":
        if self.promised_dispatch_at <= self.created_at:
            raise ValueError("promised_dispatch_at must be after created_at")
        return self


class FulfillmentSnapshotEvent(BaseModel):
    event_id: str
    event_type: EventType = EventType.FULFILLMENT_SNAPSHOT
    source: EventSource = EventSource.FULFILLMENT_SIM
    facility_id: str
    occurred_at: datetime
    open_orders: int = Field(ge=0)
    work_units_completed_last_hour: float = Field(ge=0)


class OrderStatusUpdatedEvent(BaseModel):
    event_id: str
    event_type: EventType = EventType.ORDER_STATUS_UPDATED
    source: EventSource = EventSource.FULFILLMENT_SIM
    order_id: str
    facility_id: str
    occurred_at: datetime
    status: OrderStatus
