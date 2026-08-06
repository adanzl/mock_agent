"""SQLite 本地存储：data.db。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.config import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_session (
    provider TEXT PRIMARY KEY,
    storage_state TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversation (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'deepseek',
    title TEXT,
    mode TEXT,
    deep_thinking INTEGER NOT NULL DEFAULT 0,
    search INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_conversation_provider_updated
ON conversation(provider, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (conversation_id) REFERENCES conversation(id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_message_conv
ON conversation_message(conversation_id, id);
"""

_lock = threading.Lock()
_initialized = False


def sqlite_path() -> Path:
    return config.sqlite_path


def connect() -> sqlite3.Connection:
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database() -> Path:
    global _initialized
    with _lock:
        path = sqlite_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
        _initialized = True
        logger.info("sqlite ready path=%s", path)
        return path


def get_browser_session(provider: str) -> dict[str, Any] | None:
    init_database()
    with connect() as conn:
        row = conn.execute(
            "SELECT storage_state FROM browser_session WHERE provider = ?",
            (provider,),
        ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["storage_state"])
    except json.JSONDecodeError:
        logger.warning("invalid storage_state for provider=%s", provider)
        return None
    return data if isinstance(data, dict) else None


def save_browser_session(provider: str, storage_state: dict[str, Any]) -> None:
    init_database()
    payload = json.dumps(storage_state, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO browser_session (provider, storage_state, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(provider) DO UPDATE SET
                storage_state = excluded.storage_state,
                updated_at = datetime('now')
            """,
            (provider, payload),
        )
        conn.commit()
    logger.info("browser_session saved provider=%s bytes=%s", provider, len(payload))


def has_browser_session(provider: str) -> bool:
    return get_browser_session(provider) is not None


def delete_browser_session(provider: str) -> None:
    init_database()
    with connect() as conn:
        conn.execute("DELETE FROM browser_session WHERE provider = ?", (provider,))
        conn.commit()


def upsert_conversation(
    *,
    conversation_id: str,
    provider: str = "deepseek",
    title: str | None = None,
    mode: str | None = None,
    deep_thinking: bool = False,
    search: bool = False,
    url: str | None = None,
) -> None:
    init_database()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, title FROM conversation WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO conversation (
                    id, provider, title, mode, deep_thinking, search, url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    conversation_id,
                    provider,
                    title,
                    mode,
                    1 if deep_thinking else 0,
                    1 if search else 0,
                    url,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE conversation
                SET title = COALESCE(?, title),
                    mode = COALESCE(?, mode),
                    deep_thinking = ?,
                    search = ?,
                    url = COALESCE(?, url),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    title,
                    mode,
                    1 if deep_thinking else 0,
                    1 if search else 0,
                    url,
                    conversation_id,
                ),
            )
        conn.commit()


def add_conversation_messages(
    conversation_id: str,
    messages: list[tuple[str, str]],
) -> None:
    """messages: list of (role, content)."""
    if not messages:
        return
    init_database()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO conversation_message (conversation_id, role, content)
            VALUES (?, ?, ?)
            """,
            [(conversation_id, role, content) for role, content in messages],
        )
        conn.execute(
            "UPDATE conversation SET updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
        conn.commit()


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    init_database()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversation WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_conversations(provider: str = "deepseek", limit: int = 50) -> list[dict[str, Any]]:
    init_database()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM conversation
            WHERE provider = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (provider, max(1, min(limit, 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def list_conversation_messages(
    conversation_id: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    init_database()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM conversation_message
            WHERE conversation_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (conversation_id, max(1, min(limit, 1000))),
        ).fetchall()
    return [dict(r) for r in rows]
