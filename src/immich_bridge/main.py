"""Main entrypoint for running both Admin API and WebDAV servers."""

import signal
import sys
import threading
from typing import Any

import uvicorn

from immich_bridge.cache import init_cache
from immich_bridge.config import get_settings
from immich_bridge.logging import get_logger, setup_logging
from immich_bridge.webdav_server import WebDAVServer

logger = get_logger(__name__)


def run_servers() -> None:
    """Run both Admin API and WebDAV servers."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    redis_password = settings.redis_password.get_secret_value() if settings.redis_password else None

    init_cache(
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_db=settings.redis_db,
        redis_password=redis_password,
    )

    logger.info(
        "starting_servers",
        admin_port=settings.admin_port,
        webdav_port=settings.webdav_port,
        redis_enabled=bool(settings.redis_host),
        webdav_locks_enabled=bool(settings.redis_host),
        metrics_enabled=settings.immich_bridge_metrics,
        blob_cache_enabled=settings.blob_cache_enabled,
        webdav_max_concurrent_requests=settings.webdav_max_concurrent_requests,
        webdav_max_concurrent_streams=settings.webdav_max_concurrent_streams,
    )

    webdav_server = WebDAVServer(
        host="0.0.0.0",
        port=settings.webdav_port,
        immich_url=settings.immich_url,
        auth_cache_ttl_seconds=settings.auth_cache_ttl_seconds,
        immich_timeout_seconds=settings.immich_timeout_seconds,
        album_cache_ttl_seconds=settings.album_cache_ttl_seconds,
        search_cache_ttl_seconds=settings.search_cache_ttl_seconds,
        asset_cache_ttl_seconds=settings.asset_cache_ttl_seconds,
        search_page_size=settings.search_page_size,
        search_max_pages=settings.search_max_pages,
        album_folder_split_threshold=settings.album_folder_split_threshold,
        day_folder_split_threshold=settings.day_folder_split_threshold,
        auth_failure_limit=settings.auth_failure_limit,
        auth_failure_window_seconds=settings.auth_failure_window_seconds,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_db=settings.redis_db,
        redis_password=redis_password,
        metrics_enabled=settings.immich_bridge_metrics,
        webdav_max_request_body_bytes=settings.webdav_max_request_body_bytes,
        webdav_max_path_length=settings.webdav_max_path_length,
        webdav_max_path_segments=settings.webdav_max_path_segments,
        webdav_max_concurrent_requests=settings.webdav_max_concurrent_requests,
        webdav_max_concurrent_streams=settings.webdav_max_concurrent_streams,
        blob_cache_enabled=settings.blob_cache_enabled,
        blob_cache_dir=settings.blob_cache_dir,
        blob_cache_max_bytes=settings.blob_cache_max_bytes,
        blob_cache_max_range_bytes=settings.blob_cache_max_range_bytes,
        blob_cache_ttl_seconds=settings.blob_cache_ttl_seconds,
    )

    webdav_thread = threading.Thread(target=webdav_server.start, daemon=True)
    webdav_thread.start()
    logger.info("webdav_server_started", port=settings.webdav_port)

    def shutdown(signum: int, frame: Any) -> None:
        logger.info("shutdown_signal_received", signal=signum)
        webdav_server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    uvicorn.run(
        "immich_bridge.app:app",
        host="0.0.0.0",
        port=settings.admin_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run_servers()
