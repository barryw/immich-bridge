"""Reusable Immich filesystem naming and metadata helpers."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import PurePosixPath
from typing import Any

ROOT_COLLECTIONS = ("Albums", "Timeline", "Favorites", "Views", ".well-known")
WINDOWS_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


class SearchTruncatedError(Exception):
    """Raised when a folder would hide results due to page limits."""


@dataclass(frozen=True)
class AlbumEntry:
    """Resolved album folder entry."""

    name: str
    album: dict[str, Any]

    @property
    def album_id(self) -> str:
        """Return the Immich album ID."""
        return str(self.album["id"])


@dataclass(frozen=True)
class DateRange:
    """Date-filter range used for bucketed virtual folders."""

    start: date
    end: date


@dataclass(frozen=True)
class HourRange:
    """Hour-filter range inside a day bucket."""

    day: date
    hour: int

    @property
    def start_iso(self) -> str:
        """Return inclusive ISO start boundary."""
        return (
            datetime.combine(
                self.day,
                time(self.hour, 0),
                tzinfo=timezone.utc,
            )
            .isoformat()
            .replace("+00:00", "Z")
        )

    @property
    def end_iso(self) -> str:
        """Return exclusive ISO end boundary."""
        if self.hour == 23:
            return iso_boundary(date.fromordinal(self.day.toordinal() + 1))
        return (
            datetime.combine(
                self.day,
                time(self.hour + 1, 0),
                tzinfo=timezone.utc,
            )
            .isoformat()
            .replace("+00:00", "Z")
        )


def date_range_from_parts(parts: list[str]) -> DateRange | None:
    """Parse timeline/favorites path parts into a date range."""
    try:
        if len(parts) == 2 and len(parts[1]) == 4 and parts[1].isdigit():
            year = int(parts[1])
            return DateRange(date(year, 1, 1), date(year + 1, 1, 1))
        if (
            len(parts) == 3
            and len(parts[1]) == 4
            and parts[1].isdigit()
            and len(parts[2]) == 7
            and parts[2].startswith(f"{parts[1]}-")
        ):
            year, month = [int(value) for value in parts[2].split("-")]
            if not 1 <= month <= 12:
                return None
            end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
            return DateRange(date(year, month, 1), date(end_year, end_month, 1))
        if (
            len(parts) == 4
            and len(parts[1]) == 4
            and parts[1].isdigit()
            and len(parts[2]) == 7
            and len(parts[3]) == 10
            and parts[3].startswith(f"{parts[2]}-")
        ):
            start = date.fromisoformat(parts[3])
            return DateRange(start, date.fromordinal(start.toordinal() + 1))
    except ValueError:
        return None
    return None


def hour_range_from_parts(parts: list[str]) -> HourRange | None:
    """Parse an hour bucket from timeline/favorites path parts."""
    if len(parts) != 5 or len(parts[4]) != 2 or not parts[4].isdigit():
        return None
    day_range = date_range_from_parts(parts[:4])
    if day_range is None:
        return None
    hour = int(parts[4])
    if not 0 <= hour <= 23:
        return None
    return HourRange(day=day_range.start, hour=hour)


def parse_immich_datetime(value: Any) -> datetime | None:
    """Parse Immich ISO datetime values."""
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def asset_datetime(asset: dict[str, Any]) -> datetime:
    """Return the best capture datetime for an asset."""
    for key in ("localDateTime", "fileCreatedAt", "createdAt"):
        parsed = parse_immich_datetime(asset.get(key))
        if parsed is not None:
            return parsed
    return datetime.fromtimestamp(0, timezone.utc)


def timestamp(asset: dict[str, Any], *keys: str) -> float:
    """Return the first available timestamp from an asset."""
    for key in keys:
        parsed = parse_immich_datetime(asset.get(key))
        if parsed is not None:
            return parsed.timestamp()
    return datetime.fromtimestamp(0, timezone.utc).timestamp()


def iso_boundary(value: date) -> str:
    """Return an ISO UTC midnight boundary for Immich search filters."""
    return (
        datetime.combine(value, time.min, tzinfo=timezone.utc)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def month_bucket_date(bucket: dict[str, Any]) -> date | None:
    """Parse a timeline bucket month returned by Immich."""
    value = bucket.get("timeBucket")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def short_id(value: Any, length: int = 8) -> str:
    """Return a short stable identifier."""
    text = str(value or "")
    return text.replace("-", "")[:length] or "unknown"


def safe_segment(value: Any, *, fallback: str = "Untitled", max_length: int = 140) -> str:
    """Return a Windows/WebDAV-safe path segment."""
    text = str(value or fallback)
    text = WINDOWS_UNSAFE_CHARS.sub("-", text)
    text = WHITESPACE.sub(" ", text)
    text = text.strip(" .")
    if not text:
        text = fallback
    return text[:max_length].strip(" .") or fallback


def album_entries(albums: list[dict[str, Any]]) -> list[AlbumEntry]:
    """Return deterministic album path entries with collision suffixes."""
    bases: dict[str, list[dict[str, Any]]] = {}
    for album in albums:
        base = safe_segment(album.get("albumName"), fallback="Album")
        bases.setdefault(base, []).append(album)

    entries: list[AlbumEntry] = []
    for base, group in bases.items():
        if len(group) == 1:
            entries.append(AlbumEntry(name=base, album=group[0]))
            continue
        for album in group:
            entries.append(
                AlbumEntry(
                    name=f"{base}--{short_id(album.get('id'))}",
                    album=album,
                ),
            )

    return sorted(entries, key=lambda entry: entry.name.casefold())


def asset_display_name(asset: dict[str, Any]) -> str:
    """Return a deterministic asset filename."""
    asset_id = str(asset.get("id") or "")
    original_name = str(asset.get("originalFileName") or f"{asset_id or 'asset'}.bin")
    original_path = PurePosixPath(original_name)
    stem = safe_segment(original_path.stem, fallback="asset", max_length=90)
    suffix = original_path.suffix.lower()
    if not suffix:
        suffix = mimetypes.guess_extension(str(asset.get("originalMimeType") or "")) or ".bin"
    when = asset_datetime(asset).strftime("%Y-%m-%d %H.%M.%S")
    return f"{when} {stem}--{short_id(asset_id)}{suffix}"


def asset_entries(assets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return deterministic named asset entries."""
    sorted_assets = sorted(
        assets,
        key=lambda asset: (
            asset_datetime(asset),
            str(asset.get("originalFileName") or "").casefold(),
            str(asset.get("id") or ""),
        ),
    )
    entries: dict[str, dict[str, Any]] = {}
    for asset in sorted_assets:
        name = asset_display_name(asset)
        if name in entries:
            path = PurePosixPath(name)
            name = f"{path.stem}--{short_id(asset.get('id'), 12)}{path.suffix}"
        entries[name] = asset
    return entries
