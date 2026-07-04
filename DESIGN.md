# immich-bridge Design Document

Date: 2026-07-03
Status: Draft
Author: Barry (with Codex)

## Problem Statement

Immich is excellent as a photo library and mobile backup system, but many workflows still expect a filesystem:

- File managers on macOS, Windows, Linux, iOS, and Android
- Photo frames and screensavers that can read WebDAV folders
- Backup and sync tools such as rclone, Kopia, FolderSync, and Cyberduck
- Desktop photo editors and DAM tools that work best with mounted files
- Family users who understand folders better than web app concepts

Immich already exposes strong API primitives for assets, albums, tags, people, search, upload, download, and trash. What is missing is a careful filesystem-shaped bridge that maps those API resources into predictable WebDAV semantics without poking Immich's database or media directory.

The goal of `immich-bridge` is to make an Immich library mountable as a DAV volume and to do it with better correctness, safety, compatibility, and operability than existing wrappers.

## Demand Signal

Relevant upstream requests and prior work:

- Immich discussion #1687: WebDAV import/export support, with comments asking to expose the library as an OS-mountable folder.
- Immich discussion #7488: virtual WebDAV shares for smart albums, photo frames, and screensavers.
- Immich discussion #6592: round-trip editing by exposing selected photos through WebDAV.
- Immich discussion #20522: SFTP file-level bridge for Immich, showing demand for standards-based file access.
- Immich discussion #24608: native cloud/WebDAV/OneDrive/S3 storage integrations.
- Immich PR #18986: an experimental in-core `/webdav` implementation that was closed as not ready.
- Existing projects:
- `PersistentCloud/immich-webdav-wrapper`
  - `Demian98/immich-sftp-server`

The signal is not one giant feature request. It is a cluster of adjacent workflows: mount the library, browse albums, expose smart sets, sync files in, sync files out, edit externally, and feed devices that speak WebDAV.

## Goals

- Expose an Immich account as a WebDAV volume using only supported Immich HTTP APIs.
- Work with common DAV clients: macOS Finder, Windows Explorer, GNOME/KDE file managers, `davfs2`, Cyberduck, rclone, Kopia, and mobile DAV clients.
- Provide a useful read/write V1 mount while keeping destructive behavior conservative.
- Preserve Immich as the source of truth for asset identity, metadata, album membership, trash, and permissions.
- Make file names stable, human-readable, and collision-safe.
- Avoid destructive surprises. A delete from an album view must not silently delete the underlying asset unless explicitly configured.
- Support multi-user deployments where each DAV request uses the authenticated user's Immich permissions.
- Include an admin UI for shares, safety settings, cache state, auth diagnostics, and DAV activity.
- Ship as a container with good defaults, health checks, structured logs, and integration tests.

## Non-Goals

- Do not mount Immich's on-disk upload directory directly.
- Do not read or write Immich's database.
- Do not attempt full POSIX filesystem behavior. WebDAV is the contract.
- Do not promise safe two-way sync until conflict handling is explicit and tested.
- Do not replace Immich's web UI, mobile apps, sharing system, or storage backend.
- Do not implement a general WebDAV storage backend for Immich. This service exposes Immich outward as DAV.

## Current Immich API Assumptions

Based on the current OpenAPI spec from `immich-app/immich` main:

Stable API primitives we should use:

| Capability | Endpoint family | Notes |
| --- | --- | --- |
| List albums | `GET /albums` | Stable, authenticated user scoped |
| Album details | `GET /albums/{id}` | Required to list album assets |
| Add/remove album assets | `PUT/DELETE /albums/{id}/assets` | Stable album membership mapping |
| Search assets | `POST /search/metadata` | Stable, supports album IDs, tags, people, dates, favorites, visibility, pagination |
| Upload assets | `POST /assets` | Stable multipart upload, supports checksum header |
| Download original | `GET /assets/{id}/original` | Stable binary stream |
| Asset thumbnails | `GET /assets/{id}/thumbnail` | Stable for optional preview folders |
| Delete assets | `DELETE /assets` | Stable, should be guarded behind explicit write mode |
| Restore assets | `POST /trash/restore/assets` | Stable |
| Tags | `GET /tags`, tag asset endpoints | Stable |
| People | `GET /people`, `GET /people/{id}` | Stable |
| Current user | `GET /users/me` | Useful for API key validation |
| API key identity | `GET /api-keys/me` | Useful for diagnostics |

