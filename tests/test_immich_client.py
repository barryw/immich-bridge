"""Tests for the Immich API client."""

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
