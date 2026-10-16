"""Playwright client for www.doubao.com.

Login is QR-only. A successful scan is kept in a persistent Chrome profile and
copied into sqlite, so later startups skip the QR until that session dies.
"""

from __future__ import annotations

import io
import logging
import queue
import re
import threading
import time
from typing import Any

import qrcode
import zxingcpp
from PIL import Image
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from app.config import config
from app.repositories.database import db_mgr

logger = logging.getLogger(__name__)

PROVIDER = "doubao"
CHAT_URL = "https://www.doubao.com/chat/"
THREAD_URL = "https://www.doubao.com/chat/{conversation_id}"
CONVERSATION_ID_RE = re.compile(r"/chat/([^/?#]+)", re.IGNORECASE)
QR_REPRINT_S = 60
# The on-page mark is a dotted SVG. zxing reads it after a nearest-neighbor scale-up.
_QR_SCALE = 4

_ANSWER_SELECTORS = (
    '[data-testid="message_text_content"]',
    '[data-testid*="receive_message"]',
    '[data-testid*="message_text"]',
    '[data-testid="chat-route-layout"] [class*="markdown"]',
    '[data-testid="chat-route-layout"] [class*="paragraph"]',
)


def decode_qr_png(png: bytes) -> str | None:
    """Read a login QR screenshot. Returns the payload, or None."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image = image.resize(
        (image.width * _QR_SCALE, image.height * _QR_SCALE),
        Image.Resampling.NEAREST,
    )
    for item in zxingcpp.read_barcodes(image):
        text = (getattr(item, "text", "") or "").strip()
        if text:
            return text
    return None


# Braille dots, top to bottom. Each character holds 2 columns x 4 rows.
_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def format_qr_for_log(payload: str) -> str:
    """Compact terminal QR. qrcode builds the matrix; braille packs 2x4 modules per cell."""
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        border=4,
    )
    code.add_data(payload)
    code.make(fit=True)
    modules = code.modules
    count = code.modules_count
    border = code.border
    size = count + border * 2
    width = size + (size % 2)
    height = size + (-size % 4)

    def dark(row: int, col: int) -> bool:
        if row >= size or col >= size:
            return False
        r = row - border
        c = col - border
        if r < 0 or c < 0 or r >= count or c >= count:
            return False
        return bool(modules[r][c])

    fg = "\033[38;5;16m"
    bg = "\033[48;5;231m"
    reset = "\033[0m"
    lines: list[str] = []
    for y in range(0, height, 4):
        chars: list[str] = []
        for x in range(0, width, 2):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    if dark(y + dy, x + dx):
                        bits |= _BRAILLE_BITS[dy][dx]
            chars.append(chr(0x2800 + bits))
        lines.append(f"{fg}{bg}{''.join(chars)}{reset}")
    return "\n".join(lines)


class DoubaoClient:
    def __init__(self) -> None:
        self.headless = config.headless
        self.timeout_ms = config.doubao_timeout_ms
        self.login_timeout_s = config.doubao_login_timeout_s
        self.user_data_dir = config.doubao_user_data_dir
        self.browser_channel = config.browser_channel
        self.executable_path = config.chrome_path
        self._tasks: queue.Queue[tuple[Any, queue.Queue[tuple[bool, Any]]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._login_gave_up = False
        self._state = "stopped"
        self._browser_info = ""
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def status(self) -> dict[str, Any]:
        page = self._page
        url = None
        if page is not None:
            try:
                url = page.url
            except Exception:
                url = None
        return {
            "ready": self._state == "ready",
            "state": self._state,
            "url": url,
            "profile": str(self.user_data_dir),
            "session_saved": db_mgr.has_browser_session(PROVIDER),
            "login_gave_up": self._login_gave_up,
            "browser": self._browser_info or None,
        }

    def ensure_ready(self) -> dict[str, Any]:
        return self._submit(self._ensure_ready_impl)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is None or not self._thread.is_alive():
            self._close_browser()
            return
        try:
            self._submit(self._close_browser)
        except Exception:
            logger.exception("doubao stop failed")

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
        if images:
            raise ValueError("doubao image upload is not supported yet")
        if not question:
            raise ValueError("question is empty")
        if str(mode or "").strip().lower() in {"think", "thinking", "expert", "深度思考", "思考"}:
            deep_thinking = True
        if timeout_s is None:
            timeout_s = (
                config.doubao_think_timeout_s if deep_thinking else config.doubao_chat_timeout_s
            )
        timeout_s = max(30, int(timeout_s))
        return self._submit(
            lambda: self._ask_impl(
                question=question,
                conversation_id=conversation_id,
                mode=mode,
                deep_thinking=deep_thinking,
                search=search,
                timeout_s=timeout_s,
            )
        )

    def _submit(self, fn: Any) -> Any:
        self._ensure_thread()
        box: queue.Queue[tuple[bool, Any]] = queue.Queue(1)
        self._tasks.put((fn, box))
        ok, payload = box.get()
        if not ok:
            raise payload
        return payload

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="doubao-playwright",
                daemon=True,
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                fn, box = self._tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                box.put((True, fn()))
            except Exception as exc:
                box.put((False, exc))

    def _ensure_ready_impl(self) -> dict[str, Any]:
        if self._login_gave_up:
            return self._status_payload(ready=False, state="login_timeout")
        page = self._page
        if page is not None and self._context is not None:
            try:
                if self._logged_in(page):
                    self._state = "ready"
                    return self._status_payload(ready=True, state="ready")
            except Exception:
                logger.debug("doubao existing page check failed", exc_info=True)
        self._open_chat()
        page = self._page
        assert page is not None
        if self._logged_in(page):
            self._save_storage()
            self._state = "ready"
            logger.info("doubao: session reused profile=%s url=%s", self.user_data_dir, page.url)
            return self._status_payload(ready=True, state="ready")
        self._restore_saved_cookies()
        page = self._page
        assert page is not None
        if self._logged_in(page):
            self._save_storage()
            self._state = "ready"
            logger.info("doubao: session restored from sqlite url=%s", page.url)
            return self._status_payload(ready=True, state="ready")
        self._state = "waiting_login"
        if not self._wait_for_scan(page):
            self._login_gave_up = True
            self._close_browser()
            self._state = "login_timeout"
            raise TimeoutError(
                f"doubao qr login timeout after {self.login_timeout_s}s; browser closed"
            )
        page = self._page
        assert page is not None
        self._save_storage()
        self._state = "ready"
        logger.info("doubao: login saved profile=%s", self.user_data_dir)
        return self._status_payload(ready=True, state="ready")

    def _status_payload(self, *, ready: bool, state: str) -> dict[str, Any]:
        self._state = state
        url = None
        if self._page is not None:
            try:
                url = self._page.url
            except Exception:
                url = None
        return {
            "ok": True,
            "ready": ready,
            "state": state,
            "url": url,
            "profile": str(self.user_data_dir),
            "session_saved": db_mgr.has_browser_session(PROVIDER),
        }

    def _open_chat(self) -> None:
        if self._context is None or self._page is None:
            self._launch()
        page = self._page
        assert page is not None
        self._goto(page, CHAT_URL)
        self._dismiss_promo(page)
        try:
            page.locator('[data-testid="chat_input"] .ProseMirror').wait_for(
                state="visible",
                timeout=30_000,
            )
        except Exception as exc:
            logger.warning("doubao composer not visible yet: %s", exc)

    def _launch(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        playwright = self._playwright
        common: dict[str, Any] = {
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "args": [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-notifications",
                "--lang=zh-CN",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        errors: list[str] = []
        user_data = str(self.user_data_dir)
        context: BrowserContext | None = None
        if self.executable_path:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data,
                    executable_path=self.executable_path,
                    **common,
                )
                self._browser_info = f"persistent:{self.executable_path}"
            except Exception as exc:
                errors.append(f"executable_path={self.executable_path}: {exc}")
                logger.warning("doubao launch via executable_path failed: %s", exc)
        channel = self.browser_channel
        if context is None and channel and channel.lower() not in {"", "chromium", "bundled"}:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data,
                    channel=channel,
                    **common,
                )
                self._browser_info = f"persistent-channel:{channel}"
            except Exception as exc:
                errors.append(f"channel={channel}: {exc}")
                logger.warning("doubao launch via channel=%s failed: %s", channel, exc)
        if context is None:
            try:
                context = playwright.chromium.launch_persistent_context(user_data, **common)
                self._browser_info = "persistent-bundled:chromium"
            except Exception as exc:
                errors.append(f"bundled chromium: {exc}")
                detail = " | ".join(errors)
                self._close_browser()
                raise RuntimeError(f"failed to launch doubao browser; tried: {detail}") from exc
        self._context = context
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(self.timeout_ms)
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        self._page = page
        logger.info(
            "doubao browser started profile=%s headless=%s via=%s",
            user_data,
            self.headless,
            self._browser_info,
        )

    def _close_browser(self) -> None:
        context = self._context
        playwright = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                logger.warning("doubao context close failed: %s", exc)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                logger.warning("doubao playwright stop failed: %s", exc)
        if self._state != "login_timeout":
            self._state = "stopped"

    def _goto(self, page: Page, url: str) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("doubao goto attempt=%s url=%s error=%s", attempt, url, exc)
                time.sleep(1.0 * attempt)
        raise RuntimeError(f"doubao goto failed url={url}: {last_exc}")

    def _dismiss_promo(self, page: Page) -> None:
        try:
            download = page.get_by_role("button", name="立即下载")
            if download.count() == 0 or not download.first.is_visible():
                return
            close = page.get_by_role("button", name="关闭")
            if close.count() > 0 and close.first.is_visible():
                close.first.click(timeout=2_000)
                logger.info("doubao: dismissed download promo")
        except Exception as exc:
            logger.debug("doubao promo dismiss skipped: %s", exc)

    def _logged_in(self, page: Page) -> bool:
        login = page.locator('[data-testid="to_login_button"]')
        try:
            if login.count() > 0 and login.first.is_visible():
                return False
        except Exception:
            return False
        composer = page.locator('[data-testid="chat_input"] .ProseMirror')
        try:
            return composer.count() > 0 and composer.first.is_visible()
        except Exception:
            return False

    def _restore_saved_cookies(self) -> None:
        state = db_mgr.get_browser_session(PROVIDER)
        context = self._context
        page = self._page
        if not state or context is None or page is None:
            return
        cookies = state.get("cookies") or []
        if not cookies:
            return
        try:
            context.add_cookies(cookies)
            self._goto(page, CHAT_URL)
            self._dismiss_promo(page)
            logger.info("doubao: applied saved cookies count=%s", len(cookies))
        except Exception as exc:
            logger.warning("doubao restore cookies failed: %s", exc)

    def _save_storage(self) -> None:
        context = self._context
        if context is None:
            return
        try:
            db_mgr.save_browser_session(PROVIDER, context.storage_state())
        except Exception as exc:
            logger.warning("doubao save session failed: %s", exc)

    def _wait_for_scan(self, page: Page) -> bool:
        self._open_login_dialog(page)
        deadline = time.time() + max(30, self.login_timeout_s)
        last_payload = ""
        last_print = 0.0
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if self._logged_in(page):
                return True
            payload = self._read_qr_payload(page)
            now = time.time()
            if payload and (payload != last_payload or now - last_print >= QR_REPRINT_S):
                block = format_qr_for_log(payload)
                remaining = max(0, deadline - now)
                logger.info(
                    "doubao: scan qr remaining=%.0fs\n%s",
                    remaining,
                    block,
                )
                last_payload = payload
                last_print = now
            elif not payload and now - last_print >= 15:
                logger.info(
                    "doubao: qr not on page yet remaining=%.0fs url=%s",
                    max(0, deadline - now),
                    page.url,
                )
                last_print = now
                self._open_login_dialog(page)
            time.sleep(1.5)
        logger.error("doubao: qr login timeout; closing browser profile=%s", self.user_data_dir)
        return False

    def _open_login_dialog(self, page: Page) -> None:
        self._dismiss_promo(page)
        dialog = page.locator('[role="dialog"]')
        try:
            visible = dialog.count() > 0 and dialog.first.is_visible()
        except Exception:
            visible = False
        if not visible:
            button = page.locator('[data-testid="to_login_button"]')
            try:
                if button.count() > 0:
                    button.first.click(timeout=5_000)
                else:
                    page.get_by_role("button", name="登录", exact=True).click(timeout=5_000)
            except Exception as exc:
                logger.warning("doubao open login failed: %s", exc)
                return
            try:
                page.locator('[role="dialog"]').first.wait_for(state="visible", timeout=8_000)
            except Exception as exc:
                logger.warning("doubao login dialog not visible: %s", exc)
                return
        checkbox = page.locator('[role="dialog"] [role="checkbox"]')
        try:
            if checkbox.count() > 0 and checkbox.first.get_attribute("aria-checked") == "false":
                checkbox.first.click(timeout=3_000)
                logger.info("doubao: accepted login agreement")
        except Exception as exc:
            logger.warning("doubao agreement checkbox skipped: %s", exc)

    def _read_qr_payload(self, page: Page) -> str | None:
        loc = page.locator('[class*="qrcode-"]')
        try:
            if loc.count() == 0:
                return None
            png = loc.first.screenshot(type="png")
        except Exception as exc:
            logger.debug("doubao qr screenshot failed: %s", exc)
            return None
        try:
            return decode_qr_png(png)
        except Exception as exc:
            logger.debug("doubao qr decode failed: %s", exc)
            return None

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
        if self._login_gave_up:
            raise RuntimeError(
                "doubao login timed out; restart the service to scan again"
            )
        ready = self._ensure_ready_impl()
        if not ready.get("ready"):
            raise RuntimeError("doubao is not logged in")
        page = self._page
        assert page is not None
        self._open_conversation(page, conversation_id)
        if search:
            logger.info("doubao: search follows deep thinking; no separate toggle")
        self._set_deep_thinking(page, deep_thinking)
        before = self._read_answer(page)
        self._send(page, question)
        answer = self._wait_answer(page, before=before, timeout_s=timeout_s, question=question)
        conv = self._conversation_id_from_url(page.url)
        if not conv:
            raise RuntimeError(f"doubao reply finished but thread id missing url={page.url}")
        title = question[:40]
        db_mgr.upsert_conversation(
            conversation_id=conv,
            provider=PROVIDER,
            title=title,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            url=page.url,
        )
        db_mgr.add_conversation_messages(
            conv,
            [("user", question), ("assistant", answer)],
        )
        self._save_storage()
        return {
            "ok": True,
            "question": question,
            "answer": answer,
            "conversation_id": conv,
            "mode": mode,
            "deep_thinking": deep_thinking,
            "search": search,
            "url": page.url,
        }

    def _open_conversation(self, page: Page, conversation_id: str | None) -> None:
        if conversation_id:
            conv = conversation_id.strip()
            match = CONVERSATION_ID_RE.search(conv)
            if match:
                conv = match.group(1)
            if not conv:
                raise ValueError(f"invalid conversation_id: {conversation_id}")
            if self._conversation_id_from_url(page.url) != conv:
                self._goto(page, THREAD_URL.format(conversation_id=conv))
            return
        if self._conversation_id_from_url(page.url):
            button = page.locator('[data-testid="create_conversation_button"]')
            if button.count() > 0:
                button.first.click(timeout=5_000)
                page.wait_for_timeout(500)
                return
        if "doubao.com/chat" not in (page.url or ""):
            self._goto(page, CHAT_URL)

    def _set_deep_thinking(self, page: Page, enabled: bool) -> None:
        if not enabled:
            return
        button = page.locator('[data-testid="chat_input_action_model"]')
        try:
            if button.count() == 0:
                logger.info("doubao: deep thinking control not found")
                return
            button.first.click(timeout=3_000)
            option = page.get_by_text("深度思考", exact=False)
            if option.count() == 0:
                logger.info("doubao: deep thinking option not found")
                page.keyboard.press("Escape")
                return
            option.first.click(timeout=3_000)
            logger.info("doubao: deep thinking enabled")
        except Exception as exc:
            logger.warning("doubao deep thinking skipped: %s", exc)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    def _send(self, page: Page, question: str) -> None:
        composer = page.locator('[data-testid="chat_input"] .ProseMirror')
        composer.first.click(timeout=10_000)
        # execCommand leaves the editor marked empty, so Enter never sends.
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(question)
        send = page.locator('[data-testid="chat_input_send_button"]')
        try:
            send.first.wait_for(state="visible", timeout=8_000)
            send.first.click(timeout=5_000)
        except Exception as exc:
            logger.warning("doubao send button missed, pressing Enter: %s", exc)
            page.keyboard.press("Enter")
        if not self._wait_send_started(page):
            raise RuntimeError(f"doubao send did not start url={page.url}")

    def _wait_send_started(self, page: Page) -> bool:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                url = page.url
            except Exception:
                time.sleep(0.4)
                continue
            if self._conversation_id_from_url(url):
                return True
            try:
                if self._read_answer(page):
                    return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def _wait_answer(
        self,
        page: Page,
        *,
        before: str,
        timeout_s: int,
        question: str,
    ) -> str:
        deadline = time.time() + timeout_s
        stable = ""
        stable_since = 0.0
        while time.time() < deadline:
            if self._stop_event.is_set():
                break
            current = self._read_answer(page)
            if current and current != before and current.strip() != question.strip():
                if current == stable:
                    if time.time() - stable_since >= 2.0 and not self._generating(page):
                        return current
                else:
                    stable = current
                    stable_since = time.time()
            time.sleep(0.6)
        raise TimeoutError(f"doubao answer timeout after {timeout_s}s url={page.url}")

    def _generating(self, page: Page) -> bool:
        try:
            stop = page.get_by_role("button", name=re.compile("停止"))
            return stop.count() > 0 and stop.first.is_visible()
        except Exception:
            return False

    def _read_answer(self, page: Page) -> str:
        scoped = page.locator(
            '[data-testid="receive_message"] [data-testid="message_text_content"]'
        )
        try:
            count = scoped.count()
        except Exception:
            count = 0
        if count > 0:
            try:
                text = (scoped.nth(count - 1).inner_text() or "").strip()
            except Exception:
                text = ""
            if text:
                return text
        for selector in _ANSWER_SELECTORS:
            loc = page.locator(selector)
            try:
                count = loc.count()
            except Exception:
                continue
            if count <= 0:
                continue
            try:
                text = (loc.nth(count - 1).inner_text() or "").strip()
            except Exception:
                continue
            if text:
                return text
        return ""

    @staticmethod
    def _conversation_id_from_url(url: str) -> str | None:
        match = CONVERSATION_ID_RE.search(url or "")
        if not match:
            return None
        conv = match.group(1).strip()
        return conv or None


_client: DoubaoClient | None = None
_client_lock = threading.Lock()


def get_client() -> DoubaoClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = DoubaoClient()
        return _client
