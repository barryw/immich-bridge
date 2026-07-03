"""Tests for the reusable Immich filesystem projection."""

from typing import Any

import pytest

from immich_bridge.fs_model import SearchTruncatedError
from immich_bridge.fs_service import ImmichFilesystem
from immich_bridge.immich_client import ImmichApiError, SearchPage


ASSET = {
    "id": "asset-1",
    "originalFileName": "IMG_0001.jpg",
    "originalMimeType": "image/jpeg",
    "localDateTime": "2026-06-28T16:38:37.120Z",
    "fileCreatedAt": "2026-06-28T16:38:37.120Z",
    "createdAt": "2026-06-28T16:38:37.120Z",
}


class TimelineFallbackClient:
    """Fake Immich client whose timeline endpoint is unavailable."""

    def timeline_buckets(self, *, is_favorite: bool | None = None) -> list[dict[str, Any]]:
        """Simulate an Immich version where timeline buckets cannot be used."""
        raise ImmichApiError("not found", status_code=404)

    def search_assets(self, **kwargs: Any) -> SearchPage:
        """Return one asset for date-bound fallback queries."""
        if kwargs.get("order") in {"asc", "desc"}:
            return SearchPage(items=[ASSET], next_page=None, total=1)

        taken_after = kwargs.get("taken_after")
        taken_before = kwargs.get("taken_before")
        if taken_after and taken_before:
            if taken_after <= ASSET["localDateTime"] < taken_before:
                return SearchPage(items=[ASSET], next_page=None, total=1)
            return SearchPage(items=[], next_page=None, total=0)

        return SearchPage(items=[ASSET], next_page=None, total=1)


class PaginatedClient:
    """Fake Immich client that always has another page."""

    def search_assets(self, **kwargs: Any) -> SearchPage:
        """Return a never-ending pagination sequence."""
        return SearchPage(items=[ASSET], next_page="2", total=2)


class CaptureSearchClient:
    """Fake Immich client that records search filters."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search_assets(self, **kwargs: Any) -> SearchPage:
        """Record search calls and return one page."""
        self.calls.append(kwargs)
        return SearchPage(items=[ASSET], next_page=None, total=1)


def test_timeline_buckets_fall_back_to_search_bounds() -> None:
    """The filesystem should not depend on Immich internal timeline APIs."""
    fs = ImmichFilesystem(TimelineFallbackClient())  # type: ignore[arg-type]

    assert fs.list_date_buckets(None, level="year", is_favorite=False) == ["2026"]


def test_search_all_assets_refuses_partial_directory_listing() -> None:
    """Directory listings should fail explicitly when pagination exceeds the cap."""
    fs = ImmichFilesystem(PaginatedClient(), search_max_pages=1)  # type: ignore[arg-type]

    with pytest.raises(SearchTruncatedError):
        fs.search_all_assets(order="asc")


def test_saved_view_search_filters_include_text_and_geography() -> None:
    """Saved views should pass text and geographic filters to Immich search."""
    client = CaptureSearchClient()
    fs = ImmichFilesystem(client)  # type: ignore[arg-type]

    fs.list_view_assets(
        {
            "query": "beach",
            "city": "Los Angeles",
            "state": "California",
            "country": "USA",
        }
    )

    assert client.calls[0] == {
        "query": "beach",
        "city": "Los Angeles",
        "state": "California",
        "country": "USA",
        "order": "asc",
        "page": 1,
        "size": 500,
        "with_exif": True,
    }
