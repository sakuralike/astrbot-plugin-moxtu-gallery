from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS uploaded_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    message_id TEXT,
    platform_name TEXT NOT NULL,
    platform_id TEXT,
    bot_self_id TEXT,
    group_id TEXT,
    sender_id TEXT NOT NULL,
    sender_name TEXT,
    lsky_version TEXT,
    album_id TEXT,
    photo_id TEXT,
    photo_key TEXT,
    filename TEXT,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    size_bytes INTEGER,
    sha256 TEXT NOT NULL,
    public_url TEXT,
    visibility TEXT,
    caption TEXT,
    caption_status TEXT,
    upload_status TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, sha256),
    UNIQUE(lsky_version, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_uploaded_group_time
    ON uploaded_images(group_id, uploaded_at);
CREATE INDEX IF NOT EXISTS idx_uploaded_sender_time
    ON uploaded_images(sender_id, uploaded_at);
CREATE TABLE IF NOT EXISTS reply_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    photo_id TEXT,
    source_type TEXT NOT NULL,
    source_url_hash TEXT NOT NULL,
    caption_status TEXT,
    replied_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reply_history_group_time
    ON reply_history(group_id, replied_at);
CREATE TABLE IF NOT EXISTS caption_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id TEXT,
    sha256 TEXT,
    provider_id TEXT,
    model_name TEXT,
    caption TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_caption_photo
    ON caption_records(photo_id);
"""


class MetadataStore:
    """SQLite-backed metadata store; image bytes never enter this database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def get_upload_by_event(self, event_id: str, sha256: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM uploaded_images WHERE event_id=? AND sha256=?",
                (event_id, sha256),
            ).fetchone()
        return dict(row) if row else None

    def record_upload(self, record: dict[str, Any]) -> int:
        fields = [
            "event_id",
            "message_id",
            "platform_name",
            "platform_id",
            "bot_self_id",
            "group_id",
            "sender_id",
            "sender_name",
            "lsky_version",
            "album_id",
            "photo_id",
            "photo_key",
            "filename",
            "mime_type",
            "width",
            "height",
            "size_bytes",
            "sha256",
            "public_url",
            "visibility",
            "caption",
            "caption_status",
            "upload_status",
            "uploaded_at",
        ]
        values = [record.get(field) for field in fields]
        placeholders = ",".join("?" for _ in fields)
        assignments = ",".join(f"{field}=excluded.{field}" for field in fields[1:])
        sql = (
            f"INSERT INTO uploaded_images ({','.join(fields)}) VALUES ({placeholders}) "
            f"ON CONFLICT(event_id, sha256) DO UPDATE SET {assignments}"
        )
        with self._lock, self._connection() as connection:
            cursor = connection.execute(sql, values)
            row = connection.execute(
                "SELECT id FROM uploaded_images WHERE event_id=? AND sha256=?",
                (record.get("event_id"), record.get("sha256")),
            ).fetchone()
        return int(row["id"]) if row else int(cursor.lastrowid or 0)

    def record_caption(
        self,
        *,
        photo_id: str | int | None,
        sha256: str | None,
        provider_id: str,
        model_name: str,
        caption: str,
        status: str,
        error_code: str = "",
    ) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO caption_records
                    (photo_id, sha256, provider_id, model_name, caption, status, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(photo_id) if photo_id is not None else None,
                    sha256,
                    provider_id,
                    model_name,
                    caption,
                    status,
                    error_code,
                ),
            )
        return int(cursor.lastrowid or 0)

    def record_reply(
        self,
        *,
        group_id: str,
        photo_id: str | int | None,
        source_type: str,
        source_url_hash: str,
        caption_status: str,
        replied_at: str,
    ) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reply_history
                    (group_id, photo_id, source_type, source_url_hash, caption_status, replied_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    str(photo_id) if photo_id is not None else None,
                    source_type,
                    source_url_hash,
                    caption_status,
                    replied_at,
                ),
            )
        return int(cursor.lastrowid or 0)

    def recent_reply_hashes(
        self,
        group_id: str,
        *,
        limit: int = 50,
        window_seconds: float | int | None = None,
        now: datetime | None = None,
    ) -> set[str]:
        cutoff: datetime | None = None
        if window_seconds is not None:
            window = max(0.0, float(window_seconds))
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            cutoff = current.astimezone(timezone.utc) - timedelta(seconds=window)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT source_url_hash, replied_at
                FROM reply_history
                WHERE group_id=?
                ORDER BY id DESC
                """,
                (group_id,),
            ).fetchall()
        hashes: set[str] = set()
        max_items = max(1, int(limit))
        for row in rows:
            if cutoff is not None:
                value = str(row["replied_at"] or "").strip()
                try:
                    replied_at = datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if replied_at.tzinfo is None:
                    replied_at = replied_at.replace(tzinfo=timezone.utc)
                if replied_at.astimezone(timezone.utc) < cutoff:
                    continue
            hashes.add(str(row["source_url_hash"]))
            if len(hashes) >= max_items:
                break
        return hashes

    def list_uploads(self, *, group_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            if group_id is None:
                rows = connection.execute(
                    "SELECT * FROM uploaded_images ORDER BY id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM uploaded_images
                    WHERE group_id=?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (group_id, max(1, int(limit))),
                ).fetchall()
        return [dict(row) for row in rows]

    def prune_before(self, iso_timestamp: str) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM reply_history WHERE replied_at < ?",
                (iso_timestamp,),
            )
            deleted = int(cursor.rowcount or 0)
            cursor = connection.execute(
                "DELETE FROM caption_records WHERE created_at < ?",
                (iso_timestamp,),
            )
            return deleted + int(cursor.rowcount or 0)
