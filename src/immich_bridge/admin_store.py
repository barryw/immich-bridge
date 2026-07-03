"""Durable SQLite storage for bridge-owned admin configuration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from immich_bridge.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MOUNT_SETTINGS = {
    "albumsEnabled": True,
    "timelineEnabled": True,
    "favoritesEnabled": True,
    "viewsEnabled": True,
    "tagsEnabled": False,
    "peopleEnabled": False,
    "albumFolderSplitThreshold": 200,
    "dayFolderSplitThreshold": 1000,
    "filenameMode": "date-original-id",
}

DEFAULT_WRITE_POLICY = {
    "rootUploads": True,
    "albumUploads": True,
    "albumCreate": True,
    "albumMembershipDelete": True,
    "permanentDelete": False,
    "moveCopy": False,
    "overwrite": False,
}


@dataclass(frozen=True)
class AdminStore:
    """SQLite-backed admin configuration store."""

    database_path: Path

    @classmethod
    def from_database_url(cls, database_url: str) -> "AdminStore":
        """Create a store from a SQLite URL or raw path."""
        return cls(sqlite_path_from_url(database_url))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a SQLite connection with row dictionaries."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create tables and default settings when missing."""
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    email TEXT,
                    name TEXT,
                    api_key_name TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS views (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    layout TEXT NOT NULL DEFAULT 'date_buckets',
                    filters_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_views_enabled_name
                    ON views(enabled, name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
                    ON admin_sessions(expires_at);
                """
            )
            self._ensure_setting(connection, "mount", DEFAULT_MOUNT_SETTINGS)
            self._ensure_setting(connection, "write_policy", DEFAULT_WRITE_POLICY)
        logger.info("admin_store_initialized", database_path=str(self.database_path))

    def _ensure_setting(
        self,
        connection: sqlite3.Connection,
        key: str,
        value: dict[str, Any],
    ) -> None:
        existing = connection.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
        if existing is not None:
            return
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, sort_keys=True), utc_now()),
        )

    def get_setting(self, key: str) -> dict[str, Any]:
        """Return a JSON setting value."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return {}
        parsed = json.loads(str(row["value_json"]))
        return parsed if isinstance(parsed, dict) else {}

    def set_setting(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        """Persist a JSON setting value."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, sort_keys=True), utc_now()),
            )
        return value

    def list_views(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Return saved view definitions."""
        sql = "SELECT * FROM views"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_view_from_row(row) for row in rows]

    def get_view(self, view_id: str) -> dict[str, Any] | None:
        """Return one saved view."""
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM views WHERE id = ?", (view_id,)).fetchone()
        return _view_from_row(row) if row is not None else None

    def upsert_view(self, view: dict[str, Any]) -> dict[str, Any]:
        """Create or update a saved view."""
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM views WHERE id = ?",
                (view["id"],),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO views (
                    id, name, description, enabled, layout, filters_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    enabled = excluded.enabled,
                    layout = excluded.layout,
                    filters_json = excluded.filters_json,
                    updated_at = excluded.updated_at
                """,
                (
                    view["id"],
                    view["name"],
                    view.get("description", ""),
                    1 if view.get("enabled", True) else 0,
                    view.get("layout", "date_buckets"),
                    json.dumps(view.get("filters", {}), sort_keys=True),
                    created_at,
                    now,
                ),
            )
        stored = self.get_view(str(view["id"]))
        if stored is None:
            raise RuntimeError("saved view was not returned after upsert")
        return stored

    def delete_view(self, view_id: str) -> bool:
        """Delete one saved view."""
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM views WHERE id = ?", (view_id,))
            return cursor.rowcount > 0

    def create_session(self, session: dict[str, Any]) -> None:
        """Persist an admin session by hashed token."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_sessions (
                    token_hash, user_id, email, name, api_key_name,
                    created_at, expires_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["token_hash"],
                    session["user_id"],
                    session.get("email"),
                    session.get("name"),
                    session.get("api_key_name"),
                    session["created_at"],
                    session["expires_at"],
                    session["last_seen_at"],
                ),
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """Return a live admin session."""
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
        return dict(row)

    def delete_session(self, token_hash: str) -> None:
        """Delete an admin session."""
        with self.connect() as connection:
            connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def prune_sessions(self) -> int:
        """Remove expired admin sessions."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?",
                (utc_now(),),
            )
            return cursor.rowcount


def sqlite_path_from_url(database_url: str) -> Path:
    """Return a filesystem path for a SQLite database URL."""
    if database_url.startswith("sqlite:////"):
        return Path(f"/{database_url.removeprefix('sqlite:////')}")
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///")).resolve()
    if database_url.startswith("sqlite://"):
        raise ValueError("Only filesystem SQLite URLs are supported")
    return Path(database_url).resolve()


@lru_cache
def get_admin_store(database_url: str) -> AdminStore:
    """Return an initialized admin store for a database URL."""
    store = AdminStore.from_database_url(database_url)
    store.initialize()
    return store


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(UTC).isoformat()


def _view_from_row(row: sqlite3.Row) -> dict[str, Any]:
    filters = json.loads(str(row["filters_json"]))
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "layout": row["layout"],
        "filters": filters if isinstance(filters, dict) else {},
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
