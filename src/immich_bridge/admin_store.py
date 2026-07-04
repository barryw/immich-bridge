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
DEFAULT_LIBRARY_ID = "default"
DEFAULT_LIBRARY_NAME = "Default Library"

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

                CREATE TABLE IF NOT EXISTS libraries (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    immich_url TEXT NOT NULL DEFAULT '',
                    public_url TEXT,
                    share_hosts_json TEXT NOT NULL DEFAULT '[]',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS library_settings (
                    library_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (library_id, key),
                    FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY,
                    principal_id TEXT,
                    principal_kind TEXT,
                    user_id TEXT NOT NULL,
                    email TEXT,
                    name TEXT,
                    api_key_name TEXT,
                    grants_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS views (
                    id TEXT PRIMARY KEY,
                    library_id TEXT NOT NULL DEFAULT 'default',
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    layout TEXT NOT NULL DEFAULT 'date_buckets',
                    filters_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE,
                    UNIQUE (library_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
                    ON admin_sessions(expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_libraries_default
                    ON libraries(is_default)
                    WHERE is_default = 1;
                """
            )
            self._migrate_libraries(connection)
            self._migrate_admin_sessions(connection)
            self._ensure_default_library_row(connection)
            self._migrate_settings(connection)
            self._migrate_views(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_views_enabled_name "
                "ON views(library_id, enabled, name COLLATE NOCASE)",
            )
            self._ensure_setting(connection, "mount", DEFAULT_MOUNT_SETTINGS)
            self._ensure_setting(connection, "write_policy", DEFAULT_WRITE_POLICY)
            self._ensure_library_setting(
                connection,
                DEFAULT_LIBRARY_ID,
                "mount",
                DEFAULT_MOUNT_SETTINGS,
            )
            self._ensure_library_setting(
                connection,
                DEFAULT_LIBRARY_ID,
                "write_policy",
                DEFAULT_WRITE_POLICY,
            )
        logger.info("admin_store_initialized", database_path=str(self.database_path))

    def _table_columns(self, connection: sqlite3.Connection, table: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate_libraries(self, connection: sqlite3.Connection) -> None:
        columns = self._table_columns(connection, "libraries")
        if "public_url" not in columns:
            connection.execute("ALTER TABLE libraries ADD COLUMN public_url TEXT")
        if "share_hosts_json" not in columns:
            connection.execute(
                "ALTER TABLE libraries ADD COLUMN share_hosts_json TEXT NOT NULL DEFAULT '[]'",
            )

    def _migrate_admin_sessions(self, connection: sqlite3.Connection) -> None:
        columns = self._table_columns(connection, "admin_sessions")
        if "principal_id" not in columns:
            connection.execute("ALTER TABLE admin_sessions ADD COLUMN principal_id TEXT")
        if "principal_kind" not in columns:
            connection.execute("ALTER TABLE admin_sessions ADD COLUMN principal_kind TEXT")
        if "grants_json" not in columns:
            connection.execute(
                "ALTER TABLE admin_sessions ADD COLUMN grants_json TEXT NOT NULL DEFAULT '[]'",
            )
        connection.execute(
            """
            UPDATE admin_sessions
            SET
                principal_id = COALESCE(principal_id, user_id),
                principal_kind = COALESCE(principal_kind, 'library_admin')
            """,
        )

    def _ensure_default_library_row(self, connection: sqlite3.Connection) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO libraries (id, name, immich_url, is_default, created_at, updated_at)
            VALUES (?, ?, '', 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                is_default = 1,
                updated_at = excluded.updated_at
            """,
            (DEFAULT_LIBRARY_ID, DEFAULT_LIBRARY_NAME, now, now),
        )

    def _migrate_settings(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT key, value_json, updated_at FROM settings").fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO library_settings (library_id, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(library_id, key) DO NOTHING
                """,
                (
                    DEFAULT_LIBRARY_ID,
                    row["key"],
                    row["value_json"],
                    row["updated_at"],
                ),
            )

    def _migrate_views(self, connection: sqlite3.Connection) -> None:
        columns = self._table_columns(connection, "views")
        if "library_id" in columns:
            connection.execute(
                "UPDATE views SET library_id = ? WHERE library_id IS NULL OR library_id = ''",
                (DEFAULT_LIBRARY_ID,),
            )
            return

        connection.execute("ALTER TABLE views RENAME TO views_legacy")
        connection.execute(
            """
            CREATE TABLE views (
                id TEXT PRIMARY KEY,
                library_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                layout TEXT NOT NULL DEFAULT 'date_buckets',
                filters_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE,
                UNIQUE (library_id, name)
            )
            """,
        )
        connection.execute(
            """
            INSERT INTO views (
                id, library_id, name, description, enabled, layout,
                filters_json, created_at, updated_at
            )
            SELECT
                id, ?, name, description, enabled, layout,
                filters_json, created_at, updated_at
            FROM views_legacy
            """,
            (DEFAULT_LIBRARY_ID,),
        )
        connection.execute("DROP TABLE views_legacy")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_views_enabled_name "
            "ON views(library_id, enabled, name COLLATE NOCASE)",
        )

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

    def _ensure_library_setting(
        self,
        connection: sqlite3.Connection,
        library_id: str,
        key: str,
        value: dict[str, Any],
    ) -> None:
        existing = connection.execute(
            "SELECT 1 FROM library_settings WHERE library_id = ? AND key = ?",
            (library_id, key),
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            """
            INSERT INTO library_settings (library_id, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (library_id, key, json.dumps(value, sort_keys=True), utc_now()),
        )

    def ensure_default_library(self, immich_url: str) -> dict[str, Any]:
        """Ensure the legacy/default configured Immich library exists."""
        with self.connect() as connection:
            self._ensure_default_library_row(connection)
            now = utc_now()
            connection.execute(
                """
                UPDATE libraries
                SET
                    name = CASE WHEN name = '' THEN ? ELSE name END,
                    immich_url = CASE WHEN immich_url = '' THEN ? ELSE immich_url END,
                    is_default = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    DEFAULT_LIBRARY_NAME,
                    immich_url.rstrip("/"),
                    now,
                    DEFAULT_LIBRARY_ID,
                ),
            )
            self._ensure_library_setting(
                connection,
                DEFAULT_LIBRARY_ID,
                "mount",
                DEFAULT_MOUNT_SETTINGS,
            )
            self._ensure_library_setting(
                connection,
                DEFAULT_LIBRARY_ID,
                "write_policy",
                DEFAULT_WRITE_POLICY,
            )
        library = self.get_library(DEFAULT_LIBRARY_ID)
        if library is None:
            raise RuntimeError("default library was not returned after initialization")
        return library

    def list_libraries(self) -> list[dict[str, Any]]:
        """Return configured Immich libraries."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM libraries
                ORDER BY is_default DESC, name COLLATE NOCASE
                """,
            ).fetchall()
        return [_library_from_row(row) for row in rows]

    def get_library(self, library_id: str) -> dict[str, Any] | None:
        """Return one configured Immich library."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM libraries WHERE id = ?",
                (library_id,),
            ).fetchone()
        return _library_from_row(row) if row is not None else None

    def upsert_library(self, library: dict[str, Any]) -> dict[str, Any]:
        """Create or update an Immich library."""
        now = utc_now()
        library_id = str(library["id"])
        public_url = _optional_url(library.get("public_url", library.get("publicUrl")))
        share_hosts = _normalize_share_hosts(library.get("share_hosts", library.get("shareHosts")))
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM libraries WHERE id = ?",
                (library_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            is_default = 1 if library.get("is_default", False) else 0
            if is_default:
                connection.execute(
                    "UPDATE libraries SET is_default = 0 WHERE id != ?",
                    (library_id,),
                )
            connection.execute(
                """
                INSERT INTO libraries (
                    id, name, immich_url, public_url, share_hosts_json,
                    is_default, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    immich_url = excluded.immich_url,
                    public_url = excluded.public_url,
                    share_hosts_json = excluded.share_hosts_json,
                    is_default = excluded.is_default,
                    updated_at = excluded.updated_at
                """,
                (
                    library_id,
                    library["name"],
                    str(library["immich_url"]).rstrip("/"),
                    public_url,
                    json.dumps(share_hosts, sort_keys=True),
                    is_default,
                    created_at,
                    now,
                ),
            )
            self._ensure_library_setting(
                connection,
                library_id,
                "mount",
                DEFAULT_MOUNT_SETTINGS,
            )
            self._ensure_library_setting(
                connection,
                library_id,
                "write_policy",
                DEFAULT_WRITE_POLICY,
            )
        stored = self.get_library(library_id)
        if stored is None:
            raise RuntimeError("library was not returned after upsert")
        return stored

    def default_library_id(self) -> str:
        """Return the default library id."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM libraries WHERE is_default = 1 LIMIT 1",
            ).fetchone()
        return str(row["id"]) if row is not None else DEFAULT_LIBRARY_ID

    def get_setting(self, key: str) -> dict[str, Any]:
        """Return a JSON setting value."""
        return self.get_library_setting(DEFAULT_LIBRARY_ID, key)

    def get_library_setting(self, library_id: str, key: str) -> dict[str, Any]:
        """Return a JSON setting value scoped to one library."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM library_settings WHERE library_id = ? AND key = ?",
                (library_id, key),
            ).fetchone()
        if row is None:
            return {}
        parsed = json.loads(str(row["value_json"]))
        return parsed if isinstance(parsed, dict) else {}

    def set_setting(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        """Persist a JSON setting value."""
        return self.set_library_setting(DEFAULT_LIBRARY_ID, key, value)

    def set_library_setting(
        self,
        library_id: str,
        key: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a JSON setting value scoped to one library."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO library_settings (library_id, key, value_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(library_id, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (library_id, key, json.dumps(value, sort_keys=True), utc_now()),
            )
        return value

    def list_views(
        self,
        *,
        library_id: str = DEFAULT_LIBRARY_ID,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return saved view definitions."""
        sql = "SELECT * FROM views WHERE library_id = ?"
        params: tuple[Any, ...] = (library_id,)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_view_from_row(row) for row in rows]

    def get_view(
        self,
        view_id: str,
        *,
        library_id: str = DEFAULT_LIBRARY_ID,
    ) -> dict[str, Any] | None:
        """Return one saved view."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM views WHERE id = ? AND library_id = ?",
                (view_id, library_id),
            ).fetchone()
        return _view_from_row(row) if row is not None else None

    def upsert_view(
        self,
        view: dict[str, Any],
        *,
        library_id: str = DEFAULT_LIBRARY_ID,
    ) -> dict[str, Any]:
        """Create or update a saved view."""
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM views WHERE id = ? AND library_id = ?",
                (view["id"], library_id),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO views (
                    id, library_id, name, description, enabled, layout,
                    filters_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    library_id = excluded.library_id,
                    name = excluded.name,
                    description = excluded.description,
                    enabled = excluded.enabled,
                    layout = excluded.layout,
                    filters_json = excluded.filters_json,
                    updated_at = excluded.updated_at
                """,
                (
                    view["id"],
                    library_id,
                    view["name"],
                    view.get("description", ""),
                    1 if view.get("enabled", True) else 0,
                    view.get("layout", "date_buckets"),
                    json.dumps(view.get("filters", {}), sort_keys=True),
                    created_at,
                    now,
                ),
            )
        stored = self.get_view(str(view["id"]), library_id=library_id)
        if stored is None:
            raise RuntimeError("saved view was not returned after upsert")
        return stored

    def delete_view(
        self,
        view_id: str,
        *,
        library_id: str = DEFAULT_LIBRARY_ID,
    ) -> bool:
        """Delete one saved view."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM views WHERE id = ? AND library_id = ?",
                (view_id, library_id),
            )
            return cursor.rowcount > 0

    def create_session(self, session: dict[str, Any]) -> None:
        """Persist an admin session by hashed token."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_sessions (
                    token_hash, principal_id, principal_kind,
                    user_id, email, name, api_key_name, grants_json,
                    created_at, expires_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["token_hash"],
                    session.get("principal_id") or session["user_id"],
                    session.get("principal_kind") or "library_admin",
                    session["user_id"],
                    session.get("email"),
                    session.get("name"),
                    session.get("api_key_name"),
                    json.dumps(session.get("grants") or [], sort_keys=True),
                    session["created_at"],
                    session["expires_at"],
                    session["last_seen_at"],
                ),
            )

    def update_session_grants(self, token_hash: str, grants: list[dict[str, Any]]) -> None:
        """Replace a live session's grants."""
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE admin_sessions
                SET grants_json = ?, last_seen_at = ?
                WHERE token_hash = ? AND expires_at > ?
                """,
                (
                    json.dumps(grants, sort_keys=True),
                    utc_now(),
                    token_hash,
                    utc_now(),
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
        session = dict(row)
        grants = json.loads(str(session.pop("grants_json", "[]")))
        session["grants"] = grants if isinstance(grants, list) else []
        return session

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
        "libraryId": row["library_id"],
        "name": row["name"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "layout": row["layout"],
        "filters": filters if isinstance(filters, dict) else {},
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _library_from_row(row: sqlite3.Row) -> dict[str, Any]:
    share_hosts = json.loads(str(row["share_hosts_json"]))
    return {
        "id": row["id"],
        "name": row["name"],
        "immichUrl": row["immich_url"],
        "publicUrl": row["public_url"],
        "shareHosts": share_hosts if isinstance(share_hosts, list) else [],
        "isDefault": bool(row["is_default"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _optional_url(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().rstrip("/")
    return normalized or None


def _normalize_share_hosts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    hosts: list[str] = []
    seen: set[str] = set()
    for item in value:
        host = str(item).strip().casefold()
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts
