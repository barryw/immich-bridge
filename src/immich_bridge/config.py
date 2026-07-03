"""Application configuration via environment variables."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    immich_url: str = Field(description="Immich API base URL, usually ending in /api")

    admin_port: int = Field(default=8080, description="Admin API port")
    webdav_port: int = Field(default=8081, description="WebDAV server port")

    redis_host: str | None = Field(default=None, description="Redis host for cache and locks")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_db: int = Field(default=0, description="Redis database number")
    redis_password: SecretStr | None = Field(default=None, description="Redis password")

    auth_cache_ttl_seconds: int = Field(default=300, description="Immich API-key auth cache TTL")
    auth_failure_limit: int = Field(default=10, description="Failed Basic Auth limit per window")
    auth_failure_window_seconds: int = Field(default=300, description="Auth failure window")
    immich_timeout_seconds: float = Field(default=10.0, description="Immich API timeout")
    album_cache_ttl_seconds: int = Field(default=60, description="Album listing cache TTL")
    search_cache_ttl_seconds: int = Field(default=30, description="Asset search cache TTL")
    asset_cache_ttl_seconds: int = Field(default=300, description="Asset metadata cache TTL")
    search_page_size: int = Field(default=500, description="Immich search page size")
    search_max_pages: int = Field(default=20, description="Maximum pages per DAV directory")
    album_folder_split_threshold: int = Field(
        default=200,
        description="Album asset count above which album folders are split into date buckets",
    )
    day_folder_split_threshold: int = Field(
        default=1000,
        description="Asset count above which a day folder is split into hours",
    )
    webdav_max_request_body_bytes: int = Field(
        default=1_048_576,
        description="Maximum accepted WebDAV request body size",
    )
    webdav_max_path_length: int = Field(
        default=2048,
        description="Maximum accepted WebDAV path length in bytes",
    )
    webdav_max_path_segments: int = Field(
        default=32,
        description="Maximum accepted WebDAV path segment count",
    )
    webdav_max_concurrent_requests: int = Field(
        default=32,
        description="Maximum concurrent WebDAV requests before returning 503",
    )
    webdav_max_concurrent_streams: int = Field(
        default=8,
        description="Maximum concurrent WebDAV GET streams before returning 503",
    )
    blob_cache_enabled: bool = Field(
        default=True,
        description="Enable local disk cache for bounded original-asset byte ranges",
    )
    blob_cache_dir: str = Field(
        default="/tmp/immich-bridge/blob-cache",
        description="Local disk cache directory for original-asset byte ranges",
    )
    blob_cache_max_bytes: int = Field(
        default=1_073_741_824,
        description="Maximum local disk bytes for cached original-asset ranges",
    )
    blob_cache_max_range_bytes: int = Field(
        default=8_388_608,
        description="Maximum single original-asset byte range to cache",
    )
    blob_cache_ttl_seconds: int = Field(
        default=86_400,
        description="Local disk cache TTL for original-asset byte ranges",
    )

    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json", pattern="^(json|console)$")
    immich_bridge_metrics: bool = Field(
        default=False,
        description="Enable verbose WebDAV and upstream streaming metrics",
    )

    model_config = {"env_prefix": "", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()  # type: ignore[call-arg]
