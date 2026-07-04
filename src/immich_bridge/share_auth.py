"""Immich shared-link validation for guest sessions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from immich_bridge.logging import get_logger

logger = get_logger(__name__)
SHARE_KEY_HEADER = "x-immich-share-key"


class ShareLinkError(Exception):
    """Raised when a shared link cannot be parsed or validated."""


@dataclass(frozen=True)
class ParsedShareLink:
    """A parsed Immich shared-link URL."""

    url: str
    share_key: str
    scheme: str
    hostname: str
    port: int | None


@dataclass(frozen=True)
class ShareIdentity:
    """Validated Immich shared-link metadata."""

    share_id: str
    name: str
    description: str | None
    allow_download: bool
    allow_upload: bool
    expires_at: str | None
    asset_count: int | None
    album_id: str | None


def parse_share_link(url: str) -> ParsedShareLink:
    """Parse an Immich share URL and extract its share key."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ShareLinkError("Enter a valid Immich share URL")

    query = parse_qs(parsed.query)
    share_key = _first_query_value(query, "key", "shareKey", "sharedKey")
    if share_key is None:
        share_key = _share_key_from_path(parsed.path)
    if not share_key:
        raise ShareLinkError("Immich share URL did not include a share key")

    return ParsedShareLink(
        url=url.strip(),
        share_key=share_key,
        scheme=parsed.scheme,
        hostname=parsed.hostname.casefold(),
        port=parsed.port,
    )


def share_key_hash(share_key: str) -> str:
    """Return a stable, non-secret share-key hash."""
    return hashlib.sha256(share_key.encode()).hexdigest()


def share_link_matches_library(
    share_link: ParsedShareLink,
    library_url: str,
    *,
    public_url: str | None = None,
    share_hosts: list[str] | None = None,
) -> bool:
    """Return whether a share URL belongs to a configured Immich library."""
    if _url_matches_share_link(share_link, library_url):
        return True
    if public_url and _url_matches_share_link(share_link, public_url):
        return True
    return any(_host_matches_share_link(share_link, host) for host in share_hosts or [])


def validate_immich_share_link(
    immich_url: str,
    share_key: str,
    *,
    timeout_seconds: float = 10.0,
) -> ShareIdentity:
    """Validate a shared link against Immich and return share metadata."""
    base_url = immich_url.rstrip("/")
    headers = {SHARE_KEY_HEADER: share_key}
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        response = _first_successful_share_response(client, base_url, headers)

    if response.status_code in {401, 403, 404}:
        raise ShareLinkError("Immich shared link is invalid or expired")
    if response.status_code >= 400:
        raise ShareLinkError(f"Immich shared-link validation failed: HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as e:
        raise ShareLinkError("Immich shared-link validation returned invalid JSON") from e
    if not isinstance(payload, dict):
        raise ShareLinkError("Immich shared-link validation returned an unexpected payload")

    return _identity_from_payload(payload)


def _first_successful_share_response(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
) -> httpx.Response:
    """Call known Immich shared-link metadata endpoints."""
    response = client.get(f"{base_url}/shared-link/me", headers=headers)
    if response.status_code != 404:
        return response
    return client.get(f"{base_url}/shared-links/me", headers=headers)


def _identity_from_payload(payload: dict[str, Any]) -> ShareIdentity:
    share_id = str(payload.get("id") or payload.get("key") or "shared-link")
    raw_album = payload.get("album")
    album: dict[str, Any] = raw_album if isinstance(raw_album, dict) else {}
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    description = (
        payload.get("description") if isinstance(payload.get("description"), str) else None
    )
    album_name = album.get("albumName") if isinstance(album.get("albumName"), str) else None
    name = (
        description
        or album_name
        or (str(payload.get("type")).title() if payload.get("type") else None)
        or "Immich Share"
    )
    expires_at = payload.get("expiresAt") if isinstance(payload.get("expiresAt"), str) else None
    album_id = album.get("id") if isinstance(album.get("id"), str) else None
    asset_count = _optional_int(payload.get("assetCount"))
    if asset_count is None and assets:
        asset_count = len(assets)

    return ShareIdentity(
        share_id=share_id,
        name=name,
        description=description,
        allow_download=bool(payload.get("allowDownload", True)),
        allow_upload=bool(payload.get("allowUpload", False)),
        expires_at=expires_at,
        asset_count=asset_count,
        album_id=album_id,
    )


def _first_query_value(query: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return None


def _share_key_from_path(path: str) -> str | None:
    segments = [segment for segment in path.split("/") if segment]
    for marker in ("share", "shared-link", "shared-links"):
        if marker in segments:
            index = segments.index(marker)
            if len(segments) > index + 1:
                return segments[index + 1]
    return segments[-1] if segments else None


def _normalized_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _url_matches_share_link(share_link: ParsedShareLink, url: str) -> bool:
    parsed = urlparse(url.rstrip("/"))
    if not parsed.hostname:
        return False
    return _host_and_port_match(
        share_link,
        parsed.hostname,
        _normalized_port(parsed.scheme, _safe_port(parsed)),
    )


def _host_matches_share_link(share_link: ParsedShareLink, host: str) -> bool:
    raw_host = host.strip().casefold()
    if not raw_host:
        return False
    parsed = urlparse(raw_host.rstrip("/") if "://" in raw_host else f"//{raw_host}")
    if not parsed.hostname:
        return False

    port = _safe_port(parsed)
    if parsed.scheme in {"http", "https"}:
        port = _normalized_port(parsed.scheme, port)
    return _host_and_port_match(share_link, parsed.hostname, port)


def _host_and_port_match(
    share_link: ParsedShareLink,
    host: str,
    port: int | None,
) -> bool:
    if host.casefold() != share_link.hostname:
        return False
    if port is None:
        return True
    return port == _normalized_port(share_link.scheme, share_link.port)


def _safe_port(parsed: Any) -> int | None:
    try:
        return parsed.port
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
