"""Async chat job facade: create / get; runner executes asks."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

__all__ = ["ChatJobMgr", "chat_job_mgr"]


class ChatJobMgr:
    """HTTP-facing chat job manager (shared across providers)."""

    def create(
        self,
        *,
        provider: str,
        question: str,
        conversation_id: str | None = None,
        mode: str | None = None,
        deep_thinking: bool = False,
        search: bool = False,
        timeout_s: int | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        question = (question or "").strip()
        image_list = [str(x) for x in (images or []) if str(x).strip()]
        if not question and not image_list:
            raise ValueError("question is empty")
        if not question and image_list:
            question = "请描述这张图片" if len(image_list) == 1 else "请理解这些图片"
        job_id = str(uuid.uuid4())
        job = db_mgr.create_chat_job(
            job_id=job_id,
            provider=provider,
            question=question,
            conversation_id=conversation_id,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
            images=image_list or None,
        )
        logger.info(
            "chat job created job_id=%s provider=%s conv=%s mode=%s think=%s search=%s images=%s chars=%s question=%s",
            job_id,
            provider,
            conversation_id,
            mode,
            deep_thinking,
            search,
            len(image_list),
            len(question),
            question,
        )
        # Wake runner if sleeping.
        try:
            from app.services.chat_jobs.runner import chat_job_runner

            chat_job_runner.kick()
        except Exception:
            logger.exception("chat job kick failed job_id=%s", job_id)
        return job

    def get(self, job_id: str, *, provider: str | None = None) -> dict[str, Any] | None:
        job = db_mgr.get_chat_job(job_id)
        if job is None:
            return None
        if provider is not None and job.get("provider") != provider:
            return None
        return job

    def to_api_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "ok": True,
            "job_id": job.get("id"),
            "provider": job.get("provider"),
            "status": job.get("status"),
            "question": job.get("question"),
            "conversation_id": job.get("conversation_id"),
            "mode": job.get("mode"),
            "deep_thinking": bool(job.get("deep_thinking")),
            "search": bool(job.get("search")),
            "timeout_s": job.get("timeout_s"),
            "image_count": len(job.get("images") or []),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
        }
        status = job.get("status")
        if status == "succeeded":
            payload["result"] = job.get("result")
        elif status == "failed":
            payload["error"] = job.get("error")
            payload["error_kind"] = job.get("error_kind")
        return payload


chat_job_mgr = ChatJobMgr()
