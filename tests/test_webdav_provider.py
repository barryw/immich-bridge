"""Tests for the V1 WebDAV provider."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from wsgidav.dav_error import HTTP_BAD_GATEWAY, HTTP_FORBIDDEN, DAVError

from immich_bridge.immich_client import ImmichApiError, SearchPage
from immich_bridge.webdav_provider import (
    AlbumDateBucketCollection,
    ImmichProvider,
    MacOSMetadataResource,
    README_FILENAME,
    ReadmeResource,
    RootResource,
    UploadReceiptResource,
    byte_range_from_header,
    is_macos_metadata_file,
    range_start_from_header,
)


ASSET = {
    "id": "c1ada054-cbb6-4305-b9d3-52c17f869115",
    "originalFileName": "IMG:0001.HEIC",
    "originalMimeType": "image/heic",
    "localDateTime": "2026-06-28T16:38:37.120Z",
    "fileCreatedAt": "2026-06-28T16:38:37.120Z",
    "fileModifiedAt": "2026-06-28T16:38:37.120Z",
    "updatedAt": "2026-06-28T16:40:00.000Z",
    "checksum": "checksum-1",
    "exifInfo": {"fileSizeInByte": 1234},
}
ASSET_CAPTURE_TIME = "2026-06-28T16:38:37.120Z"


class FakeImmichClient:
    """Small fake for provider tests."""

    def __init__(self) -> None:
        self.created_albums: list[str] = []
        self.uploads: list[dict[str, Any]] = []
        self.album_adds: list[tuple[str, str]] = []
        self.album_removes: list[tuple[str, str]] = []

    def list_albums(self) -> list[dict[str, Any]]:
        """Return a visible album."""
        return [
            {
                "id": "album-1",
                "albumName": "Vacation/2026",
                "assetCount": 1,
                "updatedAt": "2026-06-29T00:00:00.000Z",
            },
        ]

    def search_assets(self, **kwargs: Any) -> SearchPage:
        """Return matching assets for the provider query."""
        taken_after = kwargs.get("taken_after")
        taken_before = kwargs.get("taken_before")
        if taken_after and taken_before:
            if taken_after <= ASSET_CAPTURE_TIME < taken_before:
                return SearchPage(items=[ASSET], next_page=None, total=1)
            return SearchPage(items=[], next_page=None, total=0)

        if kwargs.get("album_ids") == ["album-1"]:
            return SearchPage(items=[ASSET], next_page=None, total=1)

        if kwargs.get("size") == 1:
            return SearchPage(items=[ASSET], next_page=None, total=1)

        return SearchPage(items=[ASSET], next_page=None, total=1)

    def timeline_buckets(self, *, is_favorite: bool | None = None) -> list[dict[str, Any]]:
        """Return timeline month buckets."""
        if is_favorite:
            return []
        return [{"timeBucket": "2026-06-01", "count": 1}]

    def create_album(self, name: str) -> dict[str, Any]:
        """Record album creation."""
        self.created_albums.append(name)
        return {"id": "created-album", "albumName": name}

    def upload_asset(
        self,
        file_path: str | Path,
        *,
        filename: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Record an upload."""
        data = Path(file_path).read_bytes()
        self.uploads.append(
            {
                "filename": filename,
                "content_type": content_type,
                "data": data,
            }
        )
        return {"id": f"uploaded-{len(self.uploads)}", "status": "created"}

    def add_asset_to_album(self, album_id: str, asset_id: str) -> None:
        """Record album membership add."""
        self.album_adds.append((album_id, asset_id))

    def remove_asset_from_album(self, album_id: str, asset_id: str) -> None:
        """Record album membership removal."""
        self.album_removes.append((album_id, asset_id))

    def open_original(self, asset_id: str, **kwargs: Any) -> io.BytesIO:
        """Return uploaded content for receipt tests."""
        return io.BytesIO(b"uploaded")


