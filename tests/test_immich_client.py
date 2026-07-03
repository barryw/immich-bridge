"""Tests for the Immich API client."""

import json
from typing import Any
from unittest.mock import patch

import httpx

from immich_bridge.blob_cache import BlobCache
from immich_bridge.immich_client import ImmichClient


def test_original_stream_reuses_stream_client_for_range_seek() -> None:
    """Range seeks should reopen responses without closing the shared stream client."""
    requests: list[httpx.Request] = []
    body = b"abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers.get("range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(
                206,
                headers={
                    "content-length": str(len(body) - start),
                    "content-range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                },
                content=body[start:],
            )
        return httpx.Response(
            200,
            headers={"content-length": str(len(body))},
            content=body,
        )

    stream_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
        stream_http_client=stream_http_client,
    )

    stream = client.open_original("asset-1")
    assert stream.read(2) == b"ab"
    assert stream.seek(3) == 3
    assert stream.read(2) == b"de"
    stream.close()
    client.close()

    assert [request.url.path for request in requests] == [
        "/api/assets/asset-1/original",
        "/api/assets/asset-1/original",
    ]
    assert requests[0].headers["x-api-key"] == "api-key"
    assert requests[1].headers["range"] == "bytes=3-"
    assert stream_http_client.is_closed is False
    stream_http_client.close()


def test_original_stream_can_start_at_request_range() -> None:
    """Opening at a known range start should avoid a second seek-time upstream open."""
    requests: list[httpx.Request] = []
    body = b"abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers.get("range")
        start = int(range_header.removeprefix("bytes=").removesuffix("-"))
        return httpx.Response(
            206,
            headers={
                "content-length": str(len(body) - start),
                "content-range": f"bytes {start}-{len(body) - 1}/{len(body)}",
            },
            content=body[start:],
        )

    stream_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
        stream_http_client=stream_http_client,
    )

    stream = client.open_original("asset-1", range_start=3)
    assert stream.tell() == 3
    assert stream.seek(3) == 3
    assert stream.read(2) == b"de"
    stream.close()
    client.close()

    assert len(requests) == 1
    assert requests[0].headers["range"] == "bytes=3-"
    stream_http_client.close()


def test_original_stream_caches_bounded_ranges(tmp_path) -> None:
    """Repeated bounded range requests should be served from the blob cache."""
    requests: list[httpx.Request] = []
    body = b"abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers.get("range")
        assert range_header == "bytes=1-3"
        return httpx.Response(
            206,
            headers={
                "content-length": "3",
                "content-range": f"bytes 1-3/{len(body)}",
            },
            content=body[1:4],
        )

    stream_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = BlobCache(
        tmp_path,
        max_bytes=1024,
        max_range_bytes=16,
        ttl_seconds=60,
    )
    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
        stream_http_client=stream_http_client,
        blob_cache=cache,
        blob_cache_namespace="test",
    )

    first = client.open_original("asset-1", range_start=1, range_end=3)
    assert first.tell() == 1
    assert first.seek(1) == 1
    assert first.read() == b"bcd"
    first.close()

    second = client.open_original("asset-1", range_start=1, range_end=3)
    assert second.tell() == 1
    assert second.read(2) == b"bc"
    assert second.read() == b"d"
    second.close()
    client.close()
    stream_http_client.close()

    assert len(requests) == 1


