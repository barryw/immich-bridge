"""Small synchronous Immich API client used by the WebDAV provider."""

from __future__ import annotations

import atexit
import base64
import hashlib
import io
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from immich_bridge.blob_cache import BlobCache, CachedByteRange
from immich_bridge.cache import DEFAULT_TTL, get_cache
from immich_bridge.logging import get_logger
from immich_bridge.observability import get_request_id

logger = get_logger(__name__)
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_REQUEST_ATTEMPTS = 3
STALE_CACHE_TTL_SECONDS = 86_400
STREAM_MAX_CONNECTIONS = 64
STREAM_MAX_KEEPALIVE_CONNECTIONS = 16


class ImmichApiError(Exception):
    """Raised when Immich returns an upstream error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize an API error."""
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SearchPage:
    """A page of Immich metadata search results."""

    items: list[dict[str, Any]]
    next_page: str | None
    total: int | None


@dataclass(frozen=True)
class StreamClientPoolKey:
    """Connection-pool key for upstream original asset streams."""

    base_url: str
    timeout_seconds: float


_stream_clients: dict[StreamClientPoolKey, httpx.Client] = {}
_stream_clients_lock = threading.Lock()


def _shared_stream_client(base_url: str, timeout_seconds: float) -> httpx.Client:
    """Return a process-shared HTTP client for original asset streams."""
    key = StreamClientPoolKey(base_url=base_url.rstrip("/"), timeout_seconds=timeout_seconds)
    with _stream_clients_lock:
        client = _stream_clients.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=STREAM_MAX_CONNECTIONS,
                    max_keepalive_connections=STREAM_MAX_KEEPALIVE_CONNECTIONS,
                ),
            )
            _stream_clients[key] = client
        return client


def _close_shared_stream_clients() -> None:
    """Close process-shared stream clients during interpreter shutdown."""
    with _stream_clients_lock:
        clients = list(_stream_clients.values())
        _stream_clients.clear()
    for client in clients:
        client.close()


atexit.register(_close_shared_stream_clients)


def _parse_optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _file_sha1_base64(path: Path) -> str:
    """Return base64-encoded SHA-1 digest for an upload file."""
    digest = hashlib.sha1()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _utc_iso_from_timestamp(timestamp: float) -> str:
    """Return an Immich-compatible UTC ISO timestamp."""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class OriginalResponseFactory(Protocol):
    """Factory that opens an Immich original response at a byte offset."""

    def __call__(
        self,
        range_start: int | None = None,
        range_end: int | None = None,
    ) -> httpx.Response: ...


