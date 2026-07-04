"""Tests for admin API sessions and configuration."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from immich_bridge.admin_store import get_admin_store
from immich_bridge.api import admin as admin_api
from immich_bridge.api.admin import SESSION_COOKIE, _token_hash
from immich_bridge.app import create_app
from immich_bridge.authz import verify_grants_token
from immich_bridge.config import Settings, get_settings
from immich_bridge.immich_auth import ImmichIdentity
from immich_bridge.immich_client import ImmichApiError, SearchPage
from immich_bridge.share_auth import ShareIdentity


def make_client(tmp_path: Path, *, superadmin_password: str | None = None) -> TestClient:
    """Create an admin API client backed by a temporary database."""
    get_admin_store.cache_clear()
    settings = Settings(
        immich_url="http://immich.test/api",
        database_url=f"sqlite:///{tmp_path / 'bridge.db'}",
        redis_host=None,
        log_format="console",
        superadmin_username="root" if superadmin_password else None,
        superadmin_password=SecretStr(superadmin_password) if superadmin_password else None,
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


def superadmin_login(client: TestClient) -> str:
    """Login as local bridge superadmin and return bearer token."""
    response = client.post(
        "/api/admin/session",
        json={"username": "root", "api_key": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["principal"]["kind"] == "superadmin"
    assert response.json()["grants"][0]["scope"] == "instance"
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
    assert cookie_response.json()["principal"]["kind"] == "immich_admin"
    assert cookie_response.json()["grants"][0]["library_id"] == "default"
    assert "manage_views" in cookie_response.json()["grants"][0]["capabilities"]
    assert bearer_response.status_code == 200
    assert bearer_response.json()["user"]["email"] == "barry@example.com"
    grant_payload = verify_grants_token(
        "immich-bridge-dev-grant-secret-change-me",
        cookie_response.json()["grant_token"],
    )
    assert grant_payload is not None
    assert grant_payload["sub"] == "user-1"
    assert (
        verify_grants_token(
            "immich-bridge-dev-grant-secret-change-me",
            f"{cookie_response.json()['grant_token']}tampered",
        )
        is None
    )


def test_admin_libraries_are_discovered_from_session_grants(tmp_path: Path) -> None:
    """Admin login should grant access to configured libraries where the key is admin."""
    client = make_client(tmp_path)
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    store.ensure_default_library("http://immich.test/api")
    store.upsert_library(
        {
            "id": "work",
            "name": "Work Photos",
            "immich_url": "http://work-immich.test/api",
            "is_default": False,
        }
    )
    token = login(client)

    response = client.get("/api/admin/libraries", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert [library["id"] for library in response.json()["libraries"]] == ["default", "work"]


def test_library_admin_gets_default_library_mount(tmp_path: Path) -> None:
    """A library admin should see the library content as a mount."""
    client = make_client(tmp_path)
    token = login(client)

    response = client.get("/api/me/mounts", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["mounts"] == [
        {
            "id": "library:default",
            "kind": "library",
            "library_id": "default",
            "library_name": "Default Library",
            "display_name": "Default Library",
            "scope": "library",
            "capabilities": [
                "browse",
                "download",
                "thumbnail",
                "upload",
                "manage_views",
                "manage_policy",
                "manage_library",
                "diagnostics",
            ],
            "share_id": None,
            "asset_count": None,
            "expires_at": None,
        }
    ]


def test_superadmin_gets_one_mount_per_library(tmp_path: Path) -> None:
    """Superadmins should see every configured Immich library as a mount."""
    client = make_client(tmp_path, superadmin_password="secret")
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    store.ensure_default_library("http://immich.test/api")
    store.upsert_library(
        {
            "id": "work",
            "name": "Work Photos",
            "immich_url": "http://work-immich.test/api",
            "is_default": False,
        }
    )
    token = superadmin_login(client)

    response = client.get("/api/me/mounts", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert [mount["id"] for mount in response.json()["mounts"]] == [
        "library:default",
        "library:work",
    ]
    assert [mount["display_name"] for mount in response.json()["mounts"]] == [
        "Default Library",
        "Work Photos",
    ]


def test_legacy_admin_session_without_grants_gets_default_mount(tmp_path: Path) -> None:
    """Older admin sessions without stored grants should still expose the default mount."""
    client = make_client(tmp_path)
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    token = "legacy-token"
    now = datetime.now(UTC)
    store.create_session(
        {
            "token_hash": _token_hash(token),
            "principal_id": "user-1",
            "principal_kind": "library_admin",
            "user_id": "user-1",
            "email": "barry@example.com",
            "name": "Barry",
            "api_key_name": "admin-ui",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "last_seen_at": now.isoformat(),
        }
    )

    mounts_response = client.get("/api/me/mounts", headers={"Authorization": f"Bearer {token}"})
    me_response = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert mounts_response.status_code == 200
    assert [mount["id"] for mount in mounts_response.json()["mounts"]] == ["library:default"]
    assert me_response.status_code == 200
    assert me_response.json()["grants"][0]["library_id"] == "default"


def test_superadmin_can_create_and_update_libraries(tmp_path: Path) -> None:
    """Local bridge superadmins should manage configured libraries."""
    client = make_client(tmp_path, superadmin_password="secret")
    token = superadmin_login(client)

    create_response = client.post(
        "/api/admin/libraries",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "family",
            "name": "Family Photos",
            "immich_url": "https://pics.example.test/api/",
            "public_url": "https://pics.example.test/",
            "share_hosts": ["share.example.test", "share.example.test", "HTTPS://OLD.EXAMPLE.TEST"],
            "is_default": False,
        },
    )
    assert create_response.status_code == 201
    assert create_response.json()["id"] == "family"
    assert create_response.json()["immich_url"] == "https://pics.example.test/api"
    assert create_response.json()["public_url"] == "https://pics.example.test"
    assert create_response.json()["share_hosts"] == [
        "share.example.test",
        "https://old.example.test",
    ]

    update_response = client.put(
        "/api/admin/libraries/family",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Family Archive",
            "immich_url": "https://archive.example.test/api",
            "public_url": "https://archive-public.example.test",
            "share_hosts": ["archive-share.example.test:8443"],
            "is_default": False,
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Family Archive"
    assert update_response.json()["public_url"] == "https://archive-public.example.test"
    assert update_response.json()["share_hosts"] == ["archive-share.example.test:8443"]
    libraries = client.get(
        "/api/admin/libraries",
        headers={"Authorization": f"Bearer {token}"},
    ).json()["libraries"]
    assert {library["id"] for library in libraries} == {"default", "family"}


def test_library_admin_cannot_create_libraries(tmp_path: Path) -> None:
    """Immich library admins should not configure arbitrary upstream libraries."""
    client = make_client(tmp_path)
    token = login(client)

    response = client.post(
        "/api/admin/libraries",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "blocked",
            "name": "Blocked",
            "immich_url": "https://blocked.example.test/api",
        },
    )

    assert response.status_code == 403


def test_share_link_login_creates_share_guest_mount(tmp_path: Path) -> None:
    """Shared links should create share-scoped, non-admin sessions."""
    client = make_client(tmp_path)
    with patch(
        "immich_bridge.api.auth.validate_immich_share_link",
        return_value=ShareIdentity(
            share_id="share-1",
            name="Summer Trip",
            description="Summer Trip",
            allow_download=True,
            allow_upload=False,
            expires_at=None,
            asset_count=23,
            album_id="album-1",
        ),
    ) as validate_share:
        response = client.post(
            "/api/auth/share-link",
            json={"share_url": "http://immich.test/share/share-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    token = payload["session_token"]
    assert payload["principal"]["kind"] == "share_guest"
    assert payload["grants"][0]["scope"] == "share"
    assert payload["grants"][0]["library_id"] == "default"
    assert payload["grants"][0]["share_name"] == "Summer Trip"
    assert "browse" in payload["grants"][0]["capabilities"]
    assert "download" in payload["grants"][0]["capabilities"]
    assert "manage_views" not in payload["grants"][0]["capabilities"]
    assert "share-secret" not in str(payload["grants"])
    assert (
        verify_grants_token(
            "immich-bridge-dev-grant-secret-change-me",
            payload["grant_token"],
        )
        is not None
    )
    validate_share.assert_called_once_with(
        "http://immich.test/api",
        "share-secret",
        timeout_seconds=10.0,
    )

    mounts = client.get("/api/me/mounts", headers={"Authorization": f"Bearer {token}"})
    assert mounts.status_code == 200
    assert mounts.json()["mounts"] == [
        {
            "id": "share:default:share-1",
            "kind": "share",
            "library_id": "default",
            "library_name": "Default Library",
            "display_name": "Summer Trip",
            "scope": "share",
            "capabilities": ["browse", "thumbnail", "download"],
            "share_id": "share-1",
            "asset_count": 23,
            "expires_at": None,
        }
    ]

    assert (
        client.get("/api/admin/views", headers={"Authorization": f"Bearer {token}"}).status_code
        == 403
    )
    assert client.get(
        "/api/admin/libraries", headers={"Authorization": f"Bearer {token}"}
    ).json() == {"libraries": []}


def test_viewer_session_can_add_multiple_share_mounts(tmp_path: Path) -> None:
    """Viewer sessions should be able to attach multiple share links as mounts."""
    client = make_client(tmp_path)
    with patch(
        "immich_bridge.api.auth.validate_immich_share_link",
        side_effect=[
            ShareIdentity(
                share_id="share-1",
                name="Summer Trip",
                description=None,
                allow_download=True,
                allow_upload=False,
                expires_at=None,
                asset_count=23,
                album_id="album-1",
            ),
            ShareIdentity(
                share_id="share-2",
                name="Winter Trip",
                description=None,
                allow_download=True,
                allow_upload=True,
                expires_at=None,
                asset_count=12,
                album_id="album-2",
            ),
        ],
    ) as validate_share:
        first = client.post(
            "/api/auth/share-link",
            json={"share_url": "http://immich.test/share/share-one"},
        )
        token = first.json()["session_token"]
        second = client.post(
            "/api/auth/session/share-link",
            headers={"Authorization": f"Bearer {token}"},
            json={"share_url": "http://immich.test/share/share-two"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["session_token"] is None
    assert second.json()["principal"]["kind"] == "share_guest"
    assert [grant["share_name"] for grant in second.json()["grants"]] == [
        "Summer Trip",
        "Winter Trip",
    ]
    assert validate_share.call_count == 2

    mounts = client.get("/api/me/mounts", headers={"Authorization": f"Bearer {token}"})
    assert mounts.status_code == 200
    assert [mount["id"] for mount in mounts.json()["mounts"]] == [
        "share:default:share-1",
        "share:default:share-2",
    ]
    assert [mount["display_name"] for mount in mounts.json()["mounts"]] == [
        "Summer Trip",
        "Winter Trip",
    ]
    assert mounts.json()["mounts"][1]["capabilities"] == [
        "browse",
        "thumbnail",
        "download",
        "upload",
    ]


def test_share_link_login_accepts_public_url_for_internal_library_url(tmp_path: Path) -> None:
    """Public share-link URLs should map to the library's internal API URL."""
    client = make_client(tmp_path)
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    store.upsert_library(
        {
            "id": "default",
            "name": "Default Library",
            "immich_url": "http://immich.svc.cluster.local/api",
            "public_url": "https://pics.example.test",
            "is_default": True,
        }
    )

    with patch(
        "immich_bridge.api.auth.validate_immich_share_link",
        return_value=ShareIdentity(
            share_id="share-1",
            name="Shared Album",
            description=None,
            allow_download=True,
            allow_upload=False,
            expires_at=None,
            asset_count=2,
            album_id="album-1",
        ),
    ) as validate_share:
        response = client.post(
            "/api/auth/share-link",
            json={"share_url": "https://pics.example.test/share/share-secret"},
        )

    assert response.status_code == 200
    assert response.json()["grants"][0]["library_id"] == "default"
    validate_share.assert_called_once_with(
        "http://immich.svc.cluster.local/api",
        "share-secret",
        timeout_seconds=10.0,
    )


