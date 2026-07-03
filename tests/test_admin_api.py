"""Tests for admin API sessions and configuration."""

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from immich_bridge.admin_store import get_admin_store
from immich_bridge.api.admin import SESSION_COOKIE
from immich_bridge.app import create_app
from immich_bridge.config import Settings, get_settings
from immich_bridge.immich_auth import ImmichIdentity


def make_client(tmp_path: Path) -> TestClient:
    """Create an admin API client backed by a temporary database."""
    get_admin_store.cache_clear()
    settings = Settings(
        immich_url="http://immich.test/api",
        database_url=f"sqlite:///{tmp_path / 'bridge.db'}",
        redis_host=None,
        log_format="console",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def admin_identity(*, is_admin: bool = True) -> ImmichIdentity:
    """Return a fake Immich identity."""
    return ImmichIdentity(
        user_id="user-1",
        email="barry@example.com",
        name="Barry",
        api_key_name="admin-ui",
        is_admin=is_admin,
    )


def login(client: TestClient) -> str:
    """Login and return bearer token from the API response."""
    with patch(
        "immich_bridge.api.admin.validate_immich_api_key",
        return_value=admin_identity(),
    ):
        response = client.post(
            "/api/admin/session",
            json={"username": "barry@example.com", "api_key": "key"},
        )
    assert response.status_code == 200
    assert response.cookies.get(SESSION_COOKIE)
    return str(response.json()["session_token"])


def test_admin_session_requires_immich_admin(tmp_path: Path) -> None:
    """Only Immich admin users should receive admin sessions."""
    client = make_client(tmp_path)

    with patch(
        "immich_bridge.api.admin.validate_immich_api_key",
        return_value=admin_identity(is_admin=False),
    ):
        response = client.post(
            "/api/admin/session",
            json={"username": "barry@example.com", "api_key": "key"},
        )

    assert response.status_code == 403


def test_admin_session_cookie_and_bearer_auth(tmp_path: Path) -> None:
    """Admin APIs should accept session cookies and bearer session tokens."""
    client = make_client(tmp_path)
    token = login(client)

    cookie_response = client.get("/api/admin/session")
    bearer_response = client.get(
        "/api/admin/session",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert cookie_response.status_code == 200
    assert cookie_response.json()["user"]["id"] == "user-1"
    assert bearer_response.status_code == 200
    assert bearer_response.json()["user"]["email"] == "barry@example.com"


def test_admin_views_crud_and_diagnostics(tmp_path: Path) -> None:
    """Saved views should be durable API resources."""
    client = make_client(tmp_path)
    login(client)

    create_response = client.post(
        "/api/admin/views",
        json={
            "name": "Kids",
            "description": "",
            "enabled": True,
            "layout": "date_buckets",
            "filters": {
                "album_ids": [],
                "person_ids": ["person-1"],
                "tag_ids": ["tag-1"],
                "is_favorite": True,
                "media_type": "IMAGE",
                "taken_after": None,
                "taken_before": None,
                "rating": None,
                "original_file_name": None,
                "ocr": None,
                "city": None,
                "country": None,
            },
        },
    )

    assert create_response.status_code == 201
    view_id = create_response.json()["id"]

    list_response = client.get("/api/admin/views")
    assert list_response.status_code == 200
    assert list_response.json()["views"][0]["name"] == "Kids"

    update_response = client.put(
        f"/api/admin/views/{view_id}",
        json={
            **create_response.json(),
            "name": "Kids Favorites",
            "layout": "flat",
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["layout"] == "flat"

    diagnostics = client.get("/api/admin/diagnostics")
    assert diagnostics.status_code == 200
    assert diagnostics.json()["view_count"] == 1

    delete_response = client.delete(f"/api/admin/views/{view_id}")
    assert delete_response.status_code == 204
    assert client.get("/api/admin/views").json()["views"] == []


def test_admin_mount_and_write_policy_updates(tmp_path: Path) -> None:
    """Mount settings and write policy should be mutable through JSON APIs."""
    client = make_client(tmp_path)
    login(client)

    mount = client.get("/api/admin/mount").json()
    mount["people_enabled"] = True
    mount["day_folder_split_threshold"] = 250
    mount_response = client.put("/api/admin/mount", json=mount)
    assert mount_response.status_code == 200
    assert mount_response.json()["people_enabled"] is True
    assert mount_response.json()["day_folder_split_threshold"] == 250

    policy = client.get("/api/admin/write-policy").json()
    policy["root_uploads"] = False
    policy_response = client.put("/api/admin/write-policy", json=policy)
    assert policy_response.status_code == 200
    assert policy_response.json()["root_uploads"] is False

    policy["permanent_delete"] = True
    rejected = client.put("/api/admin/write-policy", json=policy)
    assert rejected.status_code == 400