class FailingAlbumAddClient(FakeImmichClient):
    """Fake that uploads successfully but fails album membership add."""

    def add_asset_to_album(self, album_id: str, asset_id: str) -> None:
        """Record and fail album membership add."""
        self.album_adds.append((album_id, asset_id))
        raise ImmichApiError("album add failed", status_code=500)


def provider_with_fake_client() -> ImmichProvider:
    """Return a provider that uses fake Immich data."""
    provider = ImmichProvider("http://immich.test/api")
    provider._client = lambda environ: FakeImmichClient()  # type: ignore[method-assign]
    return provider


def provider_with_client(client: FakeImmichClient) -> ImmichProvider:
    """Return a provider backed by a specific fake client."""
    provider = ImmichProvider("http://immich.test/api")
    provider._client = lambda environ: client  # type: ignore[method-assign]
    return provider


def auth_environ() -> dict[str, Any]:
    """Return minimal authenticated WSGI environ."""
    return {"immich.username": "barry", "immich.user_id": "user-1", "immich.api_key": "key"}


def test_root_lists_v1_collections() -> None:
    """Root should expose the V1 mount contract."""
    provider = ImmichProvider("http://immich.test/api")
    resource = provider.get_resource_inst("/", {"immich.username": "barry"})

    assert isinstance(resource, RootResource)
    assert resource.get_member_names() == [
        README_FILENAME,
        "Albums",
        "Timeline",
        "Favorites",
        ".well-known",
    ]


def test_albums_collection_lists_albums_and_assets() -> None:
    """Albums should be populated from Immich album and asset search results."""
    provider = provider_with_fake_client()
    albums = provider.get_resource_inst("/Albums", auth_environ())

    assert albums is not None
    assert albums.get_member_names() == [README_FILENAME, "Vacation-2026"]

    album = albums.get_member("Vacation-2026")
    assert album is not None
    assert album.get_member_names() == ["2026-06-28 16.38.37 IMG-0001--c1ada054.heic"]

    asset = album.get_member("2026-06-28 16.38.37 IMG-0001--c1ada054.heic")
    assert asset is not None
    assert asset.get_content_length() == 1234
    assert asset.get_content_type() == "image/heic"
    assert asset.support_ranges() is True


def test_large_album_collection_splits_into_date_buckets() -> None:
    """Large albums should avoid exposing every asset in one Finder folder."""
    provider = ImmichProvider("http://immich.test/api", album_folder_split_threshold=0)
    provider._client = lambda environ: FakeImmichClient()  # type: ignore[method-assign]
    album = provider.get_resource_inst("/Albums/Vacation-2026", auth_environ())

    assert album is not None
    assert album.get_member_names() == ["2026"]

    year = album.get_member("2026")
    assert isinstance(year, AlbumDateBucketCollection)
    assert year.get_member_names() == ["2026-06"]

    month = year.get_member("2026-06")
    assert isinstance(month, AlbumDateBucketCollection)
    assert month.get_member_names() == ["2026-06-28"]

    day = month.get_member("2026-06-28")
    assert isinstance(day, AlbumDateBucketCollection)
    assert day.get_member_names() == ["2026-06-28 16.38.37 IMG-0001--c1ada054.heic"]

    asset = provider.get_resource_inst(
        "/Albums/Vacation-2026/2026/2026-06/2026-06-28/2026-06-28 16.38.37 IMG-0001--c1ada054.heic",
        auth_environ(),
    )
    assert asset is not None
    assert asset.get_content_length() == 1234


def test_timeline_lists_date_buckets_and_assets() -> None:
    """Timeline should expose year, month, day, then assets."""
    provider = provider_with_fake_client()
    timeline = provider.get_resource_inst("/Timeline", auth_environ())

    assert timeline is not None
    assert timeline.get_member_names() == [README_FILENAME, "2026"]

    year = timeline.get_member("2026")
    assert year is not None
    assert year.get_member_names() == ["2026-06"]

    month = year.get_member("2026-06")
    assert month is not None
    assert month.get_member_names() == ["2026-06-28"]

    day = month.get_member("2026-06-28")
    assert day is not None
    assert day.get_member_names() == ["2026-06-28 16.38.37 IMG-0001--c1ada054.heic"]