def test_share_link_login_accepts_allowed_share_host(tmp_path: Path) -> None:
    """Allowed share hosts should support public aliases without changing the API URL."""
    client = make_client(tmp_path)
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    store.upsert_library(
        {
            "id": "default",
            "name": "Default Library",
            "immich_url": "http://immich.svc.cluster.local/api",
            "share_hosts": ["pics.example.test"],
            "is_default": True,
        }
    )

    with patch(
        "immich_bridge.api.auth.validate_immich_share_link",
        return_value=ShareIdentity(
            share_id="share-1",
            name="Shared Album",
            description=None,
            allow_download=True,
            allow_upload=False,
            expires_at=None,
            asset_count=2,
            album_id="album-1",
        ),
    ) as validate_share:
        response = client.post(
            "/api/auth/share-link",
            json={"share_url": "https://pics.example.test/share/share-secret"},
        )

    assert response.status_code == 200
    validate_share.assert_called_once_with(
        "http://immich.svc.cluster.local/api",
        "share-secret",
        timeout_seconds=10.0,
    )


def test_share_link_login_rejects_unconfigured_hosts(tmp_path: Path) -> None:
    """The bridge must not proxy shared links from arbitrary Immich hosts."""
    client = make_client(tmp_path)

    with patch("immich_bridge.api.auth.validate_immich_share_link") as validate_share:
        response = client.post(
            "/api/auth/share-link",
            json={"share_url": "https://other.example.test/share/share-secret"},
        )

    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]
    validate_share.assert_not_called()


