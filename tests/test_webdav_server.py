"""Tests for WebDAV server setup."""

import io
from unittest.mock import MagicMock, patch

from immich_bridge.webdav_auth import ImmichBasicAuthenticator
from immich_bridge.webdav_server import (
    ClientCompatibilityMiddleware,
    WebDAVClient,
    WebDAVServer,
    create_webdav_app,
    detect_webdav_client,
)


def test_detect_webdav_client() -> None:
    """Known WebDAV clients should be detected from User-Agent."""
    assert detect_webdav_client("Cyberduck/8.7.0") == WebDAVClient.CYBERDUCK
    assert detect_webdav_client("rclone/v1.65.0") == WebDAVClient.RCLONE
    assert detect_webdav_client("Microsoft-WebDAV-MiniRedir/10.0") == WebDAVClient.WINDOWS
    assert detect_webdav_client("WebDAVFS/3.0 Darwin/23.0.0") == WebDAVClient.MACOS
    assert detect_webdav_client("") == WebDAVClient.UNKNOWN


def test_create_webdav_app_configures_basic_auth() -> None:
    """WebDAV app should use Basic auth with digest disabled."""
    with patch("immich_bridge.webdav_server.WsgiDAVApp") as mock_wsgi_app:
        mock_wsgi_app.return_value = MagicMock()

        create_webdav_app("http://immich.test/api")

    config = mock_wsgi_app.call_args[0][0]
    auth_config = config["http_authenticator"]
    auth_class = auth_config["domain_controller"]

    assert auth_config["accept_basic"] is True
    assert auth_config["accept_digest"] is False
    assert issubclass(auth_class, ImmichBasicAuthenticator)
    assert config["dir_browser"]["enable"] is False
    assert config["hotfixes"]["treat_root_options_as_asterisk"] is False
    assert config["lock_storage"] is False
    assert config["verbose"] == 1


def test_create_webdav_app_metrics_enables_verbose_wsgidav_logging() -> None:
    """Metrics mode should opt into WsgiDAV's noisier request diagnostics."""
    with patch("immich_bridge.webdav_server.WsgiDAVApp") as mock_wsgi_app:
        mock_wsgi_app.return_value = MagicMock()

        create_webdav_app("http://immich.test/api", metrics_enabled=True)

    config = mock_wsgi_app.call_args[0][0]
    assert config["verbose"] == 5


def test_redis_configures_webdav_lock_storage() -> None:
    """DAV lock storage should use Redis whenever Redis is configured."""
    lock_storage = MagicMock()
    with patch("immich_bridge.webdav_server.LockStorageRedis") as mock_lock_storage:
        mock_lock_storage.return_value = lock_storage
        with patch("immich_bridge.webdav_server.WsgiDAVApp") as mock_wsgi_app:
            mock_wsgi_app.return_value = MagicMock()

            create_webdav_app("http://immich.test/api", redis_host="redis")

    config = mock_wsgi_app.call_args[0][0]
    assert config["lock_storage"] is lock_storage


def test_client_middleware_adds_request_id_and_closes_request_resources() -> None:
    """Middleware should expose request IDs and clean up per-request clients."""
    closeable = MagicMock()

    def app(environ: dict[str, object], start_response: object) -> list[bytes]:
        environ["immich_bridge.closeables"] = [closeable]
        start_response("200 OK", [])  # type: ignore[operator]
        return [b"ok"]

    headers: list[tuple[str, str]] = []

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        headers.extend(response_headers)

    middleware = ClientCompatibilityMiddleware(app)  # type: ignore[arg-type]
    body = list(
        middleware(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/",
                "HTTP_USER_AGENT": "WebDAVFS/3.0 Darwin/23.0.0",
                "HTTP_X_REQUEST_ID": "req-test-1",
            },
            start_response,
        )
    )

    assert body == [b"ok"]
    assert ("X-Request-Id", "req-test-1") in headers
    assert ("X-Content-Type-Options", "nosniff") in headers
    assert ("X-Frame-Options", "DENY") in headers
    assert ("Referrer-Policy", "no-referrer") in headers
    assert ("Cache-Control", "private, max-age=300") in headers
    closeable.close.assert_called_once()


def test_client_middleware_rejects_oversized_request_bodies() -> None:
    """Request bodies over the configured limit should not reach WsgiDAV."""
    app = MagicMock()
    statuses: list[str] = []
    headers: list[tuple[str, str]] = []

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        statuses.append(status)
        headers.extend(response_headers)

    middleware = ClientCompatibilityMiddleware(  # type: ignore[arg-type]
        app,
        max_request_body_bytes=8,
    )
    body = list(
        middleware(
            {
                "REQUEST_METHOD": "PROPFIND",
                "PATH_INFO": "/",
                "CONTENT_LENGTH": "9",
                "HTTP_X_REQUEST_ID": "req-too-large",
            },
            start_response,
        )
    )

    assert len(statuses) == 1
    assert statuses[0].startswith("413 ")
    assert len(body) == 1
    assert body[0].endswith(b": request body too large\n")
    assert ("X-Content-Type-Options", "nosniff") in headers
    app.assert_not_called()


