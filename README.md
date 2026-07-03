# immich-bridge

Mount Immich like a filesystem.

`immich-bridge` is a standalone WebDAV bridge for Immich libraries. It exposes albums,
timeline views, favorites, and diagnostics through a normal DAV mount while keeping
Immich as the source of truth for assets, metadata, permissions, and album membership.

> Status: alpha. The current server supports practical WebDAV reads and guarded
> writes: root imports, album imports, album creation, and safe album-level deletes.

## Why

Immich is excellent as a photo library, but a lot of real workflows still expect
folders: Finder, Windows Explorer, Cyberduck, rclone, photo frames, backup tools,
desktop editors, and family users who understand drag-and-drop.

`immich-bridge` gives those tools a filesystem-shaped view without mounting Immich's
storage directory, touching its database, or bypassing Immich permissions.

## What Works Today

- WebDAV Basic Auth using Immich username plus Immich API key.
- Per-request Immich API access using the authenticated user's key.
- Virtual root with `Albums/`, `Timeline/`, `Favorites/`, and `.well-known/`.
- Album browsing with date bucketing for large albums.
- Timeline and favorites browsing grouped by year, month, day, and hour when needed.
- Original asset streaming with Range support and a bounded disk-backed range cache.
- Redis-backed auth/cache state and DAV lock storage in the Compose deployment.
- Guarded write support: `PUT` media at root or in albums, `MKCOL` under `Albums/`,
  and `DELETE` from album views to remove membership without deleting the asset.
- Permanent asset deletion, overwrite, rename, move, and copy are blocked.
- Virtual `README.txt` files at top-level directories to explain mount behavior.
- Structured logs, request metrics toggle, Docker Compose deployment, and tests.

## Mount Layout

```text
/
  README.txt
  Albums/
    README.txt
    <album>/
      <media files or date buckets>
  Timeline/
    README.txt
    YYYY/
      YYYY-MM/
        YYYY-MM-DD/
          <media files or hour buckets>
  Favorites/
    README.txt
    YYYY/
  .well-known/
    immich-bridge.json
```

Top-level directories are virtual. They are not Immich storage folders. Root-level
media uploads import into Immich and then appear through `Timeline/`, not as
permanent root files. Recently uploaded root files may resolve briefly through an
upload receipt so DAV clients can finish their post-upload checks.

## Quick Start

Create a `.env` file:

```env
IMMICH_URL=https://your-immich.example.com/api
```

Start the bridge:

```bash
docker compose up --build
```

Connect your WebDAV client to:

```text
http://localhost:8081/
```

Use your Immich username or email as the DAV username and an Immich API key as the
password.

Use HTTPS in production. WebDAV Basic Auth sends the Immich API key as the DAV
password, so the bridge should sit behind TLS outside local development.

The admin health endpoint is available at:

```text
http://localhost:8080/health
```

## Configuration

Important environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `IMMICH_URL` | required | Immich API base URL, including `/api` |
| `WEBDAV_PORT` | `8081` | WebDAV listener port |
| `ADMIN_PORT` | `8080` | Admin/API listener port |
| `REDIS_HOST` | `redis` in Compose | Redis for auth cache and DAV locks |
| `IMMICH_BRIDGE_METRICS` | `false` | Verbose DAV/range/upstream metrics |
| `ALBUM_FOLDER_SPLIT_THRESHOLD` | `200` | Split large albums into date buckets |
| `DAY_FOLDER_SPLIT_THRESHOLD` | `1000` | Split large days into hour buckets |
| `BLOB_CACHE_MAX_BYTES` | `1073741824` | Local range-cache size cap |
| `BLOB_CACHE_MAX_RANGE_BYTES` | `8388608` | Largest single range to cache |
| `WEBDAV_MAX_UPLOAD_BYTES` | `10737418240` | Largest accepted DAV `PUT` body |
| `UPLOAD_RECEIPT_TTL_SECONDS` | `1800` | How long recent uploads stay directly resolvable |

See `.env.example` for the full list.

## Development

```bash
uv sync
make test
make lint
make typecheck
docker compose up --build
```

The test suite is intentionally focused on DAV semantics, Immich API boundaries,
cache behavior, auth behavior, and safety around destructive operations.

## Write Semantics

Write behavior is intentionally narrow:

- `PUT /file.jpg` imports media into Immich.
- `MKCOL /Albums/New Album` creates an Immich album.
- `PUT /Albums/<album>/file.jpg` imports media and adds it to that album.
- `DELETE /Albums/<album>/file.jpg` removes the asset from that album, not from the
  Immich library.

Unsupported writes return an error instead of guessing. Permanent deletion remains
disabled until explicit safety controls exist.

## Roadmap

Next work includes duplicate handling, richer client compatibility testing, sidecar
files, edit-session workflows, optional thumbnails, tags, people, places, shares,
and native filesystem clients.

## Safety Model

`immich-bridge` uses Immich HTTP APIs only. It does not read Immich's database and it
does not mount the Immich upload directory. All authorization comes from the Immich API
key supplied by the DAV user.

Destructive asset operations are rejected by default. Album membership changes are
kept separate from permanent asset deletion.
