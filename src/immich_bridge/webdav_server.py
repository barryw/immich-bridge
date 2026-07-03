"""WebDAV server using wsgidav and cheroot."""

import threading
from collections.abc import Callable as ABCCallable, Iterable
from enum import Enum
from http import HTTPStatus
from time import perf_counter
from typing import Any

import cheroot.wsgi
from wsgidav.lock_man.lock_storage_redis import LockStorageRedis
from wsgidav.wsgidav_app import WsgiDAVApp

from immich_bridge.logging import get_logger
from immich_bridge.observability import new_request_id, reset_request_id, set_request_id
from immich_bridge.webdav_auth import ImmichBasicAuthenticator
from immich_bridge.webdav_provider import ImmichProvider

logger = get_logger(__name__)
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
)


class RequestBodyTooLarge(Exception):
    """Raised when a request body exceeds the configured read cap."""


class LimitedInput:
    """WSGI input wrapper that enforces a maximum number of readable bytes."""

    def __init__(self, stream: Any, max_bytes: int) -> None:
        """Initialize the wrapper."""
        self._stream = stream
        self._max_bytes = max_bytes
        self._bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        """Read bytes without allowing callers to exceed the cap."""
        read_size = self._bounded_read_size(size)
        data = self._stream.read(read_size)
        self._record_read(len(data))
        return data

    def readline(self, size: int = -1) -> bytes:
        """Read one line without allowing callers to exceed the cap."""
        read_size = self._bounded_read_size(size)
        data = self._stream.readline(read_size)
        self._record_read(len(data))
        return data

    def readlines(self, hint: int = -1) -> list[bytes]:
        """Read lines without allowing callers to exceed the cap."""
        lines: list[bytes] = []
        total = 0
        for line in self:
            lines.append(line)
            total += len(line)
            if hint >= 0 and total >= hint:
                break
        return lines

    def __iter__(self) -> "LimitedInput":
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def _bounded_read_size(self, size: int) -> int:
        remaining = self._max_bytes - self._bytes_read
        if remaining < 0:
            raise RequestBodyTooLarge
        if size is None or size < 0:
            return remaining + 1
        return min(size, remaining + 1)

    def _record_read(self, byte_count: int) -> None:
        self._bytes_read += byte_count
        if self._bytes_read > self._max_bytes:
            raise RequestBodyTooLarge


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


