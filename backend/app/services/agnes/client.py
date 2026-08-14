"""Playwright client that drives app.agnes-ai.com like a real user."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from app.config import config
from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

PROVIDER = "agnes"
CHAT_URL = "https://app.agnes-ai.com/"
AUTH_URL = "https://app.agnes-ai.com/login"
# Real Agnes chats use ?conversationId=<digits> (not /c/<id>).
CONVERSATION_ID_PATTERNS = (
    re.compile(r"[?&]conversationId=([^&#]+)", re.IGNORECASE),
    re.compile(r"/chat/([^/?#]+)", re.IGNORECASE),
    re.compile(r"/c/([^/?#]+)", re.IGNORECASE),
    re.compile(r"/conversation/([^/?#]+)", re.IGNORECASE),
    re.compile(r"/agent/([^/?#]+)", re.IGNORECASE),
)
RESERVED_CONV_IDS = {"new-chat", "guest", "new", "login"}

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


# mode=auto keeps current model; other values accepted but UI switch is no-op.
MODE_ALIASES = {
    "auto": "auto",
    "default": "auto",
}

CHAT_READY_SELECTORS = [
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'].min-h-\\[48px\\]",
    "[role='textbox'][contenteditable='true']",
    "textarea[placeholder*='分配一个任务']",
    "textarea[placeholder*='任务']",
    "textarea",
]
SIGN_IN_SELECTORS = [
    "#login_email",
    "#login_password",
    "#login",
    "button:has-text('登 录')",
    "a:has-text('登录')",
]
# User rows include justify-end; assistant rows are animate-message-in without it.
ASSISTANT_SELECTORS = [
    "div.animate-message-in.group:not(.justify-end)",
    "div.animate-message-in:not(.justify-end) .prose-agent",
    ".prose-agent",
]
SEND_SELECTORS = [
    "button[title='发送']",
    "button[title='Send']",
    "button:has(.enter-btn):not([disabled])",
    "button[aria-label='Send']",
    "[aria-label*='Send']",
    "button:has-text('发送')",
]
STOP_SELECTORS = [
    "button[title='取消']",
    "button[title='停止']",
    "button[title='Stop']",
    "button:has-text('停止')",
    "button:has-text('Stop')",
    "[aria-label*='Stop']",
    "[aria-label*='停止']",
    "[aria-label*='取消']",
    "[role='status'][aria-live='polite']",
]
NEW_CHAT_SELECTORS = [
    "button:has-text('新任务')",
    "a:has-text('新任务')",
    "button:has-text('新对话')",
    "button:has-text('New Chat')",
    "button:has-text('新建')",
    "a:has-text('新对话')",
    "a:has-text('New Chat')",
    "a:has-text('新建')",
]
DONE_HINT_SELECTORS = [
    "button:has-text('复制')",
    "button:has-text('Copy')",
    "[aria-label*='Copy']",
    "[aria-label*='复制']",
    "button.copy-response-button",
    ".copy-response-button",
]

# Agnes auth cookie / localStorage keys (avoid matching analytics like ttcsid).
_AUTH_COOKIE_NAMES = {"token"}
_AUTH_STORAGE_KEYS = {"token", "userinfo"}


class AgnesClient:
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
            config.agnes_timeout_ms if timeout_ms is None else timeout_ms
        )
        self.browser_channel = config.browser_channel
        self.executable_path = config.chrome_path
        self.username = config.agnes_username
        self.password = config.agnes_password
        self.auto_login = config.agnes_auto_login
        self.worker_count = max(
            1,
            min(4, int(config.agnes_workers if workers is None else workers)),
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
                name=f"agnes-playwright-{slot.worker_id}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        logger.info("agnes: pool workers=%s", self.worker_count)

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
            raise RuntimeError("agnes browser call must run on a worker thread")
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

    def _submit_all(self, func: Callable[[], Any]) -> list[Any]:
        """Run func once on every worker (start / ensure_ready / stop prep)."""
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
            "logged_in": self._has_token_cookie_in(slot.context, slot.page),
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
        self._submit_all(self._start_impl)

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
            logger.exception("agnes worker pool stop failed")
        self._publish_status()
        logger.info("agnes worker pool browsers closed workers=%s", self.worker_count)

    def _stop_impl(self) -> None:
        self._stop_unlocked()

    def status(self) -> dict[str, Any]:
        self._publish_status()
        if self._last_status is not None:
            return dict(self._last_status)
        return self._default_status()

    def ensure_ready(self) -> dict[str, Any]:
        """Start every worker browser and auto-login from .env if needed."""
        results = self._submit_all(self._ensure_ready_impl)
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
        return "auto login failed; check AGNES_USERNAME/AGNES_PASSWORD in .env"

    def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        mode: str = "auto",
        deep_thinking: bool = False,
        search: bool = False,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            raise ValueError("question is empty")

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
            return max(30, config.agnes_think_timeout_s)
        return max(30, config.agnes_chat_timeout_s)

    def _ask_impl(
        self,
        *,
        question: str,
        conversation_id: str | None,
        mode: str,
        deep_thinking: bool,
        search: bool,
        timeout_s: int,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        slot = self._current_slot()
        page = self._ensure_page_unlocked()
        state = self._detect_shell_state(page)
        logger.info(
            "ask start worker=%s state=%s conv=%s mode=%s think=%s search=%s chars=%s timeout_s=%s question=%s",
            slot.worker_id,
            state,
            conversation_id,
            mode,
            deep_thinking,
            search,
            len(question),
            timeout_s,
            question,
        )
        if self._needs_account_login() and not self._has_token_cookie():
            if not self._ensure_logged_in(page, force=True):
                raise RuntimeError(
                    "not logged in; check AGNES_USERNAME/AGNES_PASSWORD in .env "
                    f"(sqlite={db_mgr.path()})"
                )
            state = self._detect_shell_state(page)
        elif state == "auth":
            if not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check AGNES_USERNAME/AGNES_PASSWORD in .env "
                    f"(sqlite={db_mgr.path()})"
                )
            state = self._detect_shell_state(page)

        if state != "chat":
            self._goto(page, CHAT_URL)
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if self._needs_account_login() and not self._has_token_cookie():
                if not self._ensure_logged_in(page, force=True):
                    raise RuntimeError(
                        "not logged in; check AGNES_USERNAME/AGNES_PASSWORD in .env"
                    )
            elif state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check AGNES_USERNAME/AGNES_PASSWORD in .env"
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

        before = self._assistant_count(page)
        self._enter_message(page, question)
        self._send_message(page)
        answer = self._wait_answer(page, before_count=before, timeout_s=timeout_s)
        self._save_storage_unlocked()

        conv_id = self._wait_conversation_id(
            page,
            fallback=conversation_id,
            timeout_s=20,
        )
        if not conv_id:
            raise RuntimeError(
                "conversation_id missing after reply; expected ?conversationId=..."
            )
        if not page.url or "conversationId=" not in page.url:
            # Keep a canonical URL even if the SPA lagged updating location.
            page_url = self._conversation_url(conv_id)
        else:
            page_url = page.url

        title = question.replace("\n", " ").strip()[:40] or None
        db_mgr.upsert_conversation(
            conversation_id=conv_id,
            provider=PROVIDER,
            title=title if not conversation_id else None,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            url=page_url,
        )
        db_mgr.add_conversation_messages(
            conv_id,
            [("user", question), ("assistant", answer)],
        )

        logger.info(
            "ask done worker=%s answer_chars=%s conv=%s elapsed=%.1fs answer=%s",
            slot.worker_id,
            len(answer),
            conv_id,
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
            "url": page_url,
            "worker_id": slot.worker_id,
        }

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        raw = (mode or "auto").strip()
        key = raw.lower()
        if key in MODE_ALIASES:
            return MODE_ALIASES[key]
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
        # Agnes UI may not expose model / think / search toggles; keep stubs
        # so ask() API stays compatible with qwen / chat_jobs.
        actual_mode = mode
        if switch_mode and mode and mode != "auto":
            actual_mode = self._select_model(page, mode) or mode
        self._set_thinking_mode(page, enabled=deep_thinking)
        self._set_search_mode(page, enabled=search)
        return actual_mode

    def _select_model(self, page: Page, mode: str) -> str | None:
        _ = page
        logger.info("agnes model switch no-op; keep mode=%s", mode)
        return mode

    def _set_thinking_mode(self, page: Page, *, enabled: bool) -> None:
        _ = page
        logger.debug("agnes thinking toggle no-op enabled=%s", enabled)

    def _set_search_mode(self, page: Page, *, enabled: bool) -> None:
        _ = page
        logger.debug("agnes search toggle no-op enabled=%s", enabled)

    def _conversation_id_from_url(self, url: str) -> str | None:
        raw = url or ""
        for pattern in CONVERSATION_ID_PATTERNS:
            match = pattern.search(raw)
            if not match:
                continue
            conv = match.group(1).strip()
            try:
                conv = unquote(conv)
            except Exception:
                pass
            if not conv or conv.lower() in RESERVED_CONV_IDS:
                continue
            return conv
        return None

    def _conversation_url(self, conversation_id: str) -> str:
        return f"{CHAT_URL.rstrip('/')}/?conversationId={conversation_id}"

    def _wait_conversation_id(
        self,
        page: Page,
        *,
        fallback: str | None = None,
        timeout_s: float = 30,
    ) -> str | None:
        deadline = time.time() + max(1.0, float(timeout_s))
        while time.time() < deadline:
            conv = self._conversation_id_from_url(page.url)
            if conv:
                return conv
            time.sleep(0.25)
        return self._conversation_id_from_url(page.url) or fallback

    def _open_conversation(self, page: Page, conversation_id: str) -> None:
        conv = conversation_id.strip()
        parsed = self._conversation_id_from_url(conv)
        if parsed:
            conv = parsed
        # Also accept bare id or full URL containing conversationId=.
        if "conversationId=" in conv:
            parsed = self._conversation_id_from_url(
                conv if "://" in conv else f"{CHAT_URL}?{conv}"
            )
            if parsed:
                conv = parsed
        if not conv or conv.lower() in RESERVED_CONV_IDS:
            raise ValueError(f"invalid conversation_id: {conversation_id}")

        record = db_mgr.get_conversation(conv)
        stored_url = (record or {}).get("url")
        target = str(stored_url) if stored_url else self._conversation_url(conv)
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
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-notifications",
            "--lang=zh-CN",
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
        """Navigate with retries when the SPA hangs before domcontentloaded.

        Agnes under headless sometimes never reaches a clean domcontentloaded
        within the full UI timeout; use a short soft wait, then fall back to
        commit so callers can proceed to shell/login checks.
        """
        timeout = self.timeout_ms if timeout_ms is None else int(timeout_ms)
        soft_timeout = min(30_000, max(5_000, timeout))
        last_exc: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                page.goto(url, wait_until=wait_until, timeout=soft_timeout)
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
                try:
                    page.goto(url, wait_until="commit", timeout=soft_timeout)
                    return
                except Exception as exc2:
                    last_exc = exc2
                    if attempt >= attempts:
                        break
                    time.sleep(1.0)
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
    def _cookie_looks_authed(cookie: dict[str, Any]) -> bool:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").lower()
        if name not in _AUTH_COOKIE_NAMES or not value:
            return False
        return "agnes-ai.com" in domain

    def _local_storage_looks_authed(self, page: Page | None) -> bool:
        if page is None:
            return False
        try:
            keys = page.evaluate("() => Object.keys(window.localStorage || {})")
        except Exception:
            return False
        if not isinstance(keys, list):
            return False
        return any(str(key or "") in _AUTH_STORAGE_KEYS for key in keys)

    def _ui_looks_logged_in(self, page: Page | None) -> bool:
        if page is None:
            return False
        url = (page.url or "").lower()
        if "/login" in url:
            return False
        if not self._has_visible(page, CHAT_READY_SELECTORS):
            return False
        # Header "登录" link or oauth popup => guest.
        try:
            login_btn = page.locator(
                "a:has-text('登录'), button:has-text('登录'), "
                "button.oauthButtons_loginBtn__eWwPG, [class*='oauthButtons_loginBtn']"
            )
            count = min(login_btn.count(), 6)
            for i in range(count):
                if login_btn.nth(i).is_visible():
                    return False
        except Exception:
            pass
        return True

    def _has_token_cookie_in(
        self,
        context: BrowserContext | None,
        page: Page | None = None,
    ) -> bool:
        if context is not None:
            try:
                cookies = context.cookies()
            except Exception:
                cookies = []
            for cookie in cookies:
                if self._cookie_looks_authed(cookie):
                    return True
        return self._local_storage_looks_authed(page)

    def _has_token_cookie(self) -> bool:
        slot = self._current_slot()
        return self._has_token_cookie_in(slot.context, slot.page)

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
            logger.warning("auth page but AGNES_AUTO_LOGIN disabled")
            return False
        if not self.username or not self.password:
            logger.warning("auth page but AGNES_USERNAME/AGNES_PASSWORD missing in .env")
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
        if "/login" not in (page.url or "").lower():
            self._goto(page, AUTH_URL)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(0.8)

        # Prefer email tab when phone/other tabs are present.
        email_tab = page.locator(
            "button:has-text('邮箱'), [role='tab']:has-text('邮箱')"
        )
        if email_tab.count() > 0:
            try:
                email_tab.first.click(timeout=5_000)
                time.sleep(0.3)
            except Exception as exc:
                logger.warning("click email tab failed: %s", exc)

        page.wait_for_selector(
            "#login_email, #login_password, input[type='password']",
            timeout=30_000,
        )

        email = page.locator("#login_email")
        if email.count() == 0:
            email = page.locator(
                "input[name='login_email'], input[type='email'], "
                "input[placeholder*='邮箱'], input[placeholder*='Email']"
            )
        if email.count() == 0:
            raise RuntimeError("login email input not found")
        email.first.fill(self.username, timeout=15_000)

        password = page.locator("#login_password")
        if password.count() == 0:
            password = page.locator("input[name='login_password'], input[type='password']")
        if password.count() == 0:
            raise RuntimeError("login password input not found")
        password.first.fill(self.password, timeout=15_000)

        submit = page.locator("button#login")
        if submit.count() == 0:
            submit = page.locator(
                "button:has-text('登 录'), button:has-text('登录'), "
                "button[type='submit']"
            )
        if submit.count() == 0:
            raise RuntimeError("login button not found")
        submit.first.click(timeout=10_000)

        deadline = time.time() + 90
        while time.time() < deadline:
            left_login = "/login" not in (page.url or "").lower()
            if self._has_token_cookie() or (
                left_login and self._detect_shell_state(page) == "chat"
            ):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("login submit did not leave login / set auth")

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
        url = (page.url or "").lower()
        if any(token in url for token in ("/login", "/auth", "sign_in", "signin")):
            if self._has_visible(page, SIGN_IN_SELECTORS):
                return "auth"
            # Still on login URL even without form yet.
            return "auth"
        if self._has_visible(page, CHAT_READY_SELECTORS):
            return "chat"
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

    def _enter_message(self, page: Page, message: str) -> None:
        box = self._textarea(page)
        box.click()
        tag = ""
        try:
            tag = (box.evaluate("el => el.tagName") or "").upper()
        except Exception:
            tag = ""
        if tag == "TEXTAREA" or tag == "INPUT":
            box.fill(message)
            return
        # Logged-in Agnes composer is a contenteditable div.
        try:
            box.fill(message)
            return
        except Exception:
            pass
        page.keyboard.press("Meta+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(message, delay=10)

    def _send_message(self, page: Page) -> None:
        # Composer fill may take a beat before Agnes enables the send button.
        deadline = time.time() + 5
        while time.time() < deadline:
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
                    title = (loc.first.get_attribute("title") or "").lower()
                    if "stop" in cls or title in {"取消", "停止", "stop"}:
                        continue
                    loc.first.click(timeout=5_000)
                    return
                except Exception:
                    continue
            time.sleep(0.2)
        # Last resort: Enter on some builds submits; contenteditable often needs Ctrl/Meta+Enter.
        box = self._textarea(page)
        try:
            box.press("Control+Enter")
            return
        except Exception:
            pass
        try:
            box.press("Meta+Enter")
            return
        except Exception:
            pass
        box.press("Enter")

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
        self._goto(page, CHAT_URL)
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
        # Prefer the last assistant message row; answer lives in .prose-agent
        # (reasoning is .prose-reasoning / "Thought").
        rows = page.locator("div.animate-message-in.group:not(.justify-end)")
        try:
            count = rows.count()
        except Exception:
            count = 0
        if count > 0:
            try:
                row = rows.nth(count - 1)
                agents = row.locator(".prose-agent")
                agent_n = agents.count()
                if agent_n > 0:
                    text = (agents.nth(agent_n - 1).inner_text() or "").strip()
                    if text:
                        return text
                text = (row.inner_text() or "").strip()
                return self._strip_thought_text(text)
            except Exception:
                pass
        loc = self._assistant_locator(page)
        try:
            count = loc.count()
            if count <= 0:
                return ""
            text = (loc.nth(count - 1).inner_text() or "").strip()
            return self._strip_thought_text(text)
        except Exception:
            return ""

    @staticmethod
    def _strip_thought_text(text: str) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        out: list[str] = []
        skipping = False
        for line in lines:
            stripped = line.strip()
            if re.match(
                r"^(Thought completed|Thought|思考完成|Thinking|深度思考)\b.*$",
                stripped,
                flags=re.IGNORECASE,
            ):
                skipping = True
                continue
            if skipping and not stripped:
                skipping = False
                continue
            if skipping:
                # Drop reasoning block until a blank line separates answer.
                continue
            out.append(line)
        cleaned = "\n".join(out).strip()
        return cleaned or text.strip()

    def _is_generating(self, page: Page) -> bool:
        for selector in STOP_SELECTORS:
            try:
                loc = page.locator(selector)
                count = min(loc.count(), 6)
                for i in range(count):
                    node = loc.nth(i)
                    if not node.is_visible():
                        continue
                    # Loading status may stay in DOM empty; require content or cancel btn.
                    if selector.startswith("[role='status']"):
                        html = (node.inner_html() or "").lower()
                        if "load" in html or "spin" in html or "anim" in html:
                            return True
                        continue
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


_client: AgnesClient | None = None
_client_lock = threading.Lock()


def get_client() -> AgnesClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AgnesClient()
        return _client
