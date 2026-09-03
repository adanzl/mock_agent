"""Playwright client that drives chatgpt.com via a local HTTP proxy."""

from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.config import config
from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

PROVIDER = "chatgpt"
CHAT_URL = "https://chatgpt.com/"
LOGIN_URL = "https://chatgpt.com/auth/login"
CONVERSATION_ID_RE = re.compile(
    r"/c/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)

MODE_ALIASES = {
    "auto": "auto",
    "default": "auto",
    "gpt-4o": "gpt-4o",
    "4o": "gpt-4o",
    "gpt-4.1": "gpt-4.1",
    "4.1": "gpt-4.1",
    "gpt-5": "gpt-5",
    "o3": "o3",
    "o4-mini": "o4-mini",
    "o4mini": "o4-mini",
}

CHAT_READY_SELECTORS = [
    '#prompt-textarea[contenteditable="true"]',
    '[data-testid="prompt-textarea"][contenteditable="true"]',
    'div[contenteditable="true"][data-testid="prompt-textarea"]',
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
]
COMPOSER_LOCATOR = (
    '#prompt-textarea[contenteditable="true"], '
    '[data-testid="prompt-textarea"][contenteditable="true"], '
    'div[contenteditable="true"][data-testid="prompt-textarea"]'
)
# ChatGPT keeps a disabled fallback <textarea>; never treat it as the composer.
COMPOSER_READY_JS = """() => {
    const el = document.querySelector('#prompt-textarea[contenteditable="true"]')
        || document.querySelector('[data-testid="prompt-textarea"][contenteditable="true"]')
        || document.querySelector('div[contenteditable="true"][data-testid="prompt-textarea"]');
    if (!el) return { ready: false, reason: 'missing' };
    const rect = el.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) return { ready: false, reason: 'hidden' };
    if (el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('disabled')) {
        return { ready: false, reason: 'disabled' };
    }
    return { ready: true };
}"""
SIGN_IN_SELECTORS = [
    'button:has-text("Log in")',
    'button:has-text("登录")',
    'a:has-text("Log in")',
    'a:has-text("登录")',
    'button:has-text("Sign up")',
    'button:has-text("免费注册")',
    '[data-testid="login-button"]',
    'input[type="email"]',
    'input[name="email"]',
    'input[type="password"]',
]
LOGGED_IN_SELECTORS = [
    '[data-testid="profile-button"]',
    'button[aria-label*="Open profile"]',
    'button[aria-label*="用户"]',
    'button[aria-label*="Profile"]',
]
ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
NEW_CHAT_SELECTORS = [
    '[data-testid="create-new-chat-button"]',
    '[data-testid="new-chat-button"]',
    'a[data-testid="create-new-chat-button"]',
    'button:has-text("New chat")',
    'button:has-text("新聊天")',
    'a:has-text("New chat")',
    'a:has-text("新聊天")',
]
SEND_SELECTORS = [
    '[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[aria-label*="Send message"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
    'button[aria-label="提交"]',
]
STOP_SELECTORS = [
    '[data-testid="stop-button"]',
    'button[aria-label*="Stop"]',
    'button[aria-label*="停止"]',
    'button:has-text("Stop generating")',
    'button:has-text("停止生成")',
]


class ChatGPTClient:
    """Single shared browser session. Calls are serialized by a lock."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        self.headless = config.headless if headless is None else headless
        self.timeout_ms = int(
            config.chatgpt_timeout_ms if timeout_ms is None else timeout_ms
        )
        self.browser_channel = config.browser_channel
        self.executable_path = config.chrome_path
        self.proxy = config.chatgpt_proxy
        self.username = config.chatgpt_username
        self.password = config.chatgpt_password
        self.manual_login = bool(config.chatgpt_manual_login)
        self.auto_login = config.chatgpt_auto_login and not self.manual_login
        self.captcha_timeout_s = int(config.chatgpt_captcha_timeout_s)
        self.user_data_dir = Path(config.chatgpt_user_data_dir)
        self.cdp_url = (config.chatgpt_cdp_url or "").strip() or None
        self.auto_launch_chrome = bool(config.chatgpt_auto_launch_chrome)
        self.chatgpt_chrome_path = config.chatgpt_chrome_path
        self._attached_cdp = False
        self._chrome_proc: subprocess.Popen[bytes] | None = None
        # Manual login requires a visible browser window.
        if self.manual_login and self.headless and not self.cdp_url:
            logger.warning(
                "CHATGPT_MANUAL_LOGIN=1 forces headed browser (override HEADLESS)"
            )
            self.headless = False

        self._tasks: "queue.Queue[tuple[Callable[[], Any], 'queue.Queue[tuple]']]" = (
            queue.Queue()
        )
        self._last_status: dict[str, Any] | None = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="chatgpt-playwright",
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
            "manual_login": self.manual_login,
            "browser": self._browser_info or self.browser_channel,
            "proxy": self.proxy,
            "sqlite_path": str(db_mgr.path()),
            "session_saved": db_mgr.has_browser_session(PROVIDER),
            "profile": str(self.user_data_dir),
            "cdp_url": self.cdp_url,
            "auto_launch_chrome": self.auto_launch_chrome,
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
            "manual_login": self.manual_login,
            "browser": self._browser_info or self.browser_channel,
            "proxy": self.proxy,
            "sqlite_path": str(db_mgr.path()),
            "session_saved": db_mgr.has_browser_session(PROVIDER),
            "profile": str(self.user_data_dir),
            "cdp_url": self.cdp_url,
            "auto_launch_chrome": self.auto_launch_chrome,
        }

    def start(self) -> None:
        self._submit(self._start_impl)

    def _start_impl(self) -> None:
        logger.info(
            "browser start requested headless=%s channel=%s proxy=%s",
            self.headless,
            self.browser_channel,
            self.proxy,
        )
        self._start_unlocked()

    def stop(self) -> None:
        self._submit(self._stop_impl)

    def _stop_impl(self) -> None:
        logger.info("browser stop")
        self._stop_unlocked()

    def status(self) -> dict[str, Any]:
        if self._last_status is not None:
            return dict(self._last_status)
        return self._default_status()

    def ensure_ready(self) -> dict[str, Any]:
        """Start browser; manual login waits for user, else optional auto-login."""
        return self._submit(self._ensure_ready_impl)

    def _ensure_ready_impl(self) -> dict[str, Any]:
        # CDP + manual login: keep Playwright detached until the user finishes
        # clicking in Chrome (Playwright CDP attach breaks login buttons).
        if self.cdp_url and self.manual_login:
            self._ensure_cdp_chrome()
            self._wait_manual_login_via_cdp()
        page = self._ensure_page_unlocked()
        self._wait_human_challenge(page)
        state = self._detect_shell_state(page)
        if state == "unknown":
            page.goto(CHAT_URL, wait_until="domcontentloaded")
            self._wait_human_challenge(page)
            state = self._wait_shell_state(page, timeout_ms=60_000)
        if state == "auth":
            ok = self._ensure_logged_in(page)
            state = self._detect_shell_state(page)
            if not ok or state != "chat":
                raise RuntimeError(self._login_failed_message())
        elif state != "chat":
            page.goto(CHAT_URL, wait_until="domcontentloaded")
            self._wait_human_challenge(page)
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(self._login_failed_message())
            state = self._detect_shell_state(page)
            if state != "chat":
                raise RuntimeError(f"chat UI not ready, state={state}")
        self._save_storage_unlocked()
        return {
            "ok": True,
            "ready": True,
            "state": state,
            "url": page.url,
            "proxy": self.proxy,
            "manual_login": self.manual_login,
            "session_saved": db_mgr.has_browser_session(PROVIDER),
            "profile": str(self.user_data_dir),
            "cdp_url": self.cdp_url,
            "auto_launch_chrome": self.auto_launch_chrome,
        }

    def _login_failed_message(self) -> str:
        if self.manual_login:
            return (
                "manual login not completed in time; finish login in the browser window "
                f"(profile={self.user_data_dir})"
            )
        return "auto login failed; check CHATGPT_USERNAME/CHATGPT_PASSWORD in .env"

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
        timeout_s = self._resolve_chat_timeout_s(timeout_s)
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
            except PlaywrightTimeoutError as exc:
                msg = str(exc).split("\n", 1)[0].strip()
                last_exc = RuntimeError(f"composer not ready: {msg}")
                will_retry = attempt < attempts
                logger.warning(
                    "ask composer attempt=%s/%s will_retry=%s conv=%s error=%s",
                    attempt,
                    attempts,
                    will_retry,
                    conversation_id,
                    msg,
                )
                if not will_retry:
                    raise last_exc from None
        assert last_exc is not None
        raise last_exc

    def _resolve_chat_timeout_s(self, timeout_s: int | None) -> int:
        if timeout_s is not None:
            return max(30, int(timeout_s))
        return max(30, config.chatgpt_chat_timeout_s)

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
            "ask start state=%s conv=%s mode=%s think=%s search=%s chars=%s timeout_s=%s",
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
                raise RuntimeError(self._login_failed_message())
            state = self._detect_shell_state(page)

        if state != "chat":
            page.goto(CHAT_URL, wait_until="domcontentloaded")
            state = self._wait_shell_state(page, timeout_ms=60_000)
            if state == "auth" and not self._ensure_logged_in(page):
                raise RuntimeError(self._login_failed_message())
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
        logger.info("composer enter chars=%s before_assistant=%s", len(question), before)
        self._enter_message(page, question)
        self._send_message(page)
        logger.info("composer sent; waiting answer timeout_s=%s", timeout_s)
        answer = self._wait_answer(page, before_count=before, timeout_s=timeout_s)
        self._save_storage_unlocked()

        conv_id = self._conversation_id_from_url(page.url) or conversation_id
        if not conv_id:
            # URL may lag; wait briefly for /c/<id>
            deadline = time.time() + 15
            while time.time() < deadline and not conv_id:
                time.sleep(0.5)
                conv_id = self._conversation_id_from_url(page.url) or conversation_id
        if not conv_id:
            raise RuntimeError("conversation_id missing after reply; ChatGPT URL has no chat id")

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
        raw = (mode or "auto").strip()
        key = raw.lower()
        resolved = MODE_ALIASES.get(raw) or MODE_ALIASES.get(key)
        if not resolved:
            # Allow free-form model labels shown in the UI (e.g. "GPT-5 Thinking").
            if len(raw) > 40:
                raise ValueError("mode is too long")
            return raw
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
        actual_mode = mode
        if switch_mode and mode and mode != "auto":
            actual_mode = self._select_mode(page, mode)
        if search:
            self._set_search(page, enabled=True)
        if deep_thinking:
            self._set_deep_thinking(page, enabled=True)
        return actual_mode

    def _select_mode(self, page: Page, mode: str) -> str:
        """Open model picker and choose an option matching mode text."""
        selectors = [
            'button[aria-label="Model selector"]',
            'button[aria-label*="Model"]',
            '[data-testid="model-selector"]',
            'button[aria-haspopup="menu"]',
        ]
        opened = False
        for selector in selectors:
            loc = page.locator(selector)
            try:
                if loc.count() == 0 or not loc.first.is_visible():
                    continue
                loc.first.click(timeout=5_000)
                opened = True
                time.sleep(0.4)
                break
            except Exception:
                continue
        if not opened:
            logger.warning("model selector not found; keep mode=%s", mode)
            return mode

        option = page.locator(
            f'[role="menuitem"]:has-text("{mode}"), '
            f'[role="option"]:has-text("{mode}"), '
            f'button:has-text("{mode}"), '
            f'div[role="menuitem"]:has-text("{mode}")'
        )
        try:
            if option.count() > 0 and option.first.is_visible():
                option.first.click(timeout=5_000)
                logger.info("mode set to %s", mode)
                time.sleep(0.3)
                return mode
        except Exception as exc:
            logger.warning("mode select failed mode=%s err=%s", mode, exc)

        # Close menu if still open
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        logger.warning("mode option not found: %s", mode)
        return mode

    def _set_search(self, page: Page, *, enabled: bool) -> None:
        """Best-effort Search / web toggle."""
        if not enabled:
            return
        labels = ["Search", "搜索", "Web search", "联网搜索"]
        for label in labels:
            loc = page.locator(
                f'button:has-text("{label}"), '
                f'[role="menuitem"]:has-text("{label}"), '
                f'div[role="button"]:has-text("{label}")'
            )
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3_000)
                    logger.info("search toggled via %s", label)
                    time.sleep(0.2)
                    return
            except Exception:
                continue
        logger.warning("search control not found; ignored")

    def _set_deep_thinking(self, page: Page, *, enabled: bool) -> None:
        """Best-effort thinking / reasoning toggle."""
        if not enabled:
            return
        labels = [
            "Think",
            "Thinking",
            "Reason",
            "Reasoning",
            "深度思考",
            "思考",
            "Pro thinking",
        ]
        for label in labels:
            loc = page.locator(
                f'button:has-text("{label}"), '
                f'[role="menuitem"]:has-text("{label}"), '
                f'div[role="button"]:has-text("{label}")'
            )
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3_000)
                    logger.info("deep_thinking toggled via %s", label)
                    time.sleep(0.2)
                    return
            except Exception:
                continue
        logger.warning("deep_thinking control not found; ignored")

    def _conversation_id_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        match = CONVERSATION_ID_RE.search(url)
        return match.group(1) if match else None

    def _open_conversation(self, page: Page, conversation_id: str) -> None:
        conv = (conversation_id or "").strip()
        if not conv:
            raise ValueError("conversation_id is empty")
        current = self._conversation_id_from_url(page.url)
        if current and current.lower() == conv.lower():
            return
        target = f"https://chatgpt.com/c/{conv}"
        logger.info("open conversation id=%s url=%s", conv, target)
        page.goto(target, wait_until="domcontentloaded")
        state = self._wait_shell_state(page, timeout_ms=60_000)
        if state != "chat":
            raise RuntimeError(f"failed to open conversation {conv}, state={state}")
        self._wait_composer_ready(page)
        opened = self._conversation_id_from_url(page.url)
        if opened and opened.lower() != conv.lower():
            logger.warning("opened conv mismatch want=%s got=%s", conv, opened)

    def _proxy_kwargs(self) -> dict[str, Any]:
        proxy = (self.proxy or "").strip()
        if not proxy:
            return {}
        return {"proxy": {"server": proxy}}

    def _launch_args(self) -> list[str]:
        return [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-notifications",
            "--lang=zh-CN",
        ]

    def _launch_persistent_context(self, playwright: Playwright) -> BrowserContext:
        """Persistent profile keeps CF cookies; fewer repeated human checks."""
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        common: dict[str, Any] = {
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "args": self._launch_args(),
            "ignore_default_args": ["--enable-automation"],
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            **self._proxy_kwargs(),
        }
        errors: list[str] = []
        user_data = str(self.user_data_dir)

        if self.executable_path:
            try:
                ctx = playwright.chromium.launch_persistent_context(
                    user_data,
                    executable_path=self.executable_path,
                    **common,
                )
                self._browser_info = f"persistent:{self.executable_path}"
                logger.info(
                    "persistent browser via executable_path=%s profile=%s proxy=%s",
                    self.executable_path,
                    user_data,
                    self.proxy,
                )
                return ctx
            except Exception as exc:
                errors.append(f"executable_path={self.executable_path}: {exc}")
                logger.warning("persistent launch via executable_path failed: %s", exc)

        channel = self.browser_channel
        if channel and channel.lower() not in {"", "chromium", "bundled"}:
            try:
                ctx = playwright.chromium.launch_persistent_context(
                    user_data,
                    channel=channel,
                    **common,
                )
                self._browser_info = f"persistent-channel:{channel}"
                logger.info(
                    "persistent browser via channel=%s profile=%s proxy=%s",
                    channel,
                    user_data,
                    self.proxy,
                )
                return ctx
            except Exception as exc:
                errors.append(f"channel={channel}: {exc}")
                logger.warning("persistent launch via channel=%s failed: %s", channel, exc)

        try:
            ctx = playwright.chromium.launch_persistent_context(user_data, **common)
            self._browser_info = "persistent-bundled:chromium"
            logger.info(
                "persistent browser via bundled chromium profile=%s proxy=%s",
                user_data,
                self.proxy,
            )
            return ctx
        except Exception as exc:
            errors.append(f"bundled chromium: {exc}")
            detail = " | ".join(errors) if errors else str(exc)
            raise RuntimeError(
                "failed to launch ChatGPT persistent browser; "
                f"Tried: {detail}"
            ) from exc

    def _cdp_port(self) -> int:
        if self.cdp_url:
            parsed = urlparse(self.cdp_url)
            if parsed.port:
                return parsed.port
        return int(config.chatgpt_cdp_port)

    def _is_cdp_ready(self, cdp_url: str | None = None) -> bool:
        url = (cdp_url or self.cdp_url or "").rstrip("/")
        if not url:
            return False
        try:
            with urllib.request.urlopen(f"{url}/json/version", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _resolve_chrome_executable(self) -> str:
        if self.chatgpt_chrome_path:
            path = Path(self.chatgpt_chrome_path)
            if path.is_file():
                return str(path)
            raise RuntimeError(f"CHATGPT_CHROME_PATH not found: {path}")
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
        if self.executable_path and "headless" not in Path(self.executable_path).name.lower():
            return self.executable_path
        raise RuntimeError(
            "Google Chrome not found for ChatGPT; install Chrome or set CHATGPT_CHROME_PATH"
        )

    def _launch_chrome_for_cdp(self) -> None:
        port = self._cdp_port()
        chrome = self._resolve_chrome_executable()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=AutomationControlled",
            CHAT_URL,
        ]
        proxy = (self.proxy or "").strip()
        if proxy:
            args.append(f"--proxy-server={proxy}")
        logger.info(
            "launching Chrome for ChatGPT cdp_port=%s profile=%s proxy=%s",
            port,
            self.user_data_dir,
            proxy or "(none)",
        )
        self._chrome_proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._browser_info = f"auto-launch:{chrome}"

    def _ensure_cdp_chrome(self) -> None:
        if self._is_cdp_ready():
            return
        if not self.auto_launch_chrome:
            raise RuntimeError(
                f"CDP Chrome not reachable at {self.cdp_url}; "
                "start Chrome manually or set CHATGPT_AUTO_LAUNCH_CHROME=1"
            )
        self._launch_chrome_for_cdp()
        deadline = time.time() + 45
        while time.time() < deadline:
            if self._is_cdp_ready():
                logger.info("auto-launched Chrome CDP ready url=%s", self.cdp_url)
                return
            if self._chrome_proc is not None and self._chrome_proc.poll() is not None:
                raise RuntimeError(
                    "auto-launched Chrome exited early "
                    f"code={self._chrome_proc.returncode}"
                )
            time.sleep(0.5)
        raise RuntimeError(
            f"auto-launched Chrome did not open CDP on {self.cdp_url} within 45s"
        )

    def _cdp_tab_urls(self) -> list[str]:
        base = (self.cdp_url or "").rstrip("/")
        if not base:
            return []
        try:
            with urllib.request.urlopen(f"{base}/json/list", timeout=3) as resp:
                tabs = json.loads(resp.read())
        except Exception:
            return []
        return [
            str(item.get("url") or "")
            for item in tabs
            if str(item.get("type") or "") == "page"
        ]

    def _cdp_has_auth_flow(self, urls: list[str] | None = None) -> bool:
        urls = urls if urls is not None else self._cdp_tab_urls()
        for url in urls:
            low = url.lower()
            if "/auth/login" in low or "auth.openai.com" in low:
                return True
        return False

    def _cdp_has_chat_session(self, urls: list[str] | None = None) -> bool:
        urls = urls if urls is not None else self._cdp_tab_urls()
        return any("/c/" in url and "chatgpt.com" in url.lower() for url in urls)

    def _wait_manual_login_via_cdp(self) -> None:
        """Wait for login without Playwright attached (CDP HTTP polling only)."""
        urls = self._cdp_tab_urls()
        if self._cdp_has_chat_session(urls):
            return
        logger.warning(
            "ChatGPT manual login: 请在弹出的 Chrome 窗口完成登录"
            "（Playwright 尚未连接，登录按钮可正常点击）。"
            " timeout_s=%s profile=%s",
            self.captcha_timeout_s,
            self.user_data_dir,
        )
        deadline = time.time() + max(60, self.captcha_timeout_s)
        last_log = 0.0
        saw_auth = self._cdp_has_auth_flow(urls)
        while time.time() < deadline:
            urls = self._cdp_tab_urls()
            if self._cdp_has_chat_session(urls):
                logger.info("cdp manual login ok (conversation tab detected)")
                return
            if self._cdp_has_auth_flow(urls):
                saw_auth = True
            elif saw_auth:
                try:
                    self._start_unlocked()
                    state = self._detect_shell_state(self._page)
                    if state == "chat":
                        logger.info(
                            "cdp manual login ok state=chat url=%s", self._page.url
                        )
                        return
                except Exception as exc:
                    logger.warning("cdp login verify failed: %s", exc)
                finally:
                    if self._page is None or self._detect_shell_state(self._page) != "chat":
                        self._stop_unlocked()
                saw_auth = False
            now = time.time()
            if now - last_log > 20:
                logger.info(
                    "waiting for manual login (cdp detached)… remaining=%.0fs tabs=%s",
                    deadline - now,
                    urls,
                )
                last_log = now
            time.sleep(2)
        raise RuntimeError(self._login_failed_message())

    def _pick_cdp_page(self, context: BrowserContext) -> Page | None:
        """Prefer an existing logged-in chatgpt.com tab (local CDP workflow)."""
        for page in context.pages:
            url = (page.url or "").lower()
            if "chatgpt.com" not in url:
                continue
            try:
                if self._detect_shell_state(page) == "chat":
                    logger.info("cdp: reuse logged-in tab url=%s", page.url)
                    return page
            except Exception:
                continue
        return None

    def _pick_cdp_page_any(self, context: BrowserContext) -> Page | None:
        """Reuse any chatgpt.com tab (including login page)."""
        for page in context.pages:
            url = (page.url or "").lower()
            if "chatgpt.com" in url:
                logger.info("cdp: reuse chatgpt tab url=%s", page.url)
                return page
        return None

    def _connect_cdp(self, playwright: Playwright) -> tuple[Browser, BrowserContext, Page]:
        assert self.cdp_url
        self._ensure_cdp_chrome()
        logger.info("connecting to Chrome via CDP %s", self.cdp_url)
        browser = playwright.chromium.connect_over_cdp(self.cdp_url)
        contexts = list(browser.contexts)
        if not contexts:
            contexts = [browser.new_context()]
        page: Page | None = None
        context = contexts[0]
        for ctx in contexts:
            page = self._pick_cdp_page(ctx)
            if page is not None:
                context = ctx
                break
        if page is None:
            for ctx in contexts:
                page = self._pick_cdp_page_any(ctx)
                if page is not None:
                    context = ctx
                    break
        if page is None:
            if context.pages:
                page = context.pages[0]
                logger.info("cdp: use first tab url=%s", page.url)
            else:
                page = context.new_page()
                logger.info("cdp: opened new tab")
        self._browser_info = self._browser_info or f"cdp:{self.cdp_url}"
        self._attached_cdp = True
        return browser, context, page

    def _start_unlocked(self) -> None:
        if self._page is not None:
            return
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        try:
            if self.cdp_url:
                # Real Chrome started by the user — Cloudflare is much less hostile.
                self._browser, self._context, self._page = self._connect_cdp(
                    self._playwright
                )
            else:
                self._browser = None
                self._context = self._launch_persistent_context(self._playwright)
                if self._context.pages:
                    self._page = self._context.pages[0]
                else:
                    self._page = self._context.new_page()
                self._attached_cdp = False

            self._page.set_default_timeout(self.timeout_ms)
            if not self._attached_cdp:
                self._page.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = window.chrome || { runtime: {} };
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
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
            state = self._detect_shell_state(self._page)
            if state != "chat":
                self._page.goto(CHAT_URL, wait_until="domcontentloaded")
                self._wait_human_challenge(self._page)
                state = self._wait_shell_state(self._page, timeout_ms=60_000)
            logger.debug(
                "chat page ready state=%s url=%s cdp=%s",
                state,
                self._page.url,
                bool(self.cdp_url),
            )
        except Exception:
            self._stop_unlocked()
            raise

    def _stop_unlocked(self) -> None:
        # CDP attach: do NOT close the user's real Chrome — only disconnect.
        if self._attached_cdp:
            self._page = None
            self._context = None
            self._browser = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self._playwright = None
            self._attached_cdp = False
            logger.info("disconnected from CDP Chrome (browser left running)")
            return

        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                self._browser.close()
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
        state = self._detect_shell_state(page)
        if state == "chat":
            return True
        if state != "auth" and state != "unknown":
            return False

        if self.manual_login:
            if self.cdp_url:
                self._stop_unlocked()
                try:
                    self._wait_manual_login_via_cdp()
                    page = self._ensure_page_unlocked()
                    if self._detect_shell_state(page) == "chat":
                        self._save_storage_unlocked()
                        logger.info("manual login ok, storage/profile saved url=%s", page.url)
                        return True
                    return False
                except RuntimeError:
                    return False
            return self._wait_manual_login(page)

        if not self.auto_login:
            logger.warning("auth page but CHATGPT_AUTO_LOGIN disabled")
            return False
        if not self.username or not self.password:
            logger.warning("auth page but CHATGPT_USERNAME/CHATGPT_PASSWORD missing in .env")
            return False
        try:
            self._password_login(page)
        except Exception as exc:
            logger.exception("password login failed: %s", exc)
            return False
        state = self._wait_shell_state(page, timeout_ms=90_000)
        if state == "chat":
            self._save_storage_unlocked()
            logger.info("password login ok, storage saved")
            return True
        logger.error("password login finished but state=%s url=%s", state, page.url)
        return False

    def _wait_manual_login(self, page: Page) -> bool:
        """Open login page and wait until the user finishes auth in the window."""
        logger.warning(
            "ChatGPT manual login: 请在弹出的浏览器窗口完成登录"
            "（含真人验证）。完成后会自动继续。 timeout_s=%s profile=%s",
            self.captcha_timeout_s,
            self.user_data_dir,
        )
        try:
            if self._detect_shell_state(page) != "chat":
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("open login page failed: %s", exc)

        deadline = time.time() + max(60, self.captcha_timeout_s)
        last_log = 0.0
        while time.time() < deadline:
            try:
                self._wait_human_challenge(page, timeout_s=min(30, self.captcha_timeout_s))
            except RuntimeError:
                # Keep waiting for the full manual-login window.
                pass
            state = self._detect_shell_state(page)
            if state == "chat":
                self._save_storage_unlocked()
                logger.info("manual login ok, storage/profile saved url=%s", page.url)
                return True
            now = time.time()
            if now - last_log > 20:
                logger.info(
                    "waiting for manual login… state=%s remaining=%.0fs url=%s",
                    state,
                    deadline - now,
                    page.url,
                )
                last_log = now
            time.sleep(1.5)

        logger.error(
            "manual login timeout state=%s url=%s",
            self._detect_shell_state(page),
            page.url,
        )
        return False

    def _click_continue(self, page: Page) -> bool:
        """Click Continue / 继续, never social / phone SSO buttons."""
        candidates = page.locator(
            'button:visible, [role="button"]:visible, button[type="submit"]:visible'
        )
        # Exact-ish labels only; do NOT match "使用电话号码继续".
        exact_ok = {
            "continue",
            "继续",
            "next",
            "下一步",
            "log in",
            "login",
            "登录",
            "sign in",
            "使用密码继续",
            "用密码继续",
            "continue with password",
            "log in with password",
        }
        try:
            count = min(candidates.count(), 20)
        except Exception:
            count = 0
        for i in range(count):
            btn = candidates.nth(i)
            try:
                text = (btn.inner_text() or "").strip()
            except Exception:
                continue
            low = text.lower().strip()
            if any(
                bad in low
                for bad in (
                    "google",
                    "apple",
                    "microsoft",
                    "github",
                    "phone",
                    "电话",
                    "手机",
                    "sms",
                )
            ):
                continue
            # Accept exact label, or "Continue" with minor suffix like "Continue →"
            if low in exact_ok or low.rstrip("→›>") in exact_ok:
                try:
                    btn.click(timeout=8_000)
                    logger.info("clicked continue-like button text=%r", text)
                    return True
                except Exception:
                    continue
        submit = page.locator('button[type="submit"]:visible')
        try:
            if submit.count() > 0:
                text = (submit.first.inner_text() or "").strip().lower()
                if not any(
                    bad in text
                    for bad in ("google", "apple", "microsoft", "phone", "电话", "手机")
                ):
                    submit.first.click(timeout=8_000)
                    logger.info("clicked submit button text=%r", text)
                    return True
        except Exception:
            pass
        return False

    def _prefer_email_login(self, page: Page) -> None:
        """If SSO screen is shown, switch to email/password login."""
        labels = [
            "Continue with email",
            "Log in with email",
            "使用邮箱",
            "邮箱登录",
            "电子邮件",
            "Email",
        ]
        for label in labels:
            loc = page.locator(
                f'button:has-text("{label}"), a:has-text("{label}"), '
                f'[role="button"]:has-text("{label}")'
            )
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5_000)
                    logger.info("chose email login via %r", label)
                    time.sleep(1.0)
                    return
            except Exception:
                continue

    def _click_password_continue(self, page: Page) -> bool:
        """Prefer「使用密码继续」/ Continue with password over phone/SSO."""
        labels = [
            "使用密码继续",
            "用密码继续",
            "密码继续",
            "Continue with password",
            "Log in with password",
            "Password",
        ]
        for label in labels:
            loc = page.locator(
                f'button:has-text("{label}"), a:has-text("{label}"), '
                f'[role="button"]:has-text("{label}"), '
                f'div[role="button"]:has-text("{label}")'
            )
            try:
                if loc.count() == 0:
                    continue
                btn = loc.first
                if not btn.is_visible():
                    continue
                text = (btn.inner_text() or "").strip()
                # Skip phone variants even if they partially match.
                low = text.lower()
                if any(bad in low for bad in ("电话", "手机", "phone", "sms")):
                    continue
                btn.click(timeout=8_000)
                logger.info("clicked password continue text=%r", text)
                time.sleep(1.0)
                return True
            except Exception:
                continue
        return False

    def _password_login(self, page: Page) -> None:
        assert self.username and self.password
        logger.info("password login as %s", self.username)

        if self._detect_shell_state(page) == "chat":
            return

        # Always enter the dedicated login entry; avoid guest-page SSO shortcuts.
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        self._wait_human_challenge(page)
        time.sleep(1.0)

        # Some builds show a Log in button again inside auth shell.
        login_btn = page.locator(
            'button:has-text("Log in"), a:has-text("Log in"), '
            'button:has-text("登录"), a:has-text("登录"), '
            '[data-testid="login-button"]'
        )
        try:
            # Prefer a button that is NOT "Log in with Google".
            count = min(login_btn.count(), 8)
            for i in range(count):
                btn = login_btn.nth(i)
                if not btn.is_visible():
                    continue
                text = (btn.inner_text() or "").lower()
                if any(bad in text for bad in ("google", "apple", "microsoft")):
                    continue
                btn.click(timeout=8_000)
                logger.info("clicked login entry text=%r", text)
                time.sleep(1.0)
                break
        except Exception:
            pass

        self._prefer_email_login(page)
        # Method choice screen: 使用密码继续 vs 使用电话号码继续
        self._click_password_continue(page)
        self._wait_human_challenge(page)

        # Email step (Auth0 / auth.openai.com)
        email = page.locator(
            'input[name="email"]:visible, input[id="email-input"]:visible, '
            'input[id="email"]:visible, input[type="email"]:visible, '
            'input[autocomplete="email"]:visible, '
            'input[autocomplete="username"]:visible, '
            'input[placeholder*="email" i]:visible, '
            'input[placeholder*="邮箱"]:visible'
        )
        deadline = time.time() + 45
        while time.time() < deadline and email.count() == 0:
            if "accounts.google.com" in (page.url or "").lower():
                raise RuntimeError(
                    "login redirected to Google OAuth; use email/password account "
                    "or complete Google login manually in headed mode"
                )
            self._prefer_email_login(page)
            self._click_password_continue(page)
            time.sleep(0.5)
        if email.count() == 0:
            raise RuntimeError(
                f"login email input not found; url={page.url!r} title={page.title()!r}"
            )
        email.first.fill(self.username, timeout=15_000)
        # After email: prefer 使用密码继续, then generic Continue.
        if not self._click_password_continue(page):
            if not self._click_continue(page):
                raise RuntimeError("continue button not found after email")
        time.sleep(1.2)

        if "accounts.google.com" in (page.url or "").lower():
            raise RuntimeError(
                "after email continue, redirected to Google OAuth; "
                "refusing to auto-fill Google password"
            )

        # Password step may still show the method chooser first.
        self._click_password_continue(page)

        # Password step on auth.openai.com (not Google hidden password fields)
        pw = page.locator(
            'input[name="password"]:visible, '
            'input[type="password"]:visible:not([aria-hidden="true"])'
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            url = (page.url or "").lower()
            if "accounts.google.com" in url:
                raise RuntimeError("password step redirected to Google OAuth")
            if self._click_password_continue(page):
                time.sleep(0.5)
            if pw.count() > 0:
                break
            time.sleep(0.5)
        if pw.count() == 0:
            raise RuntimeError(
                f"password input not found; url={page.url!r} title={page.title()!r}"
            )
        pw.first.fill(self.password, timeout=15_000)
        if not self._click_password_continue(page):
            if not self._click_continue(page):
                raise RuntimeError("continue button not found after password")

        # OpenAI often shows Cloudflare / 真人验证 here — stop clicking and wait.
        self._wait_human_challenge(page)

        deadline = time.time() + max(90, self.captcha_timeout_s)
        while time.time() < deadline:
            if self._is_human_challenge(page):
                self._wait_human_challenge(page)
                continue
            if self._detect_shell_state(page) == "chat":
                return
            for label in ("Skip", "跳过", "Okay", "好的", "Got it"):
                loc = page.locator(f'button:has-text("{label}")')
                try:
                    if loc.count() == 0 or not loc.first.is_visible():
                        continue
                    loc.first.click(timeout=2_000)
                    time.sleep(0.5)
                    break
                except Exception:
                    pass
            time.sleep(0.5)
        raise RuntimeError(f"login submit did not reach chat UI; url={page.url!r}")


    def _save_storage_unlocked(self) -> None:
        if self._context is None:
            return
        state = self._context.storage_state()
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

    def _is_human_challenge(self, page: Page) -> bool:
        """Cloudflare / Turnstile / OpenAI 真人验证 interstitial."""
        try:
            title = (page.title() or "").strip()
        except Exception:
            title = ""
        title_l = title.lower()
        if (
            "请稍候" in title
            or "just a moment" in title_l
            or "attention required" in title_l
            or "verify you are human" in title_l
        ):
            return True
        try:
            hit = page.evaluate(
                """() => {
                    const t = ((document.body && document.body.innerText) || '').slice(0, 2000);
                    if (/请稍候|真人验证|人机验证|Verify you are human|Just a moment|Checking your browser/i.test(t)) {
                        return true;
                    }
                    if (document.querySelector(
                        '#challenge-form, #challenge-running, .cf-turnstile, iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]'
                    )) {
                        return true;
                    }
                    const bodyLen = (document.body && document.body.innerText || '').trim().length;
                    if (bodyLen === 0 && /chatgpt\\.com|openai\\.com|auth\\.openai/.test(location.href)) {
                        return true;
                    }
                    return false;
                }"""
            )
            return bool(hit)
        except Exception:
            return False

    def _wait_human_challenge(self, page: Page, timeout_s: int | None = None) -> None:
        """
        When OpenAI shows 真人验证 / Cloudflare: stop automation and wait.
        In headed mode, user completes the challenge manually in the browser window.
        """
        if not self._is_human_challenge(page):
            return
        timeout_s = int(timeout_s if timeout_s is not None else self.captcha_timeout_s)
        mode = "headed-manual" if not self.headless else "auto-wait"
        logger.warning(
            "human verification detected (%s). "
            "请在弹出的浏览器窗口里完成真人验证/Cloudflare，完成后会自动继续。 "
            "url=%s title=%r wait_s=%s",
            mode,
            page.url,
            page.title(),
            timeout_s,
        )
        deadline = time.time() + max(30, timeout_s)
        last_log = 0.0
        while time.time() < deadline:
            time.sleep(1.5)
            if not self._is_human_challenge(page):
                logger.info(
                    "human verification cleared url=%s title=%r",
                    page.url,
                    page.title(),
                )
                time.sleep(0.8)
                return
            now = time.time()
            if now - last_log > 20:
                logger.info(
                    "still waiting for human verification… remaining=%.0fs url=%s",
                    deadline - now,
                    page.url,
                )
                last_log = now
        raise RuntimeError(
            "human verification not completed in time; "
            f"url={page.url!r} title={page.title()!r}. "
            "有界面模式下请手动完成验证后重试；登录成功后会话会写入 "
            f"profile={self.user_data_dir}"
        )

    def _wait_cloudflare(self, page: Page, timeout_ms: int = 90_000) -> None:
        """Backward-compatible alias → human challenge wait."""
        self._wait_human_challenge(page, timeout_s=max(30, int(timeout_ms / 1000)))

    def _detect_shell_state(self, page: Page) -> str:
        if self._is_human_challenge(page):
            return "unknown"

        # Guest landing shows Login / 登录; treat as auth even if a composer exists.
        if self._has_visible(page, SIGN_IN_SELECTORS):
            # Fully logged-in chat: prompt + profile, and no login CTA.
            if self._has_visible(page, CHAT_READY_SELECTORS) and self._has_visible(
                page, LOGGED_IN_SELECTORS
            ):
                return "chat"
            return "auth"

        if self._has_visible(page, CHAT_READY_SELECTORS):
            return "chat"

        url = (page.url or "").lower()
        if any(
            token in url
            for token in (
                "auth/login",
                "auth.openai.com",
                "accounts.google.com",
                "/login",
                "sign-in",
                "signin",
            )
        ):
            return "auth"
        try:
            hint = page.evaluate(
                """() => {
                    const t = (document.body && document.body.innerText || '');
                    if (/免费注册|Sign up for free/i.test(t) && /登录|Log in/i.test(t)) {
                        return 'auth';
                    }
                    return null;
                }"""
            )
            if hint == "auth":
                return "auth"
        except Exception:
            pass
        return "unknown"

    def _wait_shell_state(self, page: Page, timeout_ms: int = 60_000) -> str:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self._is_human_challenge(page):
                # Don't burn the shell timeout on captcha; wait explicitly once.
                try:
                    self._wait_human_challenge(page)
                except RuntimeError:
                    return "unknown"
                continue
            state = self._detect_shell_state(page)
            if state != "unknown":
                return state
            time.sleep(0.25)
        return self._detect_shell_state(page)

    def _wait_composer_ready(self, page: Page, timeout_s: float = 15) -> None:
        deadline = time.time() + timeout_s
        last_reason = "missing"
        while time.time() < deadline:
            try:
                state = page.evaluate(COMPOSER_READY_JS)
            except Exception:
                state = {"ready": False, "reason": "evaluate_failed"}
            if state.get("ready"):
                return
            last_reason = str(state.get("reason") or "missing")
            time.sleep(0.25)
        raise RuntimeError(f"composer not ready ({last_reason})")

    def _textarea(self, page: Page):
        self._wait_composer_ready(page)
        loc = page.locator(COMPOSER_LOCATOR)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
        raise RuntimeError("message composer not found")

    def _enter_message(self, page: Page, message: str) -> None:
        self._wait_composer_ready(page)
        # ProseMirror / contenteditable: fill() often does not update React state.
        inserted = page.evaluate(
            """(msg) => {
                const el = document.querySelector('#prompt-textarea[contenteditable="true"]')
                    || document.querySelector('[data-testid="prompt-textarea"][contenteditable="true"]')
                    || document.querySelector('div[contenteditable="true"][data-testid="prompt-textarea"]');
                if (!el) return false;
                el.focus();
                try {
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, msg);
                } catch (e) {
                    el.textContent = msg;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: msg, inputType: 'insertText' }));
                }
                return (el.innerText || el.textContent || '').trim().length > 0;
            }""",
            message,
        )
        if not inserted:
            box = self._textarea(page)
            try:
                box.click(timeout=3_000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    f"composer not clickable: {str(exc).split(chr(10), 1)[0]}"
                ) from None
            time.sleep(0.2)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(message, delay=8)
        # Wait until composer has text / send button enables.
        deadline = time.time() + 10
        while time.time() < deadline:
            has_text = page.evaluate(
                """() => {
                    const el = document.querySelector('#prompt-textarea[contenteditable="true"]')
                        || document.querySelector('[data-testid="prompt-textarea"][contenteditable="true"]')
                        || document.querySelector('div[contenteditable="true"][data-testid="prompt-textarea"]');
                    return !!(el && (el.innerText || el.textContent || '').trim());
                }"""
            )
            if has_text:
                break
            time.sleep(0.2)
        logger.info("composer text ready inserted=%s", bool(inserted))

    def _send_message(self, page: Page) -> None:
        # Send button usually appears only after there is text.
        deadline = time.time() + 8
        while time.time() < deadline:
            for selector in SEND_SELECTORS:
                loc = page.locator(selector)
                try:
                    if loc.count() == 0:
                        continue
                    btn = loc.first
                    if not btn.is_visible():
                        continue
                    disabled = btn.get_attribute("disabled")
                    aria = btn.get_attribute("aria-disabled")
                    if disabled is not None or aria == "true":
                        continue
                    btn.click(timeout=5_000)
                    logger.info("clicked send via %s", selector)
                    return
                except Exception:
                    continue
            time.sleep(0.25)

        logger.info("send button not found; pressing Enter")
        try:
            self._textarea(page).press("Enter")
        except Exception:
            page.keyboard.press("Enter")

    def _open_new_chat(self, page: Page) -> None:
        # Avoid clicking bare a[href="/"] (logo) which is flaky on chatgpt.com.
        for selector in NEW_CHAT_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5_000)
                    time.sleep(0.8)
                    logger.info("opened new chat via %s", selector)
                    return
            except Exception:
                continue
        logger.info("new chat fallback goto %s", CHAT_URL)
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        self._wait_shell_state(page, timeout_ms=30_000)

    def _assistant_count(self, page: Page) -> int:
        try:
            return page.locator(ASSISTANT_SELECTOR).count()
        except Exception:
            return 0

    def _last_assistant_text(self, page: Page) -> str:
        text = page.evaluate(
            """() => {
                const phrases = [
                    'ChatGPT said:', 'ChatGPT said',
                    'Pro thinking', 'Answer now',
                    'Extended thinking', 'Show thinking', 'Hide thinking',
                    'Reasoning', 'Thinking...', 'Thinking…',
                ];
                const clean = (raw) => {
                    let t = (raw || '').trim();
                    for (const p of phrases) {
                        while (t.includes(p)) t = t.replace(p, '');
                    }
                    t = t.replace(/^Thinking\\s*/i, '');
                    t = t.replace(/\\s+/g, ' ').trim();
                    return t;
                };
                const msgs = document.querySelectorAll(
                    '[data-message-author-role="assistant"]'
                );
                if (msgs.length > 0) {
                    const last = msgs[msgs.length - 1];
                    const md = last.querySelector('.markdown, .prose, [class*="markdown"]');
                    const raw = (md ? md.innerText : last.innerText) || '';
                    const cleaned = clean(raw);
                    if (cleaned) return cleaned;
                }
                const turns = document.querySelectorAll(
                    '[data-testid^="conversation-turn-"]'
                );
                if (turns.length > 0) {
                    const last = turns[turns.length - 1];
                    return clean(last.innerText || '');
                }
                return '';
            }"""
        )
        return (text or "").strip()

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
        # Copy button on last turn often appears only after completion.
        try:
            done = page.evaluate(
                """() => {
                    const turns = document.querySelectorAll(
                        '[data-testid^="conversation-turn-"]'
                    );
                    if (!turns.length) return false;
                    const last = turns[turns.length - 1];
                    return !!last.querySelector('[data-testid="copy-turn-action-button"]');
                }"""
            )
            if done:
                return False
        except Exception:
            pass
        return False

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
            if saw_new and text and not generating and stable >= need_stable:
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


_client: ChatGPTClient | None = None
_client_lock = threading.Lock()


def get_client() -> ChatGPTClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = ChatGPTClient()
        return _client