class CachedRangeStream(io.RawIOBase):
    """File-like wrapper for a cached byte range using absolute offsets."""

    def __init__(
        self,
        cached_range: CachedByteRange,
        *,
        asset_id: str,
        metrics_enabled: bool,
    ) -> None:
        """Initialize the stream."""
        super().__init__()
        self._range = cached_range
        self._asset_id = asset_id
        self._metrics_enabled = metrics_enabled
        self._position = cached_range.start
        self._read_calls = 0
        self._bytes_read = 0
        self._opened_at = time.perf_counter()

    def readable(self) -> bool:
        """Return whether the stream supports reading."""
        return True

    def seekable(self) -> bool:
        """Return whether the stream supports seeking."""
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Seek within the cached absolute byte range."""
        if whence != io.SEEK_SET:
            raise OSError("Only absolute seeks are supported")
        if offset < self._range.start or offset > self._range.end + 1:
            raise OSError("Seek outside cached byte range")
        self._position = offset
        return self._position

    def tell(self) -> int:
        """Return the current absolute stream position."""
        return self._position

    def read(self, size: int = -1) -> bytes:
        """Read from the cached range."""
        if self.closed:
            return b""

        relative_start = self._position - self._range.start
        if relative_start >= len(self._range.data):
            return b""

        if size is None or size < 0:
            data = self._range.data[relative_start:]
        else:
            data = self._range.data[relative_start : relative_start + size]

        self._position += len(data)
        self._read_calls += 1
        self._bytes_read += len(data)
        return data

    def close(self) -> None:
        """Close the cached stream and emit optional metrics."""
        if self.closed:
            return
        if self._metrics_enabled:
            logger.info(
                "immich_cached_range_stream_metrics",
                request_id=get_request_id(),
                asset_id=str(self._asset_id)[:8],
                range_start=self._range.start,
                range_end=self._range.end,
                bytes_read=self._bytes_read,
                read_calls=self._read_calls,
                elapsed_ms=round((time.perf_counter() - self._opened_at) * 1000, 2),
            )
        super().close()


class ImmichOriginalStream(io.RawIOBase):
    """File-like wrapper around an httpx streaming response."""

    def __init__(
        self,
        response: httpx.Response,
        response_factory: OriginalResponseFactory,
        *,
        asset_id: str,
        metrics_enabled: bool,
        initial_position: int = 0,
        range_end: int | None = None,
    ) -> None:
        """Initialize the stream wrapper."""
        super().__init__()
        self._response = response
        self._response_factory = response_factory
        self._iterator = response.iter_bytes()
        self._buffer = bytearray()
        self._position = initial_position
        self._started_reading = False
        self._asset_id = asset_id
        self._metrics_enabled = metrics_enabled
        self._range_end = range_end
        self._opened_at = time.perf_counter()
        self._read_calls = 0
        self._bytes_read = 0
        self._seek_count = 0
        self._first_byte_ms: float | None = None

    def readable(self) -> bool:
        """Return whether the stream supports reading."""
        return True

    def seekable(self) -> bool:
        """Return whether the stream supports seeking."""
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Seek by reopening the upstream response with a Range header."""
        if whence != io.SEEK_SET:
            raise OSError("Only absolute seeks are supported")
        if offset < 0:
            raise OSError("Negative seeks are not supported")
        if offset == self._position and not self._started_reading:
            return self._position

        self._response.close()
        self._response = self._response_factory(offset, self._range_end)
        self._iterator = self._response.iter_bytes()
        self._buffer.clear()
        self._position = offset
        self._started_reading = False
        self._seek_count += 1
        return self._position

    def tell(self) -> int:
        """Return the current stream position."""
        return self._position

    def read(self, size: int = -1) -> bytes:
        """Read bytes from the upstream response."""
        if self.closed:
            return b""
        self._started_reading = True
        self._read_calls += 1

        if size is None or size < 0:
            chunks = [bytes(self._buffer)]
            self._buffer.clear()
            chunks.extend(self._iterator)
            data = b"".join(chunks)
            self._position += len(data)
            self._record_read(len(data))
            return data

        while len(self._buffer) < size:
            try:
                self._buffer.extend(next(self._iterator))
            except StopIteration:
                break

        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        self._position += len(data)
        self._record_read(len(data))
        return data

    def _record_read(self, byte_count: int) -> None:
        """Track stream metrics for a completed read call."""
        self._bytes_read += byte_count
        if byte_count and self._first_byte_ms is None:
            self._first_byte_ms = round((time.perf_counter() - self._opened_at) * 1000, 2)

    def close(self) -> None:
        """Close the upstream response."""
        if self.closed:
            return
        self._response.close()
        if self._metrics_enabled:
            logger.info(
                "immich_original_stream_metrics",
                request_id=get_request_id(),
                asset_id=str(self._asset_id)[:8],
                range_end=self._range_end,
                bytes_read=self._bytes_read,
                read_calls=self._read_calls,
                seek_count=self._seek_count,
                final_position=self._position,
                first_byte_ms=self._first_byte_ms,
                elapsed_ms=round((time.perf_counter() - self._opened_at) * 1000, 2),
            )
        super().close()


