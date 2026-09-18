from datetime import datetime

from simulator.config import FACILITY_ID, FULFILLMENT_BASELINE_WU_PER_HOUR
from simulator.models import (
    FulfillmentSnapshotEvent,
    OrderStatus,
    OrderStatusUpdatedEvent,
)


class FulfillmentSimulator:
    def __init__(self, run_id: str = "run-001") -> None:
        self.run_id = run_id
        self.reset()

    def reset(self) -> None:
        self.open_orders = 0
        self.work_units_completed_last_hour = 0.0
        self.snapshot_number = 0

    def snapshot(
        self,
        *,
        occurred_at: datetime,
        open_orders: int,
        work_units_completed_last_hour: float | None = None,
    ) -> FulfillmentSnapshotEvent:
        if work_units_completed_last_hour is None:
            work_units_completed_last_hour = FULFILLMENT_BASELINE_WU_PER_HOUR

        self.snapshot_number += 1
        self.open_orders = open_orders
        self.work_units_completed_last_hour = work_units_completed_last_hour

        return FulfillmentSnapshotEvent(
            event_id=(
                f"{self.run_id}-fulfillment-"
                f"{occurred_at.strftime('%Y%m%d%H%M%S')}-{self.snapshot_number:04d}"
            ),
            facility_id=FACILITY_ID,
            occurred_at=occurred_at,
            open_orders=open_orders,
            work_units_completed_last_hour=work_units_completed_last_hour,
        )

    def status_event(
        self,
        *,
        order_id: str,
        occurred_at: datetime,
        status: OrderStatus,
    ) -> OrderStatusUpdatedEvent:
        return OrderStatusUpdatedEvent(
            event_id=(
                f"{self.run_id}-status-{order_id}-"
                f"{status.value}-{occurred_at.strftime('%Y%m%d%H%M%S')}"
            ),
            order_id=order_id,
            facility_id=FACILITY_ID,
            occurred_at=occurred_at,
            status=status,
        )
