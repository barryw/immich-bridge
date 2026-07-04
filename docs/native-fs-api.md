# Native Filesystem API

This contract is the shared backend target for native filesystem clients. The
macOS FSKit client and future Windows clients should call this API instead of
reimplementing Immich-specific layout rules.

## Goals

- Expose the same virtual filesystem semantics across macOS and Windows.
- Keep Immich API quirks, saved views, write policy, naming, and pagination in
  `immich-bridge`.
- Let clients focus on OS integration: mounting, local credentials, shell
  status, local cache, and file read/write callbacks.

## Client Model

Each mounted library is a client profile. A profile points at one bridge URL and
authenticates as one Immich user or shared-link scope. Native clients may mount
multiple profiles at the same time.

```text
Home Photos   -> https://bridge.example.com/api/fs/v1
Family Photos -> https://family-bridge.example.com/api/fs/v1
```

## Authentication

Native clients should use bearer tokens issued by `immich-bridge`, or a future
client credential derived from an Immich-backed login. The API must not require a
platform-specific auth mechanism. macOS stores secrets in Keychain; Windows stores
secrets in Windows Credential Manager.

## Node Contract

Every filesystem object is represented as a node:

```json
{
  "id": "asset:01J...",
  "parentId": "view:timeline:2026-07-04",
  "name": "2026-07-04 14.03.22 IMG_1234--abc123.jpg",
  "type": "file",
  "size": 4839201,
  "mtime": "2026-07-04T18:03:22Z",
  "etag": "asset-version",
  "capabilities": ["read"]
}
```

`id` values are opaque, stable within a profile, and independent of display
names. Clients map them to local inode/object IDs. Directory child ordering is
server-defined and stable for a given `etag`.

## Endpoints

```text
GET /api/fs/v1/root
GET /api/fs/v1/nodes/{node_id}
GET /api/fs/v1/nodes/{node_id}/children?cursor=...
GET /api/fs/v1/nodes/{node_id}/content
HEAD /api/fs/v1/nodes/{node_id}/content
```

Child responses return:

```json
{
  "items": [],
  "nextCursor": null,
  "etag": "directory-version"
}
```

Content endpoints must support HTTP Range requests. Clients should prefer range
reads and cache only bounded byte ranges unless the user pins content locally.

## Write Semantics

Initial native clients are read-only. Writes should be added through explicit
server capabilities:

- `create-file`
- `append`
- `commit-upload`
- `album-membership-delete`
- `mkdir-album`

Random write calls from an OS filesystem callback should stage locally and commit
through a server upload session. Clients must not infer destructive behavior from
rename, overwrite, or delete unless the server advertises that capability.

## Platform Notes

macOS uses FSKit volumes. Windows should use Cloud Files API for Explorer-native
sync roots, with WinFsp as a possible power-user mounted-drive option. Both
platforms consume the same node and content endpoints.
