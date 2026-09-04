"""Playwright client that drives chat.deepseek.com like a real user."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from app.config import config
from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

PROVIDER = "deepseek"
CHAT_URL = "https://chat.deepseek.com/"
CONVERSATION_ID_RE = re.compile(
    r"/a/chat/s/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
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
    last_status: dict[str, Any] | None = None

MODE_ALIASES = {
    "fast": "instant",
    "instant": "instant",
    "quick": "instant",
    "快速": "instant",
    "快速模式": "instant",
    "expert": "expert",
    "专家": "expert",
    "专家模式": "expert",
}

CHAT_READY_SELECTORS = [
    "textarea[placeholder='Message DeepSeek']",
    "textarea[placeholder*='DeepSeek']",
    "textarea[placeholder*='发送']",
    "textarea",
]
SIGN_IN_SELECTORS = [
    ".ds-sign-in-form__main",
    ".ds-sign-in-form-wrapper",
    ".ds-auth-form-wrapper",
    ".ds-sign-up-form__main",
    "input[type='password']",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
    "button:has-text('登录')",
]
ASSISTANT_SELECTOR = "div.ds-message:has(.ds-markdown)"
COLLECT_MESSAGES_JS = """() => {
    const nodes = Array.from(document.querySelectorAll('.ds-message'));
    return nodes.map((node) => {
        const isAssistant = !!node.querySelector('.ds-markdown');
        const content = (node.innerText || '').trim();
        if (!content) return null;
        return {
            role: isAssistant ? 'assistant' : 'user',
            content,
        };
    }).filter(Boolean);
}"""
SCROLL_MESSAGE_CONTAINER_JS = """(delta) => {
    let best = null;
    let bestOverflow = 0;
    for (const el of document.querySelectorAll('div')) {
        const overflow = el.scrollHeight - el.clientHeight;
        if (overflow > bestOverflow) {
            bestOverflow = overflow;
            best = el;
        }
    }
    if (!best) return { ok: false, at_bottom: true };
    if (delta === 0) {
        best.scrollTop = 0;
    } else {
        best.scrollTop += delta;
    }
    const atBottom = best.scrollTop + best.clientHeight >= best.scrollHeight - 4;
    return { ok: true, at_bottom: atBottom };
}"""
COMPOSER_BUTTON_SELECTORS = [
    "div.ds-button.ds-button--circle",
    "div.ds-icon-button",
]
NEW_CHAT_SELECTORS = [
    "button:has-text('新对话')",
    "button:has-text('New chat')",
    "button[aria-label*='New']",
    "button[aria-label*='新']",
    "a[href='/']",
]
# Same control humans watch: empty textarea + enabled circle button = generating/stop.
COMPOSER_STATE_JS = """() => {
    const ta = document.querySelector('textarea');
    const textareaLen = ta ? (ta.value || '').length : 0;
    let root = ta;
    let btn = null;
    for (let i = 0; i < 10 && root; i++) {
        btn = root.querySelector('.ds-button--circle, .ds-icon-button');
        if (btn) break;
        root = root.parentElement;
    }
    if (!btn) {
        btn = document.querySelector('.ds-button--circle, .ds-icon-button');
    }
    if (!btn) {
        return {
            ok: false,
            generating: false,
            can_send: false,
            disabled: true,
            textarea_len: textareaLen,
        };
    }
    const cls = (btn.className || '').toString();
    const disabled = cls.includes('ds-button--disabled')
        || btn.getAttribute('aria-disabled') === 'true'
        || btn.hasAttribute('disabled');
    const html = (btn.innerHTML || '').toLowerCase();
    const svg = btn.querySelector('svg');
    const hasRect = !!(svg && svg.querySelector('rect'));
    const looksStop = hasRect
        || html.includes('stop')
        || html.includes('停止');
    const generating = !disabled && textareaLen === 0;
    const canSend = !disabled && textareaLen > 0 && !looksStop;
    return {
        ok: true,
        generating,
        can_send: canSend,
        disabled,
        textarea_len: textareaLen,
        looks_stop: looksStop,
    };
}"""


class DeepSeekClient:
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
            config.deepseek_timeout_ms if timeout_ms is None else timeout_ms
        )
        # Prefer system Chrome (channel=chrome). Override with CHROME_PATH
        # or BROWSER_CHANNEL=chromium for Playwright's bundled browser.
        self.browser_channel = config.browser_channel
        self.executable_path = config.chrome_path
        self.username = config.deepseek_username
        self.password = config.deepseek_password
        self.auto_login = config.deepseek_auto_login
        self.worker_count = max(
            1,
            min(4, int(config.deepseek_workers if workers is None else workers)),
        )

        # Each worker thread owns its Playwright stack (sync API is thread-bound).
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
                name=f"deepseek-playwright-{slot.worker_id}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        logger.info("deepseek: pool workers=%s", self.worker_count)

    def _worker_loop(self, slot: _WorkerSlot) -> None:
        while True:
            func, done = slot.tasks.get()
            if func is None:
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
            raise RuntimeError("deepseek browser call must run on a worker thread")
        return slot

    def _pick_slot(self) -> _WorkerSlot:
        """Prefer an idle worker; otherwise the shortest queue."""
        with self._pick_lock:
            idle = [s for s in self._slots if not s.busy]
            pool = idle or list(self._slots)
            return min(pool, key=lambda s: s.tasks.qsize())

    def _submit_to(self, slot: _WorkerSlot, func: Callable[[], Any]) -> Any:
        done: "queue.Queue[tuple]" = queue.Queue()
        slot.tasks.put((func, done))
        ok, result = done.get()
        if not ok:
            raise result
        return result

    def _submit(self, func: Callable[[], Any]) -> Any:
        """Run func on one free (or least-loaded) Playwright worker."""
        slot = self._pick_slot()
        logger.debug(
            "submit worker=%s busy=%s queued=%s",
            slot.worker_id,
            slot.busy,
            slot.tasks.qsize(),
        )
        return self._submit_to(slot, func)

    def _submit_all(self, func: Callable[[], Any]) -> list[Any]:
        """Run func once on every worker (start / ensure_ready / stop prep)."""
        dones: list["queue.Queue[tuple]"] = []
        for slot in self._slots:
            done: "queue.Queue[tuple]" = queue.Queue()
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
                queued += slot.tasks.qsize()
                detail = dict(slot.last_status or {})
                detail.setdefault("worker_id", slot.worker_id)
                detail["busy"] = slot.busy
                detail["queued"] = slot.tasks.qsize()
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
            logger.exception("deepseek worker pool stop failed")
        self._publish_status()
        logger.info("deepseek worker pool browsers closed workers=%s", self.worker_count)

    def _stop_impl(self) -> None:
        self._stop_unlocked()

    def status(self) -> dict[str, Any]:
        # Non-blocking: last aggregate snapshot from workers.
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
        if state == "auth":
            ok = self._ensure_logged_in(page)
            state = self._detect_shell_state(page)
            if not ok or state != "chat":
                raise RuntimeError(
                    "auto login failed; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env"
                )
        elif state != "chat":
            page.goto(CHAT_URL, wait_until="domcontentloaded")
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(
                    "auto login failed; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env"
                )
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
            "worker_id": self._current_slot().worker_id,
        }

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
        question = (question or "").strip()
        if not question:
            raise ValueError("question is empty")

        resolved_mode = self._normalize_mode(mode)
        if resolved_mode == "expert" and search:
            raise ValueError("expert mode does not support search; use mode=instant")

        timeout_s = self._resolve_chat_timeout_s(
            timeout_s,
            mode=resolved_mode,
            deep_thinking=deep_thinking,
        )
        # Validate on caller thread; browser work runs on a pool worker.
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

    def sync_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Pull conversation history from chat.deepseek.com into local SQLite."""
        return self._submit(lambda: self._sync_conversation_impl(conversation_id))

    def _sync_conversation_impl(self, conversation_id: str) -> dict[str, Any]:
        conv = (conversation_id or "").strip()
        match = CONVERSATION_ID_RE.search(conv)
        if match:
            conv = match.group(1)
        elif not re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            conv,
            flags=re.IGNORECASE,
        ):
            raise ValueError(f"invalid conversation_id: {conversation_id}")

        t0 = time.perf_counter()
        slot = self._current_slot()
        page = self._ensure_page_unlocked()
        state = self._detect_shell_state(page)
        logger.info(
            "sync conversation start worker=%s state=%s conv=%s",
            slot.worker_id,
            state,
            conv,
        )
        if state == "auth":
            if not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env"
                )
            state = self._detect_shell_state(page)
        if state != "chat":
            page.goto(CHAT_URL, wait_until="domcontentloaded")
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env"
                )
            state = self._detect_shell_state(page)
            if state != "chat":
                raise RuntimeError(f"chat UI not ready, state={state}")

        self._open_conversation(page, conv)
        messages = self._collect_conversation_messages(page)
        if not messages:
            raise RuntimeError(f"no messages found for conversation {conv}")

        conv_id = self._conversation_id_from_url(page.url) or conv
        title = (page.title() or "").replace(" - DeepSeek", "").strip() or None
        mode = self._detect_current_mode(page) or "instant"
        deep_thinking = bool(
            page.evaluate(
                """() => {
                    const btn = document.querySelectorAll('.ds-toggle-button')[0];
                    return !!(btn && btn.classList.contains('ds-toggle-button--selected'));
                }"""
            )
        )
        url = page.url
        db_mgr.upsert_conversation(
            conversation_id=conv_id,
            provider=PROVIDER,
            title=title,
            mode=mode,
            deep_thinking=deep_thinking,
            search=False,
            url=url,
        )
        db_mgr.replace_conversation_messages(
            conv_id,
            [(str(m["role"]), str(m["content"])) for m in messages],
        )
        self._save_storage_unlocked()

        logger.info(
            "sync conversation done worker=%s conv=%s messages=%s elapsed=%.1fs",
            slot.worker_id,
            conv_id,
            len(messages),
            time.perf_counter() - t0,
        )
        return {
            "ok": True,
            "conversation_id": conv_id,
            "title": title,
            "mode": mode,
            "deep_thinking": deep_thinking,
            "search": False,
            "url": url,
            "message_count": len(messages),
            "synced": True,
            "worker_id": slot.worker_id,
        }

    def _resolve_chat_timeout_s(
        self,
        timeout_s: int | None,
        *,
        mode: str,
        deep_thinking: bool,
    ) -> int:
        if timeout_s is not None:
            return max(30, int(timeout_s))
        if mode == "expert" or deep_thinking:
            return max(30, config.deepseek_think_timeout_s)
        return max(30, config.deepseek_chat_timeout_s)

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
            "ask start worker=%s state=%s conv=%s mode=%s think=%s search=%s chars=%s timeout_s=%s",
            slot.worker_id,
            state,
            conversation_id,
            mode,
            deep_thinking,
            search,
            len(question),
            timeout_s,
        )
        if state == "auth":
            if not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env "
                    f"(sqlite={db_mgr.path()})"
                )
            state = self._detect_shell_state(page)

        if state != "chat":
            page.goto(CHAT_URL, wait_until="domcontentloaded")
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env"
                )
            state = self._detect_shell_state(page)
            if state != "chat":
                raise RuntimeError(f"chat UI not ready, state={state}")

        if conversation_id:
            self._open_conversation(page, conversation_id)
        else:
            self._open_new_chat(page)

        # Mode is fixed once a conversation exists; only switch on new chats.
        mode = self._apply_chat_options(
            page,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            switch_mode=not bool(conversation_id),
        )

        self._scroll_latest_into_view(page)
        before_text = self._last_assistant_text(page)
        if conversation_id and not before_text:
            history_deadline = time.time() + 5
            while time.time() < history_deadline:
                time.sleep(0.3)
                self._scroll_latest_into_view(page)
                before_text = self._last_assistant_text(page)
                if before_text:
                    break
        logger.info("composer before_text_chars=%s", len(before_text or ""))
        self._enter_message(page, question)
        self._send_message(page)
        answer = self._wait_answer(page, before_text=before_text, timeout_s=timeout_s)
        self._save_storage_unlocked()

        conv_id = self._conversation_id_from_url(page.url) or conversation_id
        if not conv_id:
            raise RuntimeError("conversation_id missing after reply; DeepSeek URL has no chat id")

        title = question.replace("\n", " ").strip()[:40] or None
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
            "ask done worker=%s answer_chars=%s conv=%s elapsed=%.1fs",
            slot.worker_id,
            len(answer),
            conv_id,
            time.perf_counter() - t0,
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
        }

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        key = (mode or "instant").strip().lower()
        # keep Chinese keys as-is for alias lookup before lowercasing broke them
        raw = (mode or "instant").strip()
        resolved = MODE_ALIASES.get(raw) or MODE_ALIASES.get(key)
        if not resolved:
            raise ValueError("mode must be instant/fast or expert")
        return resolved

    def _apply_chat_options(
        self,
        page: Page,
        *,
        mode: str,
        deep_thinking: bool,
        search: bool,
        switch_mode: bool = True,
    ) -> str:
        """Apply mode/toggles. Returns the mode actually in effect."""
        if switch_mode:
            actual_mode = self._select_mode(page, mode)
        else:
            actual_mode = self._detect_current_mode(page) or mode
        self._set_feature_toggle(page, index=0, enabled=deep_thinking, label="deep_thinking")
        if actual_mode == "instant":
            self._set_feature_toggle(page, index=1, enabled=search, label="search")
        elif search:
            logger.warning("search ignored in expert mode")
        return actual_mode

    def _detect_current_mode(self, page: Page) -> str | None:
        """Read selected mode from radios or conversation header badge."""
        return page.evaluate(
            """() => {
                const radios = Array.from(document.querySelectorAll('[role="radio"]'));
                for (const radio of radios) {
                    if (radio.getAttribute('aria-checked') !== 'true') continue;
                    const text = (radio.innerText || '').trim();
                    if (/专家|Expert/i.test(text)) return 'expert';
                    if (/快速|Instant/i.test(text)) return 'instant';
                }
                const header = document.querySelector('.the-header');
                const text = header ? (header.innerText || '') : '';
                if (/专家模式|Expert/i.test(text)) return 'expert';
                if (/快速模式|Instant/i.test(text)) return 'instant';
                return null;
            }"""
        )

    def _select_mode(self, page: Page, mode: str) -> str:
        """Select chat mode on new-chat page (radios). Returns mode in effect."""
        labels = (
            ["快速模式", "快速", "Instant"]
            if mode == "instant"
            else ["专家模式", "专家", "Expert"]
        )
        for label in labels:
            loc = page.locator(
                f'[role="radiogroup"] [role="radio"]:has-text("{label}"), '
                f'div[role="radio"]:has-text("{label}")'
            )
            if loc.count() == 0:
                continue
            target = loc.first
            if target.get_attribute("aria-checked") != "true":
                target.click()
            logger.info("mode set to %s", mode)
            time.sleep(0.3)
            return mode
        return self._detect_current_mode(page) or mode

    def _set_feature_toggle(
        self,
        page: Page,
        *,
        index: int,
        enabled: bool,
        label: str,
    ) -> None:
        result = page.evaluate(
            """({ index, enabled }) => {
                const toggles = Array.from(document.querySelectorAll('.ds-toggle-button'));
                const btn = toggles[index];
                if (!btn) return { ok: false };
                const active = btn.classList.contains('ds-toggle-button--selected');
                if (enabled !== active) btn.click();
                return { ok: true, toggled: enabled !== active, active: enabled };
            }""",
            {"index": index, "enabled": enabled},
        )
        if result and result.get("ok"):
            logger.info("%s set enabled=%s toggled=%s", label, enabled, result.get("toggled"))
            time.sleep(0.2)
            return

        # text fallback for localized UI
        texts = (
            ["深度思考", "DeepThink", "Deep Think"]
            if index == 0
            else ["智能搜索", "Search", "联网搜索"]
        )
        for text in texts:
            loc = page.locator(f'.ds-toggle-button:has-text("{text}"), [role="button"]:has-text("{text}")')
            if loc.count() == 0:
                continue
            btn = loc.first
            class_attr = btn.get_attribute("class") or ""
            active = "ds-toggle-button--selected" in class_attr
            if active != enabled:
                btn.click()
                logger.info("%s toggled via text=%s enabled=%s", label, text, enabled)
            else:
                logger.info("%s already enabled=%s", label, enabled)
            time.sleep(0.2)
            return
        if enabled:
            logger.warning("%s toggle not found", label)

    def _conversation_id_from_url(self, url: str) -> str | None:
        match = CONVERSATION_ID_RE.search(url or "")
        return match.group(1) if match else None

    def _open_conversation(self, page: Page, conversation_id: str) -> None:
        conv = conversation_id.strip()
        match = CONVERSATION_ID_RE.search(conv)
        if match:
            conv = match.group(1)
        elif not re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            conv,
            flags=re.IGNORECASE,
        ):
            raise ValueError(f"invalid conversation_id: {conversation_id}")

        record = db_mgr.get_conversation(conv)
        target = (record or {}).get("url") or f"{CHAT_URL.rstrip('/')}/a/chat/s/{conv}"
        if self._conversation_id_from_url(page.url) == conv:
            logger.info("already on conversation id=%s", conv)
            return
        logger.info("open conversation id=%s url=%s", conv, target)
        page.goto(str(target), wait_until="domcontentloaded")
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
                "CHROME_PATH to chrome.exe. "
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
        # Make the browser look like a normal (non-automated) user so sites
        # like chat.deepseek.com do not flag Playwright's headless fingerprint.
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
        slot.page.goto(CHAT_URL, wait_until="domcontentloaded")
        state = self._wait_shell_state(slot.page, timeout_ms=60_000)
        logger.debug(
            "chat page ready worker=%s state=%s url=%s",
            slot.worker_id,
            state,
            slot.page.url,
        )
        # NOTE: login is left to callers (ensure_ready / ask) so it is not
        # attempted twice per flow.

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

    def _ensure_logged_in(self, page: Page) -> bool:
        """If on auth page, try password login from .env. Returns True when chat is ready."""
        state = self._detect_shell_state(page)
        if state == "chat":
            return True
        if state != "auth":
            return False
        if not self.auto_login:
            logger.warning("auth page but DEEPSEEK_AUTO_LOGIN disabled")
            return False
        if not self.username or not self.password:
            logger.warning("auth page but DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD missing in .env")
            return False
        try:
            self._password_login(page)
        except Exception as exc:
            logger.exception("password login failed: %s", exc)
            return False
        state = self._wait_shell_state(page, timeout_ms=60_000)
        if state == "chat":
            self._save_storage_unlocked()
            logger.info("password login ok, storage saved")
            return True
        logger.error("password login finished but state=%s", state)
        return False

    def _password_login(self, page: Page) -> None:
        assert self.username and self.password
        logger.info("password login as %s", self.username)
        if "sign_in" not in (page.url or "").lower():
            page.goto("https://chat.deepseek.com/sign_in", wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(0.8)

        # Default UI is phone + SMS; switch to password login first.
        # On the live page this control is a visible .ds-button labeled 密码登录.
        switched = False
        pw_mode = page.locator("div.ds-button:visible:has-text('密码登录')")
        if pw_mode.count() == 0:
            pw_mode = page.locator(
                "div.ds-button:visible:has-text('Password'), "
                "button:visible:has-text('密码登录')"
            )
        if pw_mode.count() > 0:
            pw_mode.first.click(timeout=8_000)
            switched = True
            logger.info("switched to password login")
        if not switched:
            raise RuntimeError("password-login control not found on sign-in page")

        page.wait_for_selector("input[type='password']", timeout=20_000)

        account = page.locator(
            "input[placeholder*='手机号/邮箱'], "
            "input[placeholder*='手机号'], input[placeholder*='邮箱'], "
            "input[placeholder*='Email'], input[placeholder*='Phone'], "
            "input[type='email'], input[autocomplete='username'], input[type='tel']"
        )
        if account.count() == 0:
            account = page.locator("input[type='text']")
        if account.count() == 0:
            raise RuntimeError("login account input not found")

        account.first.fill(self.username, timeout=15_000)
        page.locator("input[type='password']").first.fill(self.password, timeout=15_000)

        login_button = page.locator("div.ds-button--filled:visible:has-text('登录')")
        if login_button.count() == 0:
            login_button = page.locator(
                "div.ds-button--filled:has-text('Log in'), "
                "button:has-text('登录'), button:has-text('Log in')"
            )
        if login_button.count() == 0:
            raise RuntimeError("login button not found")
        login_button.first.click(timeout=10_000)

        deadline = time.time() + 45
        while time.time() < deadline:
            if self._detect_shell_state(page) == "chat":
                return
            time.sleep(0.5)
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
        # Check chat first: after login the page may still keep a (hidden or
        # off-screen) sign-in form in the DOM, so the presence of a visible
        # chat textarea is the authoritative "logged in" signal.
        if self._has_visible(page, CHAT_READY_SELECTORS):
            return "chat"
        url = (page.url or "").lower()
        if any(token in url for token in ("sign_in", "signin", "sign-in")):
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

    def _enter_message(self, page: Page, message: str) -> None:
        box = self._textarea(page)
        box.click()
        box.fill(message)

    def _composer_state(self, page: Page) -> dict[str, Any]:
        fallback = {
            "ok": False,
            "generating": False,
            "can_send": False,
            "disabled": True,
            "textarea_len": 0,
        }
        try:
            state = page.evaluate(COMPOSER_STATE_JS)
        except Exception:
            return fallback
        return state if isinstance(state, dict) else fallback

    def _composer_button(self, page: Page):
        for selector in COMPOSER_BUTTON_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() > 0 and loc.last.is_visible():
                    return loc.last
            except Exception:
                continue
        return None

    def _send_message(self, page: Page) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            state = self._composer_state(page)
            if state.get("can_send"):
                btn = self._composer_button(page)
                if btn is not None:
                    btn.click()
                    logger.info(
                        "composer send clicked textarea_len=%s",
                        state.get("textarea_len"),
                    )
                    return
            time.sleep(0.2)
        logger.warning("composer send not ready; fallback Enter")
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
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        self._wait_shell_state(page, timeout_ms=30_000)

    def _collect_conversation_messages(self, page: Page) -> list[dict[str, str]]:
        """Scroll through the chat thread and collect all visible messages."""
        page.evaluate(SCROLL_MESSAGE_CONTAINER_JS, 0)
        time.sleep(0.8)

        ordered: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        stale_rounds = 0

        for _ in range(200):
            batch = page.evaluate(COLLECT_MESSAGES_JS) or []
            added = 0
            for item in batch:
                role = str(item.get("role") or "").strip()
                content = str(item.get("content") or "").strip()
                if role not in {"user", "assistant"} or not content:
                    continue
                key = (role, content)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(key)
                added += 1

            scroll = page.evaluate(
                """() => {
                    let best = null;
                    let bestOverflow = 0;
                    for (const el of document.querySelectorAll('div')) {
                        const overflow = el.scrollHeight - el.clientHeight;
                        if (overflow > bestOverflow) {
                            bestOverflow = overflow;
                            best = el;
                        }
                    }
                    if (!best) return { at_bottom: true };
                    const atBottom = best.scrollTop + best.clientHeight >= best.scrollHeight - 4;
                    if (!atBottom) {
                        best.scrollTop += Math.max(200, best.clientHeight * 0.85);
                    }
                    return { at_bottom: atBottom };
                }"""
            ) or {"at_bottom": True}

            if added == 0:
                stale_rounds += 1
            else:
                stale_rounds = 0

            if scroll.get("at_bottom") and stale_rounds >= 2:
                break
            time.sleep(0.35)

        return [{"role": role, "content": content} for role, content in ordered]

    def _last_assistant_text(self, page: Page) -> str:
        loc = page.locator(ASSISTANT_SELECTOR)
        count = loc.count()
        if count <= 0:
            return ""
        try:
            return (loc.nth(count - 1).inner_text() or "").strip()
        except Exception:
            return ""

    def _scroll_latest_into_view(self, page: Page) -> None:
        try:
            page.evaluate(
                """() => {
                    let best = null;
                    let bestOverflow = 0;
                    for (const el of document.querySelectorAll('div')) {
                        const overflow = el.scrollHeight - el.clientHeight;
                        if (overflow > bestOverflow) {
                            bestOverflow = overflow;
                            best = el;
                        }
                    }
                    if (best) best.scrollTop = best.scrollHeight;
                    const nodes = document.querySelectorAll(
                        'div.ds-message:has(.ds-markdown)'
                    );
                    const last = nodes[nodes.length - 1];
                    if (last) last.scrollIntoView({ block: 'end' });
                }"""
            )
        except Exception:
            pass

    def _is_generating(self, page: Page) -> bool:
        """True while the composer button is in stop/generating state."""
        return bool(self._composer_state(page).get("generating"))

    def _wait_answer(self, page: Page, *, before_text: str, timeout_s: int) -> str:
        deadline = time.time() + timeout_s
        started = time.time()
        previous = ""
        stable = 0
        saw_generating = False
        idle_stable = 0
        need_stable = 2

        while time.time() < deadline:
            state = self._composer_state(page)
            generating = bool(state.get("generating"))
            if generating:
                if not saw_generating:
                    logger.info("composer generating started")
                saw_generating = True
                idle_stable = 0
                stable = 0
                time.sleep(1)
                continue

            if not saw_generating:
                textarea_len = int(state.get("textarea_len") or 0)
                if textarea_len > 0:
                    time.sleep(0.2)
                    continue
                # Fast replies can flip stop->send before the next 1s poll.
                if time.time() - started >= 8:
                    self._scroll_latest_into_view(page)
                    text = self._last_assistant_text(page)
                    if text and text != before_text:
                        logger.info("composer missed stop; idle with new text")
                        saw_generating = True
                        previous = text
                        idle_stable = 1
                time.sleep(0.2)
                continue

            idle_stable += 1
            self._scroll_latest_into_view(page)
            text = self._last_assistant_text(page)
            if text and text != before_text and text == previous:
                stable += 1
            else:
                stable = 0
            if text and text != before_text:
                previous = text
            if (
                idle_stable >= need_stable
                and previous
                and previous != before_text
                and stable >= need_stable
            ):
                return previous
            time.sleep(1)

        generating = self._is_generating(page)
        if previous and previous != before_text and not generating:
            logger.warning(
                "answer wait ended with partial text chars=%s timeout_s=%s generating=%s",
                len(previous),
                timeout_s,
                generating,
            )
            return previous
        logger.error(
            "no complete answer within %ss saw_generating=%s chars=%s generating=%s",
            timeout_s,
            saw_generating,
            len(previous),
            generating,
        )
        raise TimeoutError(
            f"no complete answer within {timeout_s}s "
            f"(saw_generating={saw_generating}, generating={generating})"
        )


_client: DeepSeekClient | None = None
_client_lock = threading.Lock()


def get_client() -> DeepSeekClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = DeepSeekClient()
        return _client
