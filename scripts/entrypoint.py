#!/usr/bin/env python3
"""Container entrypoint for immich-bridge."""

import os


def main() -> None:
    """Start the combined Admin API and WebDAV process."""
    os.execvp("uv", ["uv", "run", "python", "-m", "immich_bridge.main"])


if __name__ == "__main__":
    main()