def test_library_scoped_views_do_not_bleed_between_libraries(tmp_path: Path) -> None:
    """Saved views should be scoped by library id."""
    client = make_client(tmp_path)
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    store.ensure_default_library("http://immich.test/api")
    store.upsert_library(
        {
            "id": "work",
            "name": "Work Photos",
            "immich_url": "http://work-immich.test/api",
            "is_default": False,
        }
    )
    login(client)

    payload = {
        "name": "Favorites",
        "description": "",
        "enabled": True,
        "layout": "flat",
        "filters": {
            "album_ids": [],
            "person_ids": [],
            "tag_ids": [],
            "is_favorite": True,
            "media_type": None,
            "taken_after": None,
            "taken_before": None,
            "rating": None,
            "query": None,
            "original_file_name": None,
            "ocr": None,
            "city": None,
            "state": None,
            "country": None,
        },
    }

    default_response = client.post("/api/admin/views", json=payload)
    work_response = client.post("/api/admin/libraries/work/views", json=payload)

    assert default_response.status_code == 201
    assert work_response.status_code == 201
    assert default_response.json()["id"] != work_response.json()["id"]
    assert [view["id"] for view in client.get("/api/admin/views").json()["views"]] == [
        default_response.json()["id"]
    ]
    assert [
        view["id"] for view in client.get("/api/admin/libraries/work/views").json()["views"]
    ] == [work_response.json()["id"]]


