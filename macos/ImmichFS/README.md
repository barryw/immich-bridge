# ImmichFS

This directory contains the macOS native client scaffold.

The current Xcode project includes:

```text
ImmichFS.xcodeproj
ImmichFSApp/
  menu bar app target and settings window
ImmichFSFileSystem/
  FSKit ExtensionKit target
  static read-only filesystem tree
```

## First Build Target

The first target is not a full Immich mount. It is a static read-only FSKit volume
that proves packaging, signing, extension loading, mount/unmount behavior, and
Finder visibility.

```text
/
  README.txt
  Albums/
  Timeline/
  Views/
```

Build locally with:

```bash
xcodebuild -project ImmichFS.xcodeproj -scheme ImmichFS -configuration Debug -derivedDataPath .DerivedData CODE_SIGNING_ALLOWED=NO build
```

Use normal signing settings when installing or running the FSKit extension outside
of a local compile check.

After the static volume works, add profile storage, bridge authentication, and
directory enumeration from `/api/fs/v1/nodes/{id}/children`.
