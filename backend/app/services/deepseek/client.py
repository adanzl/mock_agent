"""Playwright client that drives chat.deepseek.com like a real user."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Any, Callable

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from app.config import config
from app.repositories.database import (
    add_conversation_messages,
    get_browser_session,
    get_conversation,
    has_browser_session,
    save_browser_session,
    sqlite_path,
    upsert_conversation,
)

logger = logging.getLogger(__name__)

PROVIDER = "deepseek"
CHAT_URL = "https://chat.deepseek.com/"
CONVERSATION_ID_RE = re.compile(
    r"/a/chat/s/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)

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
NEW_CHAT_SELECTORS = [
    "button:has-text('新对话')",
    "button:has-text('New chat')",
    "button[aria-label*='New']",
    "button[aria-label*='新']",
    "a[href='/']",
]


class DeepSeekClient:
    """Single shared browser session. Calls are serialized by a lock."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        timeout_ms: int | None = None,
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

        # Playwright's sync API is bound to the thread that created it, so all
        # browser work runs on ONE dedicated worker thread; public methods
        # submit tasks and wait for the result.
        self._tasks: "queue.Queue[tuple[Callable[[], Any], 'queue.Queue[tuple]']]" = (
            queue.Queue()
        )
        self._last_status: dict[str, Any] | None = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="deepseek-playwright",
            daemon=True,
        )
        self._worker.start()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._browser_info = ""

    def _worker_loop(self) -> None:
        while True:
            func, done = self._tasks.get()
            if func is None:
                self._tasks.task_done()
                return
            try:
                result = func()
                done.put((True, result))
            except BaseException as exc:  # noqa: BLE001
                done.put((False, exc))
            finally:
                try:
                    self._refresh_last_status()
                except Exception:
                    pass
                self._tasks.task_done()

    def _submit(self, func: Callable[[], Any]) -> Any:
        """Run func on the single Playwright worker thread and return its result."""
        done: "queue.Queue[tuple]" = queue.Queue()
        self._tasks.put((func, done))
        ok, result = done.get()
        if not ok:
            raise result
        return result

    def _default_status(self) -> dict[str, Any]:
        return {
            "ready": False,
            "state": "unknown",
            "url": "",
            "headless": self.headless,
            "browser": self._browser_info or self.browser_channel,
            "sqlite_path": str(sqlite_path()),
            "session_saved": has_browser_session(PROVIDER),
        }

    def _refresh_last_status(self) -> None:
        if self._page is None:
            return
        state = self._detect_shell_state(self._page)
        self._last_status = {
            "ready": state == "chat",
            "state": state,
            "url": self._page.url,
            "headless": self.headless,
            "browser": self._browser_info or self.browser_channel,
            "sqlite_path": str(sqlite_path()),
            "session_saved": has_browser_session(PROVIDER),
        }

    def start(self) -> None:
        self._submit(self._start_impl)

    def _start_impl(self) -> None:
        logger.info(
            "browser start requested headless=%s channel=%s",
            self.headless,
            self.browser_channel,
        )
        self._start_unlocked()

    def stop(self) -> None:
        self._submit(self._stop_impl)

    def _stop_impl(self) -> None:
        logger.info("browser stop")
        self._stop_unlocked()

    def status(self) -> dict[str, Any]:
        # Non-blocking: return the last known state captured by the worker.
        if self._last_status is not None:
            return dict(self._last_status)
        return self._default_status()

    def ensure_ready(self) -> dict[str, Any]:
        """Start browser and auto-login from .env if needed."""
        return self._submit(self._ensure_ready_impl)

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
        return {
            "ok": True,
            "ready": True,
            "state": state,
            "url": page.url,
            "session_saved": has_browser_session(PROVIDER),
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

        timeout_s = timeout_s or max(30, self.timeout_ms // 1000)
        # Validate/normalize on the caller thread, then run all browser work on
        # the single Playwright worker thread (sync API is thread-bound).
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
        page = self._ensure_page_unlocked()
        state = self._detect_shell_state(page)
        logger.info(
            "ask start state=%s conv=%s mode=%s think=%s search=%s chars=%s",
            state,
            conversation_id,
            mode,
            deep_thinking,
            search,
            len(question),
        )
        if state == "auth":
            if not self._ensure_logged_in(page):
                raise RuntimeError(
                    "not logged in; check DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD in .env "
                    f"(sqlite={sqlite_path()})"
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

        self._apply_chat_options(
            page,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
        )

        before = self._assistant_count(page)
        self._enter_message(page, question)
        self._send_message(page)
        answer = self._wait_answer(page, before_count=before, timeout_s=timeout_s)
        self._save_storage_unlocked()

        conv_id = self._conversation_id_from_url(page.url) or conversation_id
        if not conv_id:
            raise RuntimeError("conversation_id missing after reply; DeepSeek URL has no chat id")

        title = question.replace("\n", " ").strip()[:40] or None
        upsert_conversation(
            conversation_id=conv_id,
            provider=PROVIDER,
            title=title if not conversation_id else None,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            url=page.url,
        )
        add_conversation_messages(
            conv_id,
            [("user", question), ("assistant", answer)],
        )

        logger.info(
            "ask done answer_chars=%s conv=%s elapsed=%.1fs",
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
    ) -> None:
        self._select_mode(page, mode)
        self._set_feature_toggle(page, index=0, enabled=deep_thinking, label="deep_thinking")
        if mode == "instant":
            self._set_feature_toggle(page, index=1, enabled=search, label="search")
        elif search:
            logger.warning("search ignored in expert mode")

    def _select_mode(self, page: Page, mode: str) -> None:
        index = 0 if mode == "instant" else 1
        result = page.evaluate(
            """(index) => {
                const radios = document.querySelectorAll('div[role="radio"]');
                if (!radios.length || index >= radios.length) return { ok: false };
                const target = radios[index];
                const already = target.getAttribute('aria-checked') === 'true';
                if (!already) target.click();
                return { ok: true, toggled: !already };
            }""",
            index,
        )
        if not result or not result.get("ok"):
            # fallback by visible text
            labels = (
                ["快速", "Instant", "快速模式"]
                if mode == "instant"
                else ["专家", "Expert", "专家模式"]
            )
            clicked = False
            for label in labels:
                loc = page.locator(f'div[role="radio"]:has-text("{label}")')
                if loc.count() > 0:
                    loc.first.click()
                    clicked = True
                    break
            if not clicked:
                logger.warning("mode radio not found mode=%s", mode)
                return
        logger.info("mode set to %s", mode)
        time.sleep(0.3)

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

        record = get_conversation(conv)
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
                logger.info("browser launched via executable_path=%s", self.executable_path)
                return browser
            except Exception as exc:
                errors.append(f"executable_path={self.executable_path}: {exc}")
                logger.warning("launch via executable_path failed: %s", exc)

        channel = self.browser_channel
        if channel and channel.lower() not in {"", "chromium", "bundled"}:
            try:
                browser = playwright.chromium.launch(channel=channel, **common)
                self._browser_info = f"channel:{channel}"
                logger.info("browser launched via channel=%s headless=%s", channel, self.headless)
                return browser
            except Exception as exc:
                errors.append(f"channel={channel}: {exc}")
                logger.warning("launch via channel=%s failed: %s", channel, exc)

        try:
            browser = playwright.chromium.launch(**common)
            self._browser_info = "bundled:chromium"
            logger.info("browser launched via bundled chromium headless=%s", self.headless)
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
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._launch_browser(self._playwright)
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
        }
        stored = get_browser_session(PROVIDER)
        if stored:
            context_kwargs["storage_state"] = stored
            logger.info("load storage_state from sqlite provider=%s", PROVIDER)
        self._context = self._browser.new_context(**context_kwargs)
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        # Make the browser look like a normal (non-automated) user so sites
        # like chat.deepseek.com do not flag Playwright's headless fingerprint.
        self._page.add_init_script(
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
        self._page.goto(CHAT_URL, wait_until="domcontentloaded")
        state = self._wait_shell_state(self._page, timeout_ms=60_000)
        logger.info("chat page ready state=%s url=%s", state, self._page.url)
        # NOTE: login is left to callers (ensure_ready / ask) so it is not
        # attempted twice per flow.

    def _stop_unlocked(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _ensure_page_unlocked(self) -> Page:
        if self._page is None:
            self._start_unlocked()
        assert self._page is not None
        return self._page

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
        if self._context is None:
            return
        state = self._context.storage_state()
        save_browser_session(PROVIDER, state)

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

    def _send_message(self, page: Page) -> None:
        send = page.locator("div.ds-icon-button").last
        try:
            if send.count() > 0 and send.is_visible():
                disabled = send.get_attribute("aria-disabled")
                if disabled != "true":
                    send.click()
                    return
        except Exception:
            pass
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

    def _assistant_count(self, page: Page) -> int:
        try:
            return page.locator(ASSISTANT_SELECTOR).count()
        except Exception:
            return 0

    def _last_assistant_text(self, page: Page) -> str:
        loc = page.locator(ASSISTANT_SELECTOR)
        count = loc.count()
        if count <= 0:
            return ""
        try:
            return (loc.nth(count - 1).inner_text() or "").strip()
        except Exception:
            return ""

    def _wait_answer(self, page: Page, *, before_count: int, timeout_s: int) -> str:
        deadline = time.time() + timeout_s
        previous = ""
        stable = 0
        saw_new = False

        while time.time() < deadline:
            count = self._assistant_count(page)
            if count > before_count:
                saw_new = True
            text = self._last_assistant_text(page) if saw_new or count > 0 else ""
            if text and text == previous:
                stable += 1
            else:
                stable = 0
            previous = text
            # text unchanged across ~2s of polling => generation finished
            if saw_new and text and stable >= 2:
                return text
            time.sleep(1)

        if previous:
            logger.warning(
                "answer wait ended with partial text chars=%s timeout_s=%s",
                len(previous),
                timeout_s,
            )
            return previous
        logger.error("no answer within %ss", timeout_s)
        raise TimeoutError(f"no answer within {timeout_s}s")


_client: DeepSeekClient | None = None
_client_lock = threading.Lock()


def get_client() -> DeepSeekClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = DeepSeekClient()
        return _client