Avoid for core behavior:

- `GET /timeline/bucket` and `GET /timeline/buckets` are marked internal. They can be optional optimizations, but the filesystem should not depend on them.

Open API questions to verify in implementation:

- Whether Immich's original download endpoint supports `HEAD` and `Range`.
- Whether original byte length is exposed anywhere stable. If not, `immich-bridge` needs a size cache populated from download response headers or range probes.
- Exact behavior for live photos, stacks, edited assets, archived assets, locked assets, and partner-shared assets.
- Whether replacing an existing asset should map to edited assets, a new upload, or remain unsupported.

## Product Shape

`immich-bridge` is a standalone bridge service with two surfaces:

- Admin/API service: configure shares, safety settings, cache, auth diagnostics, and DAV activity.
- WebDAV service: mountable virtual filesystem backed by Immich APIs.

Native filesystem clients are separate platform packages that consume a shared bridge API:

- macOS: a Swift menu bar app plus an FSKit extension, with one mounted volume per library profile.
- Windows: a future tray app using the Cloud Files API for Explorer-native sync roots, with WinFsp as a possible mounted-drive option.
- Shared backend: `/api/fs/v1`, documented in `docs/native-fs-api.md`, exposes opaque nodes, children, content streams, and capability flags so clients do not reimplement Immich layout or write policy.

Default mount experience:

```text
/
  Albums/
  Timeline/
  Favorites/
  .well-known/
```

There is no root-level flat photo listing. The root is an index of bounded virtual views and a generic upload target. Media dropped at the root imports into Immich and appears through `Timeline/`, not as permanent root files.

Deferred root entries include `Tags/`, `People/`, `Places/`, `Uploads/`, `Trash/`, and `shares/`.

## Architecture

```text
                      HTTPS / WebDAV
  File manager  <---------------------->  immich-bridge WebDAV service
  rclone
  Cyberduck
  Photo frame

                      HTTPS / Admin UI
  Browser       <---------------------->  immich-bridge Admin/API service

                                           +----------------------+
                                           | ImmichProvider       |
                                           | - path resolver      |
                                           | - resource factory   |
                                           | - write policy       |
                                           +----------+-----------+
                                                      |
                                                      v
                                           +----------------------+
                                           | ImmichFilesystem     |
                                           | - virtual views      |
                                           | - naming/buckets     |
                                           | - pagination policy  |
                                           +----------+-----------+
                                                      |
                                                      v
                                           +----------------------+
                                           | ImmichClient         |
                                           | - API key auth       |
                                           | - streaming download |
                                           | - upload             |
                                           | - pagination         |
                                           +----------+-----------+
                                                      |
                       +------------------------------+------------------+
                       v                              v                  v
                Immich HTTP API                metadata cache      local database
                                               size cache          users/shares/audit
                                               optional blob cache audit/config
```

Recommended initial stack:

| Component | Default | Reason |
| --- | --- | --- |
| Language | Python 3.12+ | Reuses lessons from `paperless-webdav`; mature DAV libraries |
| Admin service | FastAPI | Existing pattern, async HTTP, simple health endpoints |
| Admin UI | React + TypeScript + Vite | API-first, reactive config UI served from the admin port |
| WebDAV | WsgiDAV + Cheroot | Mature provider model and known client workarounds |
| HTTP client | httpx | Async streaming, connection pooling, timeout control |
| Database | SQLite default, PostgreSQL optional later | Durable bridge-owned configuration |
| ORM/migrations | stdlib `sqlite3` initially | Keeps the first config store small |
| Encryption | cryptography AES-GCM | Deferred until stored API keys or DAV app passwords exist |
| Locking | Redis when configured; disabled otherwise | Avoids unsafe per-process DAV locks |
| Logging | structlog JSON | Useful for troubleshooting DAV clients |
| Proxy/TLS | Caddy examples | Same deployment ergonomics as `paperless-webdav` |