def test_library_scoped_policy_does_not_change_default_library(tmp_path: Path) -> None:
    """Mount/write policy updates should be library-scoped."""
    client = make_client(tmp_path)
    store = get_admin_store(f"sqlite:///{tmp_path / 'bridge.db'}")
    store.ensure_default_library("http://immich.test/api")
    store.upsert_library(
        {
            "id": "work",
            "name": "Work Photos",
            "immich_url": "http://work-immich.test/api",
            "is_default": False,
        }
    )
    login(client)

    work_mount = client.get("/api/admin/libraries/work/mount").json()
    work_mount["people_enabled"] = True
    assert client.put("/api/admin/libraries/work/mount", json=work_mount).status_code == 200

    assert client.get("/api/admin/libraries/work/mount").json()["people_enabled"] is True
    assert client.get("/api/admin/mount").json()["people_enabled"] is False


def test_admin_views_crud_and_diagnostics(tmp_path: Path) -> None:
    """Saved views should be durable API resources."""
    client = make_client(tmp_path)
    login(client)

    with patch("immich_bridge.api.admin._count_matching_assets", return_value=12):
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
    assert create_response.json()["match_count"] == 12

    with patch("immich_bridge.api.admin._count_matching_assets", return_value=12):
        list_response = client.get("/api/admin/views")
    assert list_response.status_code == 200
    assert list_response.json()["views"][0]["name"] == "Kids"
    assert list_response.json()["views"][0]["match_count"] == 12

    with patch("immich_bridge.api.admin._count_matching_assets") as count_assets:
        fast_list_response = client.get("/api/admin/views?include_counts=false")
    assert fast_list_response.status_code == 200
    assert fast_list_response.json()["views"][0]["match_count"] is None
    count_assets.assert_not_called()

    with patch("immich_bridge.api.admin._count_matching_assets", return_value=4):
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
    assert update_response.json()["match_count"] == 4

    diagnostics = client.get("/api/admin/diagnostics")
    assert diagnostics.status_code == 200
    assert diagnostics.json()["view_count"] == 1

    delete_response = client.delete(f"/api/admin/views/{view_id}")
    assert delete_response.status_code == 204
    with patch("immich_bridge.api.admin._count_matching_assets", return_value=0):
        assert client.get("/api/admin/views").json()["views"] == []


