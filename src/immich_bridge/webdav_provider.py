"""WsgiDAV provider for the V1 Immich Bridge mount."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from wsgidav.dav_error import (  # type: ignore[import-untyped]
    HTTP_BAD_GATEWAY,
    HTTP_FORBIDDEN,
    HTTP_GATEWAY_TIMEOUT,
    HTTP_INSUFFICIENT_STORAGE,
    HTTP_NOT_FOUND,
    DAVError,
)
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider  # type: ignore[import-untyped]

from immich_bridge.admin_store import DEFAULT_MOUNT_SETTINGS, AdminStore, get_admin_store
from immich_bridge.blob_cache import BlobCache
from immich_bridge.cache import get_cache
from immich_bridge.fs_model import (
    ROOT_COLLECTIONS,
    AlbumEntry,
    DateRange,
    HourRange,
    SearchTruncatedError,
    date_range_from_parts,
    hour_range_from_parts,
    safe_segment,
    timestamp,
)
from immich_bridge.fs_service import ImmichFilesystem
from immich_bridge.immich_client import ImmichApiError, ImmichClient
from immich_bridge.logging import get_logger

logger = get_logger(__name__)

MACOS_METADATA_PATTERNS = (
    ".DS_Store",
    "._.DS_Store",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
)
README_FILENAME = "README.txt"
UPLOAD_DEVICE_NAME = "immich-bridge-webdav"
MEDIA_UPLOAD_EXTENSIONS = {
    ".3gp",
    ".ari",
    ".arw",
    ".avi",
    ".avif",
    ".cr2",
    ".cr3",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".nef",
    ".orf",
    ".png",
    ".raf",
    ".raw",
    ".rw2",
    ".tif",
    ".tiff",
    ".webm",
    ".webp",
}


@dataclass(frozen=True)
class ByteRange:
    """Simple single HTTP byte range."""

    start: int
    end: int | None = None


def is_supported_media_upload_name(name: str) -> bool:
    """Return whether a DAV filename is acceptable for raw Immich upload."""
    if not name or "/" in name or name in {README_FILENAME, ".well-known"}:
        return False
    if name in ROOT_COLLECTIONS or is_macos_metadata_file(name):
        return False
    if name.startswith("."):
        return False
    suffix = Path(name).suffix.lower()
    if suffix in MEDIA_UPLOAD_EXTENSIONS:
        return True
    guessed_type = mimetypes.guess_type(name)[0]
    return bool(guessed_type and guessed_type.split("/", 1)[0] in {"image", "video"})


def is_macos_metadata_file(name: str) -> bool:
    """Check if a filename is macOS metadata that can be ignored safely."""
    if name.startswith("._"):
        return True
    return name in MACOS_METADATA_PATTERNS


def byte_range_from_header(range_header: Any) -> ByteRange | None:
    """Return a simple single HTTP byte range."""
    if not isinstance(range_header, str) or not range_header.startswith("bytes="):
        return None

    range_spec = range_header.removeprefix("bytes=").split(",", 1)[0].strip()
    if range_spec.startswith("-"):
        return None

    if "-" not in range_spec:
        return None

    start_text, end_text = [part.strip() for part in range_spec.split("-", 1)]
    if not start_text:
        return None

    try:
        start = int(start_text)
        end = int(end_text) if end_text else None
    except ValueError:
        return None
    if start < 0 or (end is not None and end < start):
        return None
    return ByteRange(start=start, end=end)


def range_start_from_header(range_header: Any) -> int | None:
    """Return the start offset for a simple single HTTP byte range."""
    byte_range = byte_range_from_header(range_header)
    return byte_range.start if byte_range is not None else None


def album_date_range_from_parts(parts: list[str]) -> DateRange | None:
    """Parse album date bucket path parts into a date range."""
    return date_range_from_parts(["Album", *parts])


def album_hour_range_from_parts(parts: list[str]) -> HourRange | None:
    """Parse album date bucket path parts into an hour range."""
    return hour_range_from_parts(["Album", *parts])


def _immich_error_to_dav(error: ImmichApiError) -> DAVError:
    if error.status_code == 404:
        return DAVError(HTTP_NOT_FOUND)
    if error.status_code in {401, 403}:
        return DAVError(HTTP_FORBIDDEN)
    if error.status_code is None:
        return DAVError(HTTP_GATEWAY_TIMEOUT)
    return DAVError(HTTP_BAD_GATEWAY)


class ImmichProvider(DAVProvider):  # type: ignore[misc]
    """V1 WebDAV provider backed by Immich APIs."""

    def __init__(
        self,
        immich_url: str,
        *,
        timeout_seconds: float = 10.0,
        album_cache_ttl_seconds: int = 60,
        search_cache_ttl_seconds: int = 30,
        asset_cache_ttl_seconds: int = 300,
        search_page_size: int = 500,
        search_max_pages: int = 20,
        album_folder_split_threshold: int = 200,
        day_folder_split_threshold: int = 1000,
        webdav_locks_enabled: bool = False,
        metrics_enabled: bool = False,
        blob_cache_enabled: bool = True,
        blob_cache_dir: str = "/tmp/immich-bridge/blob-cache",
        blob_cache_max_bytes: int = 1_073_741_824,
        blob_cache_max_range_bytes: int = 8_388_608,
        blob_cache_ttl_seconds: int = 86_400,
        upload_receipt_ttl_seconds: int = 1800,
        database_url: str = "sqlite:////tmp/immich-bridge/immich-bridge.db",
    ) -> None:
        """Initialize the provider."""
        super().__init__()
        self._immich_url = immich_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._album_cache_ttl_seconds = album_cache_ttl_seconds
        self._search_cache_ttl_seconds = search_cache_ttl_seconds
        self._asset_cache_ttl_seconds = asset_cache_ttl_seconds
        self._search_page_size = search_page_size
        self._search_max_pages = search_max_pages
        self._album_folder_split_threshold = album_folder_split_threshold
        self._day_folder_split_threshold = day_folder_split_threshold
        self._webdav_locks_enabled = webdav_locks_enabled
        self._metrics_enabled = metrics_enabled
        self._upload_receipt_ttl_seconds = upload_receipt_ttl_seconds
        self._database_url = database_url
        self._blob_cache = (
            BlobCache(
                blob_cache_dir,
                max_bytes=blob_cache_max_bytes,
                max_range_bytes=blob_cache_max_range_bytes,
                ttl_seconds=blob_cache_ttl_seconds,
                metrics_enabled=metrics_enabled,
            )
            if blob_cache_enabled
            else None
        )

    def get_resource_inst(
        self,
        path: str,
        environ: dict[str, Any],
    ) -> Any | None:
        """Resolve a WebDAV path to a resource."""
        logger.debug("get_resource_inst_called", path=path)
        environ.setdefault("wsgidav.provider", self)
        normalized = path.rstrip("/") or "/"
        parts = [p for p in normalized.split("/") if p]

        if self._is_virtual_readme_path(parts):
            return ReadmeResource(normalized, environ, self._readme_content(parts))

        if parts and len(parts) > 1 and is_macos_metadata_file(parts[-1]):
            return MacOSMetadataResource(normalized, environ)

        if not parts:
            return RootResource(normalized, environ)

        first = parts[0]
        if first in {"Albums", "Timeline", "Favorites", "Views"} and (
            first not in self.root_collection_names()
        ):
            return None

        if first == "Albums":
            return self._resolve_albums(
                normalized, parts, environ
            ) or self._upload_receipt_resource(
                normalized,
                environ,
            )

        if first in {"Timeline", "Favorites"}:
            return self._resolve_date_view(
                normalized,
                parts,
                environ,
                is_favorite=first == "Favorites",
            ) or self._upload_receipt_resource(normalized, environ)

        if first == "Views":
            return self._resolve_saved_view(normalized, parts, environ)

        if parts == [".well-known"]:
            return WellKnownCollection(normalized, environ)

        if parts == [".well-known", "immich-bridge.json"]:
            return DiagnosticsResource(normalized, environ, self)

        receipt = self._upload_receipt_resource(normalized, environ)
        if receipt is not None:
            return receipt

        return None

    def _is_virtual_readme_path(self, parts: list[str]) -> bool:
        """Return whether the path is a reserved top-level README."""
        if parts == [README_FILENAME]:
            return True
        return (
            len(parts) == 2
            and parts[1] == README_FILENAME
            and parts[0]
            in {
                "Albums",
                "Timeline",
                "Favorites",
                "Views",
            }
        )

    def _readme_content(self, parts: list[str]) -> str:
        """Return README text for a virtual top-level directory."""
        if parts == [README_FILENAME]:
            return (
                "Immich Bridge root\n"
                "\n"
                "This folder is a virtual Immich WebDAV mount. The only regular file "
                "shown at this level is this README.\n"
                "\n"
                "- Albums contains Immich albums.\n"
                "- Timeline contains media grouped by capture date.\n"
                "- Favorites contains favorite media grouped by capture date.\n"
                "- Views contains admin-defined saved searches.\n"
                "\n"
                "Media uploaded at the root is imported into Immich and appears through "
                "Timeline after Immich indexes it; it is not kept as a root file.\n"
            )

        collection_name = parts[0]
        if collection_name == "Albums":
            return (
                "Immich Bridge Albums\n"
                "\n"
                "This folder lists Immich albums as directories. The only regular file "
                "shown here is this README.\n"
                "\n"
                "Create an album by creating a directory here. Drop media into an album "
                "directory to upload it to Immich and add it to that album.\n"
            )
        if collection_name == "Timeline":
            return (
                "Immich Bridge Timeline\n"
                "\n"
                "This folder is a virtual date view generated from Immich metadata. The "
                "only regular file shown here is this README.\n"
                "\n"
                "Use root or album folders for uploads. Timeline contents are organized "
                "by Immich capture time and metadata.\n"
            )
        if collection_name == "Views":
            return (
                "Immich Bridge Views\n"
                "\n"
                "This folder lists admin-defined saved searches as virtual folders. The "
                "only regular file shown here is this README.\n"
                "\n"
                "Configure views in the Immich Bridge admin UI. View folders are read-only "
                "projections of Immich metadata search results.\n"
            )
        return (
            "Immich Bridge Favorites\n"
            "\n"
            "This folder is a virtual date view of favorite media. The only regular "
            "file shown here is this README.\n"
            "\n"
            "Use Immich to change favorite state. Use root or album folders for uploads.\n"
        )

    def _client(self, environ: dict[str, Any]) -> ImmichClient:
        existing = environ.get("immich_bridge.client")
        if isinstance(existing, ImmichClient):
            return existing

        api_key = environ.get("immich.api_key")
        if not api_key:
            raise DAVError(HTTP_FORBIDDEN)
        user_scope = str(
            environ.get("immich.user_id") or hashlib.sha256(str(api_key).encode()).hexdigest(),
        )
        client = ImmichClient(
            base_url=self._immich_url,
            api_key=str(api_key),
            user_scope=user_scope,
            timeout_seconds=self._timeout_seconds,
            album_cache_ttl_seconds=self._album_cache_ttl_seconds,
            search_cache_ttl_seconds=self._search_cache_ttl_seconds,
            asset_cache_ttl_seconds=self._asset_cache_ttl_seconds,
            metrics_enabled=self._metrics_enabled,
            blob_cache=self._blob_cache,
            blob_cache_namespace=self._immich_url,
        )
        environ["immich_bridge.client"] = client
        environ.setdefault("immich_bridge.closeables", []).append(client)
        return client

    def _user_scope(self, environ: dict[str, Any]) -> str | None:
        api_key = environ.get("immich.api_key")
        if environ.get("immich.user_id"):
            return str(environ["immich.user_id"])
        if api_key:
            return hashlib.sha256(str(api_key).encode()).hexdigest()
        return None

    def _receipt_key(self, path: str, environ: dict[str, Any]) -> str | None:
        user_scope = self._user_scope(environ)
        if user_scope is None:
            return None
        digest = hashlib.sha256(path.encode()).hexdigest()
        return f"dav-receipt:{user_scope}:{digest}"

    def remember_upload_receipt(
        self,
        path: str,
        environ: dict[str, Any],
        *,
        asset_id: str,
        filename: str,
        content_type: str,
        size: int,
    ) -> None:
        """Remember a successful upload for short-lived direct DAV lookups."""
        key = self._receipt_key(path, environ)
        if key is None or self._upload_receipt_ttl_seconds <= 0:
            return
        get_cache().set_json(
            key,
            {
                "asset_id": asset_id,
                "filename": filename,
                "content_type": content_type,
                "size": size,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ttl=self._upload_receipt_ttl_seconds,
        )

    def _upload_receipt_resource(
        self,
        path: str,
        environ: dict[str, Any],
    ) -> "UploadReceiptResource | None":
        key = self._receipt_key(path, environ)
        if key is None:
            return None
        receipt = get_cache().get_json(key)
        if not receipt:
            return None
        asset_id = receipt.get("asset_id")
        filename = receipt.get("filename")
        content_type = receipt.get("content_type")
        size = receipt.get("size")
        if not isinstance(asset_id, str) or not isinstance(filename, str):
            return None
        return UploadReceiptResource(
            path,
            environ,
            asset_id=asset_id,
            filename=filename,
            content_type=content_type if isinstance(content_type, str) else None,
            size=size if isinstance(size, int) else None,
        )

    def _filesystem(self, environ: dict[str, Any]) -> ImmichFilesystem:
        existing = environ.get("immich_bridge.filesystem")
        if isinstance(existing, ImmichFilesystem):
            return existing

        filesystem = ImmichFilesystem(
            self._client(environ),
            search_page_size=self._search_page_size,
            search_max_pages=self._search_max_pages,
            day_folder_split_threshold=self._configured_day_folder_split_threshold(),
        )
        environ["immich_bridge.filesystem"] = filesystem
        return filesystem

    def _admin_store(self) -> AdminStore:
        return get_admin_store(self._database_url)

    def root_collection_names(self) -> list[str]:
        """Return enabled top-level DAV collections."""
        mount = self._admin_store().get_setting("mount")
        names: list[str] = []
        if mount.get("albumsEnabled", True):
            names.append("Albums")
        if mount.get("timelineEnabled", True):
            names.append("Timeline")
        if mount.get("favoritesEnabled", True):
            names.append("Favorites")
        if mount.get("viewsEnabled", True):
            names.append("Views")
        names.append(".well-known")
        return names

    def mount_settings(self) -> dict[str, Any]:
        """Return persisted mount settings."""
        return self._admin_store().get_setting("mount")

    def write_policy(self) -> dict[str, Any]:
        """Return persisted WebDAV write policy."""
        return self._admin_store().get_setting("write_policy")

    def write_allowed(self, key: str, *, default: bool) -> bool:
        """Return whether a named WebDAV write operation is enabled."""
        value = self.write_policy().get(key)
        return bool(value) if isinstance(value, bool) else default

    def _configured_album_folder_split_threshold(self) -> int:
        mount = self.mount_settings()
        value = mount.get("albumFolderSplitThreshold")
        if value == DEFAULT_MOUNT_SETTINGS["albumFolderSplitThreshold"]:
            return self._album_folder_split_threshold
        if value is None:
            return self._album_folder_split_threshold
        try:
            return int(value)
        except (TypeError, ValueError):
            return self._album_folder_split_threshold

    def _configured_day_folder_split_threshold(self) -> int:
        mount = self.mount_settings()
        value = mount.get("dayFolderSplitThreshold")
        if value == DEFAULT_MOUNT_SETTINGS["dayFolderSplitThreshold"]:
            return self._day_folder_split_threshold
        if value is None:
            return self._day_folder_split_threshold
        try:
            return int(value)
        except (TypeError, ValueError):
            return self._day_folder_split_threshold

    def saved_view_entries(self) -> list[dict[str, Any]]:
        """Return enabled saved views with DAV-safe display names."""
        views = self._admin_store().list_views(enabled_only=True)
        entries: list[dict[str, Any]] = []
        used: dict[str, int] = {}
        for view in views:
            base = safe_segment(view.get("name"), fallback="View")
            normalized = base.casefold()
            count = used.get(normalized, 0)
            used[normalized] = count + 1
            dav_name = base if count == 0 else f"{base}--{count + 1}"
            entries.append({**view, "davName": dav_name})
        return sorted(entries, key=lambda view: str(view["davName"]).casefold())

    def saved_view_by_name(self, name: str) -> dict[str, Any] | None:
        """Resolve a saved view by DAV path segment."""
        for view in self.saved_view_entries():
            if view["davName"] == name:
                return view
        return None

    def saved_view_by_id(self, view_id: str) -> dict[str, Any] | None:
        """Resolve a saved view by durable id."""
        for view in self.saved_view_entries():
            if str(view.get("id")) == view_id:
                return view
        return None

    def _run_fs(self, operation: Any) -> Any:
        try:
            return operation()
        except ImmichApiError as e:
            raise _immich_error_to_dav(e) from e
        except SearchTruncatedError as e:
            logger.warning("webdav_listing_truncated", error=str(e))
            raise DAVError(HTTP_INSUFFICIENT_STORAGE) from e

    def _albums(self, environ: dict[str, Any]) -> list[AlbumEntry]:
        return self._run_fs(lambda: self._filesystem(environ).albums())

    def create_album(self, environ: dict[str, Any], name: str) -> None:
        """Create an Immich album for a DAV MKCOL request."""
        try:
            self._client(environ).create_album(name)
            environ.pop("immich_bridge.filesystem", None)
        except ImmichApiError as e:
            raise _immich_error_to_dav(e) from e

    def upload_asset(
        self,
        environ: dict[str, Any],
        *,
        dav_path: str,
        filename: str,
        file_path: Path,
        content_type: str | None,
        album_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload an asset and optionally add it to an album."""
        try:
            payload = self._client(environ).upload_asset(
                file_path,
                filename=filename,
                content_type=content_type,
            )
            asset_id = str(payload["id"])
            if album_id is not None:
                self._client(environ).add_asset_to_album(album_id, asset_id)
            self.remember_upload_receipt(
                dav_path,
                environ,
                asset_id=asset_id,
                filename=filename,
                content_type=content_type or "application/octet-stream",
                size=file_path.stat().st_size,
            )
            environ.pop("immich_bridge.filesystem", None)
            return payload
        except ImmichApiError as e:
            raise _immich_error_to_dav(e) from e

    def remove_asset_from_album(
        self,
        environ: dict[str, Any],
        *,
        album_id: str,
        asset_id: str,
    ) -> None:
        """Remove an asset from an album without deleting it from Immich."""
        try:
            self._client(environ).remove_asset_from_album(album_id, asset_id)
            environ.pop("immich_bridge.filesystem", None)
        except ImmichApiError as e:
            raise _immich_error_to_dav(e) from e

    def _resolve_albums(
        self,
        path: str,
        parts: list[str],
        environ: dict[str, Any],
    ) -> Any | None:
        if len(parts) == 1:
            return AlbumsRootCollection(path, environ)

        album = next(
            (entry for entry in self._albums(environ) if entry.name == parts[1]),
            None,
        )
        if album is None:
            return None
        if len(parts) == 2:
            return AlbumCollection(path, environ, album)

        if not self.should_split_album(album):
            if len(parts) != 3:
                return None
            asset = self.resolve_album_asset(environ, album.album_id, parts[2])
            if asset is None:
                return None
            return AssetResource(path, environ, asset, album_id=album.album_id)

        bucket_parts = parts[2:]
        date_range = album_date_range_from_parts(
            bucket_parts[:3] if len(bucket_parts) >= 4 else bucket_parts
        )
        if date_range is None:
            return None

        if not self.album_date_range_has_assets(environ, album.album_id, date_range):
            return None

        if len(bucket_parts) in {1, 2, 3}:
            return AlbumDateBucketCollection(path, environ, album, date_range)

        if len(bucket_parts) == 4:
            hour_range = album_hour_range_from_parts(bucket_parts)
            if hour_range is not None and self.should_split_album_day(
                environ,
                album.album_id,
                date_range,
            ):
                return AlbumHourBucketCollection(path, environ, album, hour_range)

            asset = self.resolve_album_date_asset(
                environ,
                album.album_id,
                date_range,
                bucket_parts[3],
            )
            if asset is None:
                return None
            return AssetResource(path, environ, asset, album_id=album.album_id)

        if len(bucket_parts) == 5:
            day_range = album_date_range_from_parts(bucket_parts[:3])
            hour_range = album_hour_range_from_parts(bucket_parts[:4])
            if day_range is None or hour_range is None:
                return None
            if not self.should_split_album_day(environ, album.album_id, day_range):
                return None
            asset = self.resolve_album_hour_asset(
                environ,
                album.album_id,
                hour_range,
                bucket_parts[4],
            )
            if asset is None:
                return None
            return AssetResource(path, environ, asset, album_id=album.album_id)

        return None

    def _resolve_date_view(
        self,
        path: str,
        parts: list[str],
        environ: dict[str, Any],
        *,
        is_favorite: bool,
    ) -> Any | None:
        if len(parts) == 1:
            return DateRootCollection(path, environ, parts[0], is_favorite=is_favorite)

        fs = self._filesystem(environ)
        date_range = fs.date_range_from_parts(parts[:4] if len(parts) >= 5 else parts)
        if date_range is None:
            return None

        if not self.date_range_has_assets(environ, date_range, is_favorite=is_favorite):
            return None

        if len(parts) in {2, 3, 4}:
            return DateBucketCollection(
                path, environ, parts[0], date_range, is_favorite=is_favorite
            )

        if len(parts) == 5:
            hour_range = fs.hour_range_from_parts(parts)
            if hour_range is not None and self.should_split_day(
                environ,
                date_range,
                is_favorite=is_favorite,
            ):
                return HourBucketCollection(
                    path,
                    environ,
                    hour_range,
                    is_favorite=is_favorite,
                )

            asset = self.resolve_date_asset(
                environ,
                date_range,
                parts[4],
                is_favorite=is_favorite,
            )
            if asset is None:
                return None
            return AssetResource(path, environ, asset)

        if len(parts) == 6:
            day_range = fs.date_range_from_parts(parts[:4])
            hour_range = fs.hour_range_from_parts(parts[:5])
            if day_range is None or hour_range is None:
                return None
            if not self.should_split_day(environ, day_range, is_favorite=is_favorite):
                return None
            asset = self.resolve_hour_asset(
                environ,
                hour_range,
                parts[5],
                is_favorite=is_favorite,
            )
            if asset is None:
                return None
            return AssetResource(path, environ, asset)

        return None

    def _resolve_saved_view(
        self,
        path: str,
        parts: list[str],
        environ: dict[str, Any],
    ) -> Any | None:
        if len(parts) == 1:
            return ViewsRootCollection(path, environ)

        view = self.saved_view_by_name(parts[1])
        if view is None:
            return None

        raw_filters = view.get("filters")
        filters: dict[str, Any] = raw_filters if isinstance(raw_filters, dict) else {}
        layout = str(view.get("layout") or "date_buckets")

        if len(parts) == 2:
            return SavedViewCollection(path, environ, view)

        if layout == "flat":
            if len(parts) == 3:
                asset = self.resolve_saved_view_asset(environ, filters, parts[2])
                if asset is None:
                    return None
                return AssetResource(path, environ, asset)
            return None

        date_parts = ["View", *parts[2:]]
        date_range = self._date_range_from_parts(
            date_parts[:4] if len(date_parts) >= 5 else date_parts
        )
        if date_range is None:
            return None

        if not self.saved_view_date_range_has_assets(environ, filters, date_range):
            return None

        if len(parts) in {3, 4, 5}:
            return SavedViewDateBucketCollection(path, environ, view, date_range)

        if len(parts) == 6:
            hour_range = self._hour_range_from_environ(environ, date_parts)
            if hour_range is not None and self.should_split_saved_view_day(
                environ,
                filters,
                date_range,
            ):
                return SavedViewHourBucketCollection(path, environ, view, hour_range)

            asset = self.resolve_saved_view_date_asset(
                environ,
                filters,
                date_range,
                parts[5],
            )
            if asset is None:
                return None
            return AssetResource(path, environ, asset)

        if len(parts) == 7:
            day_range = self._date_range_from_parts(date_parts[:4])
            hour_range = self._hour_range_from_environ(environ, date_parts[:5])
            if day_range is None or hour_range is None:
                return None
            if not self.should_split_saved_view_day(environ, filters, day_range):
                return None
            asset = self.resolve_saved_view_hour_asset(environ, filters, hour_range, parts[6])
            if asset is None:
                return None
            return AssetResource(path, environ, asset)

        return None

    def _date_range_from_parts(self, parts: list[str]) -> DateRange | None:
        return date_range_from_parts(parts)

    def _date_range_from_environ(
        self,
        environ: dict[str, Any],
        parts: list[str],
    ) -> DateRange | None:
        return date_range_from_parts(parts)

    def _hour_range_from_environ(
        self,
        environ: dict[str, Any],
        parts: list[str],
    ) -> HourRange | None:
        return hour_range_from_parts(parts)

    def date_range_has_assets(
        self,
        environ: dict[str, Any],
        date_range: DateRange,
        *,
        is_favorite: bool,
    ) -> bool:
        """Return whether a date range contains any matching assets."""
        return self._run_fs(
            lambda: self._filesystem(environ).date_range_has_assets(
                date_range,
                is_favorite=is_favorite,
            )
        )

    def should_split_day(
        self,
        environ: dict[str, Any],
        date_range: DateRange,
        *,
        is_favorite: bool,
    ) -> bool:
        """Return whether a day bucket should expose hour buckets."""
        return self._run_fs(
            lambda: self._filesystem(environ).should_split_day(
                date_range,
                is_favorite=is_favorite,
            )
        )

    def list_date_buckets(
        self,
        environ: dict[str, Any],
        date_range: DateRange | None,
        *,
        level: str,
        is_favorite: bool,
    ) -> list[str]:
        """List non-empty year, month, or day bucket names."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_date_buckets(
                date_range,
                level=level,
                is_favorite=is_favorite,
            )
        )

    def saved_view_date_range_has_assets(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        date_range: DateRange,
    ) -> bool:
        """Return whether a saved view has assets in a date range."""
        return self._run_fs(
            lambda: self._filesystem(environ).view_date_range_has_assets(
                filters,
                date_range,
            )
        )

    def list_saved_view_date_buckets(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        date_range: DateRange | None,
        *,
        level: str,
    ) -> list[str]:
        """List non-empty saved-view date bucket names."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_view_date_buckets(
                filters,
                date_range,
                level=level,
            )
        )

    def list_saved_view_assets(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """List named assets in a flat saved view."""
        return self._run_fs(lambda: self._filesystem(environ).list_view_assets(filters))

    def resolve_saved_view_asset(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve a flat saved-view asset."""
        return self._run_fs(lambda: self._filesystem(environ).resolve_view_asset(filters, name))

    def list_saved_view_date_assets(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        date_range: DateRange,
    ) -> dict[str, dict[str, Any]]:
        """List named saved-view assets inside a date bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_view_date_assets(filters, date_range)
        )

    def resolve_saved_view_date_asset(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        date_range: DateRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve a saved-view asset inside a date bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).resolve_view_date_asset(
                filters,
                date_range,
                name,
            )
        )

    def should_split_saved_view_day(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        date_range: DateRange,
    ) -> bool:
        """Return whether a saved-view day bucket should expose hours."""
        return self._run_fs(
            lambda: self._filesystem(environ).should_split_view_day(filters, date_range)
        )

    def list_saved_view_hour_buckets(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        date_range: DateRange,
    ) -> list[str]:
        """List non-empty saved-view hour bucket names."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_view_hour_buckets(filters, date_range)
        )

    def list_saved_view_hour_assets(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        hour_range: HourRange,
    ) -> dict[str, dict[str, Any]]:
        """List named saved-view assets inside an hour bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_view_hour_assets(filters, hour_range)
        )

    def resolve_saved_view_hour_asset(
        self,
        environ: dict[str, Any],
        filters: dict[str, Any],
        hour_range: HourRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve a saved-view asset inside an hour bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).resolve_view_hour_asset(
                filters,
                hour_range,
                name,
            )
        )

    def should_split_album(self, album: AlbumEntry) -> bool:
        """Return whether an album folder should expose date buckets."""
        try:
            asset_count = int(album.album.get("assetCount") or 0)
        except (TypeError, ValueError):
            return False
        return asset_count > self._configured_album_folder_split_threshold()

    def album_date_range_has_assets(
        self,
        environ: dict[str, Any],
        album_id: str,
        date_range: DateRange,
    ) -> bool:
        """Return whether an album date range contains any matching assets."""
        return self._run_fs(
            lambda: self._filesystem(environ).album_date_range_has_assets(
                album_id,
                date_range,
            )
        )

    def should_split_album_day(
        self,
        environ: dict[str, Any],
        album_id: str,
        date_range: DateRange,
    ) -> bool:
        """Return whether an album day bucket should expose hour buckets."""
        return self._run_fs(
            lambda: self._filesystem(environ).should_split_album_day(album_id, date_range)
        )

    def list_album_date_buckets(
        self,
        environ: dict[str, Any],
        album_id: str,
        date_range: DateRange | None,
        *,
        level: str,
    ) -> list[str]:
        """List non-empty year, month, or day bucket names inside an album."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_album_date_buckets(
                album_id,
                date_range,
                level=level,
            )
        )

    def list_album_assets(
        self, environ: dict[str, Any], album_id: str
    ) -> dict[str, dict[str, Any]]:
        """List named assets inside an album."""
        return self._run_fs(lambda: self._filesystem(environ).list_album_assets(album_id))

    def resolve_album_asset(
        self,
        environ: dict[str, Any],
        album_id: str,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve an album asset by display filename."""
        return self._run_fs(lambda: self._filesystem(environ).resolve_album_asset(album_id, name))

    def list_album_date_assets(
        self,
        environ: dict[str, Any],
        album_id: str,
        date_range: DateRange,
    ) -> dict[str, dict[str, Any]]:
        """List named album assets inside a concrete date bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_album_date_assets(album_id, date_range)
        )

    def resolve_album_date_asset(
        self,
        environ: dict[str, Any],
        album_id: str,
        date_range: DateRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve an album asset by display filename inside a date bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).resolve_album_date_asset(
                album_id,
                date_range,
                name,
            )
        )

    def list_album_hour_buckets(
        self,
        environ: dict[str, Any],
        album_id: str,
        date_range: DateRange,
    ) -> list[str]:
        """List non-empty hour buckets inside a large album day folder."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_album_hour_buckets(album_id, date_range)
        )

    def list_album_hour_assets(
        self,
        environ: dict[str, Any],
        album_id: str,
        hour_range: HourRange,
    ) -> dict[str, dict[str, Any]]:
        """List album assets inside an hour bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_album_hour_assets(album_id, hour_range)
        )

    def resolve_album_hour_asset(
        self,
        environ: dict[str, Any],
        album_id: str,
        hour_range: HourRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve an album asset inside an hour bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).resolve_album_hour_asset(
                album_id,
                hour_range,
                name,
            )
        )

    def list_date_assets(
        self,
        environ: dict[str, Any],
        date_range: DateRange,
        *,
        is_favorite: bool,
    ) -> dict[str, dict[str, Any]]:
        """List named assets inside a concrete date bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_date_assets(
                date_range,
                is_favorite=is_favorite,
            )
        )

    def resolve_date_asset(
        self,
        environ: dict[str, Any],
        date_range: DateRange,
        name: str,
        *,
        is_favorite: bool,
    ) -> dict[str, Any] | None:
        """Resolve a date bucket asset by display filename."""
        return self._run_fs(
            lambda: self._filesystem(environ).resolve_date_asset(
                date_range,
                name,
                is_favorite=is_favorite,
            )
        )

    def list_hour_assets(
        self,
        environ: dict[str, Any],
        hour_range: HourRange,
        *,
        is_favorite: bool,
    ) -> dict[str, dict[str, Any]]:
        """List named assets inside an hour bucket."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_hour_assets(
                hour_range,
                is_favorite=is_favorite,
            )
        )

    def resolve_hour_asset(
        self,
        environ: dict[str, Any],
        hour_range: HourRange,
        name: str,
        *,
        is_favorite: bool,
    ) -> dict[str, Any] | None:
        """Resolve an hour-bucket asset by display filename."""
        return self._run_fs(
            lambda: self._filesystem(environ).resolve_hour_asset(
                hour_range,
                name,
                is_favorite=is_favorite,
            )
        )

    def list_hour_buckets(
        self,
        environ: dict[str, Any],
        date_range: DateRange,
        *,
        is_favorite: bool,
    ) -> list[str]:
        """List non-empty hour buckets inside a large day folder."""
        return self._run_fs(
            lambda: self._filesystem(environ).list_hour_buckets(
                date_range,
                is_favorite=is_favorite,
            )
        )


class ReadOnlyResourceMixin:
    """Reject writes on the current read-only V1 resources."""

    def _reject_write(self) -> NoReturn:
        raise DAVError(HTTP_FORBIDDEN)

    def prevent_locking(self) -> bool:
        """Prevent lock discovery/creation on read-only resources."""
        return True

    def create_empty_resource(self, name: str) -> Any:
        """Reject `PUT` or unmapped `LOCK` below this collection."""
        self._reject_write()

    def create_collection(self, name: str) -> Any:
        """Reject `MKCOL` below this collection."""
        self._reject_write()

    def begin_write(self, *, content_type: str | None = None) -> Any:
        """Reject writes to existing resources."""
        self._reject_write()

    def handle_delete(self) -> Any:
        """Reject `DELETE` before recursive processing."""
        self._reject_write()

    def handle_copy(self, dest_path: str, *, depth_infinity: bool) -> Any:
        """Reject `COPY`."""
        self._reject_write()

    def handle_move(self, dest_path: str) -> Any:
        """Reject `MOVE`."""
        self._reject_write()

    def copy_move_single(self, dest_path: str, *, is_move: bool) -> Any:
        """Reject fallback copy/move processing."""
        self._reject_write()

    def support_recursive_delete(self) -> bool:
        """Disable recursive delete for virtual collections."""
        return False

    def support_recursive_move(self, dest_path: str) -> bool:
        """Disable recursive move for virtual collections."""
        return False

    def delete(self) -> Any:
        """Reject fallback delete processing."""
        self._reject_write()


class ReadmeResource(ReadOnlyResourceMixin, DAVNonCollection):  # type: ignore[misc]
    """Virtual read-only README file for top-level WebDAV directories."""

    def __init__(self, path: str, environ: dict[str, Any], content: str) -> None:
        """Initialize the README resource."""
        super().__init__(path, environ)
        self._content = content.encode("utf-8")
        self._created_at = datetime.now(timezone.utc).timestamp()

    def get_display_name(self) -> str:
        """Return the reserved README filename."""
        return README_FILENAME

    def get_content_length(self) -> int:
        """Return content length."""
        return len(self._content)

    def get_content_type(self) -> str:
        """Return plain text content type."""
        return "text/plain; charset=utf-8"

    def get_content(self) -> io.BytesIO:
        """Return README content."""
        return io.BytesIO(self._content)

    def support_etag(self) -> bool:
        """Return stable ETags for README content."""
        return True

    def get_etag(self) -> str:
        """Return an ETag based on README content."""
        return hashlib.sha1(self._content).hexdigest()

    def get_creation_date(self) -> float:
        """Return creation timestamp."""
        return self._created_at

    def get_last_modified(self) -> float:
        """Return modified timestamp."""
        return self._created_at


class UploadSink:
    """Writable file-like object used by WsgiDAV PUT handling."""

    def __init__(self, resource: "UploadResource") -> None:
        """Initialize the sink with a temporary file."""
        self._resource = resource
        self._file = tempfile.NamedTemporaryFile(  # noqa: SIM115
            prefix="immich-bridge-upload-",
            suffix=".tmp",
            delete=False,
        )
        self._path = Path(self._file.name)
        self._bytes_written = 0
        self._closed = False
        resource.set_temp_path(self._path)

    def write(self, data: bytes) -> int:
        """Write upload bytes to disk."""
        written = self._file.write(data)
        self._bytes_written += written
        return written

    def writelines(self, lines: Any) -> None:
        """Write upload byte chunks to disk."""
        for line in lines:
            self.write(line)

    def close(self) -> None:
        """Close the temp file and publish byte count to the resource."""
        if self._closed:
            return
        self._file.close()
        self._resource.set_bytes_written(self._bytes_written)
        self._closed = True


class UploadResource(DAVNonCollection):  # type: ignore[misc]
    """Transient DAV resource that uploads written bytes to Immich."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        *,
        filename: str,
        album_id: str | None = None,
    ) -> None:
        """Initialize the upload target."""
        super().__init__(path, environ)
        self._filename = filename
        self._album_id = album_id
        self._content_type: str | None = None
        self._temp_path: Path | None = None
        self._bytes_written = 0
        self._uploaded_payload: dict[str, Any] | None = None
        self._created_at = datetime.now(timezone.utc).timestamp()

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def set_temp_path(self, path: Path) -> None:
        """Record the active upload temp path."""
        self._temp_path = path

    def set_bytes_written(self, byte_count: int) -> None:
        """Record the uploaded byte count."""
        self._bytes_written = byte_count

    def begin_write(self, *, content_type: str | None = None) -> UploadSink:
        """Open a disk-backed sink for DAV upload bytes."""
        self._content_type = content_type or mimetypes.guess_type(self._filename)[0]
        return UploadSink(self)

    def end_write(self, *, with_errors: bool) -> None:
        """Upload the completed temp file to Immich."""
        temp_path = self._temp_path
        try:
            if with_errors:
                return
            if temp_path is None or not temp_path.exists():
                raise DAVError(HTTP_INSUFFICIENT_STORAGE, "Upload body was not staged")
            self._uploaded_payload = self._immich_provider.upload_asset(
                self.environ,
                dav_path=self.path,
                filename=self._filename,
                file_path=temp_path,
                content_type=self._content_type,
                album_id=self._album_id,
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("upload_temp_cleanup_failed", path=str(temp_path))

    def get_content_length(self) -> int:
        """Return staged upload length."""
        return self._bytes_written

    def get_content_type(self) -> str:
        """Return upload MIME type."""
        return self._content_type or "application/octet-stream"

    def get_content(self) -> io.BytesIO:
        """Return empty content for transient upload resources."""
        return io.BytesIO()

    def support_etag(self) -> bool:
        """Return whether an upload response ETag is available."""
        return self._uploaded_payload is not None

    def get_etag(self) -> str | None:
        """Return an ETag based on uploaded asset id."""
        if not self._uploaded_payload:
            return None
        return hashlib.sha1(str(self._uploaded_payload.get("id", "")).encode()).hexdigest()

    def get_creation_date(self) -> float:
        """Return creation timestamp."""
        return self._created_at

    def get_last_modified(self) -> float:
        """Return modified timestamp."""
        return self._created_at


class UploadReceiptResource(ReadOnlyResourceMixin, DAVNonCollection):  # type: ignore[misc]
    """Short-lived virtual resource for recently uploaded files."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        *,
        asset_id: str,
        filename: str,
        content_type: str | None,
        size: int | None,
    ) -> None:
        """Initialize the upload receipt resource."""
        super().__init__(path, environ)
        self._asset_id = asset_id
        self._filename = filename
        self._content_type = content_type
        self._size = size
        self._created_at = datetime.now(timezone.utc).timestamp()

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_display_name(self) -> str:
        """Return the uploaded filename."""
        return self._filename

    def get_content_length(self) -> int | None:
        """Return uploaded byte length when known."""
        return self._size

    def get_content_type(self) -> str:
        """Return upload MIME type."""
        return (
            self._content_type
            or mimetypes.guess_type(self._filename)[0]
            or "application/octet-stream"
        )

    def get_content(self) -> io.IOBase:
        """Stream the recently uploaded asset from Immich."""
        byte_range = byte_range_from_header(self.environ.get("HTTP_RANGE"))
        try:
            return self._immich_provider._client(self.environ).open_original(
                self._asset_id,
                range_start=byte_range.start if byte_range is not None else None,
                range_end=byte_range.end if byte_range is not None else None,
            )
        except ImmichApiError as e:
            raise _immich_error_to_dav(e) from e

    def support_ranges(self) -> bool:
        """Return whether byte ranges can be proxied for this receipt."""
        return self._size is not None

    def support_etag(self) -> bool:
        """Return stable ETags for upload receipts."""
        return True

    def get_etag(self) -> str:
        """Return an ETag based on the uploaded asset id."""
        return hashlib.sha1(self._asset_id.encode()).hexdigest()

    def get_creation_date(self) -> float:
        """Return creation timestamp."""
        return self._created_at

    def get_last_modified(self) -> float:
        """Return modified timestamp."""
        return self._created_at


class RootResource(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """WebDAV collection representing the root directory."""

    def create_empty_resource(self, name: str) -> UploadResource | MacOSMetadataResource:
        """Treat root PUT as a raw Immich library upload."""
        if is_macos_metadata_file(name):
            return MacOSMetadataResource(f"/{name}", self.environ)
        provider = self.provider
        if isinstance(provider, ImmichProvider) and not provider.write_allowed(
            "rootUploads",
            default=True,
        ):
            raise DAVError(HTTP_FORBIDDEN)
        if not is_supported_media_upload_name(name):
            raise DAVError(HTTP_FORBIDDEN)
        return UploadResource(f"/{name}", self.environ, filename=name)

    def get_member_names(self) -> list[str]:
        """Return V1 root collection names."""
        provider = self.provider
        if isinstance(provider, ImmichProvider):
            return [README_FILENAME, *provider.root_collection_names()]
        return [README_FILENAME, *ROOT_COLLECTIONS]

    def get_member(self, name: str) -> Any | None:
        """Return a root child collection."""
        if name == README_FILENAME:
            return ReadmeResource(
                f"/{name}",
                self.environ,
                self.provider._readme_content([name]),  # type: ignore[attr-defined]
            )
        if name == "Albums":
            return AlbumsRootCollection(f"/{name}", self.environ)
        if name in {"Timeline", "Favorites"}:
            return DateRootCollection(
                f"/{name}", self.environ, name, is_favorite=name == "Favorites"
            )
        if name == "Views":
            return ViewsRootCollection(f"/{name}", self.environ)
        if name == ".well-known":
            return WellKnownCollection(f"/{name}", self.environ)
        return None


class AlbumsRootCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection listing Immich albums."""

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_member_names(self) -> list[str]:
        """Return visible album folder names."""
        return [
            README_FILENAME,
            *[entry.name for entry in self._immich_provider._albums(self.environ)],
        ]

    def create_empty_resource(self, name: str) -> MacOSMetadataResource:
        """Discard harmless client metadata, but reject files directly under Albums."""
        if is_macos_metadata_file(name):
            return MacOSMetadataResource(f"{self.path.rstrip('/')}/{name}", self.environ)
        raise DAVError(HTTP_FORBIDDEN)

    def create_collection(self, name: str) -> None:
        """Create an Immich album."""
        if name == README_FILENAME or is_macos_metadata_file(name):
            raise DAVError(HTTP_FORBIDDEN)
        if not self._immich_provider.write_allowed("albumCreate", default=True):
            raise DAVError(HTTP_FORBIDDEN)
        self._immich_provider.create_album(self.environ, name)

    def get_member(self, name: str) -> "ReadmeResource | AlbumCollection | None":
        """Return an album folder."""
        if name == README_FILENAME:
            return ReadmeResource(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._immich_provider._readme_content(["Albums", name]),
            )
        album = next(
            (entry for entry in self._immich_provider._albums(self.environ) if entry.name == name),
            None,
        )
        if album is None:
            return None
        return AlbumCollection(f"{self.path.rstrip('/')}/{name}", self.environ, album)


class AlbumCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection listing assets in one Immich album."""

    def __init__(self, path: str, environ: dict[str, Any], album: AlbumEntry) -> None:
        """Initialize the album collection."""
        super().__init__(path, environ)
        self._album = album

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_display_name(self) -> str:
        """Return the album display name."""
        return self._album.name

    def get_member_names(self) -> list[str]:
        """Return date buckets for large albums or asset filenames for small albums."""
        if self._immich_provider.should_split_album(self._album):
            return self._immich_provider.list_album_date_buckets(
                self.environ,
                self._album.album_id,
                None,
                level="year",
            )
        return list(
            self._immich_provider.list_album_assets(self.environ, self._album.album_id).keys()
        )

    def create_empty_resource(self, name: str) -> UploadResource | MacOSMetadataResource:
        """Upload a new asset and add it to this album."""
        if is_macos_metadata_file(name):
            return MacOSMetadataResource(f"{self.path.rstrip('/')}/{name}", self.environ)
        if not self._immich_provider.write_allowed("albumUploads", default=True):
            raise DAVError(HTTP_FORBIDDEN)
        if not is_supported_media_upload_name(name):
            raise DAVError(HTTP_FORBIDDEN)
        return UploadResource(
            f"{self.path.rstrip('/')}/{name}",
            self.environ,
            filename=name,
            album_id=self._album.album_id,
        )

    def get_member(self, name: str) -> "AlbumDateBucketCollection | AssetResource | None":
        """Return a date bucket for large albums or an album asset file."""
        if self._immich_provider.should_split_album(self._album):
            date_range = album_date_range_from_parts([name])
            if date_range is None or name not in self.get_member_names():
                return None
            return AlbumDateBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._album,
                date_range,
            )

        asset = self._immich_provider.resolve_album_asset(self.environ, self._album.album_id, name)
        if asset is None:
            return None
        return AssetResource(
            f"{self.path.rstrip('/')}/{name}",
            self.environ,
            asset,
            album_id=self._album.album_id,
        )


class AlbumDateBucketCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection representing a year, month, or day inside a large album."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        album: AlbumEntry,
        date_range: DateRange,
    ) -> None:
        """Initialize the album date bucket collection."""
        super().__init__(path, environ)
        self._album = album
        self._date_range = date_range

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def _bucket_parts(self, child_name: str | None = None) -> list[str]:
        parts = [p for p in self.path.rstrip("/").split("/") if p][2:]
        if child_name is not None:
            parts.append(child_name)
        return parts

    def get_member_names(self) -> list[str]:
        """Return child month, day, hour, or file names."""
        bucket_parts = self._bucket_parts()
        if len(bucket_parts) == 1:
            return self._immich_provider.list_album_date_buckets(
                self.environ,
                self._album.album_id,
                self._date_range,
                level="month",
            )
        if len(bucket_parts) == 2:
            return self._immich_provider.list_album_date_buckets(
                self.environ,
                self._album.album_id,
                self._date_range,
                level="day",
            )
        if self._immich_provider.should_split_album_day(
            self.environ,
            self._album.album_id,
            self._date_range,
        ):
            return self._immich_provider.list_album_hour_buckets(
                self.environ,
                self._album.album_id,
                self._date_range,
            )
        return list(
            self._immich_provider.list_album_date_assets(
                self.environ,
                self._album.album_id,
                self._date_range,
            ).keys()
        )

    def get_member(
        self,
        name: str,
    ) -> "AlbumDateBucketCollection | AlbumHourBucketCollection | AssetResource | None":
        """Return a child album date bucket, hour bucket, or asset file."""
        bucket_parts = self._bucket_parts(name)
        if len(bucket_parts) <= 3:
            date_range = album_date_range_from_parts(bucket_parts)
            if date_range is None or name not in self.get_member_names():
                return None
            return AlbumDateBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._album,
                date_range,
            )

        if self._immich_provider.should_split_album_day(
            self.environ,
            self._album.album_id,
            self._date_range,
        ):
            hour_range = album_hour_range_from_parts(bucket_parts)
            if hour_range is None or name not in self.get_member_names():
                return None
            return AlbumHourBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._album,
                hour_range,
            )

        asset = self._immich_provider.resolve_album_date_asset(
            self.environ,
            self._album.album_id,
            self._date_range,
            name,
        )
        if asset is None:
            return None
        return AssetResource(
            f"{self.path.rstrip('/')}/{name}",
            self.environ,
            asset,
            album_id=self._album.album_id,
        )


class AlbumHourBucketCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection representing an hour inside a large album day bucket."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        album: AlbumEntry,
        hour_range: HourRange,
    ) -> None:
        """Initialize the album hour bucket collection."""
        super().__init__(path, environ)
        self._album = album
        self._hour_range = hour_range

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_display_name(self) -> str:
        """Return the hour bucket name."""
        return f"{self._hour_range.hour:02d}"

    def get_member_names(self) -> list[str]:
        """Return asset filenames in this album hour bucket."""
        return list(
            self._immich_provider.list_album_hour_assets(
                self.environ,
                self._album.album_id,
                self._hour_range,
            ).keys()
        )

    def get_member(self, name: str) -> "AssetResource | None":
        """Return an asset inside this album hour bucket."""
        asset = self._immich_provider.resolve_album_hour_asset(
            self.environ,
            self._album.album_id,
            self._hour_range,
            name,
        )
        if asset is None:
            return None
        return AssetResource(
            f"{self.path.rstrip('/')}/{name}",
            self.environ,
            asset,
            album_id=self._album.album_id,
        )


class DateRootCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection listing year buckets for timeline-like views."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        display_name: str,
        *,
        is_favorite: bool,
    ) -> None:
        """Initialize the date root collection."""
        super().__init__(path, environ)
        self._display_name = display_name
        self._is_favorite = is_favorite

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_display_name(self) -> str:
        """Return the collection display name."""
        return self._display_name

    def get_member_names(self) -> list[str]:
        """Return non-empty year buckets."""
        return [
            README_FILENAME,
            *self._immich_provider.list_date_buckets(
                self.environ,
                None,
                level="year",
                is_favorite=self._is_favorite,
            ),
        ]

    def get_member(self, name: str) -> "ReadmeResource | DateBucketCollection | None":
        """Return a year bucket."""
        if name == README_FILENAME:
            return ReadmeResource(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._immich_provider._readme_content([self._display_name, name]),
            )
        date_range = self._immich_provider._date_range_from_parts([self._display_name, name])
        if date_range is None or name not in self.get_member_names():
            return None
        return DateBucketCollection(
            f"{self.path.rstrip('/')}/{name}",
            self.environ,
            self._display_name,
            date_range,
            is_favorite=self._is_favorite,
        )


class DateBucketCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection representing a year, month, or day date bucket."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        view_name: str,
        date_range: DateRange,
        *,
        is_favorite: bool,
    ) -> None:
        """Initialize the date bucket collection."""
        super().__init__(path, environ)
        self._view_name = view_name
        self._date_range = date_range
        self._is_favorite = is_favorite

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def _parts(self, child_name: str | None = None) -> list[str]:
        parts = [p for p in self.path.rstrip("/").split("/") if p]
        if child_name is not None:
            parts.append(child_name)
        return parts

    def get_member_names(self) -> list[str]:
        """Return child month, day, or file names."""
        parts = self._parts()
        if len(parts) == 2:
            return self._immich_provider.list_date_buckets(
                self.environ,
                self._date_range,
                level="month",
                is_favorite=self._is_favorite,
            )
        if len(parts) == 3:
            return self._immich_provider.list_date_buckets(
                self.environ,
                self._date_range,
                level="day",
                is_favorite=self._is_favorite,
            )
        if self._immich_provider.should_split_day(
            self.environ,
            self._date_range,
            is_favorite=self._is_favorite,
        ):
            return self._immich_provider.list_hour_buckets(
                self.environ,
                self._date_range,
                is_favorite=self._is_favorite,
            )
        return list(
            self._immich_provider.list_date_assets(
                self.environ,
                self._date_range,
                is_favorite=self._is_favorite,
            ).keys(),
        )

    def get_member(
        self,
        name: str,
    ) -> "DateBucketCollection | HourBucketCollection | AssetResource | None":
        """Return a child bucket or asset file."""
        parts = self._parts(name)
        if len(parts) <= 4:
            date_range = self._immich_provider._date_range_from_parts(parts)
            if date_range is None or name not in self.get_member_names():
                return None
            return DateBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._view_name,
                date_range,
                is_favorite=self._is_favorite,
            )

        if self._immich_provider.should_split_day(
            self.environ,
            self._date_range,
            is_favorite=self._is_favorite,
        ):
            hour_range = self._immich_provider._hour_range_from_environ(self.environ, parts)
            if hour_range is None or name not in self.get_member_names():
                return None
            return HourBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                hour_range,
                is_favorite=self._is_favorite,
            )

        asset = self._immich_provider.resolve_date_asset(
            self.environ,
            self._date_range,
            name,
            is_favorite=self._is_favorite,
        )
        if asset is None:
            return None
        return AssetResource(f"{self.path.rstrip('/')}/{name}", self.environ, asset)


class HourBucketCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection representing an hour inside a large day bucket."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        hour_range: HourRange,
        *,
        is_favorite: bool,
    ) -> None:
        """Initialize the hour bucket collection."""
        super().__init__(path, environ)
        self._hour_range = hour_range
        self._is_favorite = is_favorite

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_display_name(self) -> str:
        """Return the hour bucket name."""
        return f"{self._hour_range.hour:02d}"

    def get_member_names(self) -> list[str]:
        """Return asset filenames in this hour bucket."""
        return list(
            self._immich_provider.list_hour_assets(
                self.environ,
                self._hour_range,
                is_favorite=self._is_favorite,
            ).keys()
        )

    def get_member(self, name: str) -> "AssetResource | None":
        """Return an asset inside this hour bucket."""
        asset = self._immich_provider.resolve_hour_asset(
            self.environ,
            self._hour_range,
            name,
            is_favorite=self._is_favorite,
        )
        if asset is None:
            return None
        return AssetResource(f"{self.path.rstrip('/')}/{name}", self.environ, asset)


class ViewsRootCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection listing admin-defined saved views."""

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_display_name(self) -> str:
        """Return the collection display name."""
        return "Views"

    def get_member_names(self) -> list[str]:
        """Return saved view folder names."""
        return [
            README_FILENAME,
            *[str(view["davName"]) for view in self._immich_provider.saved_view_entries()],
        ]

    def get_member(self, name: str) -> "ReadmeResource | SavedViewCollection | None":
        """Return a saved view folder."""
        if name == README_FILENAME:
            return ReadmeResource(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                self._immich_provider._readme_content(["Views", name]),
            )
        view = self._immich_provider.saved_view_by_name(name)
        if view is None:
            return None
        return SavedViewCollection(f"{self.path.rstrip('/')}/{name}", self.environ, view)


class SavedViewCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection representing one saved view."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        view: dict[str, Any],
    ) -> None:
        """Initialize the saved view collection."""
        super().__init__(path, environ)
        self._view_id = str(view.get("id"))
        self._dav_name = str(view.get("davName") or view.get("name") or "View")

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    @property
    def _view(self) -> dict[str, Any] | None:
        view = self._immich_provider.saved_view_by_id(self._view_id)
        if view is None or view.get("davName") != self._dav_name:
            return None
        return view

    def _filters(self, view: dict[str, Any]) -> dict[str, Any]:
        filters = view.get("filters")
        return filters if isinstance(filters, dict) else {}

    def get_display_name(self) -> str:
        """Return the saved view display name."""
        return self._dav_name

    def support_etag(self) -> bool:
        """Return stable collection ETags for saved view metadata."""
        return True

    def get_etag(self) -> str:
        """Return an ETag that changes when the saved view changes."""
        view = self._view
        payload = {
            "id": self._view_id,
            "davName": self._dav_name,
            "updatedAt": view.get("updatedAt") if view else None,
            "layout": view.get("layout") if view else None,
            "filters": view.get("filters") if view else None,
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def get_last_modified(self) -> float:
        """Return saved view update time for DAV cache validation."""
        view = self._view
        updated_at = view.get("updatedAt") if view else None
        if not isinstance(updated_at, str):
            return datetime.now(timezone.utc).timestamp()
        try:
            return datetime.fromisoformat(updated_at).timestamp()
        except ValueError:
            return datetime.now(timezone.utc).timestamp()

    def get_member_names(self) -> list[str]:
        """Return flat asset names or top-level year buckets."""
        view = self._view
        if view is None:
            return []
        filters = self._filters(view)
        if view.get("layout") == "flat":
            return list(
                self._immich_provider.list_saved_view_assets(
                    self.environ,
                    filters,
                ).keys()
            )
        return self._immich_provider.list_saved_view_date_buckets(
            self.environ,
            filters,
            None,
            level="year",
        )

    def get_member(self, name: str) -> "SavedViewDateBucketCollection | AssetResource | None":
        """Return a child bucket or flat asset."""
        view = self._view
        if view is None:
            return None
        filters = self._filters(view)
        if view.get("layout") == "flat":
            asset = self._immich_provider.resolve_saved_view_asset(
                self.environ,
                filters,
                name,
            )
            if asset is None:
                return None
            return AssetResource(f"{self.path.rstrip('/')}/{name}", self.environ, asset)

        parts = ["View", name]
        date_range = self._immich_provider._date_range_from_parts(parts)
        if date_range is None or name not in self.get_member_names():
            return None
        return SavedViewDateBucketCollection(
            f"{self.path.rstrip('/')}/{name}",
            self.environ,
            view,
            date_range,
        )


class SavedViewDateBucketCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Date bucket inside a saved view."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        view: dict[str, Any],
        date_range: DateRange,
    ) -> None:
        """Initialize the saved view date bucket."""
        super().__init__(path, environ)
        self._view_id = str(view.get("id"))
        self._dav_name = str(view.get("davName") or view.get("name") or "View")
        self._date_range = date_range

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    @property
    def _view(self) -> dict[str, Any] | None:
        view = self._immich_provider.saved_view_by_id(self._view_id)
        if view is None or view.get("davName") != self._dav_name:
            return None
        if view.get("layout") == "flat":
            return None
        return view

    def _filters(self, view: dict[str, Any]) -> dict[str, Any]:
        filters = view.get("filters")
        return filters if isinstance(filters, dict) else {}

    def _parts(self, child_name: str | None = None) -> list[str]:
        parts = [p for p in self.path.rstrip("/").split("/") if p]
        if len(parts) >= 2:
            parts = ["View", *parts[2:]]
        if child_name is not None:
            parts.append(child_name)
        return parts

    def get_member_names(self) -> list[str]:
        """Return child month, day, hour, or file names."""
        view = self._view
        if view is None:
            return []
        filters = self._filters(view)
        parts = self._parts()
        if len(parts) == 2:
            return self._immich_provider.list_saved_view_date_buckets(
                self.environ,
                filters,
                self._date_range,
                level="month",
            )
        if len(parts) == 3:
            return self._immich_provider.list_saved_view_date_buckets(
                self.environ,
                filters,
                self._date_range,
                level="day",
            )
        if self._immich_provider.should_split_saved_view_day(
            self.environ,
            filters,
            self._date_range,
        ):
            return self._immich_provider.list_saved_view_hour_buckets(
                self.environ,
                filters,
                self._date_range,
            )
        return list(
            self._immich_provider.list_saved_view_date_assets(
                self.environ,
                filters,
                self._date_range,
            ).keys()
        )

    def get_member(
        self,
        name: str,
    ) -> "SavedViewDateBucketCollection | SavedViewHourBucketCollection | AssetResource | None":
        """Return a child bucket or asset."""
        view = self._view
        if view is None:
            return None
        filters = self._filters(view)
        parts = self._parts(name)
        if len(parts) <= 4:
            date_range = self._immich_provider._date_range_from_parts(parts)
            if date_range is None or name not in self.get_member_names():
                return None
            return SavedViewDateBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                view,
                date_range,
            )

        if self._immich_provider.should_split_saved_view_day(
            self.environ,
            filters,
            self._date_range,
        ):
            hour_range = self._immich_provider._hour_range_from_environ(self.environ, parts)
            if hour_range is None or name not in self.get_member_names():
                return None
            return SavedViewHourBucketCollection(
                f"{self.path.rstrip('/')}/{name}",
                self.environ,
                view,
                hour_range,
            )

        asset = self._immich_provider.resolve_saved_view_date_asset(
            self.environ,
            filters,
            self._date_range,
            name,
        )
        if asset is None:
            return None
        return AssetResource(f"{self.path.rstrip('/')}/{name}", self.environ, asset)


class SavedViewHourBucketCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Hour bucket inside a saved view."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        view: dict[str, Any],
        hour_range: HourRange,
    ) -> None:
        """Initialize the saved view hour bucket."""
        super().__init__(path, environ)
        self._view_id = str(view.get("id"))
        self._dav_name = str(view.get("davName") or view.get("name") or "View")
        self._hour_range = hour_range

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    @property
    def _view(self) -> dict[str, Any] | None:
        view = self._immich_provider.saved_view_by_id(self._view_id)
        if view is None or view.get("davName") != self._dav_name:
            return None
        if view.get("layout") == "flat":
            return None
        return view

    def _filters(self, view: dict[str, Any]) -> dict[str, Any]:
        filters = view.get("filters")
        return filters if isinstance(filters, dict) else {}

    def get_display_name(self) -> str:
        """Return the hour bucket display name."""
        return f"{self._hour_range.hour:02d}"

    def get_member_names(self) -> list[str]:
        """Return asset filenames in this hour bucket."""
        view = self._view
        if view is None:
            return []
        return list(
            self._immich_provider.list_saved_view_hour_assets(
                self.environ,
                self._filters(view),
                self._hour_range,
            ).keys()
        )

    def get_member(self, name: str) -> "AssetResource | None":
        """Return an asset inside this hour bucket."""
        view = self._view
        if view is None:
            return None
        asset = self._immich_provider.resolve_saved_view_hour_asset(
            self.environ,
            self._filters(view),
            self._hour_range,
            name,
        )
        if asset is None:
            return None
        return AssetResource(f"{self.path.rstrip('/')}/{name}", self.environ, asset)