## Authentication

V1 supports one authentication contract: WebDAV Basic Auth with an Immich username and Immich API key.

- DAV username is the user's Immich username, email, or display label.
- DAV password is that user's Immich API key.
- On login, validate the key by calling Immich user/API-key identity endpoints such as `GET /users/me` and `GET /api-keys/me`.
- If validation succeeds, treat the returned Immich user ID as the authority. The supplied username is only an operator-friendly label and must not be trusted for authorization.
- Cache successful validation briefly, keyed by a hash of the API key, to avoid checking Immich on every `PROPFIND`.
- Do not store API keys at rest in V1. Keep the active request's key in request context only.

This keeps OIDC out of the WebDAV path. Even if Immich itself uses OIDC, users can create scoped Immich API keys in Immich and paste those into Finder, rclone, Cyberduck, or other DAV clients.

The admin API/UI also uses Immich as the source of truth:

- Admin login validates an Immich API key.
- The authenticated Immich user must have `isAdmin=true`.
- The bridge issues an admin session cookie for the UI and accepts bearer session tokens for automation.
- Local SQLite stores bridge configuration and sessions, not local users or passwords.

Deferred authentication modes:

- Admin UI login with OIDC plus encrypted per-user Immich API-key storage.
- Local DAV app passwords backed by stored Immich API keys.
- LDAP bind for WebDAV Basic Auth in managed deployments.
- Single service-token shares for kiosks or photo frames.
- Anonymous read-only shares, only with explicit public/LAN configuration.

## Authorization Model

Every Immich API call should use the API key supplied by the authenticated DAV user.

Rules:

- Do not broaden permissions beyond what the Immich API returns.
- If an album or asset is not visible to the user in Immich, it is not visible in DAV.
- Shared albums appear only if Immich exposes them to the user.
- Admin-created shares can restrict visibility further by Immich user ID, but never expand it.
- Write operations require both Immich permissions and local share write policy.

## Virtual Filesystem Layout

### V1 Mount Contract

```text
/
  Albums/
  Timeline/
  Favorites/
  Views/
  .well-known/
```

V1 is intended to be a practical read/write WebDAV bridge without exposing a flat "all photos" folder. The mount starts with a few stable virtual views:

- `Albums/`: user-visible Immich albums and album upload targets.
- `Timeline/`: the full library, grouped by capture date.
- `Favorites/`: favorite assets, grouped by capture date.
- `.well-known/`: machine-readable service diagnostics.

All top-level folders are virtual. None correspond to directories in Immich's storage. The root can accept raw media uploads, but uploaded assets are not listed as root children.

### Chaos Control Rules

WebDAV has no good standard pagination model for mounted file managers. The server controls scale by shaping large sets into folders:

- The root never lists assets.
- Unbounded views are always bucketed by date: year, then month, then day.
- Album folders may list files directly because albums are user-curated collections.
- There is no V1 `All Photos/`, `Camera Roll/`, or `Library/Files/` flat view.
- Leaf directories should target hundreds of files, not tens of thousands.
- If a day exceeds `DAY_FOLDER_SPLIT_THRESHOLD`, expose `HH/` hour buckets before files.
- Directory names must be deterministic so clients can cache them safely.

### Diagnostics

`/.well-known/immich-bridge.json` may expose a small diagnostics document when authenticated:

```json
{
  "service": "immich-bridge",
  "version": "0.1.0",
  "immichUrl": "https://immich.example.com",
  "user": "barry@example.com",
  "capabilities": {
    "readOnly": false,
    "writes": true,
    "rootUploads": true,
    "albumUploads": true,
    "albumCreate": true,
    "albumMembershipDelete": true,
    "permanentDelete": false,
    "rangeReads": true,
    "locks": true,
    "nativeFsApi": false
  }
}
```

### Albums

