"""Bounded disk cache for original asset byte ranges."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from immich_bridge.logging import get_logger
from immich_bridge.observability import get_request_id

logger = get_logger(__name__)


@dataclass(frozen=True)
class CachedByteRange:
    """Cached byte range payload with absolute offsets."""

    data: bytes
    start: int
    end: int


class BlobCache:
    """Small file-backed cache for bounded original-asset byte ranges."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        max_bytes: int,
        max_range_bytes: int,
        ttl_seconds: int,
        metrics_enabled: bool = False,
    ) -> None:
        """Initialize the cache."""
        self._cache_dir = Path(cache_dir)
        self._max_bytes = max(0, max_bytes)
        self._max_range_bytes = max(0, max_range_bytes)
        self._ttl_seconds = max(0, ttl_seconds)
        self._metrics_enabled = metrics_enabled
        self._lock = threading.Lock()

    @property
    def max_range_bytes(self) -> int:
        """Return the maximum cacheable range size."""
        return self._max_range_bytes

    def cacheable(self, start: int, end: int) -> bool:
        """Return whether a range is safe to materialize and store."""
        if self._max_bytes <= 0 or self._max_range_bytes <= 0 or self._ttl_seconds <= 0:
            return False
        if start < 0 or end < start:
            return False
        return (end - start + 1) <= self._max_range_bytes

    def get(
        self,
        *,
        namespace: str,
        user_scope: str,
        asset_id: str,
        start: int,
        end: int,
    ) -> CachedByteRange | None:
        """Return an exact cached range, if present and fresh."""
        if not self.cacheable(start, end):
            return None

        key = self._key(namespace, user_scope, asset_id, start, end)
        data_path, meta_path = self._paths(key)
        try:
            metadata = self._read_metadata(meta_path)
            if metadata is None or metadata.get("expires_at", 0) <= time.time():
                self._delete_pair(data_path, meta_path)
                self._log("debug", "blob_cache_miss", asset_id=asset_id, start=start, end=end)
                return None
            data = data_path.read_bytes()
        except OSError:
            self._delete_pair(data_path, meta_path)
            self._log("debug", "blob_cache_miss", asset_id=asset_id, start=start, end=end)
            return None

        if len(data) != metadata.get("size"):
            self._delete_pair(data_path, meta_path)
            self._log("debug", "blob_cache_miss", asset_id=asset_id, start=start, end=end)
            return None

        now = time.time()
        try:
            os.utime(data_path, (now, now))
            os.utime(meta_path, (now, now))
        except OSError:
            pass
        self._log(
            "info" if self._metrics_enabled else "debug",
            "blob_cache_hit",
            asset_id=asset_id,
            start=start,
            end=end,
            bytes=len(data),
        )
        return CachedByteRange(data=data, start=start, end=end)

    def set(
        self,
        *,
        namespace: str,
        user_scope: str,
        asset_id: str,
        start: int,
        end: int,
        data: bytes,
    ) -> None:
        """Store an exact range using atomic file replacement."""
        if not self.cacheable(start, end) or len(data) != end - start + 1:
            self._log(
                "debug",
                "blob_cache_store_skipped",
                asset_id=asset_id,
                start=start,
                end=end,
                bytes=len(data),
            )
            return

        key = self._key(namespace, user_scope, asset_id, start, end)
        data_path, meta_path = self._paths(key)
        data_tmp = data_path.with_suffix(".bin.tmp")
        meta_tmp = meta_path.with_suffix(".json.tmp")
        metadata = {
            "namespace": namespace,
            "user_scope_hash": hashlib.sha256(user_scope.encode()).hexdigest(),
            "asset_id": asset_id,
            "start": start,
            "end": end,
            "size": len(data),
            "created_at": time.time(),
            "expires_at": time.time() + self._ttl_seconds,
        }

        with self._lock:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                data_tmp.write_bytes(data)
                meta_tmp.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
                os.replace(data_tmp, data_path)
                os.replace(meta_tmp, meta_path)
                self._log(
                    "info" if self._metrics_enabled else "debug",
                    "blob_cache_store",
                    asset_id=asset_id,
                    start=start,
                    end=end,
                    bytes=len(data),
                )
                self._prune_locked()
            except OSError as e:
                logger.warning(
                    "blob_cache_store_failed",
                    request_id=get_request_id(),
                    error=str(e),
                )
                self._delete_pair(data_tmp, meta_tmp)

    def _key(
        self,
        namespace: str,
        user_scope: str,
        asset_id: str,
        start: int,
        end: int,
    ) -> str:
        payload = json.dumps(
            {
                "namespace": namespace,
                "user_scope": user_scope,
                "asset_id": asset_id,
                "start": start,
                "end": end,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self._cache_dir / f"{key}.bin", self._cache_dir / f"{key}.json"

    def _read_metadata(self, path: Path) -> dict[str, Any] | None:
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return metadata if isinstance(metadata, dict) else None

    def _prune_locked(self) -> None:
        now = time.time()
        entries: list[tuple[float, int, Path, Path]] = []
        total_bytes = 0

        for meta_path in self._cache_dir.glob("*.json"):
            data_path = meta_path.with_suffix(".bin")
            metadata = self._read_metadata(meta_path)
            if metadata is None or not data_path.exists():
                self._delete_pair(data_path, meta_path)
                continue
            size = int(metadata.get("size") or 0)
            if metadata.get("expires_at", 0) <= now or size <= 0:
                self._delete_pair(data_path, meta_path)
                continue
            try:
                last_used = data_path.stat().st_mtime
            except OSError:
                self._delete_pair(data_path, meta_path)
                continue
            total_bytes += size
            entries.append((last_used, size, data_path, meta_path))

        if total_bytes <= self._max_bytes:
            return

        for _last_used, size, data_path, meta_path in sorted(entries):
            self._delete_pair(data_path, meta_path)
            total_bytes -= size
            self._log("debug", "blob_cache_pruned", bytes=size)
            if total_bytes <= self._max_bytes:
                break

    def _delete_pair(self, data_path: Path, meta_path: Path) -> None:
        for path in (data_path, meta_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _log(self, level: str, event: str, **fields: Any) -> None:
        log = logger.info if level == "info" else logger.debug
        log(event, request_id=get_request_id(), **fields)