class WellKnownCollection(ReadOnlyResourceMixin, DAVCollection):  # type: ignore[misc]
    """Collection for machine-readable service metadata."""

    def get_display_name(self) -> str:
        """Return the collection display name."""
        return ".well-known"

    def get_member_names(self) -> list[str]:
        """Return child names."""
        return ["immich-bridge.json"]

    def get_member(self, name: str) -> "DiagnosticsResource | None":
        """Return a child resource."""
        if name == "immich-bridge.json":
            provider = self.provider
            return DiagnosticsResource(
                f"{self.path.rstrip('/')}/immich-bridge.json",
                self.environ,
                provider if isinstance(provider, ImmichProvider) else None,
            )
        return None


class DiagnosticsResource(ReadOnlyResourceMixin, DAVNonCollection):  # type: ignore[misc]
    """Machine-readable diagnostics resource."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        provider: ImmichProvider | None,
    ) -> None:
        """Initialize diagnostics resource."""
        super().__init__(path, environ)
        immich_url = provider._immich_url if provider is not None else ""
        write_policy = provider.write_policy() if provider is not None else {}
        payload = {
            "service": "immich-bridge",
            "version": "0.1.0",
            "immichUrl": immich_url,
            "user": environ.get("immich.email") or environ.get("immich.username"),
            "userId": environ.get("immich.user_id"),
            "requestId": environ.get("immich_bridge.request_id"),
            "client": environ.get("webdav.client_name"),
            "capabilities": {
                "webdav": True,
                "nativeFsApi": False,
                "readOnly": False,
                "writes": True,
                "rootUploads": bool(write_policy.get("rootUploads", True)),
                "albumUploads": bool(write_policy.get("albumUploads", True)),
                "albumCreate": bool(write_policy.get("albumCreate", True)),
                "albumMembershipDelete": bool(write_policy.get("albumMembershipDelete", True)),
                "permanentDelete": bool(write_policy.get("permanentDelete", False)),
                "rangeReads": True,
                "locks": bool(provider and provider._webdav_locks_enabled),
                "blobCache": bool(provider and provider._blob_cache),
            },
            "limits": {
                "searchPageSize": provider._search_page_size if provider else None,
                "searchMaxPages": provider._search_max_pages if provider else None,
                "albumFolderSplitThreshold": (
                    provider._album_folder_split_threshold if provider else None
                ),
                "dayFolderSplitThreshold": (
                    provider._day_folder_split_threshold if provider else None
                ),
                "blobCacheMaxRangeBytes": (
                    provider._blob_cache.max_range_bytes
                    if provider and provider._blob_cache
                    else None
                ),
            },
            "metrics": {
                "enabled": bool(provider and provider._metrics_enabled),
            },
        }
        self._content = json.dumps(payload, indent=2, sort_keys=True).encode()
        self._created_at = datetime.now(timezone.utc).timestamp()

    def get_content_length(self) -> int:
        """Return content length."""
        return len(self._content)

    def get_content_type(self) -> str:
        """Return content type."""
        return "application/json"

    def get_content(self) -> io.BytesIO:
        """Return diagnostics content."""
        return io.BytesIO(self._content)

    def support_etag(self) -> bool:
        """Diagnostics content is request-scoped."""
        return False

    def get_etag(self) -> str | None:
        """Return no ETag."""
        return None

    def get_creation_date(self) -> float:
        """Return creation timestamp."""
        return self._created_at

    def get_last_modified(self) -> float:
        """Return modified timestamp."""
        return self._created_at


class AssetResource(ReadOnlyResourceMixin, DAVNonCollection):  # type: ignore[misc]
    """WebDAV file resource that streams an Immich original asset."""

    def __init__(
        self,
        path: str,
        environ: dict[str, Any],
        asset: dict[str, Any],
        *,
        album_id: str | None = None,
    ) -> None:
        """Initialize an asset resource."""
        super().__init__(path, environ)
        self._asset = asset
        self._album_id = album_id

    @property
    def _immich_provider(self) -> ImmichProvider:
        return self.provider  # type: ignore[return-value]

    def get_content_length(self) -> int | None:
        """Return content length from Immich Exif metadata when available."""
        exif = self._asset.get("exifInfo") or {}
        size = exif.get("fileSizeInByte") if isinstance(exif, dict) else None
        return int(size) if isinstance(size, (int, float)) else None

    def get_content_type(self) -> str:
        """Return original asset MIME type."""
        return str(
            self._asset.get("originalMimeType")
            or mimetypes.guess_type(self.name)[0]
            or "application/octet-stream",
        )

    def get_content(self) -> io.IOBase:
        """Stream the original asset from Immich."""
        byte_range = byte_range_from_header(self.environ.get("HTTP_RANGE"))
        try:
            return self._immich_provider._client(self.environ).open_original(
                str(self._asset["id"]),
                range_start=byte_range.start if byte_range is not None else None,
                range_end=byte_range.end if byte_range is not None else None,
            )
        except ImmichApiError as e:
            raise _immich_error_to_dav(e) from e

    def handle_delete(self) -> bool:
        """Remove album membership for album-scoped assets."""
        if self._album_id is None:
            self._reject_write()
        if not self._immich_provider.write_allowed("albumMembershipDelete", default=True):
            self._reject_write()
        self._immich_provider.remove_asset_from_album(
            self.environ,
            album_id=self._album_id,
            asset_id=str(self._asset["id"]),
        )
        return True

    def delete(self) -> None:
        """Fallback delete handler used by WsgiDAV."""
        self.handle_delete()

    def support_ranges(self) -> bool:
        """Return whether byte ranges can be proxied for this asset."""
        return self.get_content_length() is not None

    def support_etag(self) -> bool:
        """Return stable ETags for files."""
        return True

    def get_etag(self) -> str:
        """Return a stable ETag based on asset identity and checksum/update time."""
        source = ":".join(
            [
                str(self._asset.get("id") or ""),
                str(self._asset.get("checksum") or ""),
                str(self._asset.get("updatedAt") or ""),
            ],
        )
        return hashlib.sha1(source.encode()).hexdigest()

    def get_creation_date(self) -> float:
        """Return creation timestamp."""
        return timestamp(self._asset, "fileCreatedAt", "createdAt")

    def get_last_modified(self) -> float:
        """Return modified timestamp."""
        return timestamp(self._asset, "fileModifiedAt", "updatedAt", "createdAt")


class MacOSMetadataResource(DAVNonCollection):  # type: ignore[misc]
    """Virtual resource for macOS metadata files outside the mount root."""

    def __init__(self, path: str, environ: dict[str, Any]) -> None:
        """Initialize metadata resource."""
        super().__init__(path, environ)
        self._created_at = datetime.now(timezone.utc).timestamp()

    def get_content_length(self) -> int:
        """Return content length."""
        return 0

    def get_content_type(self) -> str:
        """Return content type."""
        return "application/octet-stream"

    def get_content(self) -> io.BytesIO:
        """Return empty metadata content."""
        return io.BytesIO()

    def begin_write(self, *, content_type: str | None = None) -> io.BytesIO:
        """Accept writes and discard the data."""
        logger.debug("macos_metadata_write", path=self.path)
        return io.BytesIO()

    def end_write(self, *, with_errors: bool) -> None:
        """Complete a no-op metadata write."""

    def delete(self) -> None:
        """Accept delete as a no-op."""
        logger.debug("macos_metadata_delete", path=self.path)

    def support_etag(self) -> bool:
        """Return no ETag for virtual metadata files."""
        return False

    def get_etag(self) -> str | None:
        """Return no ETag."""
        return None

    def get_creation_date(self) -> float:
        """Return creation timestamp."""
        return self._created_at

    def get_last_modified(self) -> float:
        """Return modified timestamp."""
        return self._created_at
