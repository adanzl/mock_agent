"""SQLite 本地存储：data.db。仿造 my_todo DbMgr 单例。"""

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

CREATE TABLE IF NOT EXISTS chat_job (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    question TEXT NOT NULL,
    conversation_id TEXT,
    mode TEXT,
    deep_thinking INTEGER NOT NULL DEFAULT 0,
    search INTEGER NOT NULL DEFAULT 0,
    timeout_s INTEGER,
    images_json TEXT,
    result_json TEXT,
    error TEXT,
    error_kind TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_job_provider_status_created
ON chat_job(provider, status, created_at);

CREATE INDEX IF NOT EXISTS idx_chat_job_provider_created
ON chat_job(provider, created_at DESC);
"""


class DbMgr:
    """数据库管理：进程内单例，schema 只初始化一次。"""

    def __init__(self) -> None:
        self._initialized = False
        self._lock = threading.Lock()

    def path(self) -> Path:
        return config.sqlite_path

    def connect(self) -> sqlite3.Connection:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> Path:
        """初始化 schema；已初始化则直接返回路径。"""
        with self._lock:
            if self._initialized:
                return self.path()

            path = self.path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.executescript(_SCHEMA)
                self._ensure_chat_job_images_column(conn)
                conn.commit()
            self._initialized = True
            logger.info("sqlite ready path=%s", path)
            return path

    @staticmethod
    def _ensure_chat_job_images_column(conn: sqlite3.Connection) -> None:
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(chat_job)").fetchall()
        }
        if "images_json" not in cols:
            conn.execute("ALTER TABLE chat_job ADD COLUMN images_json TEXT")
            logger.info("sqlite migrated chat_job.images_json")

    def get_browser_session(self, provider: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as conn:
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

    def save_browser_session(self, provider: str, storage_state: dict[str, Any]) -> None:
        self.init()
        payload = json.dumps(storage_state, ensure_ascii=False)
        with self.connect() as conn:
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

    def has_browser_session(self, provider: str) -> bool:
        return self.get_browser_session(provider) is not None

    def delete_browser_session(self, provider: str) -> None:
        self.init()
        with self.connect() as conn:
            conn.execute("DELETE FROM browser_session WHERE provider = ?", (provider,))
            conn.commit()

    def upsert_conversation(
        self,
        *,
        conversation_id: str,
        provider: str = "deepseek",
        title: str | None = None,
        mode: str | None = None,
        deep_thinking: bool = False,
        search: bool = False,
        url: str | None = None,
    ) -> None:
        self.init()
        with self.connect() as conn:
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
        self,
        conversation_id: str,
        messages: list[tuple[str, str]],
    ) -> None:
        """messages: list of (role, content)."""
        if not messages:
            return
        self.init()
        with self.connect() as conn:
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

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversation WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_conversations(
        self, provider: str = "deepseek", limit: int = 50
    ) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
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
        self,
        conversation_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
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

    @staticmethod
    def _row_to_chat_job(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["deep_thinking"] = bool(item.get("deep_thinking"))
        item["search"] = bool(item.get("search"))
        images: list[str] = []
        images_raw = item.pop("images_json", None)
        if images_raw:
            try:
                parsed = json.loads(images_raw)
                if isinstance(parsed, list):
                    images = [str(x) for x in parsed if str(x).strip()]
            except json.JSONDecodeError:
                logger.warning("invalid chat_job images_json id=%s", item.get("id"))
        item["images"] = images
        raw = item.pop("result_json", None)
        result = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    result = parsed
            except json.JSONDecodeError:
                logger.warning("invalid chat_job result_json id=%s", item.get("id"))
        item["result"] = result
        return item

    def create_chat_job(
        self,
        *,
        job_id: str,
        provider: str,
        question: str,
        conversation_id: str | None = None,
        mode: str | None = None,
        deep_thinking: bool = False,
        search: bool = False,
        timeout_s: int | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        self.init()
        images_json = None
        if images:
            images_json = json.dumps(list(images), ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_job (
                    id, provider, status, question, conversation_id, mode,
                    deep_thinking, search, timeout_s, images_json, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    job_id,
                    provider,
                    question,
                    conversation_id,
                    mode,
                    1 if deep_thinking else 0,
                    1 if search else 0,
                    timeout_s,
                    images_json,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM chat_job WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_chat_job(row)

    def get_chat_job(self, job_id: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_job WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_chat_job(row)

    def claim_next_chat_job(self) -> dict[str, Any] | None:
        """Atomically claim the oldest queued job (status queued -> running)."""
        self.init()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM chat_job
                WHERE status = 'queued'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            job_id = row["id"]
            cur = conn.execute(
                """
                UPDATE chat_job
                SET status = 'running',
                    started_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ? AND status = 'queued'
                """,
                (job_id,),
            )
            if cur.rowcount != 1:
                conn.commit()
                return None
            conn.commit()
            claimed = conn.execute(
                "SELECT * FROM chat_job WHERE id = ?",
                (job_id,),
            ).fetchone()
        if claimed is None:
            return None
        return self._row_to_chat_job(claimed)

    def finish_chat_job_success(
        self,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        self.init()
        payload = json.dumps(result, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE chat_job
                SET status = 'succeeded',
                    result_json = ?,
                    error = NULL,
                    error_kind = NULL,
                    finished_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (payload, job_id),
            )
            conn.commit()

    def finish_chat_job_failure(
        self,
        job_id: str,
        *,
        error: str,
        error_kind: str,
    ) -> None:
        self.init()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE chat_job
                SET status = 'failed',
                    error = ?,
                    error_kind = ?,
                    finished_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (error, error_kind, job_id),
            )
            conn.commit()

    def fail_running_chat_jobs(self, *, error: str = "interrupted by process restart") -> int:
        """Mark leftover running jobs as failed (e.g. after restart)."""
        self.init()
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE chat_job
                SET status = 'failed',
                    error = ?,
                    error_kind = 'other',
                    finished_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE status = 'running'
                """,
                (error,),
            )
            conn.commit()
            return int(cur.rowcount)


db_mgr = DbMgr()