def test_large_day_folder_splits_into_hour_buckets() -> None:
    """Large day folders should be split into hour buckets before listing files."""
    provider = ImmichProvider("http://immich.test/api", day_folder_split_threshold=0)
    provider._client = lambda environ: FakeImmichClient()  # type: ignore[method-assign]
    day = provider.get_resource_inst("/Timeline/2026/2026-06/2026-06-28", auth_environ())

    assert day is not None
    assert day.get_member_names() == ["16"]

    hour = day.get_member("16")
    assert hour is not None
    assert hour.get_member_names() == ["2026-06-28 16.38.37 IMG-0001--c1ada054.heic"]


def test_virtual_readmes_are_only_files_at_top_level_directories() -> None:
    """Root-level virtual directories should expose README files but no media files."""
    provider = provider_with_fake_client()

    root_readme = provider.get_resource_inst(f"/{README_FILENAME}", auth_environ())
    albums_readme = provider.get_resource_inst(f"/Albums/{README_FILENAME}", auth_environ())
    timeline_readme = provider.get_resource_inst(f"/Timeline/{README_FILENAME}", auth_environ())
    favorites = provider.get_resource_inst("/Favorites", auth_environ())

    assert isinstance(root_readme, ReadmeResource)
    assert root_readme.get_content_type() == "text/plain; charset=utf-8"
    assert "only regular file shown at this level" in root_readme.get_content().read().decode()

    assert isinstance(albums_readme, ReadmeResource)
    assert isinstance(timeline_readme, ReadmeResource)
    assert favorites is not None
    assert favorites.get_member_names() == [README_FILENAME]
    assert isinstance(favorites.get_member(README_FILENAME), ReadmeResource)

    assert provider.get_resource_inst("/IMG_0001.jpg", auth_environ()) is None
    assert provider.get_resource_inst("/Albums/IMG_0001.jpg", auth_environ()) is None
    assert provider.get_resource_inst("/Timeline/IMG_0001.jpg", auth_environ()) is None
    assert provider.get_resource_inst("/Favorites/IMG_0001.jpg", auth_environ()) is None


def test_root_rejects_non_media_collection_delete_and_move_operations() -> None:
    """The fixed root namespace should only accept media file uploads."""
    provider = ImmichProvider("http://immich.test/api")
    resource = provider.get_resource_inst("/", {"immich.username": "barry"})

    assert isinstance(resource, RootResource)
    assert resource.prevent_locking() is True

    for operation in (
        lambda: resource.create_empty_resource("new-file.txt"),
        lambda: resource.create_collection("New Folder"),
        lambda: resource.handle_delete(),
        lambda: resource.handle_copy("/copy", depth_infinity=True),
        lambda: resource.handle_move("/moved"),
    ):
        with pytest.raises(DAVError) as exc_info:
            operation()
        assert exc_info.value.value == HTTP_FORBIDDEN


def test_root_discards_macos_metadata_writes() -> None:
    """Finder metadata writes should not become Immich uploads."""
    client = FakeImmichClient()
    provider = provider_with_client(client)
    root = provider.get_resource_inst("/", auth_environ())

    assert isinstance(root, RootResource)
    metadata = root.create_empty_resource(".DS_Store")
    assert isinstance(metadata, MacOSMetadataResource)
    sink = metadata.begin_write(content_type="application/octet-stream")
    assert sink.write(b"finder-noise") == len(b"finder-noise")
    sink.close()
    metadata.end_write(with_errors=False)

    assert client.uploads == []