class WebDAVClient(Enum):
    """Known WebDAV client types with their quirks."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    CYBERDUCK = "cyberduck"
    RCLONE = "rclone"
    UNKNOWN = "unknown"


def detect_webdav_client(user_agent: str) -> WebDAVClient:
    """Detect the WebDAV client type from User-Agent."""
    if not user_agent:
        return WebDAVClient.UNKNOWN

    ua_lower = user_agent.lower()

    if "cyberduck" in ua_lower:
        return WebDAVClient.CYBERDUCK
    if "rclone" in ua_lower:
        return WebDAVClient.RCLONE
    if "gvfs" in ua_lower or "davfs2" in ua_lower:
        return WebDAVClient.LINUX
    if "microsoft-webdav" in ua_lower or "miniredir" in ua_lower:
        return WebDAVClient.WINDOWS
    if "webdavfs" in ua_lower or "darwin" in ua_lower:
        return WebDAVClient.MACOS
    if "macos" in ua_lower or "mac os" in ua_lower:
        return WebDAVClient.MACOS

    return WebDAVClient.UNKNOWN


class ClientCompatibilityMiddleware:
    """WSGI middleware that applies client-specific quirks and workarounds."""

    def __init__(
        self,
        app: ABCCallable[..., Iterable[bytes]],
        *,
        metrics_enabled: bool = False,
        max_request_body_bytes: int = 1_048_576,
        max_path_length: int = 2048,
        max_path_segments: int = 32,
        max_concurrent_requests: int = 32,
        max_concurrent_streams: int = 8,
    ) -> None:
        """Initialize middleware."""
        self._app = app
        self._metrics_enabled = metrics_enabled
        self._max_request_body_bytes = max_request_body_bytes
        self._max_path_length = max_path_length
        self._max_path_segments = max_path_segments
        self._request_limiter = (
            threading.BoundedSemaphore(max_concurrent_requests)
            if max_concurrent_requests > 0
            else None
        )
        self._stream_limiter = (
            threading.BoundedSemaphore(max_concurrent_streams)
            if max_concurrent_streams > 0
            else None
        )

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: ABCCallable[..., Any],
    ) -> Iterable[bytes]:
        started_at = perf_counter()
        user_agent = environ.get("HTTP_USER_AGENT", "")
        client = detect_webdav_client(user_agent)
        environ["webdav.client"] = client
        environ["webdav.client_name"] = client.value
        environ["immich_bridge.metrics_enabled"] = self._metrics_enabled
        request_id = environ.get("HTTP_X_REQUEST_ID") or new_request_id()
        environ["immich_bridge.request_id"] = request_id
        method = environ.get("REQUEST_METHOD", "")
        path = environ.get("PATH_INFO", "")
        request_limiter_acquired = False
        stream_limiter_acquired = False
        status_holder = {"status": ""}
        response_header_holder: dict[str, str | None] = {
            "content_length": None,
            "content_range": None,
            "content_type": None,
        }
        response_bytes = 0
        token = set_request_id(str(request_id))

        if method == "OPTIONS":
            logger.debug(
                "webdav_client_detected",
                client=client.value,
                user_agent=user_agent[:100],
            )

        rejection = self._validate_request(environ, method=method, path=path)
        if rejection is not None:
            status, reason = rejection
            return self._reject_request(
                status,
                reason,
                started_at=started_at,
                method=method,
                path=path,
                client=client,
                request_id=str(request_id),
                start_response=start_response,
                token=token,
            )

        if self._max_request_body_bytes > 0 and "wsgi.input" in environ:
            environ["wsgi.input"] = LimitedInput(
                environ["wsgi.input"],
                self._max_request_body_bytes,
            )

        if self._request_limiter is not None:
            request_limiter_acquired = self._request_limiter.acquire(blocking=False)
            if not request_limiter_acquired:
                return self._reject_request(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "too many concurrent WebDAV requests",
                    started_at=started_at,
                    method=method,
                    path=path,
                    client=client,
                    request_id=str(request_id),
                    start_response=start_response,
                    token=token,
                )

        if method == "GET" and self._stream_limiter is not None:
            stream_limiter_acquired = self._stream_limiter.acquire(blocking=False)
            if not stream_limiter_acquired:
                if request_limiter_acquired and self._request_limiter is not None:
                    self._request_limiter.release()
                return self._reject_request(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "too many concurrent WebDAV streams",
                    started_at=started_at,
                    method=method,
                    path=path,
                    client=client,
                    request_id=str(request_id),
                    start_response=start_response,
                    token=token,
                )

        def custom_start_response(
            status: str,
            response_headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Any:
            status_holder["status"] = status
            for header_name, header_value in response_headers:
                header_key = header_name.lower()
                if header_key == "content-length":
                    response_header_holder["content_length"] = header_value
                elif header_key == "content-range":
                    response_header_holder["content_range"] = header_value
                elif header_key == "content-type":
                    response_header_holder["content_type"] = header_value
            self._apply_client_headers(client, method, response_headers)
            self._apply_security_headers(response_headers)
            self._append_header_if_missing(response_headers, "X-Request-Id", str(request_id))
            return start_response(status, response_headers, exc_info)

        try:
            app_iter = self._app(environ, custom_start_response)
        except RequestBodyTooLarge:
            if stream_limiter_acquired and self._stream_limiter is not None:
                self._stream_limiter.release()
            if request_limiter_acquired and self._request_limiter is not None:
                self._request_limiter.release()
            return self._reject_request(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request body too large",
                started_at=started_at,
                method=method,
                path=path,
                client=client,
                request_id=str(request_id),
                start_response=start_response,
                token=token,
            )
        except Exception:
            if stream_limiter_acquired and self._stream_limiter is not None:
                self._stream_limiter.release()
            if request_limiter_acquired and self._request_limiter is not None:
                self._request_limiter.release()
            reset_request_id(token)
            raise

        def timing_iter() -> Iterable[bytes]:
            nonlocal response_bytes
            try:
                try:
                    for chunk in app_iter:
                        response_bytes += len(chunk)
                        yield chunk
                except RequestBodyTooLarge:
                    if status_holder["status"]:
                        raise
                    body = (
                        f"{HTTPStatus.REQUEST_ENTITY_TOO_LARGE.phrase}: request body too large\n"
                    ).encode()
                    custom_start_response(
                        f"{HTTPStatus.REQUEST_ENTITY_TOO_LARGE.value} "
                        f"{HTTPStatus.REQUEST_ENTITY_TOO_LARGE.phrase}",
                        [
                            ("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                        ],
                    )
                    response_bytes += len(body)
                    yield body
            finally:
                close = getattr(app_iter, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        logger.warning("webdav_app_iter_close_failed", request_id=request_id)
                for closeable in environ.pop("immich_bridge.closeables", []):
                    close_resource = getattr(closeable, "close", None)
                    if close_resource is not None:
                        try:
                            close_resource()
                        except Exception:
                            logger.warning(
                                "webdav_request_resource_close_failed",
                                request_id=request_id,
                            )
                if stream_limiter_acquired and self._stream_limiter is not None:
                    self._stream_limiter.release()
                if request_limiter_acquired and self._request_limiter is not None:
                    self._request_limiter.release()
                self._log_request(
                    started_at=started_at,
                    method=method,
                    path=path,
                    status=status_holder["status"],
                    client=client,
                    request_id=str(request_id),
                    environ=environ,
                    response_headers=response_header_holder,
                    response_bytes=response_bytes,
                )
                reset_request_id(token)

        return timing_iter()

    def _validate_request(
        self,
        environ: dict[str, Any],
        *,
        method: str,
        path: str,
    ) -> tuple[HTTPStatus, str] | None:
        """Validate cheap request properties before invoking WsgiDAV."""
        content_length_raw = environ.get("CONTENT_LENGTH")
        content_length = _parse_optional_int(content_length_raw)
        if content_length_raw not in {None, ""} and content_length is None:
            return HTTPStatus.BAD_REQUEST, "invalid Content-Length"
        if (
            content_length is not None
            and self._max_request_body_bytes > 0
            and content_length > self._max_request_body_bytes
        ):
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large"

        if any(character in path for character in ("\x00", "\r", "\n")):
            return HTTPStatus.BAD_REQUEST, "invalid path characters"
        if self._max_path_length > 0 and len(path.encode("utf-8")) > self._max_path_length:
            return HTTPStatus.REQUEST_URI_TOO_LONG, "path too long"

        segment_count = len([segment for segment in path.split("/") if segment])
        if self._max_path_segments > 0 and segment_count > self._max_path_segments:
            return HTTPStatus.REQUEST_URI_TOO_LONG, "too many path segments"

        if method in {"PUT", "PATCH"} and content_length is None:
            return HTTPStatus.LENGTH_REQUIRED, "Content-Length required for write methods"

        return None

    def _reject_request(
        self,
        status: HTTPStatus,
        reason: str,
        *,
        started_at: float,
        method: str,
        path: str,
        client: WebDAVClient,
        request_id: str,
        start_response: ABCCallable[..., Any],
        token: Any,
    ) -> Iterable[bytes]:
        """Return a small plain-text rejection response."""
        body = f"{status.phrase}: {reason}\n".encode()
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]
        self._apply_security_headers(headers)
        self._append_header_if_missing(headers, "X-Request-Id", request_id)
        start_response(f"{status.value} {status.phrase}", headers)
        logger.warning(
            "webdav_request_rejected",
            request_id=request_id,
            method=method,
            path=path,
            status=status.value,
            reason=reason,
            client=client.value,
            elapsed_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        reset_request_id(token)
        return [body]

    def _log_request(
        self,
        *,
        started_at: float,
        method: str,
        path: str,
        status: str,
        client: WebDAVClient,
        request_id: str,
        environ: dict[str, Any],
        response_headers: dict[str, str | None],
        response_bytes: int,
    ) -> None:
        """Log one completed WebDAV request."""
        try:
            elapsed_ms = (perf_counter() - started_at) * 1000
            fields: dict[str, Any] = {
                "request_id": request_id,
                "method": method,
                "path": path,
                "status": status.split(" ", 1)[0] or None,
                "client": client.value,
                "elapsed_ms": round(elapsed_ms, 2),
            }
            if self._metrics_enabled:
                fields.update(
                    {
                        "depth": environ.get("HTTP_DEPTH"),
                        "range_header": environ.get("HTTP_RANGE"),
                        "request_bytes": _parse_optional_int(environ.get("CONTENT_LENGTH")),
                        "response_bytes": response_bytes,
                        "response_content_length": _parse_optional_int(
                            response_headers["content_length"],
                        ),
                        "response_content_range": response_headers["content_range"],
                        "response_content_type": response_headers["content_type"],
                        "user_agent": str(environ.get("HTTP_USER_AGENT") or "")[:160],
                    },
                )
            logger.info("webdav_request", **fields)
        except Exception:
            logger.exception("webdav_request_log_failed")

    def _apply_client_headers(
        self,
        client: WebDAVClient,
        method: str,
        headers: list[tuple[str, str]],
    ) -> None:
        """Apply client-specific response headers."""
        if client != WebDAVClient.MACOS:
            return
        if method in {"GET", "HEAD"}:
            self._append_header_if_missing(headers, "Cache-Control", "private, max-age=300")
            return
        self._append_header_if_missing(
            headers,
            "Cache-Control",
            "no-store, no-cache, must-revalidate",
        )
        self._append_header_if_missing(headers, "Pragma", "no-cache")

    def _apply_security_headers(self, headers: list[tuple[str, str]]) -> None:
        """Add common response hardening headers."""
        for name, value in SECURITY_HEADERS:
            self._append_header_if_missing(headers, name, value)

    def _append_header_if_missing(
        self,
        headers: list[tuple[str, str]],
        name: str,
        value: str,
    ) -> None:
        """Append a header unless the wrapped app already set it."""
        lower_name = name.lower()
        if not any(existing_name.lower() == lower_name for existing_name, _ in headers):
            headers.append((name, value))


NoCacheMiddleware = ClientCompatibilityMiddleware


def _make_authenticator_class(
    immich_url: str,
    cache_ttl_seconds: int,
    timeout_seconds: float,
    auth_failure_limit: int,
    auth_failure_window_seconds: int,
) -> type[ImmichBasicAuthenticator]:
    """Create a configured authenticator class that wsgidav can instantiate."""

    class ConfiguredAuthenticator(ImmichBasicAuthenticator):
        def __init__(self, wsgidav_app: Any, config: dict[str, Any]) -> None:
            super().__init__(
                immich_url=immich_url,
                cache_ttl_seconds=cache_ttl_seconds,
                timeout_seconds=timeout_seconds,
                auth_failure_limit=auth_failure_limit,
                auth_failure_window_seconds=auth_failure_window_seconds,
            )

    return ConfiguredAuthenticator


def create_webdav_app(
    immich_url: str,
    auth_cache_ttl_seconds: int = 300,
    immich_timeout_seconds: float = 10.0,
    album_cache_ttl_seconds: int = 60,
    search_cache_ttl_seconds: int = 30,
    asset_cache_ttl_seconds: int = 300,
    search_page_size: int = 500,
    search_max_pages: int = 20,
    album_folder_split_threshold: int = 200,
    day_folder_split_threshold: int = 1000,
    auth_failure_limit: int = 10,
    auth_failure_window_seconds: int = 300,
    redis_host: str | None = None,
    redis_port: int = 6379,
    redis_db: int = 0,
    redis_password: str | None = None,
    metrics_enabled: bool = False,
    webdav_max_request_body_bytes: int = 1_048_576,
    webdav_max_path_length: int = 2048,
    webdav_max_path_segments: int = 32,
    webdav_max_concurrent_requests: int = 32,
    webdav_max_concurrent_streams: int = 8,
    blob_cache_enabled: bool = True,
    blob_cache_dir: str = "/tmp/immich-bridge/blob-cache",
    blob_cache_max_bytes: int = 1_073_741_824,
    blob_cache_max_range_bytes: int = 8_388_608,
    blob_cache_ttl_seconds: int = 86_400,
) -> ClientCompatibilityMiddleware:
    """Create the wsgidav WSGI application."""
    provider = ImmichProvider(
        immich_url=immich_url,
        timeout_seconds=immich_timeout_seconds,
        album_cache_ttl_seconds=album_cache_ttl_seconds,
        search_cache_ttl_seconds=search_cache_ttl_seconds,
        asset_cache_ttl_seconds=asset_cache_ttl_seconds,
        search_page_size=search_page_size,
        search_max_pages=search_max_pages,
        album_folder_split_threshold=album_folder_split_threshold,
        day_folder_split_threshold=day_folder_split_threshold,
        webdav_locks_enabled=bool(redis_host),
        metrics_enabled=metrics_enabled,
        blob_cache_enabled=blob_cache_enabled,
        blob_cache_dir=blob_cache_dir,
        blob_cache_max_bytes=blob_cache_max_bytes,
        blob_cache_max_range_bytes=blob_cache_max_range_bytes,
        blob_cache_ttl_seconds=blob_cache_ttl_seconds,
    )
    authenticator_class = _make_authenticator_class(
        immich_url,
        auth_cache_ttl_seconds,
        immich_timeout_seconds,
        auth_failure_limit,
        auth_failure_window_seconds,
    )

    config: dict[str, Any] = {
        "provider_mapping": {"/": provider},
        "http_authenticator": {
            "domain_controller": authenticator_class,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
        },
        "simple_dc": {"user_mapping": {}},
        "dir_browser": {"enable": False},
        "lock_storage": False,
        "verbose": 5 if metrics_enabled else 1,
        "logging": {
            "enable": True,
            "enable_loggers": ["wsgidav"],
        },
        "add_header_MS_Author_Via": True,
        "hotfixes": {
            "emulate_win32_lastmod": True,
            "re_encode_path_info": True,
            "unquote_path_info": False,
            # WsgiDAV 4.3.5 applies this to every root request, not only OPTIONS.
            # Keeping it disabled is required for `PROPFIND /` to reach the provider.
            "treat_root_options_as_asterisk": False,
        },
        "immich_url": immich_url,
    }

    if redis_host:
        config["lock_storage"] = LockStorageRedis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password or None,
        )
        logger.info(
            "redis_lock_storage_configured",
            host=redis_host,
            port=redis_port,
            db=redis_db,
        )
    else:
        logger.info("webdav_lock_storage_disabled_no_redis")

    app = WsgiDAVApp(config)
    return ClientCompatibilityMiddleware(
        app,
        metrics_enabled=metrics_enabled,
        max_request_body_bytes=webdav_max_request_body_bytes,
        max_path_length=webdav_max_path_length,
        max_path_segments=webdav_max_path_segments,
        max_concurrent_requests=webdav_max_concurrent_requests,
        max_concurrent_streams=webdav_max_concurrent_streams,
    )


class WebDAVServer:
    """Cheroot-based WebDAV server."""

    def __init__(
        self,
        host: str,
        port: int,
        immich_url: str,
        auth_cache_ttl_seconds: int = 300,
        immich_timeout_seconds: float = 10.0,
        album_cache_ttl_seconds: int = 60,
        search_cache_ttl_seconds: int = 30,
        asset_cache_ttl_seconds: int = 300,
        search_page_size: int = 500,
        search_max_pages: int = 20,
        album_folder_split_threshold: int = 200,
        day_folder_split_threshold: int = 1000,
        auth_failure_limit: int = 10,
        auth_failure_window_seconds: int = 300,
        redis_host: str | None = None,
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: str | None = None,
        metrics_enabled: bool = False,
        webdav_max_request_body_bytes: int = 1_048_576,
        webdav_max_path_length: int = 2048,
        webdav_max_path_segments: int = 32,
        webdav_max_concurrent_requests: int = 32,
        webdav_max_concurrent_streams: int = 8,
        blob_cache_enabled: bool = True,
        blob_cache_dir: str = "/tmp/immich-bridge/blob-cache",
        blob_cache_max_bytes: int = 1_073_741_824,
        blob_cache_max_range_bytes: int = 8_388_608,
        blob_cache_ttl_seconds: int = 86_400,
    ) -> None:
        """Initialize the WebDAV server."""
        self._app = create_webdav_app(
            immich_url=immich_url,
            auth_cache_ttl_seconds=auth_cache_ttl_seconds,
            immich_timeout_seconds=immich_timeout_seconds,
            album_cache_ttl_seconds=album_cache_ttl_seconds,
            search_cache_ttl_seconds=search_cache_ttl_seconds,
            asset_cache_ttl_seconds=asset_cache_ttl_seconds,
            search_page_size=search_page_size,
            search_max_pages=search_max_pages,
            album_folder_split_threshold=album_folder_split_threshold,
            day_folder_split_threshold=day_folder_split_threshold,
            auth_failure_limit=auth_failure_limit,
            auth_failure_window_seconds=auth_failure_window_seconds,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_db=redis_db,
            redis_password=redis_password,
            metrics_enabled=metrics_enabled,
            webdav_max_request_body_bytes=webdav_max_request_body_bytes,
            webdav_max_path_length=webdav_max_path_length,
            webdav_max_path_segments=webdav_max_path_segments,
            webdav_max_concurrent_requests=webdav_max_concurrent_requests,
            webdav_max_concurrent_streams=webdav_max_concurrent_streams,
            blob_cache_enabled=blob_cache_enabled,
            blob_cache_dir=blob_cache_dir,
            blob_cache_max_bytes=blob_cache_max_bytes,
            blob_cache_max_range_bytes=blob_cache_max_range_bytes,
            blob_cache_ttl_seconds=blob_cache_ttl_seconds,
        )
        self._server = cheroot.wsgi.Server((host, port), self._app)
        self._host = host
        self._port = port

    def start(self) -> None:
        """Start the WebDAV server."""
        logger.info("webdav_server_starting", host=self._host, port=self._port)
        self._server.start()

    def stop(self) -> None:
        """Stop the WebDAV server."""
        logger.info("webdav_server_stopping")
        self._server.stop()
