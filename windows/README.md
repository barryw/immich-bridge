# Windows Client

The Windows client is deferred, but the backend contract should support it from
the start. Windows should consume the same `/api/fs/v1` node API as macOS.

## Preferred Product Shape

Use the Windows Cloud Files API for a OneDrive-style experience:

```text
ImmichFS tray app
  profile settings
  Windows Credential Manager secrets
  sync root registration

Cloud Files provider
  placeholder directories/files
  hydration on open
  Explorer navigation pane integration
  thumbnails and status later
```

Each Immich library should become a separate sync root. A parent grouping can hold
multiple libraries:

```text
File Explorer
  ImmichFS
    Home Photos
    Family Photos
```

## Alternative

WinFsp is the best mounted-drive option if we want drive letters or a filesystem
that behaves more like macOS FSKit:

```text
X:\ Home Photos
Y:\ Family Photos
```

WinFsp is a good power-user path, but Cloud Files is the better default for native
Explorer integration, placeholders, hydration state, and branded navigation.

## Shared Requirements

- Do not call Immich directly for filesystem layout.
- Use `/api/fs/v1` for nodes, children, and content.
- Store secrets in Windows Credential Manager.
- Treat writes as staged uploads with explicit commit semantics.
- Preserve the same server-side saved views and write policy used by WebDAV and
  macOS.
