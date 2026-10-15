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
# Seconds to wait for proof that a sent message actually started a reply.
SEND_CONFIRM_S = 20
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


# Live model list from GET https://chat.qwen.ai/api/models:
#   qwen3.7-plus (Qwen3.7-Plus), qwen3.8-max (Qwen3.8-Max),
#   qwen3.8-omni-flash (Qwen3.8-Omni-Flash)
# Legacy names are kept mapped onto live models so old callers still work.
MODEL_ALIASES = {
    "plus": "Qwen3.7-Plus",
    "qwen3.7-plus": "Qwen3.7-Plus",
    "max": "Qwen3.8-Max",
    "qwen3.8-max": "Qwen3.8-Max",
    "omni": "Qwen3.8-Omni-Flash",
    "flash": "Qwen3.8-Omni-Flash",
    "qwen3.8-omni-flash": "Qwen3.8-Omni-Flash",
    "qwen3.5-plus": "Qwen3.7-Plus",
    "qwen3.6-plus": "Qwen3.7-Plus",
    "qwen3.5-flash": "Qwen3.8-Omni-Flash",
    "qwen3-max": "Qwen3.8-Max",
}
# Qwen thinking selector options are 自动 / 思考 / 快速 only; there is no
# "expert"/"instant" mode on this frontend, so those names drive thinking.
THINKING_ALIASES = {
    "fast": "fast",
    "instant": "fast",
    "quick": "fast",
    "快速": "fast",
    "快速模式": "fast",
    "think": "think",
    "thinking": "think",
    "expert": "think",
    "专家": "think",
    "专家模式": "think",
    "思考": "think",
}
MODE_ALIASES = {
    "auto": "auto",
    "default": "auto",
    **MODEL_ALIASES,
    **THINKING_ALIASES,
}
THINKING_LABELS = {"fast": "快速", "think": "思考"}

# Qwen model picker (verified 2026-10): trigger div.wms-trigger,
# options div.wms-list__item, current label div.wms-trigger__text.
MODEL_TRIGGER_SELECTORS = [
    ".wms-trigger",
    "[class*='wms-trigger']",
    "div[class*='model-selector']",
]
# Qwen thinking picker: div.qwen-thinking-selector + dropdown items.
THINKING_TRIGGER_SELECTORS = [
    "div.qwen-thinking-selector",
    "[class*='qwen-thinking-selector']",
]
THINKING_OPTION_SELECTORS = [
    "span.qwen-chat-v2-dropdown-menu-item-content",
    "span.qwen-chat-v2-dropdown-menu-item-label",
    "div.qwen-chat-v2-dropdown-menu-item",
]

CHAT_READY_SELECTORS = [
    "textarea.message-input-textarea",
    "textarea[placeholder*='Ask']",
    "textarea[placeholder*='输入']",
    "textarea[placeholder*='Message']",
    "textarea",
]
# The SPA ships a full-screen #splash-screen overlay (z-index 9999) that is
# not removed until hydration finishes; the textarea is already "visible"
# underneath it, which used to be mistaken for a ready chat shell.
SPLASH_SELECTORS = ["#splash-screen"]
# Guest visitors get an Ant Design welcome modal that swallows the send click.
BLOCKER_SELECTORS = [
    ".ant-modal-mask",
    ".ant-modal-wrap",
    "[class*='welcome-modal']",
]
BLOCKER_CLOSE_SELECTORS = [
    ".ant-modal-close",
    "button.ant-modal-close",
    "[aria-label='Close']",
]
SIGN_IN_SELECTORS = [
    # The guest auth page defaults to 验证码登录; the password form (and its
    # inputs) only exist after clicking 使用密码登录.
    "text=使用密码登录",
    "text=使用验证码登录",
    "input[placeholder*='电子邮箱']",
    "input[placeholder*='邮箱']",
    ".qwenchat-page-auth",
    "input[type='password']",
    ".qwenchat-auth-pc-input-items",
    "button.qwenchat-auth-pc-submit-button",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
]
ASSISTANT_SELECTORS = [
    ".qwen-chat-message-assistant",
    ".chat-response-message",
]
# The answer text lives in .response-message-content; the thinking status card
# (已经完成思考) and the multi-candidate feedback widget are siblings, so reading
# the whole message node would leak chrome into the answer.
ANSWER_TEXT_SELECTORS = [
    ".response-message-content.phase-answer",
    ".response-message-content",
    ".qwen-chat-message-assistant .qwen-markdown",
]
THINKING_CHROME_RE = re.compile(
    r"^(已经完成思考|思考完成|已完成思考|正在思考|深度思考|思考中|"
    r"Thought completed|Thinking|Thought about.*|Thought for.*)[.。…\s]*$",
    flags=re.IGNORECASE,
)
# Qwen sometimes answers with two candidates and asks which one to keep; that
# widget must never be returned as the answer.
FEEDBACK_NOISE_RE = re.compile(
    r"(此反馈将帮助我们评估并提升\s*Qwen\s*的体验。?|"
    r"你更喜欢哪个回复？请选择一个以继续。?|"
    r"我更喜欢这个回复|"
    r"Which response do you prefer\??|I prefer this response)",
    flags=re.IGNORECASE,
)
# Citation "show more" chip renders as a bare "+18" line under the answer.
CITATION_COUNTER_RE = re.compile(r"^\+\d{1,3}$")
# Send button only exists once the composer has text; the stop button
# (button.stop-button, aria-label 停止) shows up as soon as a reply starts.
SEND_SELECTORS = [
    "div.message-input-right-button-send",
    "div.chat-prompt-send-button button",
    "button:has-text('发送')",
    "button:has-text('Send')",
    "[aria-label*='Send']",
    "[aria-label*='发送']",
]
STOP_SELECTORS = [
    "button.stop-button",
    "[class*='stop-button']",
    "[aria-label*='Stop']",
    "[aria-label*='停止']",
    "button:has-text('停止')",
    "button:has-text('Stop')",
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
            "logged_in": self._has_token_cookie_in(slot.context)
            and not self._ui_shows_guest(slot.page),
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
        if state == "loading":
            # #splash-screen covers the composer right after navigation; the
            # textarea is already "visible" underneath it, so wait it out
            # instead of reloading or reporting a false ready.
            state = self._wait_shell_state(page, timeout_ms=60_000)
        # Qwen now requires a login to send; a guest shell shows the textarea
        # plus a welcome modal but silently drops every message.
        if self._needs_account_login() and not self._logged_in(page):
            if not self._ensure_logged_in(page, force=True):
                raise RuntimeError(self._login_failed_message())
        elif state == "auth":
            if not self._ensure_logged_in(page):
                raise RuntimeError(self._login_failed_message())
        elif state != "chat":
            self._goto(page, CHAT_URL)
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if self._needs_account_login() and not self._logged_in(page):
                if not self._ensure_logged_in(page, force=True):
                    raise RuntimeError(self._login_failed_message())
            elif state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(self._login_failed_message())
        self._dismiss_blocker(page)
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
            "logged_in": self._logged_in(page),
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
        # "think"/"fast" (and the expert/instant aliases behind them) are
        # thinking-selector choices, so they must also drive the timeout:
        # a deep-thinking reply needs the longer budget, not the chat one.
        thinking = THINKING_ALIASES.get(resolved_mode)
        if thinking == "think":
            deep_thinking = True
        elif thinking == "fast":
            deep_thinking = False
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
        if state == "loading":
            self._wait_shell_state(page, timeout_ms=60_000)
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
        if self._needs_account_login() and not self._logged_in(page):
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
            if self._needs_account_login() and not self._logged_in(page):
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
        self._dismiss_blocker(page)

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
            try:
                self._confirm_sent(page, before_count=before)
            except RuntimeError:
                # A session can go stale while the page sits idle: the shell
                # keeps rendering (guest mode) and the composer silently drops
                # the message. Re-login once and resend before giving up.
                if not self._recover_stale_send(page):
                    raise
                before = self._assistant_count(page)
                self._enter_message(page, question)
                self._send_message(page)
                self._confirm_sent(page, before_count=before)
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
        """Return "auto", "think"/"fast", or a concrete page model label."""
        raw = (mode or "auto").strip()
        key = raw.lower()
        if key in MODE_ALIASES:
            return MODE_ALIASES[key]
        # Allow passing a concrete model label from the page, e.g. "Qwen3.7-Plus".
        if raw and key != "auto":
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
        # "think"/"fast" are thinking-selector choices, not model names.
        thinking = THINKING_ALIASES.get(mode)
        if thinking == "think":
            deep_thinking = True
        elif thinking == "fast":
            deep_thinking = False
        if switch_mode and mode and mode not in ("auto", "think", "fast"):
            actual_mode = self._select_model(page, mode) or mode
        self._set_thinking_mode(page, deep_thinking=deep_thinking, mode=mode)
        self._set_search_mode(page, enabled=search)
        return actual_mode

    def _select_model(self, page: Page, model: str) -> str | None:
        """Pick a model in the .wms-trigger dropdown; return the label chosen."""
        trigger = None
        for selector in MODEL_TRIGGER_SELECTORS:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                trigger = loc.first
                break
        if trigger is None:
            logger.warning("model selector not found; keep mode=%s", model)
            return None

        try:
            current = (
                page.locator(".wms-trigger__text").first.inner_text(timeout=2_000) or ""
            ).strip()
        except Exception:
            current = ""
        if current and current.lower() == model.lower():
            logger.info("model already=%s", current)
            return current

        try:
            trigger.click(timeout=8_000)
            time.sleep(0.6)
        except Exception as exc:
            logger.warning("open model selector failed: %s", exc)
            return None

        try:
            option = page.locator("div.wms-list__item").filter(has_text=model)
            if option.count() == 0:
                option = page.locator(
                    f"[role='option']:has-text('{model}'), "
                    f"li:has-text('{model}'), "
                    f"div[class*='wms-list__item']:has-text('{model}')"
                )
            if option.count() == 0:
                logger.warning("model option not found: %s", model)
                return None
            option.first.click(timeout=8_000)
            time.sleep(0.5)
            chosen = ""
            try:
                chosen = (
                    page.locator(".wms-trigger__text").first.inner_text(timeout=2_000)
                    or ""
                ).strip()
            except Exception:
                chosen = ""
            logger.info("model set to %s (selector=%s)", model, chosen or "?")
            return chosen or model
        except Exception as exc:
            logger.warning("select model %s failed: %s", model, exc)
            return None
        finally:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    def _set_thinking_mode(
        self,
        page: Page,
        *,
        deep_thinking: bool,
        mode: str = "auto",
    ) -> None:
        """Qwen thinking control: 自动 / 思考 / 快速.

        Default is 自动; only switch when the caller asked for something
        explicit (mode=think/fast) or wants deep thinking.
        """
        explicit = THINKING_ALIASES.get(mode)
        if not deep_thinking and explicit != "fast":
            # Keep the page's 自动 default instead of forcing 快速.
            return
        wanted_label = THINKING_LABELS["think" if deep_thinking else "fast"]

        trigger = None
        for selector in THINKING_TRIGGER_SELECTORS:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                trigger = loc.first
                break
        if trigger is None:
            logger.warning("thinking control not found wanted=%s", wanted_label)
            return

        try:
            current = (trigger.inner_text(timeout=2_000) or "").strip()
        except Exception:
            current = ""
        if wanted_label in current:
            logger.info("thinking already=%s", wanted_label)
            return

        try:
            trigger.click(timeout=5_000)
            time.sleep(0.5)
        except Exception as exc:
            logger.warning("open thinking dropdown failed: %s", exc)
            return

        try:
            for selector in THINKING_OPTION_SELECTORS:
                opt = page.locator(selector).filter(has_text=wanted_label)
                if opt.count() == 0:
                    continue
                opt.first.click(timeout=5_000)
                time.sleep(0.4)
                logger.info("thinking set to %s", wanted_label)
                return
            logger.warning("thinking option not found wanted=%s", wanted_label)
        except Exception as exc:
            logger.warning("select thinking %s failed: %s", wanted_label, exc)
        finally:
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
        """True when the context carries a Qwen credential.

        The current frontend stores the JWT in localStorage and only sets a
        `refresh_token` cookie, so a cookie literally named `token` is no
        longer required (and is absent after a fresh login).
        """
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
            if not value or "qwen.ai" not in domain:
                continue
            if name in ("token", "refresh_token"):
                return True
        return False

    @staticmethod
    def _has_local_storage_token(page: Page | None) -> bool:
        if page is None:
            return False
        try:
            return bool(page.evaluate("() => !!localStorage.getItem('token')"))
        except Exception:
            return False

    def _has_token_cookie(self) -> bool:
        slot = self._current_slot()
        return self._has_token_cookie_in(slot.context) or self._has_local_storage_token(
            slot.page
        )

    def _needs_account_login(self) -> bool:
        return bool(self.auto_login and self.username and self.password)

    def _logged_in(self, page: Page) -> bool:
        """True when the page is a usable, authenticated chat shell."""
        return self._has_token_cookie() and not self._ui_shows_guest(page)

    @staticmethod
    def _ui_shows_guest(page: Page) -> bool:
        """Qwen shows 登录/注册 in the header for guests, even with a stale cookie."""
        try:
            return bool(
                page.evaluate(
                    """() => {
                        const btns = Array.from(document.querySelectorAll('button'))
                            .filter(b => b.offsetWidth || b.offsetHeight);
                        return btns.some(b => /^(登录|注册|Log in|Sign in)$/.test(
                            (b.innerText || '').trim()));
                    }"""
                )
            )
        except Exception:
            return False

    def _ensure_logged_in(self, page: Page, *, force: bool = False) -> bool:
        if self._logged_in(page):
            return True
        state = self._detect_shell_state(page)
        if state == "loading":
            self._wait_shell_state(page, timeout_ms=30_000)
            if self._logged_in(page):
                return True
            state = self._detect_shell_state(page)
        if state == "chat" and not force and not self._ui_shows_guest(page):
            # Guest chat UI is usable without login cookie.
            return True
        if not self.auto_login:
            logger.warning("auth required but QWEN_AUTO_LOGIN disabled")
            return False
        if not self.username or not self.password:
            logger.warning("auth required but QWEN_USERNAME/QWEN_PASSWORD missing in .env")
            return False
        try:
            self._password_login(page)
        except Exception as exc:
            logger.exception("password login failed: %s", exc)
            return False
        if self._wait_shell_state(page, timeout_ms=60_000) != "chat":
            logger.error(
                "password login finished but state=%s worker=%s",
                self._detect_shell_state(page),
                self._current_slot().worker_id,
            )
            return False
        self._dismiss_blocker(page)
        self._save_storage_unlocked()
        logger.info(
            "password login ok worker=%s storage saved",
            self._current_slot().worker_id,
        )
        return True

    def _open_password_tab(self, page: Page) -> None:
        """The auth page defaults to 验证码登录; switch to the password form."""
        if page.locator("input[type='password']").count() > 0:
            return
        for selector in ("text=使用密码登录", "text=密码登录", "a:has-text('密码登录')"):
            try:
                loc = page.locator(selector)
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=10_000)
                time.sleep(1.0)
                if page.locator("input[type='password']").count() > 0:
                    return
            except Exception:
                continue
        raise RuntimeError("password login tab not found")

    def _password_login(self, page: Page) -> None:
        assert self.username and self.password
        logger.info(
            "password login as %s worker=%s",
            self.username,
            self._current_slot().worker_id,
        )
        if "/auth" not in (page.url or "").lower():
            self._goto(page, AUTH_URL)
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        time.sleep(1.0)
        self._wait_no_blocker(page, timeout_ms=20_000)

        page.wait_for_selector("input[type='text'], input[type='email']", timeout=30_000)
        self._open_password_tab(page)

        account = page.locator(
            "input[placeholder*='邮箱'], input[placeholder*='邮件'], "
            "input[placeholder*='Email'], input[type='email'], "
            "input[autocomplete='username'], input[type='text']"
        )
        if account.count() == 0:
            raise RuntimeError("login account input not found")
        account.first.fill(self.username, timeout=15_000)
        password_input = page.locator("input[type='password']")
        password_input.first.fill(self.password, timeout=15_000)

        # The submit button is the black 登录 button (class
        # qwenchat-auth-pc-submit-button); exclude 使用验证码登录 / 使用 X 登录.
        submit = page.locator("button.qwenchat-auth-pc-submit-button")
        if submit.count() == 0:
            submit = page.locator("button").filter(has_text=re.compile(r"^\s*登录\s*$"))
        if submit.count() == 0:
            submit = page.locator(
                "button:has-text('Log in'), button:has-text('Sign in'), "
                "button[type='submit']"
            )
        if submit.count() == 0:
            raise RuntimeError("login button not found")
        submit.first.click(timeout=10_000)

        deadline = time.time() + 90
        while time.time() < deadline:
            state = self._detect_shell_state(page)
            if state == "chat":
                break
            if self._has_token_cookie() and not self._ui_shows_guest(page):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("login submit did not reach the chat UI")

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

    def _has_blocker(self, page: Page) -> bool:
        """True while #splash-screen or a modal overlays the composer."""
        for selector in SPLASH_SELECTORS + BLOCKER_SELECTORS:
            try:
                loc = page.locator(selector)
                count = min(loc.count(), 4)
                for i in range(count):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _dismiss_blocker(self, page: Page) -> bool:
        """Close the guest welcome modal; return True when something was closed."""
        closed = False
        for selector in BLOCKER_CLOSE_SELECTORS:
            try:
                loc = page.locator(selector)
                if loc.count() == 0 or not loc.first.is_visible():
                    continue
                loc.first.click(timeout=3_000)
                closed = True
                time.sleep(0.5)
                break
            except Exception:
                continue
        if closed:
            logger.info("dismissed blocking modal worker=%s", self._current_slot().worker_id)
        return closed

    def _wait_no_blocker(self, page: Page, timeout_ms: int = 30_000) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if not self._has_blocker(page):
                return True
            self._dismiss_blocker(page)
            time.sleep(0.25)
        return not self._has_blocker(page)

    def _detect_shell_state(self, page: Page) -> str:
        # The chat textarea renders under #splash-screen before hydration, so a
        # visible textarea alone is not proof that the shell is usable.
        if self._has_blocker(page):
            return "loading"
        if self._has_visible(page, CHAT_READY_SELECTORS):
            return "chat"
        url = (page.url or "").lower()
        if any(token in url for token in ("/auth", "sign_in", "signin", "login")):
            return "auth"
        if self._has_visible(page, SIGN_IN_SELECTORS):
            return "auth"
        return "unknown"

    def _wait_shell_state(self, page: Page, timeout_ms: int = 60_000) -> str:
        """Wait for a settled state; 'loading' is transient, not settled."""
        deadline = time.time() + timeout_ms / 1000
        last = "unknown"
        while time.time() < deadline:
            state = self._detect_shell_state(page)
            if state == "loading":
                self._dismiss_blocker(page)
            elif state != "unknown":
                return state
            last = state
            time.sleep(0.25)
        return last if last != "unknown" else self._detect_shell_state(page)

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

    def _confirm_sent(self, page: Page, *, before_count: int) -> None:
        """Fail fast when the click did not actually start a reply.

        A blocked composer (guest welcome modal, splash overlay) accepts the
        click but drops the message, which used to burn the whole timeout.
        """
        deadline = time.time() + SEND_CONFIRM_S
        original_url = page.url
        while time.time() < deadline:
            if self._assistant_count(page) > before_count:
                return
            if self._conversation_id_from_url(page.url) != self._conversation_id_from_url(
                original_url
            ):
                return
            if self._is_generating(page):
                return
            if not self._textarea_value(page):
                return
            time.sleep(0.25)

        if self._has_blocker(page):
            self._dismiss_blocker(page)
            raise RuntimeError(
                "message not sent: a modal/overlay blocks the composer "
                "(not logged in?)"
            )
        raise RuntimeError("message not sent: no reply started and the input kept its text")

    def _recover_stale_send(self, page: Page) -> bool:
        """Re-login when the send failed because the session went stale.

        Returns True when a fresh login is in place and the caller should
        retry. Any other failure mode returns False so the original error
        (which is more informative) propagates.
        """
        self._dismiss_blocker(page)
        if not self._needs_account_login():
            logger.warning("send did not start: no reply and no auto-login available")
            return False
        try:
            self._goto(page, CHAT_URL)
            self._wait_shell_state(page, timeout_ms=30_000)
        except Exception:
            pass
        if self._logged_in(page):
            # Genuinely logged in: the send failed for another reason.
            logger.warning("send did not start although logged_in=true; not re-logging in")
            return False
        logger.warning(
            "send did not start and session looks stale; re-login worker=%s",
            self._current_slot().worker_id,
        )
        try:
            if not self._ensure_logged_in(page, force=True):
                return False
        except Exception as exc:
            logger.exception("re-login after failed send errored: %s", exc)
            return False
        self._dismiss_blocker(page)
        return self._detect_shell_state(page) == "chat"

    def _textarea_value(self, page: Page) -> str:
        try:
            return (self._textarea(page).input_value(timeout=2_000) or "").strip()
        except Exception:
            return ""

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

    def _last_assistant_node(self, page: Page):
        """Locator for the newest assistant message, or None."""
        try:
            loc = self._assistant_locator(page)
            count = loc.count()
        except Exception:
            return None
        if count <= 0:
            return None
        return loc.nth(count - 1)

    def _answer_text(self, page: Page) -> str:
        """Read only the answer container of the newest assistant message.

        Reading the whole message node used to capture the thinking status card
        (已经完成思考) and, when Qwen offers several candidates, the
        "你更喜欢哪个回复" feedback widget instead of the answer.
        """
        node = self._last_assistant_node(page)
        if node is None:
            return ""
        for selector in ANSWER_TEXT_SELECTORS:
            try:
                loc = node.locator(selector)
                count = loc.count()
            except Exception:
                continue
            if count <= 0:
                continue
            for index in range(count - 1, -1, -1):
                try:
                    text = (loc.nth(index).inner_text() or "").strip()
                except Exception:
                    continue
                text = self._strip_thinking_chrome(text)
                if text:
                    return text
        # Last resort: some render paths have no .response-message-content; read
        # the whole node and strip the chrome by hand.
        try:
            return self._strip_thinking_chrome((node.inner_text() or "").strip())
        except Exception:
            return ""

    @staticmethod
    def _strip_thinking_chrome(text: str) -> str:
        """Drop leftover thinking-card labels and the candidate feedback widget."""
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            if THINKING_CHROME_RE.match(stripped):
                continue
            if FEEDBACK_NOISE_RE.search(stripped):
                continue
            if CITATION_COUNTER_RE.match(stripped):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        # The feedback widget can also be inlined without newlines.
        cleaned = FEEDBACK_NOISE_RE.sub("", cleaned).strip()
        return cleaned

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
            text = self._answer_text(page) if saw_new else ""
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