def test_admin_views_remain_editable_without_cached_api_key(tmp_path: Path) -> None:
    """Saved views should not require the in-memory API key cache for CRUD."""
    client = make_client(tmp_path)
    login(client)

    create_response = client.post(
        "/api/admin/views",
        json={
            "name": "Trips",
            "description": "",
            "enabled": True,
            "layout": "flat",
            "filters": {
                "album_ids": [],
                "person_ids": [],
                "tag_ids": [],
                "is_favorite": None,
                "media_type": None,
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

    with (
        patch("immich_bridge.api.admin._get_admin_api_key", return_value=None),
        patch("immich_bridge.api.admin._count_matching_assets") as count_assets,
    ):
        list_response = client.get("/api/admin/views")
        update_response = client.put(
            f"/api/admin/views/{view_id}",
            json={**create_response.json(), "description": "Vacation folders"},
        )

    assert list_response.status_code == 200
    assert list_response.json()["views"][0]["match_count"] is None
    assert update_response.status_code == 200
    assert update_response.json()["match_count"] is None
    count_assets.assert_not_called()


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


def test_admin_options_and_match_count_use_immich_api(tmp_path: Path) -> None:
    """Admin API should expose Immich tags, people, and saved-view counts."""
    client = make_client(tmp_path)
    login(client)

    with patch("immich_bridge.api.admin.ImmichClient") as fake_client:
        instance = fake_client.return_value
        instance.list_tags.return_value = [
            {"id": "tag-1", "name": "Family", "color": "#00aa99", "assetCount": 5}
        ]
        instance.list_people.return_value = [{"id": "person-1", "name": "Alice", "assetCount": 7}]
        instance.count_assets.return_value = 11

        tags = client.get("/api/admin/options/tags")
        people = client.get("/api/admin/options/people")
        count = client.post(
            "/api/admin/views/match-count",
            json={
                "filters": {
                    "album_ids": [],
                    "person_ids": ["person-1"],
                    "tag_ids": ["tag-1"],
                    "is_favorite": None,
                    "media_type": None,
                    "taken_after": None,
                    "taken_before": None,
                    "rating": None,
                    "query": "beach",
                    "original_file_name": None,
                    "ocr": None,
                    "city": "Los Angeles",
                    "state": "California",
                    "country": "USA",
                }
            },
        )
        scoped_count = client.post(
            "/api/admin/libraries/default/views/match-count",
            json={
                "filters": {
                    "album_ids": [],
                    "person_ids": [],
                    "tag_ids": [],
                    "is_favorite": None,
                    "media_type": None,
                    "taken_after": None,
                    "taken_before": None,
                    "rating": None,
                    "query": "beach",
                    "original_file_name": None,
                    "ocr": None,
                    "city": "Los Angeles",
                    "state": "California",
                    "country": "USA",
                }
            },
        )

    assert tags.status_code == 200
    assert tags.json()["items"][0]["name"] == "Family"
    assert people.status_code == 200
    assert people.json()["items"][0]["name"] == "Alice"
    assert count.status_code == 200
    assert count.json()["count"] == 11
    assert scoped_count.status_code == 200
    assert scoped_count.json()["count"] == 11
    assert instance.count_assets.call_count == 2
    assert instance.count_assets.call_args.kwargs["query"] == "beach"
    assert instance.count_assets.call_args.kwargs["city"] == "Los Angeles"
    assert instance.count_assets.call_args.kwargs["state"] == "California"
    assert instance.count_assets.call_args.kwargs["country"] == "USA"
    instance.search_assets.assert_not_called()


def test_admin_match_count_fallback_ignores_deprecated_metadata_total(tmp_path: Path) -> None:
    """Metadata-search total should not cap counts at the page size."""
    client = make_client(tmp_path)
    login(client)

    with patch("immich_bridge.api.admin.ImmichClient") as fake_client:
        instance = fake_client.return_value
        instance.count_assets.side_effect = ImmichApiError("not found", status_code=404)
        instance.search_assets.side_effect = [
            SearchPage(
                items=[{"id": f"asset-{index}"} for index in range(500)],
                next_page="2",
                total=500,
            ),
            SearchPage(items=[{"id": "asset-500"}], next_page=None, total=500),
        ]

        count = client.post(
            "/api/admin/views/match-count",
            json={
                "filters": {
                    "album_ids": [],
                    "person_ids": [],
                    "tag_ids": [],
                    "is_favorite": None,
                    "media_type": None,
                    "taken_after": None,
                    "taken_before": None,
                    "rating": None,
                    "query": "beach",
                    "original_file_name": None,
                    "ocr": None,
                    "city": None,
                    "state": None,
                    "country": None,
                }
            },
        )

    assert count.status_code == 200
    assert count.json()["count"] == 501
    instance.count_assets.assert_called_once()
    assert instance.search_assets.call_count == 2
    assert instance.search_assets.call_args_list[0].kwargs["size"] == 500
    assert instance.search_assets.call_args_list[0].kwargs["with_exif"] is False


def test_admin_context_recovers_api_key_from_shared_cache(tmp_path: Path) -> None:
    """A valid session should survive a process-local admin API key cache miss."""
    client = make_client(tmp_path)
    token = login(client)

    with admin_api._admin_api_keys_lock:
        admin_api._admin_api_keys.pop(_token_hash(token), None)

    with patch("immich_bridge.api.admin.ImmichClient") as fake_client:
        instance = fake_client.return_value
        instance.list_tags.return_value = []

        response = client.get(
            "/api/admin/options/tags",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    fake_client.assert_called_once()
    assert fake_client.call_args.kwargs["api_key"] == "key"
