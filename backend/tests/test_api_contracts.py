"""Test the backend API surface consumed by the frontend and n8n."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.config import Settings
from app.main import app


def test_required_routes_are_published_under_api_prefix():
    """Expose every documented Member B route under the configured prefix."""
    paths = app.openapi()["paths"]

    assert {
        "/api/dashboard",
        "/api/orders",
        "/api/orders/at-risk",
        "/api/events/orders",
        "/api/events/fulfillment",
        "/api/events/order-status",
        "/api/simulations",
        "/api/recovery-plans",
        "/api/recovery-plans/{plan_id}/approve",
        "/api/recovery-actions/{action_id}",
        "/api/recovery-actions/{action_id}/status",
    }.issubset(paths)


def test_frontend_contracts_have_expected_methods_and_response_models():
    """Publish the methods and response models expected by Member A."""
    paths = app.openapi()["paths"]

    assert "get" in paths["/api/dashboard"]
    assert paths["/api/dashboard"]["get"]["responses"]["200"]["content"]
    assert "get" in paths["/api/orders"]
    assert "get" in paths["/api/orders/at-risk"]
    assert "post" in paths["/api/simulations"]
    assert "get" in paths["/api/recovery-plans"]
    assert "post" in paths["/api/recovery-plans/{plan_id}/approve"]


def test_health_is_available_without_database():
    """Keep the liveness endpoint independent from PostgreSQL."""
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_cors_accepts_multiple_configured_origins():
    """Parse comma-separated frontend origins without breaking one-origin setups."""
    settings = Settings(frontend_origin="http://localhost:5173, https://demo.example.com")

    assert settings.allowed_frontend_origins == [
        "http://localhost:5173",
        "https://demo.example.com",
    ]
