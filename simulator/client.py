import os
from typing import Any

import httpx

from simulator.config import EVENT_CONCURRENCY
from simulator.models import (
    EventIngestionResponse,
    FulfillmentSnapshotEvent,
    OrderCreatedEvent,
    OrderStatusUpdatedEvent,
)


class SurgeGuardClient:
    """HTTP client for sending simulator events through n8n.

    Final producer path:

        Simulator -> n8n -> SurgeGuard backend

    The simulator never writes directly to PostgreSQL.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("N8N_WEBHOOK_BASE_URL", "http://localhost:5678/webhook")
        ).rstrip("/")
        self.timeout = timeout
        # One pooled client for the process. A new connection per event cost
        # about 256ms where a kept-alive one costs 72ms, and a surge posts
        # thousands of them. httpx.Client is thread-safe, so the runner can fan
        # a tick's independent events across it.
        self._http = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=EVENT_CONCURRENCY,
                max_keepalive_connections=EVENT_CONCURRENCY,
            ),
        )

    def close(self) -> None:
        """Release pooled connections."""
        self._http.close()

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> EventIngestionResponse:
        url = f"{self.base_url}{path}"

        response = self._http.post(url, json=payload)

        response.raise_for_status()

        return EventIngestionResponse.model_validate(response.json())

    def send_order(
        self,
        event: OrderCreatedEvent,
    ) -> EventIngestionResponse:
        """Send one order-created event through n8n."""
        return self._post(
            "/commerce/orders",
            event.model_dump(mode="json"),
        )

    def send_fulfillment_snapshot(
        self,
        event: FulfillmentSnapshotEvent,
    ) -> EventIngestionResponse:
        """Send one fulfillment snapshot through n8n."""
        return self._post(
            "/fulfillment/snapshot",
            event.model_dump(mode="json"),
        )

    def send_order_status(
        self,
        event: OrderStatusUpdatedEvent,
    ) -> EventIngestionResponse:
        """Send one order-status update through n8n."""
        return self._post(
            "/fulfillment/status",
            event.model_dump(mode="json"),
        )