def test_root_put_uploads_media_and_creates_receipt() -> None:
    """Root PUT should upload media without listing it as a root child."""
    client = FakeImmichClient()
    provider = provider_with_client(client)
    environ = auth_environ()
    root = provider.get_resource_inst("/", environ)

    assert isinstance(root, RootResource)
    upload = root.create_empty_resource("IMG_0001.jpg")
    sink = upload.begin_write(content_type="image/jpeg")
    sink.write(b"image-bytes")
    sink.close()
    upload.end_write(with_errors=False)

    assert client.uploads == [
        {
            "filename": "IMG_0001.jpg",
            "content_type": "image/jpeg",
            "data": b"image-bytes",
        }
    ]
    assert "IMG_0001.jpg" not in root.get_member_names()

    receipt = provider.get_resource_inst("/IMG_0001.jpg", environ)
    assert isinstance(receipt, UploadReceiptResource)
    assert receipt.get_content_length() == len(b"image-bytes")
    assert receipt.get_content_type() == "image/jpeg"


def test_root_put_with_errors_cleans_staged_file_and_skips_upload() -> None:
    """Aborted PUTs should clean their temp file without calling Immich."""
    client = FakeImmichClient()
    provider = provider_with_client(client)
    root = provider.get_resource_inst("/", auth_environ())

    assert isinstance(root, RootResource)
    upload = root.create_empty_resource("IMG_0001.jpg")
    sink = upload.begin_write(content_type="image/jpeg")
    sink.write(b"partial-image")
    sink.close()
    temp_path = upload._temp_path
    assert temp_path is not None
    assert temp_path.exists()

    upload.end_write(with_errors=True)

    assert not temp_path.exists()
    assert client.uploads == []


def test_root_put_rejects_non_media_files() -> None:
    """Root upload target should only accept media-like filenames."""
    provider = provider_with_fake_client()
    root = provider.get_resource_inst("/", auth_environ())

    assert isinstance(root, RootResource)
    with pytest.raises(DAVError) as exc_info:
        root.create_empty_resource("notes.txt")
    assert exc_info.value.value == HTTP_FORBIDDEN


def test_top_level_collections_reject_delete_and_move_operations() -> None:
    """Fixed root entries like Albums should not be removable or renameable."""
    provider = ImmichProvider("http://immich.test/api")
    resource = provider.get_resource_inst("/Albums", {"immich.username": "barry"})

    assert resource is not None
    assert resource.prevent_locking() is True

    for operation in (
        lambda: resource.handle_delete(),
        lambda: resource.handle_copy("/Albums Copy", depth_infinity=True),
        lambda: resource.handle_move("/Renamed Albums"),
    ):
        with pytest.raises(DAVError) as exc_info:
            operation()
        assert exc_info.value.value == HTTP_FORBIDDEN


def test_albums_root_mkcol_creates_album_but_rejects_files() -> None:
    """MKCOL below Albums creates albums; PUT below Albums remains forbidden."""
    client = FakeImmichClient()
    provider = provider_with_client(client)
    albums = provider.get_resource_inst("/Albums", auth_environ())

    assert albums is not None
    albums.create_collection("New Album")
    assert client.created_albums == ["New Album"]

    with pytest.raises(DAVError) as exc_info:
        albums.create_empty_resource("IMG_0001.jpg")
    assert exc_info.value.value == HTTP_FORBIDDEN


def test_album_put_uploads_and_adds_to_album() -> None:
    """PUT inside an album should upload then add the new asset to that album."""
    client = FakeImmichClient()
    provider = provider_with_client(client)
    album = provider.get_resource_inst("/Albums/Vacation-2026", auth_environ())

    assert album is not None
    upload = album.create_empty_resource("IMG_0002.heic")
    sink = upload.begin_write(content_type="image/heic")
    sink.write(b"heic-bytes")
    sink.close()
    upload.end_write(with_errors=False)

    assert client.uploads[0]["filename"] == "IMG_0002.heic"
    assert client.uploads[0]["data"] == b"heic-bytes"
    assert client.album_adds == [("album-1", "uploaded-1")]


