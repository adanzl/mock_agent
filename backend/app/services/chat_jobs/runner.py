"""Background runner: claim queued chat jobs and call provider ask()."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

__all__ = ["ChatJobRunner", "chat_job_runner"]

AskFn = Callable[..., dict[str, Any]]


class ChatJobRunner:
    """Single background thread that drains chat_job queue."""

    def __init__(self, *, poll_interval_s: float = 0.5) -> None:
        self.poll_interval_s = max(0.1, float(poll_interval_s))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._askers: dict[str, AskFn] | None = None

    def _resolve_askers(self) -> dict[str, AskFn]:
        if self._askers is not None:
            return self._askers
        from app.services.agnes.agnes_mgr import agnes_mgr
        from app.services.chatgpt.chatgpt_mgr import chatgpt_mgr
        from app.services.deepseek.deepseek_mgr import deepseek_mgr
        from app.services.qwen.qwen_mgr import qwen_mgr

        self._askers = {
            "deepseek": deepseek_mgr.ask,
            "chatgpt": chatgpt_mgr.ask,
            "qwen": qwen_mgr.ask,
            "agnes": agnes_mgr.ask,
        }
        return self._askers

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # Leftover running jobs from a previous crash cannot be trusted.
        n = db_mgr.fail_running_chat_jobs()
        if n:
            logger.warning("marked stale running chat jobs failed count=%s", n)
        self._thread = threading.Thread(
            target=self._loop,
            name="chat-job-runner",
            daemon=True,
        )
        self._thread.start()
        logger.info("jobs: runner started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
        logger.info("jobs: runner stopped")

    def kick(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = None
            try:
                job = db_mgr.claim_next_chat_job()
            except Exception:
                logger.exception("claim chat job failed")
            if job is None:
                self._wake.wait(timeout=self.poll_interval_s)
                self._wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception:
                logger.exception(
                    "chat job unexpected failure job_id=%s provider=%s",
                    job.get("id"),
                    job.get("provider"),
                )
                try:
                    db_mgr.finish_chat_job_failure(
                        str(job.get("id")),
                        error="unexpected runner failure",
                        error_kind="other",
                    )
                except Exception:
                    logger.exception("finish chat job failure write failed")

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        provider = str(job.get("provider") or "")
        askers = self._resolve_askers()
        ask = askers.get(provider)
        if ask is None:
            db_mgr.finish_chat_job_failure(
                job_id,
                error=f"unknown provider: {provider}",
                error_kind="value",
            )
            logger.error("chat job unknown provider job_id=%s provider=%s", job_id, provider)
            return

        question = str(job.get("question") or "")
        conversation_id = job.get("conversation_id")
        mode = job.get("mode")
        deep_thinking = bool(job.get("deep_thinking"))
        search = bool(job.get("search"))
        timeout_s = job.get("timeout_s")
        images = job.get("images") or []
        logger.info(
            "chat job start job_id=%s provider=%s conv=%s mode=%s think=%s search=%s images=%s chars=%s question=%s",
            job_id,
            provider,
            conversation_id,
            mode,
            deep_thinking,
            search,
            len(images),
            len(question),
            question,
        )
        t0 = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {
                "question": question,
                "conversation_id": str(conversation_id) if conversation_id else None,
                "deep_thinking": deep_thinking,
                "search": search,
                "timeout_s": int(timeout_s) if timeout_s is not None else None,
            }
            if mode is not None:
                kwargs["mode"] = str(mode)
            if images and provider == "qwen":
                kwargs["images"] = list(images)
            result = ask(**kwargs)
        except ValueError as exc:
            db_mgr.finish_chat_job_failure(job_id, error=str(exc), error_kind="value")
            logger.warning(
                "chat job failed job_id=%s provider=%s kind=value error=%s",
                job_id,
                provider,
                exc,
            )
            return
        except RuntimeError as exc:
            db_mgr.finish_chat_job_failure(job_id, error=str(exc), error_kind="runtime")
            logger.warning(
                "chat job failed job_id=%s provider=%s kind=runtime error=%s",
                job_id,
                provider,
                exc,
            )
            return
        except TimeoutError as exc:
            db_mgr.finish_chat_job_failure(job_id, error=str(exc), error_kind="timeout")
            logger.error(
                "chat job failed job_id=%s provider=%s kind=timeout error=%s",
                job_id,
                provider,
                exc,
            )
            return
        except Exception as exc:
            db_mgr.finish_chat_job_failure(job_id, error=str(exc), error_kind="other")
            logger.exception(
                "chat job failed job_id=%s provider=%s kind=other error=%s",
                job_id,
                provider,
                exc,
            )
            return

        db_mgr.finish_chat_job_success(job_id, result)
        answer = result.get("answer") or ""
        logger.info(
            "chat job ok job_id=%s provider=%s conv=%s worker=%s answer_chars=%s elapsed=%.1fs answer=%s",
            job_id,
            provider,
            result.get("conversation_id"),
            result.get("worker_id"),
            len(answer),
            time.perf_counter() - t0,
            answer,
        )


chat_job_runner = ChatJobRunner()
