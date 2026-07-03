"""Integration-style tests for WsgiDAV write handling."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from wsgidav.wsgidav_app import WsgiDAVApp

from immich_bridge.immich_client import SearchPage
from immich_bridge.webdav_provider import ImmichProvider


TEST_ASSET = {
    "id": "asset-1",
    "originalFileName": "IMG_0001.jpg",
    "originalMimeType": "image/jpeg",
    "localDateTime": "2026-06-28T16:38:37.120Z",
    "fileCreatedAt": "2026-06-28T16:38:37.120Z",
    "fileModifiedAt": "2026-06-28T16:38:37.120Z",
    "updatedAt": "2026-06-28T16:40:00.000Z",
    "checksum": "checksum-1",
    "exifInfo": {"fileSizeInByte": 1234},
}


class IntegrationImmichClient:
    """Fake Immich client used behind a real WsgiDAV app."""

    def __init__(self) -> None:
        self.created_albums: list[str] = []
        self.uploads: list[dict[str, Any]] = []
        self.album_adds: list[tuple[str, str]] = []
        self.album_removes: list[tuple[str, str]] = []

    def list_albums(self) -> list[dict[str, Any]]:
        """Return one album."""
        return [
            {
                "id": "album-1",
                "albumName": "Vacation",
                "assetCount": 1,
                "updatedAt": "2026-06-29T00:00:00.000Z",
            }
        ]

    def search_assets(self, **kwargs: Any) -> SearchPage:
        """Return the test asset for album lookups."""
        if kwargs.get("album_ids") == ["album-1"]:
            return SearchPage(items=[TEST_ASSET], next_page=None, total=1)
        return SearchPage(items=[], next_page=None, total=0)

    def timeline_buckets(self, *, is_favorite: bool | None = None) -> list[dict[str, Any]]:
        """Return no timeline buckets."""
        return []

    def create_album(self, name: str) -> dict[str, Any]:
        """Record album creation."""
        self.created_albums.append(name)
        return {"id": "album-2", "albumName": name}

    def upload_asset(
        self,
        file_path: str | Path,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Record upload bytes."""
        self.uploads.append(
            {
                "filename": filename,
                "content_type": content_type,
                "data": Path(file_path).read_bytes(),
            }
        )
        return {"id": f"uploaded-{len(self.uploads)}", "status": "created"}

    def add_asset_to_album(self, album_id: str, asset_id: str) -> None:
        """Record album asset add."""
        self.album_adds.append((album_id, asset_id))

    def remove_asset_from_album(self, album_id: str, asset_id: str) -> None:
        """Record album asset removal."""
        self.album_removes.append((album_id, asset_id))


def make_app(client: IntegrationImmichClient) -> WsgiDAVApp:
    """Create a real WsgiDAV app backed by a fake Immich client."""
    provider = ImmichProvider("http://immich.test/api")
    provider._client = lambda environ: client  # type: ignore[method-assign]
    return WsgiDAVApp(
        {
            "provider_mapping": {"/": provider},
            "simple_dc": {"user_mapping": {"*": True}},
            "dir_browser": {"enable": False},
            "lock_storage": False,
            "verbose": 0,
            "logging": {"enable": False},
            "hotfixes": {
                "treat_root_options_as_asterisk": False,
                "re_encode_path_info": True,
                "unquote_path_info": False,
            },
        }
    )


def call_wsgi(
    app: WsgiDAVApp,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    content_type: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
) -> tuple[str, list[tuple[str, str]], bytes]:
    """Call a WSGI app and return status, headers, and body."""
    response: dict[str, Any] = {"status": "", "headers": []}
    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8081",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": content_type,
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    for name, value in (headers or {}).items():
        environ[f"HTTP_{name.upper().replace('-', '_')}"] = value

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        response["status"] = status
        response["headers"] = response_headers

    app_iter = app(environ, start_response)
    try:
        response_body = b"".join(app_iter)
    finally:
        close = getattr(app_iter, "close", None)
        if close is not None:
            close()

    return response["status"], response["headers"], response_body


def test_put_root_upload_flows_through_wsgidav() -> None:
    """PUT at root should upload to Immich and return Created."""
    client = IntegrationImmichClient()
    app = make_app(client)

    status, _headers, _body = call_wsgi(
        app,
        "PUT",
        "/IMG_0001.jpg",
        body=b"image-bytes",
        content_type="image/jpeg",
    )

    assert status.startswith("201 ")
    assert client.uploads == [
        {
            "filename": "IMG_0001.jpg",
            "content_type": "image/jpeg",
            "data": b"image-bytes",
        }
    ]
    assert client.album_adds == []


def test_mkcol_albums_flows_through_wsgidav() -> None:
    """MKCOL below Albums should create an Immich album."""
    client = IntegrationImmichClient()
    app = make_app(client)

    status, _headers, _body = call_wsgi(app, "MKCOL", "/Albums/New Album")

    assert status.startswith("201 ")
    assert client.created_albums == ["New Album"]


def test_put_album_upload_flows_through_wsgidav() -> None:
    """PUT below an album should upload and add album membership."""
    client = IntegrationImmichClient()
    app = make_app(client)

    status, _headers, _body = call_wsgi(
        app,
        "PUT",
        "/Albums/Vacation/IMG_0002.heic",
        body=b"heic-bytes",
        content_type="image/heic",
    )

    assert status.startswith("201 ")
    assert client.uploads[0]["filename"] == "IMG_0002.heic"
    assert client.uploads[0]["data"] == b"heic-bytes"
    assert client.album_adds == [("album-1", "uploaded-1")]


def test_unsafe_put_locations_return_forbidden_through_wsgidav() -> None:
    """PUT should fail in virtual directories that are not upload targets."""
    client = IntegrationImmichClient()
    app = make_app(client)

    for path in ("/Albums/IMG_0001.jpg", "/Timeline/IMG_0001.jpg", "/Favorites/IMG_0001.jpg"):
        status, _headers, _body = call_wsgi(
            app,
            "PUT",
            path,
            body=b"image-bytes",
            content_type="image/jpeg",
        )
        assert status.startswith("403 ")

    assert client.uploads == []
    assert client.album_adds == []


def test_delete_album_asset_flows_through_wsgidav() -> None:
    """DELETE on an album asset should remove album membership."""
    client = IntegrationImmichClient()
    app = make_app(client)

    status, _headers, _body = call_wsgi(
        app,
        "DELETE",
        "/Albums/Vacation/2026-06-28 16.38.37 IMG_0001--asset1.jpg",
    )

    assert status.startswith("204 ")
    assert client.album_removes == [("album-1", "asset-1")]