def test_client_middleware_caps_wsgi_input_reads() -> None:
    """Bodies without Content-Length should still be capped during reads."""
    statuses: list[str] = []

    def app(environ: dict[str, object], start_response: object) -> list[bytes]:
        environ["wsgi.input"].read()  # type: ignore[union-attr]
        start_response("200 OK", [])  # type: ignore[operator]
        return [b"ok"]

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        statuses.append(status)

    middleware = ClientCompatibilityMiddleware(  # type: ignore[arg-type]
        app,
        max_request_body_bytes=4,
    )
    body = list(
        middleware(
            {
                "REQUEST_METHOD": "PROPFIND",
                "PATH_INFO": "/",
                "wsgi.input": io.BytesIO(b"12345"),
                "HTTP_X_REQUEST_ID": "req-capped-input",
            },
            start_response,
        )
    )

    assert len(statuses) == 1
    assert statuses[0].startswith("413 ")
    assert len(body) == 1
    assert body[0].endswith(b": request body too large\n")


def test_client_middleware_rejects_long_paths() -> None:
    """Long paths should be rejected before provider resolution."""
    app = MagicMock()
    statuses: list[str] = []

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        statuses.append(status)

    middleware = ClientCompatibilityMiddleware(  # type: ignore[arg-type]
        app,
        max_path_length=4,
    )
    list(
        middleware(
            {
                "REQUEST_METHOD": "PROPFIND",
                "PATH_INFO": "/long",
                "HTTP_X_REQUEST_ID": "req-long-path",
            },
            start_response,
        )
    )

    assert len(statuses) == 1
    assert statuses[0].startswith("414 ")
    app.assert_not_called()


def test_client_middleware_limits_concurrent_requests() -> None:
    """Backpressure should return 503 instead of queueing unbounded requests."""

    def app(environ: dict[str, object], start_response: object) -> list[bytes]:
        start_response("200 OK", [])  # type: ignore[operator]
        return [b"ok"]

    statuses: list[str] = []

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        statuses.append(status)

    middleware = ClientCompatibilityMiddleware(  # type: ignore[arg-type]
        app,
        max_concurrent_requests=1,
        max_concurrent_streams=0,
    )
    first_iter = middleware(
        {
            "REQUEST_METHOD": "PROPFIND",
            "PATH_INFO": "/",
            "HTTP_X_REQUEST_ID": "req-first",
        },
        start_response,
    )
    second_body = list(
        middleware(
            {
                "REQUEST_METHOD": "PROPFIND",
                "PATH_INFO": "/",
                "HTTP_X_REQUEST_ID": "req-second",
            },
            start_response,
        )
    )
    first_body = list(first_iter)

    assert statuses == ["200 OK", "503 Service Unavailable"]
    assert second_body == [b"Service Unavailable: too many concurrent WebDAV requests\n"]
    assert first_body == [b"ok"]


def test_client_middleware_metrics_adds_range_and_byte_fields() -> None:
    """Metrics mode should add noisy request details only when explicitly enabled."""
    events: list[tuple[str, dict[str, object]]] = []

    def app(environ: dict[str, object], start_response: object) -> list[bytes]:
        start_response(  # type: ignore[operator]
            "206 Partial Content",
            [
                ("Content-Length", "2"),
                ("Content-Range", "bytes 1-2/10"),
                ("Content-Type", "image/heic"),
            ],
        )
        return [b"ok"]

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> None:
        return None

    with patch(
        "immich_bridge.webdav_server.logger.info",
        side_effect=lambda event, **fields: events.append((event, fields)),
    ):
        middleware = ClientCompatibilityMiddleware(app, metrics_enabled=True)  # type: ignore[arg-type]
        body = list(
            middleware(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/Albums/image.heic",
                    "HTTP_USER_AGENT": "WebDAVFS/3.0 Darwin/23.0.0",
                    "HTTP_X_REQUEST_ID": "req-test-2",
                    "HTTP_RANGE": "bytes=1-2",
                    "HTTP_DEPTH": "0",
                    "CONTENT_LENGTH": "12",
                },
                start_response,
            )
        )

    assert body == [b"ok"]
    request_events = [fields for event, fields in events if event == "webdav_request"]
    assert len(request_events) == 1
    assert request_events[0]["range_header"] == "bytes=1-2"
    assert request_events[0]["request_bytes"] == 12
    assert request_events[0]["response_bytes"] == 2
    assert request_events[0]["response_content_length"] == 2
    assert request_events[0]["response_content_range"] == "bytes 1-2/10"
    assert request_events[0]["response_content_type"] == "image/heic"


def test_webdav_server_creates_cheroot_server() -> None:
    """WebDAVServer should bind Cheroot to the configured host and port."""
    with patch("immich_bridge.webdav_server.create_webdav_app") as mock_create_app:
        mock_create_app.return_value = MagicMock()
        with patch("immich_bridge.webdav_server.cheroot.wsgi.Server") as mock_server:
            WebDAVServer(
                host="0.0.0.0",
                port=8081,
                immich_url="http://immich.test/api",
            )

    mock_server.assert_called_once()
    assert mock_server.call_args[0][0] == ("0.0.0.0", 8081)
