"""Reusable Immich filesystem projection service."""

from __future__ import annotations

from datetime import date
from typing import Any

from immich_bridge.fs_model import (
    AlbumEntry,
    DateRange,
    HourRange,
    SearchTruncatedError,
    album_entries,
    asset_datetime,
    asset_entries,
    date_range_from_parts,
    hour_range_from_parts,
    iso_boundary,
    month_bucket_date,
)
from immich_bridge.immich_client import ImmichApiError, ImmichClient
from immich_bridge.logging import get_logger

logger = get_logger(__name__)


class ImmichFilesystem:
    """Immich-backed virtual filesystem operations independent of WebDAV."""

    def __init__(
        self,
        client: ImmichClient,
        *,
        search_page_size: int = 500,
        search_max_pages: int = 20,
        day_folder_split_threshold: int = 1000,
    ) -> None:
        """Initialize the filesystem projection."""
        self._client = client
        self._search_page_size = search_page_size
        self._search_max_pages = search_max_pages
        self._day_folder_split_threshold = day_folder_split_threshold
        self._memo: dict[tuple[Any, ...], Any] = {}

    def _memoized(self, key: tuple[Any, ...], loader: Any) -> Any:
        if key not in self._memo:
            self._memo[key] = loader()
        return self._memo[key]

    def _freeze(self, value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(sorted((key, self._freeze(item)) for key, item in value.items()))
        if isinstance(value, list):
            return tuple(self._freeze(item) for item in value)
        return value

    def _view_search_filters(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Return ImmichClient.search_assets kwargs for a saved view."""
        allowed = {
            "album_ids",
            "person_ids",
            "tag_ids",
            "is_favorite",
            "media_type",
            "taken_after",
            "taken_before",
            "rating",
            "query",
            "original_file_name",
            "ocr",
            "city",
            "state",
            "country",
        }
        search_filters: dict[str, Any] = {}
        for key in allowed:
            value = filters.get(key)
            if value is None or value == "" or value == []:
                continue
            search_filters[key] = value
        return search_filters

    def _with_date_bounds(
        self,
        filters: dict[str, Any],
        *,
        taken_after: str,
        taken_before: str,
    ) -> dict[str, Any]:
        """Return filters narrowed by an additional date interval."""
        narrowed = dict(filters)
        existing_after = narrowed.get("taken_after")
        existing_before = narrowed.get("taken_before")
        if isinstance(existing_after, str) and existing_after > taken_after:
            narrowed["taken_after"] = existing_after
        else:
            narrowed["taken_after"] = taken_after
        if isinstance(existing_before, str) and existing_before < taken_before:
            narrowed["taken_before"] = existing_before
        else:
            narrowed["taken_before"] = taken_before
        return narrowed

    def albums(self) -> list[AlbumEntry]:
        """Return user-visible album entries."""
        return self._memoized(
            ("albums",),
            lambda: album_entries(self._client.list_albums()),
        )

    def date_range_has_assets(self, date_range: DateRange, *, is_favorite: bool) -> bool:
        """Return whether a date range contains any matching assets."""
        return self._memoized(
            ("date-range-has-assets", date_range, is_favorite),
            lambda: bool(
                self._client.search_assets(
                    page=1,
                    size=1,
                    is_favorite=True if is_favorite else None,
                    taken_after=iso_boundary(date_range.start),
                    taken_before=iso_boundary(date_range.end),
                    with_exif=False,
                ).items
            ),
        )

    def date_range_from_parts(self, parts: list[str]) -> DateRange | None:
        """Parse a timeline/favorites path into a date range."""
        return date_range_from_parts(parts)

    def hour_range_from_parts(self, parts: list[str]) -> HourRange | None:
        """Parse an hour bucket from timeline/favorites path parts."""
        return hour_range_from_parts(parts)

    def _date_bounds(self, *, is_favorite: bool) -> tuple[date, date] | None:
        return self._memoized(
            ("date-bounds", is_favorite),
            lambda: self._load_date_bounds(is_favorite=is_favorite),
        )

    def _load_date_bounds(self, *, is_favorite: bool) -> tuple[date, date] | None:
        oldest_page = self._client.search_assets(
            page=1,
            size=1,
            order="asc",
            is_favorite=True if is_favorite else None,
            with_exif=False,
        )
        newest_page = self._client.search_assets(
            page=1,
            size=1,
            order="desc",
            is_favorite=True if is_favorite else None,
            with_exif=False,
        )
        if not oldest_page.items or not newest_page.items:
            return None
        return (
            asset_datetime(oldest_page.items[0]).date(),
            asset_datetime(newest_page.items[0]).date(),
        )

    def _album_date_bounds(self, album_id: str) -> tuple[date, date] | None:
        return self._memoized(
            ("album-date-bounds", album_id),
            lambda: self._load_album_date_bounds(album_id),
        )

    def _load_album_date_bounds(self, album_id: str) -> tuple[date, date] | None:
        oldest_page = self._client.search_assets(
            page=1,
            size=1,
            album_ids=[album_id],
            order="asc",
            with_exif=False,
        )
        newest_page = self._client.search_assets(
            page=1,
            size=1,
            album_ids=[album_id],
            order="desc",
            with_exif=False,
        )
        if not oldest_page.items or not newest_page.items:
            return None
        return (
            asset_datetime(oldest_page.items[0]).date(),
            asset_datetime(newest_page.items[0]).date(),
        )

    def album_date_range_has_assets(self, album_id: str, date_range: DateRange) -> bool:
        """Return whether an album has assets in a date range."""
        return self._memoized(
            ("album-date-range-has-assets", album_id, date_range),
            lambda: bool(
                self._client.search_assets(
                    page=1,
                    size=1,
                    album_ids=[album_id],
                    taken_after=iso_boundary(date_range.start),
                    taken_before=iso_boundary(date_range.end),
                    with_exif=False,
                ).items
            ),
        )

    def _timeline_month_buckets(self, *, is_favorite: bool) -> list[date] | None:
        try:
            buckets = self._client.timeline_buckets(is_favorite=True if is_favorite else None)
        except ImmichApiError as e:
            logger.warning(
                "timeline_buckets_failed_using_search_fallback",
                is_favorite=is_favorite,
                error=str(e),
                status_code=e.status_code,
            )
            return None

        months = [
            bucket_date
            for bucket in buckets
            if (bucket_date := month_bucket_date(bucket)) is not None
        ]
        return sorted(set(months), reverse=True)

    def _fallback_date_buckets(
        self,
        date_range: DateRange | None,
        *,
        level: str,
        is_favorite: bool,
    ) -> list[str]:
        bounds = self._date_bounds(is_favorite=is_favorite)
        if bounds is None:
            return []
        oldest, newest = bounds

        if level == "year":
            buckets: list[str] = []
            for year in range(newest.year, oldest.year - 1, -1):
                candidate = DateRange(date(year, 1, 1), date(year + 1, 1, 1))
                if self.date_range_has_assets(candidate, is_favorite=is_favorite):
                    buckets.append(str(year))
            return buckets

        if level == "month" and date_range is not None:
            buckets = []
            for month in range(12, 0, -1):
                start = date(date_range.start.year, month, 1)
                if start < date_range.start or start >= date_range.end:
                    continue
                end_year, end_month = (
                    (date_range.start.year + 1, 1)
                    if month == 12
                    else (date_range.start.year, month + 1)
                )
                candidate = DateRange(start, date(end_year, end_month, 1))
                if self.date_range_has_assets(candidate, is_favorite=is_favorite):
                    buckets.append(f"{start.year:04d}-{start.month:02d}")
            return buckets

        return []

    def list_date_buckets(
        self,
        date_range: DateRange | None,
        *,
        level: str,
        is_favorite: bool,
    ) -> list[str]:
        """List non-empty year, month, or day bucket names."""
        if level in {"year", "month"}:
            month_buckets = self._memoized(
                ("timeline-month-buckets", is_favorite),
                lambda: self._timeline_month_buckets(is_favorite=is_favorite),
            )
            if month_buckets is None:
                return self._fallback_date_buckets(
                    date_range,
                    level=level,
                    is_favorite=is_favorite,
                )
            if level == "year":
                years = sorted({bucket.year for bucket in month_buckets}, reverse=True)
                return [str(year) for year in years]
            if date_range is None:
                return []
            return [
                f"{bucket.year:04d}-{bucket.month:02d}"
                for bucket in month_buckets
                if date_range.start <= bucket < date_range.end
            ]

        if level == "day" and date_range is not None:
            assets = self.search_all_assets(
                order="desc",
                is_favorite=True if is_favorite else None,
                taken_after=iso_boundary(date_range.start),
                taken_before=iso_boundary(date_range.end),
                with_exif=False,
            )
            days = sorted({asset_datetime(asset).date() for asset in assets}, reverse=True)
            return [day.isoformat() for day in days]

        return []

    def view_date_range_has_assets(
        self,
        filters: dict[str, Any],
        date_range: DateRange,
    ) -> bool:
        """Return whether a saved view has assets in a date range."""
        search_filters = self._view_search_filters(filters)
        return self._memoized(
            ("view-date-range-has-assets", self._freeze(search_filters), date_range),
            lambda: bool(
                self._client.search_assets(
                    page=1,
                    size=1,
                    **self._with_date_bounds(
                        search_filters,
                        taken_after=iso_boundary(date_range.start),
                        taken_before=iso_boundary(date_range.end),
                    ),
                    with_exif=False,
                ).items
            ),
        )

    def _view_date_bounds(self, filters: dict[str, Any]) -> tuple[date, date] | None:
        search_filters = self._view_search_filters(filters)
        return self._memoized(
            ("view-date-bounds", self._freeze(search_filters)),
            lambda: self._load_view_date_bounds(search_filters),
        )

    def _load_view_date_bounds(self, filters: dict[str, Any]) -> tuple[date, date] | None:
        oldest_page = self._client.search_assets(
            page=1,
            size=1,
            order="asc",
            **filters,
            with_exif=False,
        )
        newest_page = self._client.search_assets(
            page=1,
            size=1,
            order="desc",
            **filters,
            with_exif=False,
        )
        if not oldest_page.items or not newest_page.items:
            return None
        return (
            asset_datetime(oldest_page.items[0]).date(),
            asset_datetime(newest_page.items[0]).date(),
        )

    def list_view_date_buckets(
        self,
        filters: dict[str, Any],
        date_range: DateRange | None,
        *,
        level: str,
    ) -> list[str]:
        """List non-empty year, month, or day bucket names for a saved view."""
        bounds = self._view_date_bounds(filters)
        if bounds is None:
            return []
        oldest, newest = bounds

        if level == "year":
            buckets: list[str] = []
            for year in range(newest.year, oldest.year - 1, -1):
                candidate = DateRange(date(year, 1, 1), date(year + 1, 1, 1))
                if self.view_date_range_has_assets(filters, candidate):
                    buckets.append(str(year))
            return buckets

        if level == "month" and date_range is not None:
            buckets = []
            for month in range(12, 0, -1):
                start = date(date_range.start.year, month, 1)
                if start < date_range.start or start >= date_range.end:
                    continue
                end_year, end_month = (
                    (date_range.start.year + 1, 1)
                    if month == 12
                    else (date_range.start.year, month + 1)
                )
                candidate = DateRange(start, date(end_year, end_month, 1))
                if self.view_date_range_has_assets(filters, candidate):
                    buckets.append(f"{start.year:04d}-{start.month:02d}")
            return buckets

        if level == "day" and date_range is not None:
            assets = self.search_all_assets(
                **self._with_date_bounds(
                    self._view_search_filters(filters),
                    taken_after=iso_boundary(date_range.start),
                    taken_before=iso_boundary(date_range.end),
                ),
                order="desc",
                with_exif=False,
            )
            days = sorted({asset_datetime(asset).date() for asset in assets}, reverse=True)
            return [day.isoformat() for day in days]

        return []

    def list_view_assets(self, filters: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """List all named assets for a flat saved view."""
        assets = self.search_all_assets(
            **self._view_search_filters(filters),
            order="asc",
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_view_asset(self, filters: dict[str, Any], name: str) -> dict[str, Any] | None:
        """Resolve one flat saved view asset by display filename."""
        return self.list_view_assets(filters).get(name)

    def list_view_date_assets(
        self,
        filters: dict[str, Any],
        date_range: DateRange,
    ) -> dict[str, dict[str, Any]]:
        """List named saved-view assets inside a concrete date bucket."""
        assets = self.search_all_assets(
            **self._with_date_bounds(
                self._view_search_filters(filters),
                taken_after=iso_boundary(date_range.start),
                taken_before=iso_boundary(date_range.end),
            ),
            order="asc",
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_view_date_asset(
        self,
        filters: dict[str, Any],
        date_range: DateRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve a saved-view asset inside a date bucket."""
        return self.list_view_date_assets(filters, date_range).get(name)

    def should_split_view_day(self, filters: dict[str, Any], date_range: DateRange) -> bool:
        """Return whether a saved-view day folder should expose hour buckets."""
        return self._memoized(
            ("should-split-view-day", self._freeze(filters), date_range),
            lambda: self._load_should_split_view_day(filters, date_range),
        )

    def _load_should_split_view_day(self, filters: dict[str, Any], date_range: DateRange) -> bool:
        page = self._client.search_assets(
            page=1,
            size=1,
            **self._with_date_bounds(
                self._view_search_filters(filters),
                taken_after=iso_boundary(date_range.start),
                taken_before=iso_boundary(date_range.end),
            ),
            with_exif=False,
        )
        total = page.total or len(page.items)
        return total > self._day_folder_split_threshold

    def list_view_hour_buckets(self, filters: dict[str, Any], date_range: DateRange) -> list[str]:
        """List non-empty hour buckets inside a large saved-view day folder."""
        assets = self.search_all_assets(
            **self._with_date_bounds(
                self._view_search_filters(filters),
                taken_after=iso_boundary(date_range.start),
                taken_before=iso_boundary(date_range.end),
            ),
            order="asc",
            with_exif=False,
        )
        hours = sorted({asset_datetime(asset).hour for asset in assets})
        return [f"{hour:02d}" for hour in hours]

    def list_view_hour_assets(
        self,
        filters: dict[str, Any],
        hour_range: HourRange,
    ) -> dict[str, dict[str, Any]]:
        """List saved-view assets inside an hour bucket."""
        assets = self.search_all_assets(
            **self._with_date_bounds(
                self._view_search_filters(filters),
                taken_after=hour_range.start_iso,
                taken_before=hour_range.end_iso,
            ),
            order="asc",
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_view_hour_asset(
        self,
        filters: dict[str, Any],
        hour_range: HourRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve one saved-view asset inside an hour bucket."""
        return self.list_view_hour_assets(filters, hour_range).get(name)

    def list_album_date_buckets(
        self,
        album_id: str,
        date_range: DateRange | None,
        *,
        level: str,
    ) -> list[str]:
        """List non-empty year, month, or day bucket names inside an album."""
        if level == "year":
            bounds = self._album_date_bounds(album_id)
            if bounds is None:
                return []
            oldest, newest = bounds
            buckets = []
            for year in range(newest.year, oldest.year - 1, -1):
                candidate = DateRange(date(year, 1, 1), date(year + 1, 1, 1))
                if self.album_date_range_has_assets(album_id, candidate):
                    buckets.append(str(year))
            return buckets

        if level == "month" and date_range is not None:
            buckets = []
            for month in range(12, 0, -1):
                start = date(date_range.start.year, month, 1)
                if start < date_range.start or start >= date_range.end:
                    continue
                end_year, end_month = (
                    (date_range.start.year + 1, 1)
                    if month == 12
                    else (date_range.start.year, month + 1)
                )
                candidate = DateRange(start, date(end_year, end_month, 1))
                if self.album_date_range_has_assets(album_id, candidate):
                    buckets.append(f"{start.year:04d}-{start.month:02d}")
            return buckets

        if level == "day" and date_range is not None:
            assets = self.search_all_assets(
                album_ids=[album_id],
                order="desc",
                taken_after=iso_boundary(date_range.start),
                taken_before=iso_boundary(date_range.end),
                with_exif=False,
            )
            days = sorted({asset_datetime(asset).date() for asset in assets}, reverse=True)
            return [day.isoformat() for day in days]

        return []

    def search_all_assets(self, **filters: Any) -> list[dict[str, Any]]:
        """Return all matching assets up to the configured page cap."""
        key = (
            "search-all-assets",
            tuple(sorted((name, self._freeze(value)) for name, value in filters.items())),
        )

        def loader() -> list[dict[str, Any]]:
            assets: list[dict[str, Any]] = []
            page_number = 1
            next_page: str | None = None
            while page_number <= self._search_max_pages:
                page = self._client.search_assets(
                    page=page_number,
                    size=self._search_page_size,
                    **filters,
                )
                assets.extend(page.items)
                next_page = page.next_page
                if not next_page:
                    break
                try:
                    page_number = int(next_page)
                except ValueError as e:
                    raise ImmichApiError(
                        f"Immich returned invalid nextPage value: {next_page}",
                    ) from e

            if next_page:
                raise SearchTruncatedError(
                    "Asset result exceeded SEARCH_MAX_PAGES; refusing partial folder listing",
                )
            logger.debug(
                "fs_assets_listed",
                asset_count=len(assets),
                pages=page_number,
                filters=sorted(filters.keys()),
            )
            return assets

        return self._memoized(key, loader)

    def list_album_assets(self, album_id: str) -> dict[str, dict[str, Any]]:
        """List named assets inside an album."""
        assets = self.search_all_assets(
            album_ids=[album_id],
            order="asc",
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_album_asset(self, album_id: str, name: str) -> dict[str, Any] | None:
        """Resolve an album asset by display filename."""
        return self.list_album_assets(album_id).get(name)

    def list_album_date_assets(
        self,
        album_id: str,
        date_range: DateRange,
    ) -> dict[str, dict[str, Any]]:
        """List named album assets inside a concrete date bucket."""
        assets = self.search_all_assets(
            album_ids=[album_id],
            order="asc",
            taken_after=iso_boundary(date_range.start),
            taken_before=iso_boundary(date_range.end),
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_album_date_asset(
        self,
        album_id: str,
        date_range: DateRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve an album asset by display filename inside a date bucket."""
        return self.list_album_date_assets(album_id, date_range).get(name)

    def should_split_album_day(self, album_id: str, date_range: DateRange) -> bool:
        """Return whether an album day folder should expose hour buckets."""
        return self._memoized(
            ("should-split-album-day", album_id, date_range),
            lambda: self._load_should_split_album_day(album_id, date_range),
        )

    def _load_should_split_album_day(self, album_id: str, date_range: DateRange) -> bool:
        page = self._client.search_assets(
            page=1,
            size=1,
            album_ids=[album_id],
            taken_after=iso_boundary(date_range.start),
            taken_before=iso_boundary(date_range.end),
            with_exif=False,
        )
        total = page.total or len(page.items)
        return total > self._day_folder_split_threshold

    def list_album_hour_buckets(self, album_id: str, date_range: DateRange) -> list[str]:
        """List non-empty hour buckets inside a large album day folder."""
        assets = self.search_all_assets(
            album_ids=[album_id],
            order="asc",
            taken_after=iso_boundary(date_range.start),
            taken_before=iso_boundary(date_range.end),
            with_exif=False,
        )
        hours = sorted({asset_datetime(asset).hour for asset in assets})
        return [f"{hour:02d}" for hour in hours]

    def list_album_hour_assets(
        self,
        album_id: str,
        hour_range: HourRange,
    ) -> dict[str, dict[str, Any]]:
        """List album assets inside an hour bucket."""
        assets = self.search_all_assets(
            album_ids=[album_id],
            order="asc",
            taken_after=hour_range.start_iso,
            taken_before=hour_range.end_iso,
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_album_hour_asset(
        self,
        album_id: str,
        hour_range: HourRange,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve an album asset inside an hour bucket."""
        return self.list_album_hour_assets(album_id, hour_range).get(name)

    def list_date_assets(
        self,
        date_range: DateRange,
        *,
        is_favorite: bool,
    ) -> dict[str, dict[str, Any]]:
        """List named assets inside a concrete date bucket."""
        assets = self.search_all_assets(
            order="asc",
            is_favorite=True if is_favorite else None,
            taken_after=iso_boundary(date_range.start),
            taken_before=iso_boundary(date_range.end),
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_date_asset(
        self,
        date_range: DateRange,
        name: str,
        *,
        is_favorite: bool,
    ) -> dict[str, Any] | None:
        """Resolve a date bucket asset by display filename."""
        return self.list_date_assets(date_range, is_favorite=is_favorite).get(name)

    def should_split_day(self, date_range: DateRange, *, is_favorite: bool) -> bool:
        """Return whether a day folder should expose hour buckets."""
        return self._memoized(
            ("should-split-day", date_range, is_favorite),
            lambda: self._load_should_split_day(date_range, is_favorite=is_favorite),
        )

    def _load_should_split_day(self, date_range: DateRange, *, is_favorite: bool) -> bool:
        page = self._client.search_assets(
            page=1,
            size=1,
            is_favorite=True if is_favorite else None,
            taken_after=iso_boundary(date_range.start),
            taken_before=iso_boundary(date_range.end),
            with_exif=False,
        )
        total = page.total or len(page.items)
        return total > self._day_folder_split_threshold

    def list_hour_buckets(self, date_range: DateRange, *, is_favorite: bool) -> list[str]:
        """List non-empty hour buckets inside a large day folder."""
        assets = self.search_all_assets(
            order="asc",
            is_favorite=True if is_favorite else None,
            taken_after=iso_boundary(date_range.start),
            taken_before=iso_boundary(date_range.end),
            with_exif=False,
        )
        hours = sorted({asset_datetime(asset).hour for asset in assets})
        return [f"{hour:02d}" for hour in hours]

    def list_hour_assets(
        self,
        hour_range: HourRange,
        *,
        is_favorite: bool,
    ) -> dict[str, dict[str, Any]]:
        """List assets inside an hour bucket."""
        assets = self.search_all_assets(
            order="asc",
            is_favorite=True if is_favorite else None,
            taken_after=hour_range.start_iso,
            taken_before=hour_range.end_iso,
            with_exif=True,
        )
        return asset_entries(assets)

    def resolve_hour_asset(
        self,
        hour_range: HourRange,
        name: str,
        *,
        is_favorite: bool,
    ) -> dict[str, Any] | None:
        """Resolve an hour bucket asset by display filename."""
        return self.list_hour_assets(hour_range, is_favorite=is_favorite).get(name)
