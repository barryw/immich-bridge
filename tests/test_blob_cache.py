"""Tests for the file-backed byte-range cache."""

import time

from immich_bridge.blob_cache import BlobCache


def test_blob_cache_stores_exact_ranges_per_user(tmp_path) -> None:
    """Cached bytes should be scoped by user and exact range."""
    cache = BlobCache(
        tmp_path,
        max_bytes=1024,
        max_range_bytes=16,
        ttl_seconds=60,
    )

    cache.set(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-1",
        start=0,
        end=3,
        data=b"abcd",
    )

    hit = cache.get(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-1",
        start=0,
        end=3,
    )
    assert hit is not None
    assert hit.data == b"abcd"
    assert hit.start == 0
    assert hit.end == 3

    assert (
        cache.get(
            namespace="immich",
            user_scope="user-2",
            asset_id="asset-1",
            start=0,
            end=3,
        )
        is None
    )
    assert (
        cache.get(
            namespace="immich",
            user_scope="user-1",
            asset_id="asset-1",
            start=0,
            end=2,
        )
        is None
    )


def test_blob_cache_prunes_to_max_bytes(tmp_path) -> None:
    """Cache writes should prune least-recently-used entries by total bytes."""
    cache = BlobCache(
        tmp_path,
        max_bytes=4,
        max_range_bytes=8,
        ttl_seconds=60,
    )

    cache.set(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-1",
        start=0,
        end=3,
        data=b"abcd",
    )
    first = cache.get(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-1",
        start=0,
        end=3,
    )
    assert first is not None
    time.sleep(0.01)

    cache.set(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-2",
        start=0,
        end=3,
        data=b"efgh",
    )

    assert (
        cache.get(
            namespace="immich",
            user_scope="user-1",
            asset_id="asset-1",
            start=0,
            end=3,
        )
        is None
    )
    second = cache.get(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-2",
        start=0,
        end=3,
    )
    assert second is not None
    assert second.data == b"efgh"


def test_blob_cache_rejects_oversized_ranges(tmp_path) -> None:
    """Ranges over the configured max should not be written."""
    cache = BlobCache(
        tmp_path,
        max_bytes=1024,
        max_range_bytes=2,
        ttl_seconds=60,
    )

    cache.set(
        namespace="immich",
        user_scope="user-1",
        asset_id="asset-1",
        start=0,
        end=3,
        data=b"abcd",
    )

    assert list(tmp_path.iterdir()) == []