```text
/Albums/
  Vacation 2025/
  Family/
  Shared with me/

/Albums/Vacation 2025/
  2025-06-18 14.22.10 IMG_1234--a1b2c3d4.jpg
  2025-06-18 14.25.02 IMG_1235--b2c3d4e5.mov
```

`Albums/` is the friendly entry point. It should call `GET /albums`, list album folders, and then list each album's assets from `GET /albums/{id}` or metadata search filtered by album ID.

Album folder names:

- Sanitize filesystem-hostile characters.
- Preserve human readability.
- Add suffix on collision: `Album Name--<albumShortId>`.
- Store path-to-ID mappings in cache and make the suffix parser deterministic.

Album contents:

- Show original assets directly in the album folder.
- Sort by capture date, then filename, then asset ID.
- Do not show nested Immich concepts such as stacks, people, or tags in V1.
- If an album is empty, return an empty directory, not an error.
- If an album is no longer visible, return `404 Not Found`.

Asset file names:

```text
<localDateTime> <originalFileNameStem>--<assetShortId>.<ext>
```

Reasons:

- Stable across duplicate filenames.
- Sorts naturally by date.
- Keeps original filename visible.
- Lets the provider recover asset identity even after cache restart.

Configurable alternatives:

- `original`: original filename plus collision suffix.
- `stable`: `<assetId>.<ext>` for machines.
- `date-original-id`: default.

### Timeline

```text
/Timeline/
  2026/
    2026-07/
      2026-07-03/
        09/
          2026-07-03 09.13.44 IMG_9999--abcd1234.heic
```

`Timeline/` is the only V1 way to browse the whole library. It is never flat. Core implementation uses `POST /search/metadata` with date filters and pagination. Internal timeline endpoints may be used as optional acceleration; if they fail, the bridge falls back to search bounds.

Timeline date source:

1. `localDateTime` when present.
2. `fileCreatedAt`.
3. `createdAt`.

Timeline folder rules:

- `Timeline/` lists years only.
- `Timeline/YYYY/` lists months only.
- `Timeline/YYYY/YYYY-MM/` lists days only.
- `Timeline/YYYY/YYYY-MM/YYYY-MM-DD/` lists files for that day.
- Large day folders list `00/` through `23/` hour buckets, only for hours with assets.
- Empty buckets should not be shown.

### Favorites

```text
/Favorites/
  2026/
    2026-07/
      2026-07-03/
        2026-07-03 09.13.44 IMG_9999--abcd1234.heic
```

Backed by metadata search with `isFavorite=true`. Favorites use the same date buckets as `Timeline/` so a large favorite set does not become one giant directory.

### Deferred Views

These should not appear in the V1 root:

- `Tags/`: backed by `GET /tags` and metadata search by `tagIds`.
- `People/`: backed by `GET /people` and metadata search by `personIds`; hidden/unnamed people off by default.
- `Places/`: requires careful geospatial grouping and pagination.
- `Uploads/`: requires staging, checksums, duplicate handling, and write policy.
- `Trash/`: requires explicit restore/purge semantics.
- `shares/`: requires local share configuration and stored policy.

## WebDAV Operation Mapping

| DAV operation | V1 target behavior | Later behavior |
| --- | --- | --- |
| `OPTIONS` | Advertise DAV class 1 or 2 depending on Redis lock storage | Keep lock state in Redis only |
| `PROPFIND` | List virtual dirs and files; return type, mtime, etag, length if cached | Aggressive prefetch and client-specific tuning |
| `GET` | Stream original asset from Immich with range proxying | Edited/original variants, blob cache |
| `HEAD` | Return metadata from cache or Immich headers | Trigger background size probe |
| `PUT` | Root and album media uploads; ignore known harmless client metadata writes when safe | Sidecars, edit sessions, replacement policy |
| `DELETE` | Remove album membership by default; permanent asset deletion disabled | Trash asset by explicit policy |
| `MKCOL` | Create albums below `/Albums` | Nested virtual organization if Immich supports it |
| `MOVE` | Disabled by default | Move between albums, move to trash, edit session finalize |
| `COPY` | Disabled by default | Add asset to another album without duplicating bytes |
| `PROPPATCH` | Unsupported | Maybe map selected metadata later |
| `LOCK/UNLOCK` | No-op/unsupported initially | Real lock support if write workflows require it |

