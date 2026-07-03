"""Benchmark Immich API calls against WebDAV PROPFIND calls."""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import httpx


def timed(label: str, fn: Callable[[], Any]) -> Any:
    """Run a function and print elapsed time."""
    started_at = time.perf_counter()
    result = fn()
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    print(f"{label:<36} {elapsed_ms:>8.0f} ms  {result}")
    return result


def main() -> None:
    """Run a small performance diagnosis suite."""
    immich_url = os.environ["IMMICH_URL"].rstrip("/")
    api_key = os.environ["IMMICH_API_KEY"]
    webdav_url = os.environ.get("WEBDAV_URL", "http://127.0.0.1:8081").rstrip("/")
    webdav_username = os.environ.get("WEBDAV_USERNAME", "barry")

    auth_header = (
        "Basic "
        + base64.b64encode(
            f"{webdav_username}:{api_key}".encode(),
        ).decode()
    )

    client = httpx.Client(timeout=60)

    def immich(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = client.request(
            method,
            f"{immich_url}/{path.lstrip('/')}",
            headers={"x-api-key": api_key},
            json=body,
        )
        response.raise_for_status()
        return response.json()

    def search(body: dict[str, Any]) -> dict[str, Any]:
        payload = immich("POST", "search/metadata", body)
        assets = payload.get("assets", {})
        return {
            "items": len(assets.get("items", [])),
            "next": assets.get("nextPage"),
            "total": assets.get("total"),
        }

    def propfind(path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{webdav_url}{urllib.parse.quote(path, safe='/%')}",
            method="PROPFIND",
            headers={"Depth": "1", "Authorization": auth_header},
        )
        response = urllib.request.urlopen(request, timeout=120)
        payload = response.read()
        hrefs = [
            urllib.parse.unquote(element.text or "")
            for element in ET.fromstring(payload).findall(".//{DAV:}href")
        ]
        return {"status": response.status, "hrefs": len(hrefs), "first": hrefs[:4]}

    timed("immich GET /albums", lambda: len(immich("GET", "albums")))
    timed("immich GET /timeline/buckets", lambda: len(immich("GET", "timeline/buckets")))
    timed(
        "immich search newest",
        lambda: search({"page": 1, "size": 1, "withExif": False, "order": "desc"}),
    )
    timed("dav PROPFIND /", lambda: propfind("/"))
    timed("dav PROPFIND /Albums/", lambda: propfind("/Albums/"))
    timeline = timed("dav PROPFIND /Timeline/", lambda: propfind("/Timeline/"))

    first_year = next(
        (href for href in timeline["first"] if href.rstrip("/") != "/Timeline"),
        None,
    )
    if first_year:
        year = timed("dav PROPFIND first year", lambda: propfind(first_year))
        first_month = next(
            (href for href in year["first"] if href.rstrip("/") != first_year.rstrip("/")),
            None,
        )
        if first_month:
            timed("dav PROPFIND first month", lambda: propfind(first_month))


if __name__ == "__main__":
    main()
