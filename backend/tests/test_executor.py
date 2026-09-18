"""Test the outbound handoff that starts recovery execution in n8n."""

import httpx
import pytest

from app import executor


@pytest.fixture
def webhook_configured(monkeypatch):
    """Point the executor at a webhook URL for the duration of one test."""
    monkeypatch.setattr(
        executor.settings, "n8n_recovery_webhook_url", "http://n8n.test/webhook/recovery/action"
    )


class _StubClient:
    """Stand-in for httpx.AsyncClient that records or raises."""

    def __init__(self, calls: list, error: Exception | None = None):
        self._calls = calls
        self._error = error

    async def __aenter__(self):
        """Enter the async context like the real client."""
        return self

    async def __aexit__(self, *exc_info):
        """Leave the async context without suppressing anything."""
        return False

    async def post(self, url: str, json: dict):
        """Record the call, or raise the configured transport error."""
        self._calls.append({"url": url, "json": json})
        if self._error is not None:
            raise self._error
        return httpx.Response(200, request=httpx.Request("POST", url))


def _install(monkeypatch, calls: list, error: Exception | None = None) -> None:
    """Replace httpx.AsyncClient with the stub for one test."""
    monkeypatch.setattr(
        executor.httpx, "AsyncClient", lambda **kwargs: _StubClient(calls, error)
    )


async def test_action_is_posted_to_the_configured_webhook(webhook_configured, monkeypatch):
    """Send n8n what it needs to look the action up and execute it."""
    calls: list = []
    _install(monkeypatch, calls)

    await executor.request_execution("action-1", "balanced", "WH-01")

    assert calls == [
        {
            "url": "http://n8n.test/webhook/recovery/action",
            "json": {"action_id": "action-1", "plan_id": "balanced", "facility_id": "WH-01"},
        }
    ]


async def test_unreachable_n8n_does_not_raise(webhook_configured, monkeypatch):
    """Leave the action PENDING for collection instead of failing the handoff.

    Marking it FAILED here would claim an execution attempt that never reached
    the workflow.
    """
    _install(monkeypatch, [], error=httpx.ConnectError("connection refused"))

    await executor.request_execution("action-2", "balanced", "WH-01")


async def test_error_response_does_not_raise(webhook_configured, monkeypatch):
    """Treat a rejecting webhook the same way as an unreachable one."""
    _install(
        monkeypatch,
        [],
        error=httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://n8n.test"), response=httpx.Response(500)
        ),
    )

    await executor.request_execution("action-3", "balanced", "WH-01")


async def test_no_call_is_made_without_a_configured_webhook(monkeypatch):
    """Fall back to collection from GET /recovery-actions when n8n is unset."""
    monkeypatch.setattr(executor.settings, "n8n_recovery_webhook_url", None)
    calls: list = []
    _install(monkeypatch, calls)

    await executor.request_execution("action-4", "balanced", "WH-01")

    assert calls == []