## Resource Identity

The hardest part is that a DAV path is not the asset. A single Immich asset can appear in:

- Timeline
- Multiple albums
- Tags
- People views
- Favorites
- Search shares

Therefore:

- Asset identity is always Immich asset ID.
- DAV paths are projections.
- Deleting a projection is not necessarily deleting the asset.
- Copying a projection usually means adding album membership, not duplicating bytes.
- Moving a projection between album folders means album membership changes, not filesystem rename.

Internal model:

```text
DavPath
  raw_path
  normalized_segments
  mount_id
  view_kind
  collection_id
  asset_id
  variant

AssetResource
  asset_id
  display_name
  original_file_name
  mime_type
  checksum
  local_date_time
  modified_at
  content_length_state
```

Filename suffixes should let us recover `asset_id` without a database lookup when possible:

```text
IMG_1234--a1b2c3d4.jpg
```

If `a1b2c3d4` is ambiguous, resolve via cached path index or reject with a clear conflict.

## Variants

Default file represents the best normal original:

- If `isEdited=true`, default can be configurable:
  - `original`: always original asset
  - `edited`: edited asset where available
  - `both`: expose sibling folders

Optional layout:

```text
/Albums/Vacation/
  Original/
  Edited/
  Thumbnails/
```

V1 should serve originals only. Edited variants and thumbnails should be opt-in to avoid surprising file counts.

## Size, ETag, and Caching

WebDAV clients need stable file properties.

ETag:

- Prefer Immich checksum for original asset.
- Include variant in ETag.
- If checksum is absent, derive weak ETag from asset ID and updated timestamp.

Last-Modified:

- Prefer `fileModifiedAt`.
- Fall back to `updatedAt`.
- Fall back to `createdAt`.

Content-Length:

- If Immich exposes size in metadata, use it.
- Otherwise populate a persistent size cache from:
  1. `HEAD /assets/{id}/original` if supported.
  2. `GET` with `Range: bytes=0-0` if supported.
  3. First full download response `Content-Length`.
- If length is unknown, return no length where the DAV library allows it and schedule a background probe.

Cache layers:

| Cache | Contents | Default TTL |
| --- | --- | --- |
| Identity cache | API key to user/API-key metadata | 5 minutes |
| Album cache | Album list and album details | 60 seconds |
| Search cache | View page results | 30 seconds |
| Asset cache | Asset metadata by ID | 5 minutes |
| Stale metadata cache | Last good album/search/asset response for Immich outages | 24 hours |
| Size cache | Asset variant byte length | Persistent until asset checksum changes |
| Path cache | DAV path to asset/collection IDs | Persistent with TTL |
| Blob cache | Optional original/thumbnail bytes | Disabled by default |

Cache invalidation:

- Use Immich `updatedAt` and album `updatedAt` when available.
- Admin UI exposes manual refresh.
- Writes through `immich-bridge` invalidate affected album/search/asset keys immediately.
- Background refresh can prefetch active mount roots.

## Client Compatibility

Keep and extend the lessons from `paperless-webdav`:

- Detect client by `User-Agent`.
- macOS Finder:
  - Disable stale caching where necessary.
  - Accept and ignore `.DS_Store`, `._*`, `.Spotlight-V100`, `.Trashes`, and `.fseventsd` writes when safe.
- Windows MiniRedir:
  - Expect many `PROPFIND` requests.
  - Avoid unknown lengths where possible.
  - Keep file names Windows-safe.
  - Document HTTPS and Basic Auth requirements.
- rclone:
  - Optimize for pagination, checksums, ETags, and range support.
- Cyberduck:
  - Good reference client for debugging.
- `davfs2`/GVFS/KIO:
  - Keep POSIX-ish behavior sane but do not pretend this is a POSIX filesystem.

Filename sanitization:

