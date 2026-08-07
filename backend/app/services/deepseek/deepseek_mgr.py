"""DeepSeek 模块总入口：外部只调用本文件，内部分发至 Client / db_mgr。"""

from __future__ import annotations

from typing import Any

from app.repositories.database import db_mgr

__all__ = ["DeepSeekMgr", "deepseek_mgr"]


class DeepSeekMgr:
    """DeepSeek 管理器（仿造 video_factory llm_mgr / tts_mgr）。"""

    def _get_client(self):
        # Playwright client 必须进程级单例，复用 client.get_client()。
        from app.services.deepseek.client import get_client

        return get_client()

    def status(self) -> dict[str, Any]:
        return self._get_client().status()

    def ensure_ready(self) -> dict[str, Any]:
        return self._get_client().ensure_ready()

    def start(self) -> None:
        self._get_client().start()

    def stop(self) -> None:
        self._get_client().stop()

    def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        mode: str = "instant",
        deep_thinking: bool = False,
        search: bool = False,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        return self._get_client().ask(
            question,
            conversation_id=conversation_id,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
        )

    def list_conversations(
        self, *, provider: str = "deepseek", limit: int = 50
    ) -> list[dict[str, Any]]:
        return db_mgr.list_conversations(provider=provider, limit=limit)

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return db_mgr.get_conversation(conversation_id)

    def list_conversation_messages(
        self, conversation_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        return db_mgr.list_conversation_messages(conversation_id, limit=limit)


deepseek_mgr = DeepSeekMgr()
