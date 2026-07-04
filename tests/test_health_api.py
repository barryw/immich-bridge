"""Tests for public health endpoints."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from immich_bridge.app import create_app
from immich_bridge.config import Settings, get_settings


def make_client(tmp_path: Path) -> TestClient:
    """Create a FastAPI client backed by a temporary database."""
    settings = Settings(
        immich_url="http://immich.test/api",
        database_url=f"sqlite:///{tmp_path / 'bridge.db'}",
        redis_host=None,
        log_format="console",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_health_is_public_and_returns_200(tmp_path: Path) -> None:
    """Liveness should never require auth."""
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_ready_failure_returns_500(tmp_path: Path) -> None:
    """Readiness failures should be binary 200 or 500 responses."""
    client = make_client(tmp_path)

    with (
        patch("immich_bridge.api.health._check_immich", new=AsyncMock(return_value=False)),
        patch("immich_bridge.api.health._check_redis", new=AsyncMock(return_value=True)),
    ):
        response = client.get("/ready")

    assert response.status_code == 500
    assert response.json()["status"] == "not_ready"