- Remove or replace `< > : " / \ | ? *`.
- Strip control characters.
- Avoid trailing dots/spaces for Windows.
- Reserve collision suffix room.
- Normalize Unicode consistently, probably NFC.

## Write Safety Policy

Writes are where this project can be better than existing wrappers.

Every share has a write policy:

```text
read_only
upload_only
album_membership
trash_enabled
destructive
experimental_roundtrip
```

Recommended default:

```text
read_only = true
upload_only = false
album_membership = false
trash_enabled = false
destructive = false
experimental_roundtrip = false
```

Policy examples:

| User action | Safe mapping | Risk |
| --- | --- | --- |
| PUT to Uploads/Inbox | Upload new asset | Duplicate handling |
| PUT to Albums/X | Upload new asset and add to album | Partial success |
| DELETE from Albums/X | Remove from album | User may expect asset delete |
| DELETE from Timeline | Disabled | Could trash entire asset |
| MOVE Albums/A/file to Albums/B/file | Add to B, remove from A | Not atomic |
| COPY Albums/A/file to Albums/B/file | Add to B | Fine if explicit |
| MKCOL under Albums | Create album | Naming collisions |
| Rename file | Unsupported | Immich does not have filesystem rename semantics |
| Overwrite file | Unsupported initially | Could mean edit, replace, or duplicate |

Writes should have audit events and clear logs. The admin UI should show the exact policy in plain language.

## Upload Pipeline

```text
DAV PUT starts
  -> create staging file
  -> stream request body to staging file
  -> compute sha1 during write
  -> on close, call Immich upload API
  -> if target is album, add uploaded/duplicate asset ID to album
  -> invalidate caches
  -> delete staging file
```

Failure handling:

- If upload succeeds but album add fails, return an error and surface a repair action in admin UI.
- Keep a small failed-upload journal with staged file metadata only when configured.
- Never log file content or API keys.
- Staging directory should have quotas and periodic cleanup.

## Round-Trip Editing

Round-trip editing is valuable but should be a separate advanced workflow, not the first write feature.

Proposed model:

```text
/shares/edit-session-123/
  originals/
  edits/
  sidecars/
  manifest.json
```

Flow:

1. User creates an edit session from admin UI or API with selected assets.
2. DAV exposes originals read-only.
3. User writes edited outputs into `edits/`.
4. Optional sidecars go into `sidecars/`.
5. User finalizes session in admin UI.
6. `immich-bridge` uploads new assets, tags/album-links them, and records provenance in local audit metadata.

Avoid automatic "on dismount" behavior. DAV clients do not give a reliable dismount signal.

## Admin UI

Initial pages:

- Dashboard
- Saved views
- Mount layout
- Write policy
- Diagnostics
- Realtime health/activity stream

Saved view configuration:

| Field | Description |
| --- | --- |
| Name | DAV path segment |
| Source | Metadata search |
| Filters | Album IDs, tag IDs, person IDs, date range, favorites, asset type |
| Layout | Flat or date buckets |
| Enabled | Whether the view appears under `/Views` |

The UI calls only `/api/admin/*` JSON APIs. It is a client of the admin API, not a second configuration path.

## Local Database

SQLite lives on the long-lived `configdata` volume at `/var/lib/immich-bridge` in the Compose deployment. PostgreSQL may be supported later for multi-user or replicated deployments.