def test_original_stream_bypasses_cache_for_large_ranges(tmp_path) -> None:
    """Ranges above the configured cache max should stream from Immich."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            206,
            headers={"content-length": "4", "content-range": "bytes 0-3/4"},
            content=b"abcd",
        )

    stream_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = BlobCache(
        tmp_path,
        max_bytes=1024,
        max_range_bytes=2,
        ttl_seconds=60,
    )
    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
        stream_http_client=stream_http_client,
        blob_cache=cache,
        blob_cache_namespace="test",
    )

    first = client.open_original("asset-1", range_start=0, range_end=3)
    assert first.read() == b"abcd"
    first.close()
    second = client.open_original("asset-1", range_start=0, range_end=3)
    assert second.read() == b"abcd"
    second.close()
    client.close()
    stream_http_client.close()

    assert [request.headers["range"] for request in requests] == ["bytes=0-3", "bytes=0-3"]
    assert list(tmp_path.iterdir()) == []


def test_original_stream_metrics_logs_open_and_close_events() -> None:
    """Metrics mode should emit upstream open and aggregate stream details."""
    events: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "3", "content-type": "image/jpeg"},
            content=b"abc",
        )

    stream_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
        metrics_enabled=True,
        stream_http_client=stream_http_client,
    )

    with patch(
        "immich_bridge.immich_client.logger.info",
        side_effect=lambda event, **fields: events.append((event, fields)),
    ):
        stream = client.open_original("asset-1")
        assert stream.read(1) == b"a"
        stream.close()
    client.close()
    stream_http_client.close()

    assert [event for event, _ in events] == [
        "immich_original_stream_open",
        "immich_original_stream_metrics",
    ]
    assert events[0][1]["response_content_length"] == 3
    assert events[1][1]["bytes_read"] == 1
    assert events[1][1]["read_calls"] == 1


def test_create_album_add_and_remove_asset_send_expected_requests() -> None:
    """Album write helpers should use Immich album APIs and invalidate caches."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/api/albums":
            return httpx.Response(201, json={"id": "album-2"})
        if request.method == "PUT" and request.url.path == "/api/albums/album-2/assets":
            return httpx.Response(200, json=[{"id": "asset-1", "success": True}])
        if request.method == "DELETE" and request.url.path == "/api/albums/album-2/assets":
            return httpx.Response(200, json=[{"id": "asset-1", "success": True}])
        return httpx.Response(404)

    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
    )
    client._http_client.close()
    client._http_client = httpx.Client(transport=httpx.MockTransport(handler))

    with patch("immich_bridge.immich_client.get_cache") as mock_get_cache:
        cache = mock_get_cache.return_value
        assert client.create_album("New Album") == {"id": "album-2"}
        assert client.add_asset_to_album("album-2", "asset-1") == [
            {"id": "asset-1", "success": True}
        ]
        assert client.remove_asset_from_album("album-2", "asset-1") == [
            {"id": "asset-1", "success": True}
        ]

    client.close()

    assert [request.method for request in requests] == ["POST", "PUT", "DELETE"]
    assert json.loads(requests[0].content) == {"albumName": "New Album", "assetIds": []}
    assert json.loads(requests[1].content) == {"ids": ["asset-1"]}
    assert json.loads(requests[2].content) == {"ids": ["asset-1"]}
    assert cache.delete_prefix.call_count == 3
    cache.delete_prefix.assert_called_with("immich:user-1:")


def test_upload_asset_sends_multipart_and_returns_asset_id(tmp_path) -> None:
    """Asset uploads should use Immich multipart API without retrying."""
    upload_file = tmp_path / "IMG_0001.jpg"
    upload_file.write_bytes(b"image-bytes")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/assets"
        assert request.headers["x-api-key"] == "api-key"
        assert request.headers["x-immich-checksum"]
        assert request.headers["content-type"].startswith("multipart/form-data;")
        body = request.content
        assert b'name="deviceAssetId"' in body
        assert b"immich-bridge:" in body
        assert b'name="deviceId"' in body
        assert b"immich-bridge-webdav" in body
        assert b'name="assetData"; filename="IMG_0001.jpg"' in body
        assert b"image-bytes" in body
        return httpx.Response(201, json={"id": "asset-1", "status": "created"})

    client = ImmichClient(
        base_url="http://immich.test/api",
        api_key="api-key",
        user_scope="user-1",
    )
    client._http_client.close()
    client._http_client = httpx.Client(transport=httpx.MockTransport(handler))

    with patch("immich_bridge.immich_client.get_cache") as mock_get_cache:
        assert client.upload_asset(
            upload_file,
            filename="IMG_0001.jpg",
            content_type="image/jpeg",
        ) == {"id": "asset-1", "status": "created"}

    client.close()

    assert len(requests) == 1
    mock_get_cache.return_value.delete_prefix.assert_called_once_with("immich:user-1:")