def test_album_put_album_add_failure_cleans_staged_file() -> None:
    """Partial album upload failures should surface as DAV errors and clean temp files."""
    client = FailingAlbumAddClient()
    provider = provider_with_client(client)
    album = provider.get_resource_inst("/Albums/Vacation-2026", auth_environ())

    assert album is not None
    upload = album.create_empty_resource("IMG_0002.heic")
    sink = upload.begin_write(content_type="image/heic")
    sink.write(b"heic-bytes")
    sink.close()
    temp_path = upload._temp_path
    assert temp_path is not None
    assert temp_path.exists()

    with pytest.raises(DAVError) as exc_info:
        upload.end_write(with_errors=False)

    assert exc_info.value.value == HTTP_BAD_GATEWAY
    assert not temp_path.exists()
    assert client.uploads[0]["filename"] == "IMG_0002.heic"
    assert client.album_adds == [("album-1", "uploaded-1")]


def test_album_delete_removes_membership_without_deleting_asset() -> None:
    """DELETE on an album asset should remove album membership only."""
    client = FakeImmichClient()
    provider = provider_with_client(client)
    asset = provider.get_resource_inst(
        "/Albums/Vacation-2026/2026-06-28 16.38.37 IMG-0001--c1ada054.heic",
        auth_environ(),
    )

    assert asset is not None
    assert asset.handle_delete() is True
    assert client.album_removes == [("album-1", ASSET["id"])]


def test_timeline_delete_remains_forbidden() -> None:
    """Virtual timeline assets should not map DELETE to permanent deletion."""
    provider = provider_with_fake_client()
    asset = provider.get_resource_inst(
        "/Timeline/2026/2026-06/2026-06-28/2026-06-28 16.38.37 IMG-0001--c1ada054.heic",
        auth_environ(),
    )

    assert asset is not None
    with pytest.raises(DAVError) as exc_info:
        asset.handle_delete()
    assert exc_info.value.value == HTTP_FORBIDDEN


def test_diagnostics_resource_contains_identity() -> None:
    """Diagnostics file should include authenticated Immich identity."""
    provider = ImmichProvider("http://immich.test/api")
    resource = provider.get_resource_inst(
        "/.well-known/immich-bridge.json",
        {
            "immich.username": "barry",
            "immich.email": "barry@example.com",
            "immich.user_id": "user-1",
        },
    )

    assert resource is not None
    payload = json.loads(resource.get_content().read().decode())
    assert payload["service"] == "immich-bridge"
    assert payload["immichUrl"] == "http://immich.test/api"
    assert payload["user"] == "barry@example.com"
    assert payload["userId"] == "user-1"
    assert payload["capabilities"]["readOnly"] is False
    assert payload["capabilities"]["writes"] is True
    assert payload["capabilities"]["rootUploads"] is True
    assert payload["capabilities"]["albumMembershipDelete"] is True
    assert payload["capabilities"]["permanentDelete"] is False
    assert payload["capabilities"]["locks"] is False
    assert payload["limits"]["searchMaxPages"] == 20


def test_macos_metadata_files_are_detected() -> None:
    """macOS metadata writes should be easy to identify."""
    assert is_macos_metadata_file(".DS_Store") is True
    assert is_macos_metadata_file("._IMG_0001.jpg") is True
    assert is_macos_metadata_file("IMG_0001.jpg") is False


def test_range_start_from_header_parses_simple_ranges() -> None:
    """Simple byte ranges should seed upstream streams at the same offset."""
    assert range_start_from_header("bytes=65536-") == 65536
    assert range_start_from_header("bytes=123-456") == 123
    assert range_start_from_header("bytes=-500") is None
    assert range_start_from_header("items=1-2") is None
    assert range_start_from_header("bytes=bad-") is None


def test_byte_range_from_header_parses_bounded_ranges() -> None:
    """Bounded ranges should preserve both offsets for upstream/cache use."""
    first_bytes = byte_range_from_header("bytes=0-4095")
    assert first_bytes is not None
    assert first_bytes.start == 0
    assert first_bytes.end == 4095

    byte_range = byte_range_from_header("bytes=123-456")
    assert byte_range is not None
    assert byte_range.start == 123
    assert byte_range.end == 456

    open_ended = byte_range_from_header("bytes=65536-")
    assert open_ended is not None
    assert open_ended.start == 65536
    assert open_ended.end is None

    assert byte_range_from_header("bytes=456-123") is None
    assert byte_range_from_header("bytes=-500") is None