Core tables:

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    email TEXT,
    name TEXT,
    api_key_name TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE views (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    layout TEXT NOT NULL DEFAULT 'date_buckets',
    filters_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Potential later cache/audit tables:

```sql

CREATE TABLE asset_cache (
    asset_id TEXT PRIMARY KEY,
    checksum TEXT,
    metadata_json TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE content_length_cache (
    asset_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    checksum TEXT,
    content_length INTEGER NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (asset_id, variant)
);

CREATE TABLE path_cache (
    share_id TEXT NOT NULL,
    path_hash TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    display_path TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (share_id, path_hash)
);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user_id TEXT,
    share_id TEXT,
    event_type TEXT NOT NULL,
    client TEXT,
    path TEXT,
    immich_asset_id TEXT,
    status TEXT NOT NULL,
    details_json TEXT
);
```

## Configuration

Environment variables:

```text
IMMICH_URL=https://immich.example.com
DATABASE_URL=sqlite:////var/lib/immich-bridge/immich-bridge.db

ADMIN_PORT=8080
WEBDAV_PORT=8081

AUTH_MODE=api_key
ALLOW_ANONYMOUS=false
AUTH_CACHE_TTL_SECONDS=300
AUTH_FAILURE_LIMIT=10
AUTH_FAILURE_WINDOW_SECONDS=300
ADMIN_SESSION_TTL_SECONDS=43200

ALBUM_CACHE_TTL_SECONDS=60
SEARCH_CACHE_TTL_SECONDS=30
ASSET_CACHE_TTL_SECONDS=300
SEARCH_PAGE_SIZE=500
SEARCH_MAX_PAGES=20
DAY_FOLDER_SPLIT_THRESHOLD=1000
SIZE_PROBE_CONCURRENCY=8
IMMICH_TIMEOUT_SECONDS=60
IMMICH_STREAM_TIMEOUT_SECONDS=3600

STAGING_DIR=/data/staging
STAGING_MAX_BYTES=10737418240
BLOB_CACHE_DIR=/data/blob-cache
BLOB_CACHE_MAX_BYTES=0

LOG_LEVEL=INFO
LOG_FORMAT=json
```

## Observability

Health endpoints:

- `GET /health`: process is alive.
- `GET /ready`: database reachable and Immich `/server/ping` succeeds.
- `GET /metrics`: Prometheus metrics if enabled.

Metrics:

- DAV requests by method/status/client.
- DAV request IDs surfaced as `X-Request-Id`.
- Immich API requests by endpoint/status.
- Streaming bytes served.
- Cache hits/misses.
- Size probe queue depth.
- Upload successes/failures.
- Write operation counts by policy.

Structured audit events:

- `auth_success`
- `auth_failure`
- `share_created`
- `share_updated`
- `propfind`
- `asset_downloaded`
- `asset_uploaded`
- `album_asset_added`
- `album_asset_removed`
- `asset_trashed`
- `write_rejected`
- `immich_api_error`

Never log:

- API keys
- Passwords
- File contents
- Full EXIF payloads unless explicitly in debug diagnostics

## Testing Strategy

Unit tests:

- Path parsing and normalization.
- Filename sanitization and collision suffixing.
- Reusable `ImmichFilesystem` view builders.
- Timeline bucket fallback when Immich internal endpoints are unavailable.
- Explicit failure when a directory listing would exceed pagination limits.
- Policy decisions for write operations.
- Cache invalidation.
- API error mapping to DAV status codes.

Fake Immich tests:

- In-memory or HTTP mock server implementing the subset of Immich APIs.
- Pagination, duplicate uploads, album membership, permission errors.
- Slow streaming and interrupted downloads.

WebDAV contract tests:

- `litmus` DAV test suite where applicable.
- `cadaver` smoke tests.
- rclone `ls`, `copy`, `sync`, `mount` smoke tests.
- macOS Finder manual/automated smoke tests.
- Windows MiniRedir smoke tests.
- `davfs2` mount tests on Linux.
- Cyberduck manual smoke tests.

Integration tests:

- Docker Compose with real Immich test instance.
- Seed assets/albums/tags/people through Immich API.
- Verify DAV tree, downloads, uploads, and album membership.
- Large library listing benchmark.
- Large video streaming benchmark.

Compatibility matrix should be part of release notes. This project wins by being boringly reliable across clients.

## Error Mapping

| Immich/API condition | DAV response |
| --- | --- |
| Asset not visible | `404 Not Found` |
| User lacks Immich permission | `403 Forbidden` |
| Local write policy rejects action | `403 Forbidden` with logged reason |
| Path collision unresolved | `409 Conflict` |
| Upload duplicate accepted by Immich | `201 Created` or `204 No Content`, depending on DAV operation |
| Immich unavailable | `503 Service Unavailable` |
| Immich timeout | `504 Gateway Timeout` |
| Unsupported DAV method | `405 Method Not Allowed` |
| Unsupported overwrite/rename | `409 Conflict` or `405 Method Not Allowed` |

## Implementation Phases

### Phase 0: Scaffold

- Create Python package, Dockerfile, Compose file, Makefile, lint/test setup.
- Basic FastAPI app with `/health` and `/ready`.
- Basic WsgiDAV service with static test provider.
- Config loading and structured logging.

### Phase 1: V1 Read-Only Mount

- API-key Basic Auth.
- Immich API client.
- Album listing and album asset browsing.
- Timeline date buckets for full-library browsing.
- Favorites date buckets.
- Original downloads through `GET /assets/{id}/original`.
- Filename sanitizer and collision-safe asset names.
- Metadata/size cache.
- macOS metadata no-op handling.
- Docker Compose quick start.

Success criteria:

- Mount in macOS Finder, Cyberduck, rclone, and Linux GVFS.
- Browse albums.
- Browse the full library through `Timeline/YYYY/YYYY-MM/YYYY-MM-DD/`.
- Browse favorites through the same date-bucket shape.
- Download originals.
- No root-level flat all-assets directory.
- No direct DB/filesystem access to Immich.

### Phase 2: Full Read-Only Library

- Tags and people views.
- Configurable shares.
- Admin UI for shares, diagnostics, and cache controls.
- Size prefetch worker.
- Client compatibility headers.
- Integration tests against Immich container.

### Phase 3: Safe Uploads

- `Uploads/Inbox` write support.
- `Uploads/To Album/<album>` support.
- Staging directory with quotas.
- Checksum handling.
- Duplicate handling.
- Upload audit trail and repair screen.

### Phase 4: Album Membership Writes

- `COPY` to album adds membership.
- `MOVE` between albums adds/removes membership.
- `DELETE` from album removes membership only when enabled.
- `MKCOL` creates albums when enabled.
- Clear conflict behavior for collisions.

### Phase 5: Destructive and Advanced Workflows

- Trash folder and restore flows.
- Optional destructive delete policy.
- Edited/original variants.
- Round-trip edit sessions.
- Sidecar workflows.
- Optional blob cache.
- Optional public read-only shares for photo frames.

## Design Principles

- Immich IDs are truth. Paths are views.
- Read-only first. Writes require explicit policy.
- Never silently turn an album operation into an asset delete.
- Prefer stable, boring DAV behavior over clever filesystem illusions.
- API-only integration keeps upgrades survivable.
- Optimize for common clients, but document client limitations honestly.
- Every surprising action needs an audit trail.

## Open Questions

- Should the default mount root expose all library views, or should it expose configured shares only?
- Should the default file variant be original or edited when an edited asset exists?
- Can Immich download endpoints reliably support `HEAD` and `Range`?
- What is the best UX for API key use in OS credential stores?
- How should partner-shared assets be represented?
- Should hidden/locked/archived assets be visible by default?
- How should stacks appear: primary only, folder per stack, or all assets?
- How should live photos appear: still only, paired video, or bundle folder?
- Should album `DELETE` default to remove membership once writes exist, or remain disabled unless explicitly enabled?
- Should round-trip editing create new assets only, or try to map to Immich edited assets?

## Source Links

- Immich API docs: https://docs.immich.app/api/
- Immich OpenAPI spec: https://github.com/immich-app/immich/blob/main/open-api/immich-openapi-specs.json
- WebDAV import/export request: https://github.com/immich-app/immich/discussions/1687
- WebDAV shares request: https://github.com/immich-app/immich/discussions/7488
- Round-trip editing request: https://github.com/immich-app/immich/discussions/6592
- SFTP file-level interface: https://github.com/immich-app/immich/discussions/20522
- Cloud/WebDAV storage request: https://github.com/immich-app/immich/discussions/24608
- Experimental Immich Bridge PR: https://github.com/immich-app/immich/pull/18986
- Existing WebDAV wrapper: https://github.com/PersistentCloud/immich-webdav-wrapper
- Existing SFTP bridge: https://github.com/Demian98/immich-sftp-server
