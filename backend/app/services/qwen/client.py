"""Playwright client that drives chat.qwen.ai like a real user."""

from __future__ import annotations

import base64
import logging
import mimetypes
import queue
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from app.config import config
from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

PROVIDER = "qwen"
CHAT_URL = "https://chat.qwen.ai/"
AUTH_URL = "https://chat.qwen.ai/auth"
CONVERSATION_ID_RE = re.compile(r"/c/([^/?#]+)", re.IGNORECASE)
RESERVED_CONV_IDS = {"new-chat", "guest", "new"}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DATA_URL_RE = re.compile(
    r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)

# Playwright sync API is thread-bound; each worker loop sets this.
_tls = threading.local()


@dataclass
class _WorkerSlot:
    """One Playwright thread + its browser page."""

    worker_id: int
    tasks: "queue.Queue[tuple[Callable[[], Any] | None, queue.Queue[tuple]]]" = field(
        default_factory=queue.Queue
    )
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    busy: bool = False
    pending: int = 0
    last_status: dict[str, Any] | None = None


# mode=auto keeps current model; other values try to match model selector text.
MODE_ALIASES = {
    "auto": "auto",
    "default": "auto",
    "plus": "Qwen3.5-Plus",
    "qwen3.5-plus": "Qwen3.5-Plus",
    "flash": "Qwen3.5-Flash",
    "qwen3.5-flash": "Qwen3.5-Flash",
    "max": "Qwen3-Max",
    "qwen3-max": "Qwen3-Max",
    "qwen3.6-plus": "Qwen3.6-Plus",
}

CHAT_READY_SELECTORS = [
    "textarea.message-input-textarea",
    "textarea[placeholder*='Ask']",
    "textarea[placeholder*='输入']",
    "textarea[placeholder*='Message']",
    "textarea",
]
SIGN_IN_SELECTORS = [
    ".qwenchat-auth-pc-input-items",
    "button.qwenchat-auth-pc-submit-button",
    "input[type='password']",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "button:has-text('登录')",
]
ASSISTANT_SELECTORS = [
    ".response-message-content.phase-answer",
    ".qwen-chat-message-assistant .qwen-markdown",
    ".qwen-chat-message-assistant",
    ".chat-response-message",
]
SEND_SELECTORS = [
    "div.chat-prompt-send-button button",
    "button:has-text('发送')",
    "button:has-text('Send')",
    "[aria-label*='Send']",
    "[aria-label*='发送']",
]
STOP_SELECTORS = [
    "button.stop-button",
    "button:has-text('停止')",
    "button:has-text('Stop')",
    "[aria-label*='Stop']",
    "[aria-label*='停止']",
    "div.chat-prompt-send-button button.stop-button",
]
NEW_CHAT_SELECTORS = [
    "div.sidebar-entry-fixed-list-content:has-text('New Chat')",
    "div.sidebar-entry-fixed-list-content:has-text('新对话')",
    "div.sidebar-entry-fixed-list-content:has-text('新聊天')",
    "button:has-text('New Chat')",
    "button:has-text('新对话')",
    "a:has-text('New Chat')",
    "a:has-text('新对话')",
]
DONE_HINT_SELECTORS = [
    "button.copy-response-button",
    ".copy-response-button",
]
FILE_INPUT_SELECTORS = [
    'input[type="file"][accept*="image"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"][accept*="audio"]',
    'input[type="file"]',
]
ATTACH_BUTTON_SELECTORS = [
    "button[aria-label*='Upload']",
    "button[aria-label*='upload']",
    "button[aria-label*='上传']",
    "button[aria-label*='Attach']",
    "button[aria-label*='附件']",
    "button[aria-label*='Image']",
    "button[aria-label*='图片']",
    "[class*='upload-button']",
    "[class*='attach-button']",
    "button:has-text('上传')",
]
ATTACHMENT_PREVIEW_SELECTORS = [
    ".chat-prompt img",
    "[class*='attachment'] img",
    "[class*='preview'] img",
    "[class*='upload'] img",
    ".message-input img",
]