class ImmichClient:
    """Synchronous Immich API helper scoped to one authenticated DAV request."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_scope: str,
        timeout_seconds: float = 10.0,
        album_cache_ttl_seconds: int = 60,
        search_cache_ttl_seconds: int = 30,
        asset_cache_ttl_seconds: int = DEFAULT_TTL,
        metrics_enabled: bool = False,
        stream_http_client: httpx.Client | None = None,
        blob_cache: BlobCache | None = None,
        blob_cache_namespace: str = "",
    ) -> None:
        """Initialize the client."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._user_scope = user_scope
        self._timeout_seconds = timeout_seconds
        self._album_cache_ttl_seconds = album_cache_ttl_seconds
        self._search_cache_ttl_seconds = search_cache_ttl_seconds
        self._asset_cache_ttl_seconds = asset_cache_ttl_seconds
        self._metrics_enabled = metrics_enabled
        self._blob_cache = blob_cache
        self._blob_cache_namespace = blob_cache_namespace or self._base_url
        self._stream_http_client = stream_http_client or _shared_stream_client(
            self._base_url,
            self._timeout_seconds,
        )
        self._http_client = httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=True,
        )

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _cache_key(self, prefix: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return f"immich:{self._user_scope}:{prefix}:{digest}"

    def close(self) -> None:
        """Close request-scoped HTTP connections."""
        self._http_client.close()

    def _cached(self, key: str, ttl: int, loader: Any) -> Any:
        cache = get_cache()
        cached = cache.get_json(key)
        if cached is not None and "value" in cached:
            logger.debug("immich_cache_hit", key=key, request_id=get_request_id())
            return cached["value"]

        logger.debug("immich_cache_miss", key=key, request_id=get_request_id())
        try:
            value = loader()
        except ImmichApiError:
            stale = cache.get_json(f"{key}:stale")
            if stale is not None and "value" in stale:
                logger.warning("immich_cache_stale_hit", key=key, request_id=get_request_id())
                return stale["value"]
            raise

        cache.set_json(key, {"value": value}, ttl=ttl)
        cache.set_json(
            f"{key}:stale",
            {"value": value},
            ttl=max(ttl * 12, STALE_CACHE_TTL_SECONDS),
        )
        return value

    def _retry_delay(self, attempt: int) -> float:
        return min(0.1 * (2 ** (attempt - 1)), 1.0)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            started_at = time.perf_counter()
            status_code: int | None = None
            try:
                response = self._http_client.request(
                    method,
                    self._url(path),
                    headers=self._headers(),
                    json=json_body,
                )
                status_code = response.status_code
            except (httpx.TimeoutException, httpx.RequestError) as e:
                last_error = e
                if attempt == MAX_REQUEST_ATTEMPTS:
                    raise ImmichApiError(str(e)) from e
                time.sleep(self._retry_delay(attempt))
                continue
            finally:
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                logger.debug(
                    "immich_api_request",
                    request_id=get_request_id(),
                    method=method,
                    path=path.split("?", 1)[0],
                    status=status_code,
                    attempt=attempt,
                    elapsed_ms=round(elapsed_ms, 2),
                )

            if response.status_code in RETRY_STATUS_CODES and attempt < MAX_REQUEST_ATTEMPTS:
                response.close()
                time.sleep(self._retry_delay(attempt))
                continue

            if response.status_code == 404:
                raise ImmichApiError("not found", status_code=404)
            if response.status_code >= 400:
                raise ImmichApiError(
                    f"Immich API returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )

            if not response.content:
                return {}

            try:
                return response.json()
            except ValueError as e:
                raise ImmichApiError("Immich API returned invalid JSON") from e

        if last_error is not None:
            raise ImmichApiError(str(last_error)) from last_error
        raise ImmichApiError("Immich API request failed")

    def list_albums(self) -> list[dict[str, Any]]:
        """List albums visible to the authenticated user."""
        key = self._cache_key("albums", {})
        return self._cached(
            key,
            self._album_cache_ttl_seconds,
            lambda: self._request_json("GET", "albums"),
        )

    def _invalidate_metadata_cache(self) -> None:
        """Invalidate cached Immich metadata for this user."""
        get_cache().delete_prefix(f"immich:{self._user_scope}:")

    def create_album(self, name: str) -> dict[str, Any]:
        """Create an Immich album."""
        payload = self._request_json(
            "POST",
            "albums",
            json_body={"albumName": name, "assetIds": []},
        )
        self._invalidate_metadata_cache()
        return payload if isinstance(payload, dict) else {}

    def add_asset_to_album(self, album_id: str, asset_id: str) -> Any:
        """Add an asset to an Immich album."""
        payload = self._request_json(
            "PUT",
            f"albums/{album_id}/assets",
            json_body={"ids": [asset_id]},
        )
        self._invalidate_metadata_cache()
        return payload

    def remove_asset_from_album(self, album_id: str, asset_id: str) -> Any:
        """Remove an asset from an Immich album without deleting the asset."""
        payload = self._request_json(
            "DELETE",
            f"albums/{album_id}/assets",
            json_body={"ids": [asset_id]},
        )
        self._invalidate_metadata_cache()
        return payload

    def upload_asset(
        self,
        file_path: str | Path,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload one asset to Immich without retrying the multipart body."""
        path = Path(file_path)
        modified_at = _utc_iso_from_timestamp(path.stat().st_mtime)
        checksum = _file_sha1_base64(path)
        headers = {
            **self._headers(),
            "x-immich-checksum": checksum,
        }
        data = {
            "deviceAssetId": f"immich-bridge:{checksum}",
            "deviceId": "immich-bridge-webdav",
            "fileCreatedAt": modified_at,
            "fileModifiedAt": modified_at,
        }
        started_at = time.perf_counter()
        status_code: int | None = None
        try:
            with path.open("rb") as file:
                response = self._http_client.post(
                    self._url("assets"),
                    headers=headers,
                    data=data,
                    files={
                        "assetData": (
                            filename,
                            file,
                            content_type or "application/octet-stream",
                        )
                    },
                )
                status_code = response.status_code
        except (httpx.TimeoutException, httpx.RequestError) as e:
            raise ImmichApiError(str(e)) from e
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "immich_asset_upload",
                request_id=get_request_id(),
                filename=filename,
                bytes=path.stat().st_size if path.exists() else None,
                status=status_code,
                elapsed_ms=round(elapsed_ms, 2),
            )

        if response.status_code >= 400:
            raise ImmichApiError(
                f"Immich API returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as e:
            raise ImmichApiError("Immich API returned invalid JSON") from e
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ImmichApiError("Immich upload response did not include an asset id")

        self._invalidate_metadata_cache()
        return payload

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        """Fetch one asset by ID."""
        key = self._cache_key("asset", {"id": asset_id})
        return self._cached(
            key,
            self._asset_cache_ttl_seconds,
            lambda: self._request_json("GET", f"assets/{asset_id}"),
        )

    def timeline_buckets(self, *, is_favorite: bool | None = None) -> list[dict[str, Any]]:
        """Return Immich timeline month buckets."""
        query: dict[str, Any] = {}
        if is_favorite is not None:
            query["isFavorite"] = is_favorite
        key = self._cache_key("timeline-buckets", query)

        path = "timeline/buckets"
        if is_favorite is not None:
            path = f"{path}?isFavorite={str(is_favorite).lower()}"

        return self._cached(
            key,
            self._search_cache_ttl_seconds,
            lambda: self._request_json("GET", path),
        )

    def list_tags(self) -> list[dict[str, Any]]:
        """List Immich tags visible to the current user."""
        payload = self._request_json("GET", "tags")
        return list(payload) if isinstance(payload, list) else []

    def list_people(self) -> list[dict[str, Any]]:
        """List Immich people visible to the current user."""
        payload = self._request_json("GET", "people")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            people = payload.get("people")
            return list(people) if isinstance(people, list) else []
        return []

    def search_assets(
        self,
        *,
        page: int = 1,
        size: int = 100,
        order: str | None = None,
        album_ids: list[str] | None = None,
        person_ids: list[str] | None = None,
        tag_ids: list[str] | None = None,
        is_favorite: bool | None = None,
        media_type: str | None = None,
        taken_after: str | None = None,
        taken_before: str | None = None,
        rating: int | None = None,
        query: str | None = None,
        original_file_name: str | None = None,
        ocr: str | None = None,
        city: str | None = None,
        state: str | None = None,
        country: str | None = None,
        with_exif: bool = True,
    ) -> SearchPage:
        """Search assets by Immich metadata filters."""
        body: dict[str, Any] = {
            "page": page,
            "size": size,
            "withExif": with_exif,
        }
        if order:
            body["order"] = order
        if album_ids:
            body["albumIds"] = album_ids
        if person_ids:
            body["personIds"] = person_ids
        if tag_ids:
            body["tagIds"] = tag_ids
        if is_favorite is not None:
            body["isFavorite"] = is_favorite
        if media_type:
            body["type"] = media_type
        if taken_after:
            body["takenAfter"] = taken_after
        if taken_before:
            body["takenBefore"] = taken_before
        if rating is not None:
            body["rating"] = rating
        if query:
            body["query"] = query
        if original_file_name:
            body["originalFileName"] = original_file_name
        if ocr:
            body["ocr"] = ocr
        if city:
            body["city"] = city
        if state:
            body["state"] = state
        if country:
            body["country"] = country

        key = self._cache_key("search", body)

        def loader() -> dict[str, Any]:
            payload = self._request_json("POST", "search/metadata", json_body=body)
            assets = payload.get("assets", {}) if isinstance(payload, dict) else {}
            return {
                "items": assets.get("items", []),
                "nextPage": assets.get("nextPage"),
                "total": assets.get("total"),
            }

        cached = self._cached(key, self._search_cache_ttl_seconds, loader)
        return SearchPage(
            items=list(cached.get("items", [])),
            next_page=cached.get("nextPage"),
            total=cached.get("total"),
        )

    def _open_original_response(
        self,
        asset_id: str,
        range_start: int | None = None,
        range_end: int | None = None,
    ) -> httpx.Response:
        """Open an Immich original response, optionally from a byte offset."""
        headers = self._headers()
        range_requested = False
        if range_end is not None:
            start = range_start or 0
            headers["Range"] = f"bytes={start}-{range_end}"
            range_requested = True
        elif range_start is not None and range_start > 0:
            headers["Range"] = f"bytes={range_start}-"
            range_requested = True
        started_at = time.perf_counter()
        status_code: int | None = None
        content_length: int | None = None
        content_range: str | None = None
        content_type: str | None = None
        try:
            request = self._stream_http_client.build_request(
                "GET",
                self._url(f"assets/{asset_id}/original"),
                headers=headers,
            )
            response = self._stream_http_client.send(request, stream=True)
            status_code = response.status_code
            content_length = _parse_optional_int(response.headers.get("content-length"))
            content_range = response.headers.get("content-range")
            content_type = response.headers.get("content-type")
        except httpx.TimeoutException as e:
            raise ImmichApiError(str(e)) from e
        except httpx.RequestError as e:
            raise ImmichApiError(str(e)) from e
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            fields = {
                "request_id": get_request_id(),
                "asset_id": str(asset_id)[:8],
                "range_start": range_start,
                "range_end": range_end,
                "status": status_code,
                "elapsed_ms": round(elapsed_ms, 2),
            }
            if self._metrics_enabled:
                logger.info(
                    "immich_original_stream_open",
                    **fields,
                    response_content_length=content_length,
                    response_content_range=content_range,
                    response_content_type=content_type,
                )
            else:
                logger.debug("immich_original_stream_open", **fields)

        if response.status_code == 404:
            response.close()
            raise ImmichApiError("not found", status_code=404)
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            raise ImmichApiError(
                f"Immich API returned HTTP {status_code}",
                status_code=status_code,
            )

        if range_requested and response.status_code != 206:
            response.close()
            raise ImmichApiError(
                f"Immich API did not honor Range request: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        return response

    def _open_cached_range(
        self,
        asset_id: str,
        range_start: int,
        range_end: int,
    ) -> io.IOBase | None:
        """Open a bounded range from cache or materialize it for reuse."""
        cache = self._blob_cache
        if cache is None or not cache.cacheable(range_start, range_end):
            return None

        cached = cache.get(
            namespace=self._blob_cache_namespace,
            user_scope=self._user_scope,
            asset_id=asset_id,
            start=range_start,
            end=range_end,
        )
        if cached is not None:
            return CachedRangeStream(
                cached,
                asset_id=asset_id,
                metrics_enabled=self._metrics_enabled,
            )

        response = self._open_original_response(asset_id, range_start, range_end)
        try:
            data = response.read()
        finally:
            response.close()

        expected_size = range_end - range_start + 1
        if len(data) == expected_size:
            cache.set(
                namespace=self._blob_cache_namespace,
                user_scope=self._user_scope,
                asset_id=asset_id,
                start=range_start,
                end=range_end,
                data=data,
            )
        else:
            logger.debug(
                "blob_cache_store_skipped_size_mismatch",
                request_id=get_request_id(),
                asset_id=str(asset_id)[:8],
                expected_bytes=expected_size,
                actual_bytes=len(data),
            )

        return CachedRangeStream(
            CachedByteRange(
                data=data,
                start=range_start,
                end=range_start + len(data) - 1,
            ),
            asset_id=asset_id,
            metrics_enabled=self._metrics_enabled,
        )

    def open_original(
        self,
        asset_id: str,
        range_start: int | None = None,
        range_end: int | None = None,
    ) -> io.IOBase:
        """Open the original asset binary as a seek-aware file-like stream."""
        if range_start is not None and range_end is not None:
            cached_stream = self._open_cached_range(asset_id, range_start, range_end)
            if cached_stream is not None:
                return cached_stream

        response = self._open_original_response(asset_id, range_start, range_end)
        return ImmichOriginalStream(
            response,
            lambda range_start=None, range_end=None: self._open_original_response(
                asset_id,
                range_start,
                range_end,
            ),
            asset_id=asset_id,
            metrics_enabled=self._metrics_enabled,
            initial_position=range_start or 0,
            range_end=range_end,
        )
