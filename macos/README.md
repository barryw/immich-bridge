# macOS Client

The macOS client is a native menu bar application plus an FSKit filesystem
extension. It mounts one or more Immich libraries as Finder-visible volumes while
using `immich-bridge` for layout, policy, and Immich API behavior.

## Product Shape

```text
ImmichFS.app
  Menu bar app
  Settings window
  Keychain credentials
  profile management

ImmichFS.appex
  FSKit extension
  one volume per mounted profile
  talks to /api/fs/v1
```

The extension must stay UI-free. The menu bar app owns configuration, diagnostics,
mount/unmount controls, launch-at-login, and "Open Bridge Admin" links.

## Profiles

Each Immich library is a profile:

- display name, such as `Home Photos`
- bridge URL
- auth token stored in Keychain
- stable profile ID used for the FSKit volume identifier
- auto-mount preference
- local cache size/location
- diagnostics level

Multiple profiles may be mounted at once and should appear as separate Finder
volumes, for example `/Volumes/Home Photos` and `/Volumes/Family Photos`.

## Implementation Milestones

1. Create a signed menu bar app and FSKit extension shell.
2. Mount a static read-only volume with `README.txt`, `Albums/`, `Timeline/`, and
   `Views/`.
3. Add profile storage and connection testing against `immich-bridge`.
4. Implement `/api/fs/v1` node lookup and directory enumeration.
5. Implement file reads with HTTP Range support and bounded local caching.
6. Add write support only after upload staging and commit semantics are explicit.

## Technology

- Swift
- SwiftUI or AppKit for the menu bar app
- FSKit for the filesystem extension
- URLSession for bridge API calls
- Keychain for secrets
- App Group storage for non-secret profile config and local cache metadata

No JavaScript runtime belongs in the macOS client.