class QwenClient:
    """Browser pool: N Playwright workers run chats in parallel."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        timeout_ms: int | None = None,
        workers: int | None = None,
    ) -> None:
        self.headless = config.headless if headless is None else headless
        self.timeout_ms = int(
            config.qwen_timeout_ms if timeout_ms is None else timeout_ms
        )
        self.browser_channel = config.browser_channel
        self.executable_path = config.chrome_path
        self.username = config.qwen_username
        self.password = config.qwen_password
        self.auto_login = config.qwen_auto_login
        self.worker_count = max(
            1,
            min(4, int(config.qwen_workers if workers is None else workers)),
        )

        self._slots: list[_WorkerSlot] = [
            _WorkerSlot(worker_id=i) for i in range(self.worker_count)
        ]
        self._pick_lock = threading.Lock()
        self._storage_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_status: dict[str, Any] | None = None
        self._browser_info = ""
        self._threads: list[threading.Thread] = []
        for slot in self._slots:
            thread = threading.Thread(
                target=self._worker_loop,
                args=(slot,),
                name=f"qwen-playwright-{slot.worker_id}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        logger.info("qwen: pool workers=%s", self.worker_count)

    def _worker_loop(self, slot: _WorkerSlot) -> None:
        while True:
            func, done = slot.tasks.get()
            if func is None:
                with self._pick_lock:
                    slot.pending = max(0, slot.pending - 1)
                try:
                    _tls.slot = slot
                    self._stop_unlocked()
                except Exception:
                    logger.exception("worker %s stop failed", slot.worker_id)
                finally:
                    _tls.slot = None
                    slot.busy = False
                    done.put((True, None))
                    slot.tasks.task_done()
                return

            slot.busy = True
            _tls.slot = slot
            try:
                result = func()
                done.put((True, result))
            except BaseException as exc:  # noqa: BLE001
                done.put((False, exc))
            finally:
                with self._pick_lock:
                    slot.pending = max(0, slot.pending - 1)
                try:
                    self._refresh_slot_status(slot)
                    self._publish_status()
                except Exception:
                    pass
                _tls.slot = None
                slot.busy = False
                slot.tasks.task_done()

    def _current_slot(self) -> _WorkerSlot:
        slot = getattr(_tls, "slot", None)
        if slot is None:
            raise RuntimeError("qwen browser call must run on a worker thread")
        return slot

    def _pick_slot(self) -> _WorkerSlot:
        """Prefer an idle worker; otherwise the shortest queue."""
        with self._pick_lock:
            return min(
                self._slots,
                key=lambda s: (s.busy, s.pending, s.worker_id),
            )

    def _submit_to(self, slot: _WorkerSlot, func: Callable[[], Any]) -> Any:
        done: "queue.Queue[tuple]" = queue.Queue()
        with self._pick_lock:
            slot.pending += 1
            slot.tasks.put((func, done))
        ok, result = done.get()
        if not ok:
            raise result
        return result

    def _submit(self, func: Callable[[], Any]) -> Any:
        """Run func on one free (or least-loaded) Playwright worker.

        Pick + enqueue under one lock so concurrent /chat calls fan out to
        different workers instead of all landing on worker 0.
        """
        done: "queue.Queue[tuple]" = queue.Queue()
        with self._pick_lock:
            slot = min(
                self._slots,
                key=lambda s: (s.busy, s.pending, s.worker_id),
            )
            slot.pending += 1
            slot.tasks.put((func, done))
            logger.debug(
                "submit worker=%s busy=%s pending=%s slots=%s",
                slot.worker_id,
                slot.busy,
                slot.pending,
                [(s.worker_id, s.busy, s.pending) for s in self._slots],
            )
        ok, result = done.get()
        if not ok:
            raise result
        return result

    def _submit_all(
        self,
        func: Callable[[], Any],
        *,
        serial: bool = False,
    ) -> list[Any]:
        """Run func once on every worker (start / ensure_ready / stop prep)."""
        if serial:
            results: list[Any] = []
            errors: list[BaseException] = []
            for slot in self._slots:
                try:
                    results.append(self._submit_to(slot, func))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
            if errors and not results:
                raise errors[0]
            if errors:
                logger.warning(
                    "worker pool partial failure ok=%s err=%s",
                    len(results),
                    "; ".join(str(e) for e in errors),
                )
            return results

        dones: list["queue.Queue[tuple]"] = []
        with self._pick_lock:
            for slot in self._slots:
                done: "queue.Queue[tuple]" = queue.Queue()
                slot.pending += 1
                slot.tasks.put((func, done))
                dones.append(done)
        results: list[Any] = []
        errors: list[BaseException] = []
        for done in dones:
            ok, result = done.get()
            if ok:
                results.append(result)
            else:
                errors.append(result)
        if errors and not results:
            raise errors[0]
        if errors:
            logger.warning(
                "worker pool partial failure ok=%s err=%s",
                len(results),
                "; ".join(str(e) for e in errors),
            )
        return results

    def _default_status(self) -> dict[str, Any]:
        return {
            "ready": False,
            "state": "unknown",
            "url": "",
            "headless": self.headless,
            "browser": self._browser_info or self.browser_channel,
            "sqlite_path": str(db_mgr.path()),
            "session_saved": db_mgr.has_browser_session(PROVIDER),
            "workers": self.worker_count,
            "busy": 0,
            "idle": self.worker_count,
            "queued": 0,
        }

    def _refresh_slot_status(self, slot: _WorkerSlot) -> None:
        if slot.page is None:
            return
        state = self._detect_shell_state(slot.page)
        slot.last_status = {
            "worker_id": slot.worker_id,
            "ready": state == "chat",
            "state": state,
            "url": slot.page.url,
            "busy": slot.busy,
            "logged_in": self._has_token_cookie_in(slot.context),
        }

    def _publish_status(self) -> None:
        with self._status_lock:
            details = []
            ready_any = False
            state = "unknown"
            url = ""
            busy = 0
            queued = 0
            for slot in self._slots:
                if slot.busy:
                    busy += 1
                queued += slot.pending
                detail = dict(slot.last_status or {})
                detail.setdefault("worker_id", slot.worker_id)
                detail["busy"] = slot.busy
                detail["queued"] = slot.pending
                details.append(detail)
                if detail.get("ready"):
                    ready_any = True
                if detail.get("state") and detail.get("state") != "unknown":
                    state = str(detail.get("state"))
                if detail.get("url"):
                    url = str(detail.get("url"))
            self._last_status = {
                "ready": ready_any,
                "state": state if ready_any else state,
                "url": url,
                "headless": self.headless,
                "browser": self._browser_info or self.browser_channel,
                "sqlite_path": str(db_mgr.path()),
                "session_saved": db_mgr.has_browser_session(PROVIDER),
                "workers": self.worker_count,
                "busy": busy,
                "idle": max(0, self.worker_count - busy),
                "queued": queued,
                "workers_detail": details,
            }

    def start(self) -> None:
        self._submit_all(self._start_impl, serial=True)

    def _start_impl(self) -> None:
        logger.debug(
            "browser start requested worker=%s headless=%s channel=%s",
            self._current_slot().worker_id,
            self.headless,
            self.browser_channel,
        )
        self._start_unlocked()

    def stop(self) -> None:
        """Close every worker browser; threads stay alive for a later start/ask."""
        try:
            self._submit_all(self._stop_impl)
        except Exception:
            logger.exception("qwen worker pool stop failed")
        self._publish_status()
        logger.info("qwen worker pool browsers closed workers=%s", self.worker_count)

    def _stop_impl(self) -> None:
        self._stop_unlocked()

    def status(self) -> dict[str, Any]:
        self._publish_status()
        if self._last_status is not None:
            return dict(self._last_status)
        return self._default_status()

    def ensure_ready(self) -> dict[str, Any]:
        """Start every worker browser and auto-login from .env if needed."""
        results = self._submit_all(self._ensure_ready_impl, serial=True)
        self._publish_status()
        merged = dict(results[0] if results else {"ok": True, "ready": False})
        merged["workers"] = self.worker_count
        merged["ready_workers"] = sum(1 for r in results if r and r.get("ready"))
        if self._last_status:
            merged.update(
                {
                    "busy": self._last_status.get("busy"),
                    "idle": self._last_status.get("idle"),
                    "queued": self._last_status.get("queued"),
                    "workers_detail": self._last_status.get("workers_detail"),
                }
            )
        return merged

    def _ensure_ready_impl(self) -> dict[str, Any]:
        page = self._ensure_page_unlocked()
        state = self._detect_shell_state(page)
        # Guest chat also shows the textarea; when credentials are configured,
        # require a real login cookie instead of treating guest as ready.
        if self._needs_account_login() and not self._has_token_cookie():
            ok = self._ensure_logged_in(page, force=True)
            state = self._detect_shell_state(page)
            if not ok or state != "chat" or not self._has_token_cookie():
                raise RuntimeError(self._login_failed_message())
        elif state == "auth":
            ok = self._ensure_logged_in(page)
            state = self._detect_shell_state(page)
            if not ok or state != "chat":
                raise RuntimeError(self._login_failed_message())
        elif state != "chat":
            self._goto(page, CHAT_URL)
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if self._needs_account_login() and not self._has_token_cookie():
                if not self._ensure_logged_in(page, force=True):
                    raise RuntimeError(self._login_failed_message())
            elif state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(self._login_failed_message())
            state = self._detect_shell_state(page)
            if state != "chat":
                raise RuntimeError(f"chat UI not ready, state={state}")
        self._save_storage_unlocked()
        self._refresh_slot_status(self._current_slot())
        return {
            "ok": True,
            "ready": True,
            "state": state,
            "url": page.url,
            "session_saved": db_mgr.has_browser_session(PROVIDER),
            "logged_in": self._has_token_cookie(),
            "worker_id": self._current_slot().worker_id,
        }

    def _login_failed_message(self) -> str:
        return "auto login failed; check QWEN_USERNAME/QWEN_PASSWORD in .env"

    def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        mode: str = "auto",
        deep_thinking: bool = False,
        search: bool = False,
        timeout_s: int | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        question = (question or "").strip()
        image_list = [str(x).strip() for x in (images or []) if str(x).strip()]
        if len(image_list) > MAX_IMAGES:
            raise ValueError(f"too many images (max {MAX_IMAGES})")
        if not question and not image_list:
            raise ValueError("question is empty")
        if not question and image_list:
            question = "请描述这张图片" if len(image_list) == 1 else "请理解这些图片"

        resolved_mode = self._normalize_mode(mode)
        timeout_s = self._resolve_chat_timeout_s(
            timeout_s,
            deep_thinking=deep_thinking,
        )
        attempts = 2
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._submit(
                    lambda: self._ask_impl(
                        question=question,
                        conversation_id=conversation_id,
                        mode=resolved_mode,
                        deep_thinking=deep_thinking,
                        search=search,
                        timeout_s=timeout_s,
                        images=image_list,
                    )
                )
            except TimeoutError as exc:
                last_exc = exc
                will_retry = attempt < attempts
                logger.warning(
                    "ask incomplete attempt=%s/%s will_retry=%s conv=%s error=%s",
                    attempt,
                    attempts,
                    will_retry,
                    conversation_id,
                    exc,
                )
                if not will_retry:
                    raise
        assert last_exc is not None
        raise last_exc

    def _resolve_chat_timeout_s(
        self,
        timeout_s: int | None,
        *,
        deep_thinking: bool,
    ) -> int:
        if timeout_s is not None:
            return max(30, int(timeout_s))
        if deep_thinking:
            return max(30, config.qwen_think_timeout_s)
        return max(30, config.qwen_chat_timeout_s)

    def _ask_impl(
        self,
        *,
        question: str,
        conversation_id: str | None,
        mode: str,
        deep_thinking: bool,
        search: bool,
        timeout_s: int,
        images: list[str],
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        slot = self._current_slot()
        page = self._ensure_page_unlocked()
        state = self._detect_shell_state(page)
        logger.info(
            "ask start worker=%s state=%s conv=%s mode=%s think=%s search=%s images=%s chars=%s timeout_s=%s question=%s",
            slot.worker_id,
            state,
            conversation_id,
            mode,
            deep_thinking,
            search,
            len(images),
            len(question),
            timeout_s,
            question,
        )
        if self._needs_account_login() and not self._has_token_cookie():
            if not self._ensure_logged_in(page, force=True):
                raise RuntimeError(
                    "not logged in; check QWEN_USERNAME/QWEN_PASSWORD in .env "
                    f"(sqlite={db_mgr.path()})"
                )
            state = self._detect_shell_state(page)
        elif state == "auth":
            if not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check QWEN_USERNAME/QWEN_PASSWORD in .env "
                    f"(sqlite={db_mgr.path()})"
                )
            state = self._detect_shell_state(page)

        if state != "chat":
            self._goto(page, CHAT_URL)
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if self._needs_account_login() and not self._has_token_cookie():
                if not self._ensure_logged_in(page, force=True):
                    raise RuntimeError(
                        "not logged in; check QWEN_USERNAME/QWEN_PASSWORD in .env"
                    )
            elif state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check QWEN_USERNAME/QWEN_PASSWORD in .env"
                )
            state = self._detect_shell_state(page)
            if state != "chat":
                raise RuntimeError(f"chat UI not ready, state={state}")

        if conversation_id:
            self._open_conversation(page, conversation_id)
        else:
            self._open_new_chat(page)

        mode = self._apply_chat_options(
            page,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            switch_mode=not bool(conversation_id),
        )

        temp_dir: Path | None = None
        try:
            before = self._assistant_count(page)
            if images:
                temp_dir, local_paths = self._materialize_images(images)
                self._attach_images(page, local_paths)
            self._enter_message(page, question)
            self._send_message(page)
            answer = self._wait_answer(page, before_count=before, timeout_s=timeout_s)
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

        self._save_storage_unlocked()

        conv_id = self._conversation_id_from_url(page.url) or conversation_id
        if not conv_id:
            raise RuntimeError("conversation_id missing after reply; Qwen URL has no chat id")

        title = question.replace("\n", " ").strip()[:40] or None
        if images and (not title or title.startswith("请描述") or title.startswith("请理解")):
            title = f"图片理解({len(images)})"
        db_mgr.upsert_conversation(
            conversation_id=conv_id,
            provider=PROVIDER,
            title=title if not conversation_id else None,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            url=page.url,
        )
        db_mgr.add_conversation_messages(
            conv_id,
            [("user", question), ("assistant", answer)],
        )

        logger.info(
            "ask done worker=%s answer_chars=%s conv=%s images=%s elapsed=%.1fs answer=%s",
            slot.worker_id,
            len(answer),
            conv_id,
            len(images),
            time.perf_counter() - t0,
            answer,
        )
        return {
            "ok": True,
            "answer": answer,
            "question": question,
            "mode": mode,
            "deep_thinking": deep_thinking,
            "search": search,
            "conversation_id": conv_id,
            "url": page.url,
            "worker_id": slot.worker_id,
            "image_count": len(images),
        }

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        raw = (mode or "auto").strip()
        key = raw.lower()
        if key in MODE_ALIASES:
            return MODE_ALIASES[key]
        # Allow passing a concrete model label, e.g. "Qwen3.5-Plus".
        if raw and raw.lower() != "auto":
            return raw
        return "auto"

    def _apply_chat_options(
        self,
        page: Page,
        *,
        mode: str,
        deep_thinking: bool,
        search: bool,
        switch_mode: bool = True,
    ) -> str:
        actual_mode = mode
        if switch_mode and mode and mode != "auto":
            actual_mode = self._select_model(page, mode) or mode
        self._set_thinking_mode(page, enabled=deep_thinking)
        self._set_search_mode(page, enabled=search)
        return actual_mode

    def _select_model(self, page: Page, mode: str) -> str | None:
        trigger = page.locator(
            "[class*='model-selector-text'], "
            "div[class*='model-selector'], "
            "button:has-text('Qwen')"
        )
        if trigger.count() == 0:
            logger.warning("model selector not found; keep mode=%s", mode)
            return None
        try:
            trigger.first.click(timeout=5_000)
            time.sleep(0.4)
        except Exception as exc:
            logger.warning("open model selector failed: %s", exc)
            return None

        popup = page.locator("div[class*='model-selector-popup'], [role='menu'], ul")
        option = page.locator(
            f"div[class*='model-selector-popup'] *:has-text('{mode}'), "
            f"[role='menuitem']:has-text('{mode}'), "
            f"li:has-text('{mode}'), "
            f"div:has-text('{mode}')"
        )
        try:
            if option.count() > 0:
                option.first.click(timeout=5_000)
                logger.info("model set to %s", mode)
                time.sleep(0.3)
                return mode
        except Exception as exc:
            logger.warning("select model %s failed: %s", mode, exc)
        finally:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            _ = popup
        logger.warning("model option not found: %s", mode)
        return None

    def _set_thinking_mode(self, page: Page, *, enabled: bool) -> None:
        """Qwen thinking control: Thinking / Fast (or 思考 / 快速)."""
        wanted = ("Thinking", "思考", "深度思考") if enabled else ("Fast", "快速", "极速")
        trigger = page.locator(
            "span.ant-select-selection-item:has(div.qwen-select-thinking-label), "
            "div.qwen-select-thinking-label, "
            "span.qwen-select-thinking-label-text"
        )
        if trigger.count() == 0:
            # Fallback text buttons.
            for text in ("深度思考", "Thinking", "思考"):
                loc = page.locator(
                    f"button:has-text('{text}'), [role='button']:has-text('{text}')"
                )
                if loc.count() == 0:
                    continue
                btn = loc.first
                class_attr = (btn.get_attribute("class") or "").lower()
                aria = (btn.get_attribute("aria-pressed") or "").lower()
                active = (
                    "active" in class_attr
                    or "selected" in class_attr
                    or aria == "true"
                )
                if active != enabled:
                    try:
                        btn.click(timeout=3_000)
                        logger.info("thinking toggled via text=%s enabled=%s", text, enabled)
                        time.sleep(0.2)
                    except Exception:
                        continue
                return
            if enabled:
                logger.warning("thinking control not found")
            return

        try:
            current = (
                page.locator("span.qwen-select-thinking-label-text").first.inner_text(
                    timeout=2_000
                )
                or ""
            ).strip()
        except Exception:
            current = ""
        if any(w.lower() in current.lower() for w in wanted):
            logger.info("thinking already=%s current=%s", enabled, current)
            return

        try:
            trigger.first.click(timeout=4_000)
            time.sleep(0.3)
        except Exception as exc:
            logger.warning("open thinking dropdown failed: %s", exc)
            return

        clicked = False
        for label in wanted:
            opt = page.locator(
                f"div.rc-virtual-list-holder-inner *:has-text('{label}'), "
                f"div.qwen-select-thinking-label:has-text('{label}'), "
                f"[role='option']:has-text('{label}'), "
                f"li:has-text('{label}')"
            )
            if opt.count() == 0:
                continue
            try:
                opt.first.click(timeout=3_000)
                clicked = True
                logger.info("thinking set to %s", label)
                time.sleep(0.2)
                break
            except Exception:
                continue
        if not clicked and enabled:
            logger.warning("thinking option not found wanted=%s", wanted)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    def _set_search_mode(self, page: Page, *, enabled: bool) -> None:
        """Toggle web search via mode-select (best-effort)."""
        container = page.locator("div.mode-select-current-mode")
        currently_on = container.count() > 0 and container.first.is_visible()
        if currently_on and not enabled:
            close = page.locator("span.mode-select-current-mode-close")
            if close.count() > 0:
                try:
                    close.first.click(timeout=3_000)
                    logger.info("search disabled")
                    time.sleep(0.2)
                except Exception as exc:
                    logger.warning("disable search failed: %s", exc)
            return
        if currently_on and enabled:
            logger.info("search already enabled")
            return
        if not enabled:
            return

        trigger = page.locator(
            "div.mode-select-open, div.mode-select, "
            "button:has-text('搜索'), button:has-text('Search'), "
            "[aria-label*='Search'], [aria-label*='搜索']"
        )
        if trigger.count() == 0:
            logger.warning("search control not found")
            return
        try:
            trigger.first.click(timeout=4_000)
            time.sleep(0.3)
        except Exception as exc:
            logger.warning("open search menu failed: %s", exc)
            return

        option = page.locator(
            "ul.ant-dropdown-menu-root.qwen-dropdown-menu li:has-text('搜索'), "
            "ul.ant-dropdown-menu-root li:has-text('Search'), "
            "ul.ant-dropdown-menu-root li:has-text('Web'), "
            "li[data-menu-id]:has-text('搜索'), "
            "li[data-menu-id]:has-text('Search')"
        )
        if option.count() == 0:
            logger.warning("search menu item not found")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return
        try:
            option.first.click(timeout=3_000)
            logger.info("search enabled")
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("enable search failed: %s", exc)

    def _conversation_id_from_url(self, url: str) -> str | None:
        match = CONVERSATION_ID_RE.search(url or "")
        if not match:
            return None
        conv = match.group(1).strip()
        if not conv or conv.lower() in RESERVED_CONV_IDS:
            return None
        return conv

    def _open_conversation(self, page: Page, conversation_id: str) -> None:
        conv = conversation_id.strip()
        match = CONVERSATION_ID_RE.search(conv)
        if match:
            conv = match.group(1)
        if not conv or conv.lower() in RESERVED_CONV_IDS:
            raise ValueError(f"invalid conversation_id: {conversation_id}")

        record = db_mgr.get_conversation(conv)
        target = (record or {}).get("url") or f"{CHAT_URL.rstrip('/')}/c/{conv}"
        if self._conversation_id_from_url(page.url) == conv:
            logger.info("already on conversation id=%s", conv)
            return
        logger.info("open conversation id=%s url=%s", conv, target)
        self._goto(page, str(target))
        state = self._wait_shell_state(page, timeout_ms=60_000)
        if state != "chat":
            raise RuntimeError(f"failed to open conversation {conv}, state={state}")
        opened = self._conversation_id_from_url(page.url)
        if opened and opened.lower() != conv.lower():
            logger.warning("opened conv mismatch want=%s got=%s", conv, opened)

    def _launch_browser(self, playwright: Playwright) -> Browser:
        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-notifications",
            "--lang=zh-CN",
            "--disable-http2",
            "--disable-quic",
        ]
        common: dict[str, Any] = {
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation"],
        }
        errors: list[str] = []

        if self.executable_path:
            try:
                browser = playwright.chromium.launch(
                    executable_path=self.executable_path,
                    **common,
                )
                self._browser_info = f"executable:{self.executable_path}"
                logger.debug("browser launched via executable_path=%s", self.executable_path)
                return browser
            except Exception as exc:
                errors.append(f"executable_path={self.executable_path}: {exc}")
                logger.warning("launch via executable_path failed: %s", exc)

        channel = self.browser_channel
        if channel and channel.lower() not in {"", "chromium", "bundled"}:
            try:
                browser = playwright.chromium.launch(channel=channel, **common)
                self._browser_info = f"channel:{channel}"
                logger.debug("browser launched via channel=%s headless=%s", channel, self.headless)
                return browser
            except Exception as exc:
                errors.append(f"channel={channel}: {exc}")
                logger.warning("launch via channel=%s failed: %s", channel, exc)

        try:
            browser = playwright.chromium.launch(**common)
            self._browser_info = "bundled:chromium"
            logger.debug("browser launched via bundled chromium headless=%s", self.headless)
            return browser
        except Exception as exc:
            errors.append(f"bundled chromium: {exc}")
            detail = " | ".join(errors) if errors else str(exc)
            logger.error("browser launch failed: %s", detail)
            raise RuntimeError(
                "failed to launch browser; install Chrome or set "
                "CHROME_PATH to chrome. "
                f"Tried: {detail}"
            ) from exc


    def _start_unlocked(self) -> None:
        slot = self._current_slot()
        if slot.page is not None:
            return
        slot.playwright = sync_playwright().start()
        slot.browser = self._launch_browser(slot.playwright)
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
        }
        stored = db_mgr.get_browser_session(PROVIDER)
        if stored:
            context_kwargs["storage_state"] = stored
            logger.debug(
                "load storage_state from sqlite provider=%s worker=%s",
                PROVIDER,
                slot.worker_id,
            )
        slot.context = slot.browser.new_context(**context_kwargs)
        slot.page = slot.context.new_page()
        slot.page.set_default_timeout(self.timeout_ms)
        slot.page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters);
            """
        )
        self._goto(slot.page, CHAT_URL)
        state = self._wait_shell_state(slot.page, timeout_ms=60_000)
        logger.debug(
            "chat page ready worker=%s state=%s url=%s",
            slot.worker_id,
            state,
            slot.page.url,
        )

    def _goto(
        self,
        page: Page,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        attempts: int = 3,
        timeout_ms: int | None = None,
    ) -> None:
        """Navigate with retries for flaky empty responses from chat.qwen.ai."""
        del wait_until  # commit first; empty responses hang less than full DOM.
        timeout = self.timeout_ms if timeout_ms is None else int(timeout_ms)
        soft_timeout = min(20_000, max(8_000, timeout))
        last_exc: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                page.goto(url, wait_until="commit", timeout=soft_timeout)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=soft_timeout)
                except Exception:
                    pass
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "goto stuck attempt=%s/%s url=%s error=%s",
                    attempt,
                    attempts,
                    url,
                    exc,
                )
                if attempt >= attempts:
                    break
                try:
                    page.goto("about:blank", wait_until="commit", timeout=5_000)
                except Exception:
                    pass
                time.sleep(2.0 * attempt)
        assert last_exc is not None
        raise last_exc

    def _stop_unlocked(self) -> None:
        slot = self._current_slot()
        logger.info("browser stop worker=%s", slot.worker_id)
        for closer in (slot.context, slot.browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        if slot.playwright is not None:
            try:
                slot.playwright.stop()
            except Exception:
                pass
        slot.page = None
        slot.context = None
        slot.browser = None
        slot.playwright = None
        slot.last_status = None

    def _ensure_page_unlocked(self) -> Page:
        slot = self._current_slot()
        if slot.page is None:
            self._start_unlocked()
        assert slot.page is not None
        return slot.page

    @staticmethod
    def _has_token_cookie_in(context: BrowserContext | None) -> bool:
        if context is None:
            return False
        try:
            cookies = context.cookies()
        except Exception:
            return False
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "")
            if name == "token" and value and "qwen.ai" in domain:
                return True
        return False

    def _has_token_cookie(self) -> bool:
        return self._has_token_cookie_in(self._current_slot().context)

    def _needs_account_login(self) -> bool:
        return bool(self.auto_login and self.username and self.password)

    def _ensure_logged_in(self, page: Page, *, force: bool = False) -> bool:
        if self._has_token_cookie() and self._detect_shell_state(page) == "chat":
            return True
        state = self._detect_shell_state(page)
        if state == "chat" and not force:
            # Guest chat UI is usable without login cookie.
            return True
        if not self.auto_login:
            logger.warning("auth page but QWEN_AUTO_LOGIN disabled")
            return False
        if not self.username or not self.password:
            logger.warning("auth page but QWEN_USERNAME/QWEN_PASSWORD missing in .env")
            return False
        try:
            self._password_login(page)
        except Exception as exc:
            logger.exception("password login failed: %s", exc)
            return False
        state = self._wait_shell_state(page, timeout_ms=60_000)
        if state == "chat" and self._has_token_cookie():
            self._save_storage_unlocked()
            logger.info(
                "password login ok worker=%s storage saved",
                self._current_slot().worker_id,
            )
            return True
        logger.error(
            "password login finished but state=%s token=%s worker=%s",
            state,
            self._has_token_cookie(),
            self._current_slot().worker_id,
        )
        return False

    def _password_login(self, page: Page) -> None:
        assert self.username and self.password
        logger.info(
            "password login as %s worker=%s",
            self.username,
            self._current_slot().worker_id,
        )
        if "/auth" not in (page.url or "").lower():
            self._goto(page, AUTH_URL)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(0.8)

        page.wait_for_selector(
            ".qwenchat-auth-pc-input-items, input[type='password']",
            timeout=30_000,
        )

        root = page.locator(".qwenchat-auth-pc-input-items")
        if root.count() > 0:
            account = root.first.locator("input:not([type='password'])")
        else:
            account = page.locator(
                "input[type='email'], input[placeholder*='邮箱'], "
                "input[placeholder*='Email'], input[autocomplete='username'], "
                "input:not([type='password'])"
            )
        if account.count() == 0:
            raise RuntimeError("login account input not found")
        account.first.fill(self.username, timeout=15_000)
        page.locator("input[type='password']").first.fill(self.password, timeout=15_000)

        submit = page.locator("button.qwenchat-auth-pc-submit-button")
        if submit.count() == 0:
            submit = page.locator(
                "button:has-text('登录'), button:has-text('Log in'), "
                "button:has-text('Sign in'), button[type='submit']"
            )
        if submit.count() == 0:
            raise RuntimeError("login button not found")
        submit.first.click(timeout=10_000)

        deadline = time.time() + 90
        while time.time() < deadline:
            if self._has_token_cookie() or self._detect_shell_state(page) == "chat":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("login submit did not set token cookie")

        if self._detect_shell_state(page) != "chat":
            self._goto(page, CHAT_URL)
            if self._wait_shell_state(page, timeout_ms=60_000) != "chat":
                raise RuntimeError("login submit did not reach chat UI")

    def _save_storage_unlocked(self) -> None:
        slot = self._current_slot()
        if slot.context is None:
            return
        with self._storage_lock:
            state = slot.context.storage_state()
            db_mgr.save_browser_session(PROVIDER, state)

    def _has_visible(self, page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector)
                count = min(loc.count(), 8)
                for i in range(count):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _detect_shell_state(self, page: Page) -> str:
        if self._has_visible(page, CHAT_READY_SELECTORS):
            return "chat"
        url = (page.url or "").lower()
        if any(token in url for token in ("/auth", "sign_in", "signin", "login")):
            return "auth"
        if self._has_visible(page, SIGN_IN_SELECTORS):
            return "auth"
        return "unknown"

    def _wait_shell_state(self, page: Page, timeout_ms: int = 60_000) -> str:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            state = self._detect_shell_state(page)
            if state != "unknown":
                return state
            time.sleep(0.25)
        return self._detect_shell_state(page)

    def _textarea(self, page: Page):
        for selector in CHAT_READY_SELECTORS:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        raise RuntimeError("message textarea not found")

    def _materialize_images(self, images: list[str]) -> tuple[Path, list[Path]]:
        temp_dir = Path(tempfile.mkdtemp(prefix="qwen_img_"))
        paths: list[Path] = []
        try:
            for idx, src in enumerate(images):
                paths.append(self._materialize_one_image(src, temp_dir, idx))
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        return temp_dir, paths

    def _materialize_one_image(self, src: str, temp_dir: Path, idx: int) -> Path:
        text = (src or "").strip()
        if not text:
            raise ValueError("empty image source")

        local = Path(text)
        if local.is_file():
            raw = local.read_bytes()
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError(f"image too large (max {MAX_IMAGE_BYTES} bytes)")
            suffix = local.suffix or ".png"
            dest = temp_dir / f"img_{idx}{suffix}"
            dest.write_bytes(raw)
            return dest

        match = DATA_URL_RE.match(text)
        if match:
            mime = match.group(1).lower()
            try:
                raw = base64.b64decode(match.group(2), validate=False)
            except Exception as exc:
                raise ValueError("invalid image base64 data URL") from exc
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError(f"image too large (max {MAX_IMAGE_BYTES} bytes)")
            suffix = mimetypes.guess_extension(mime) or ".png"
            if suffix == ".jpe":
                suffix = ".jpg"
            dest = temp_dir / f"img_{idx}{suffix}"
            dest.write_bytes(raw)
            return dest

        lower = text.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            try:
                req = urllib.request.Request(
                    text,
                    headers={"User-Agent": "mock-agent-qwen/1.0"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            except urllib.error.URLError as exc:
                raise ValueError(f"failed to download image: {exc}") from exc
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError(f"image too large (max {MAX_IMAGE_BYTES} bytes)")
            if content_type.startswith("image/"):
                suffix = mimetypes.guess_extension(content_type) or ".png"
            else:
                suffix = Path(urlparse(text).path).suffix or ".png"
            if suffix == ".jpe":
                suffix = ".jpg"
            dest = temp_dir / f"img_{idx}{suffix}"
            dest.write_bytes(raw)
            return dest

        # Raw base64 (optional data-url-less payload).
        try:
            raw = base64.b64decode(text, validate=False)
        except Exception as exc:
            raise ValueError("image must be data URL, http(s) URL, or base64") from exc
        if not raw:
            raise ValueError("empty image base64")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large (max {MAX_IMAGE_BYTES} bytes)")
        dest = temp_dir / f"img_{idx}.png"
        dest.write_bytes(raw)
        return dest

    def _find_file_input(self, page: Page):
        for selector in FILE_INPUT_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    def _attach_images(self, page: Page, paths: list[Path]) -> None:
        if not paths:
            return
        files = [str(p) for p in paths]
        logger.info("attach images count=%s", len(files))

        file_input = self._find_file_input(page)
        if file_input is not None:
            try:
                file_input.set_input_files(files)
                self._wait_attachments_ready(page, expect_count=len(files))
                return
            except Exception:
                logger.exception("set_input_files on existing input failed; try filechooser")

        # Some builds only create the input after clicking an attach control.
        for selector in ATTACH_BUTTON_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() == 0 or not loc.first.is_visible():
                    continue
                with page.expect_file_chooser(timeout=5_000) as fc_info:
                    loc.first.click(timeout=5_000)
                chooser = fc_info.value
                chooser.set_files(files)
                self._wait_attachments_ready(page, expect_count=len(files))
                return
            except Exception:
                continue

        # Last resort: inject a temporary file input (works if page listens globally).
        try:
            page.evaluate(
                """() => {
                    let input = document.getElementById('mock-agent-qwen-file-input');
                    if (!input) {
                        input = document.createElement('input');
                        input.type = 'file';
                        input.accept = 'image/*';
                        input.multiple = true;
                        input.style.display = 'none';
                        input.id = 'mock-agent-qwen-file-input';
                        document.body.appendChild(input);
                    }
                }"""
            )
            locator = page.locator("#mock-agent-qwen-file-input")
            locator.set_input_files(files)
            self._wait_attachments_ready(page, expect_count=len(files))
            return
        except Exception as exc:
            raise RuntimeError(f"failed to attach images on Qwen UI: {exc}") from exc

    def _wait_attachments_ready(self, page: Page, *, expect_count: int) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._has_visible(page, ATTACHMENT_PREVIEW_SELECTORS):
                time.sleep(0.4)
                return
            # Upload may still be in progress even without a clear preview node.
            try:
                for selector in FILE_INPUT_SELECTORS:
                    loc = page.locator(selector)
                    if loc.count() == 0:
                        continue
                    # files property isn't always exposed; just wait a bit after set.
                    break
            except Exception:
                pass
            time.sleep(0.25)
        logger.warning(
            "attachment preview not confirmed expect_count=%s; continue anyway",
            expect_count,
        )
        time.sleep(1.0)

    def _enter_message(self, page: Page, message: str) -> None:
        box = self._textarea(page)
        box.click()
        box.fill(message)

    def _send_message(self, page: Page) -> None:
        for selector in SEND_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() == 0 or not loc.first.is_visible():
                    continue
                disabled = loc.first.get_attribute("disabled")
                aria = loc.first.get_attribute("aria-disabled")
                if disabled is not None or aria == "true":
                    continue
                cls = (loc.first.get_attribute("class") or "").lower()
                if "stop" in cls:
                    continue
                loc.first.click(timeout=5_000)
                return
            except Exception:
                continue
        self._textarea(page).press("Enter")

    def _open_new_chat(self, page: Page) -> None:
        for selector in NEW_CHAT_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    time.sleep(0.8)
                    return
            except Exception:
                continue
        self._goto(page, f"{CHAT_URL.rstrip('/')}/c/new-chat")
        self._wait_shell_state(page, timeout_ms=30_000)

    def _assistant_locator(self, page: Page):
        for selector in ASSISTANT_SELECTORS:
            loc = page.locator(selector)
            if loc.count() > 0:
                return loc
        return page.locator(ASSISTANT_SELECTORS[0])

    def _assistant_count(self, page: Page) -> int:
        try:
            return self._assistant_locator(page).count()
        except Exception:
            return 0

    def _last_assistant_text(self, page: Page) -> str:
        loc = self._assistant_locator(page)
        count = loc.count()
        if count <= 0:
            return ""
        try:
            text = (loc.nth(count - 1).inner_text() or "").strip()
            # Strip thinking status chrome if it leaked into the node.
            lines = [
                line
                for line in text.splitlines()
                if not re.match(
                    r"^(Thought completed|思考完成|Thinking|深度思考).*$",
                    line.strip(),
                    flags=re.IGNORECASE,
                )
            ]
            return "\n".join(lines).strip()
        except Exception:
            return ""

    def _is_generating(self, page: Page) -> bool:
        for selector in STOP_SELECTORS:
            try:
                loc = page.locator(selector)
                count = min(loc.count(), 4)
                for i in range(count):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _has_done_hint(self, page: Page) -> bool:
        return self._has_visible(page, DONE_HINT_SELECTORS)

    def _wait_answer(self, page: Page, *, before_count: int, timeout_s: int) -> str:
        deadline = time.time() + timeout_s
        previous = ""
        stable = 0
        saw_new = False
        need_stable = 3

        while time.time() < deadline:
            count = self._assistant_count(page)
            if count > before_count:
                saw_new = True
            # Only read assistant text after a new bubble appears; otherwise stale
            # text from the previous chat can be returned as a false success.
            text = self._last_assistant_text(page) if saw_new else ""
            generating = self._is_generating(page)
            if generating:
                stable = 0
            elif text and text == previous:
                stable += 1
            else:
                stable = 0
            previous = text
            done_hint = self._has_done_hint(page) if saw_new else False
            if saw_new and text and not generating and (stable >= need_stable or done_hint):
                return text
            time.sleep(1)

        generating = self._is_generating(page)
        if saw_new and previous and not generating:
            logger.warning(
                "answer wait ended with partial text chars=%s timeout_s=%s generating=%s",
                len(previous),
                timeout_s,
                generating,
            )
            return previous
        logger.error(
            "no complete answer within %ss saw_new=%s chars=%s generating=%s",
            timeout_s,
            saw_new,
            len(previous),
            generating,
        )
        raise TimeoutError(
            f"no complete answer within {timeout_s}s "
            f"(saw_new={saw_new}, generating={generating})"
        )


_client: QwenClient | None = None
_client_lock = threading.Lock()


def get_client() -> QwenClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = QwenClient()
        return _client
