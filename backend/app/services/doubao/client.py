"""Playwright client for www.doubao.com.

Login is QR-only. A successful scan is kept in a persistent Chrome profile and
copied into sqlite, so later startups skip the QR until that session dies.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import queue
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
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


class DoubaoReplyError(RuntimeError):
    """The page finished the completion stream with an error, not a timeout."""

    def __init__(self, message: str, *, status: int = 502, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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


def _browser_user_agent(version: str = "131.0.0.0") -> str:
    major = version.split(".", 1)[0] or "131"
    if platform.system() == "Darwin":
        platform_name = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform_name = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_name}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _chrome_version(executable: str) -> str:
    try:
        out = subprocess.check_output([executable, "--version"], text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "131.0.0.0"
    parts = out.strip().split()
    return parts[-1] if parts else "131.0.0.0"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _repair_utf8(text: str) -> str:
    """Undo UTF-8 bytes that were decoded as Windows-1252, keeping undefined bytes."""
    if not text:
        return text
    raw = bytearray()
    for char in text:
        try:
            encoded = char.encode("cp1252")
        except UnicodeEncodeError:
            encoded = b""
        if len(encoded) == 1:
            raw.append(encoded[0])
            continue
        if ord(char) < 256:
            raw.append(ord(char))
            continue
        return text
    try:
        repaired = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return text
    return repaired or text


def _stream_error_message(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _repair_utf8(raw.strip()) or "doubao reply failed"
    if isinstance(data, dict):
        message = str(data.get("error_msg") or data.get("message") or "").strip()
        if message:
            return _repair_utf8(message)
    return _repair_utf8(raw.strip()) or "doubao reply failed"


def format_qr_for_log(payload: str) -> str:
    """Terminal QR from qrcode.print_ascii. Low ECC and a short border keep it smaller."""
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        border=2,
    )
    code.add_data(payload)
    code.make(fit=True)
    buf = io.StringIO()
    # print_ascii(tty=True) refuses non-ttys; the log is tailed in a terminal.
    buf.isatty = lambda: True  # type: ignore[method-assign]
    code.print_ascii(out=buf, tty=True)
    return buf.getvalue().rstrip("\n")


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
        self._browser = None
        self._chrome_proc: subprocess.Popen[bytes] | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._stream_error = ""
        self._stream_ended = False
        self._pending_completion = None

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
        timeout_s = max(10, int(timeout_s))
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
                if self._logged_in(page) or self._composer_ready(page):
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
        self._restore_saved_session()
        page = self._page
        assert page is not None
        if db_mgr.has_browser_session(PROVIDER) and self._wait_until_logged_in(page, timeout_s=8):
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

    def _chrome_executable(self) -> str | None:
        candidate = self.executable_path
        if candidate and os.path.isfile(candidate):
            return candidate
        mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(mac):
            return mac
        for name in ("google-chrome", "google-chrome-stable", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def _launch(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        chrome = self._chrome_executable()
        if chrome:
            self._launch_cdp(chrome)
            return
        self._launch_playwright()

    def _launch_cdp(self, chrome: str) -> None:
        # Playwright's own launcher advertises HeadlessChrome and Doubao answers with a captcha.
        port = _free_port()
        user_agent = _browser_user_agent(_chrome_version(chrome))
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.user_data_dir}",
            f"--user-agent={user_agent}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
            "--lang=zh-CN",
            "about:blank",
        ]
        if platform.system() != "Darwin":
            # This host's Chrome has no usable sandbox. The other providers already start it this way.
            args.insert(1, "--no-sandbox")
        # --headless and HeadlessChrome both make Doubao show a verify captcha.
        # A normal Chrome window, then CDP, returns the reply.
        prefix: list[str] = []
        if platform.system() != "Darwin" and not os.environ.get("DISPLAY"):
            xvfb = shutil.which("xvfb-run")
            if not xvfb:
                raise RuntimeError(
                    "doubao needs a display or xvfb-run; headless chrome is rejected by the site"
                )
            prefix = [xvfb, "-a", "--server-args=-screen 0 1280x900x24"]
        self._chrome_proc = subprocess.Popen(
            prefix + args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._wait_cdp(port)
        assert self._playwright is not None
        browser = self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        self._browser = browser
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        self._context = context
        page = context.pages[0] if context.pages else context.new_page()
        self._browser_info = f"cdp:{chrome}"
        self._prepare_page(page)
        logger.info(
            "doubao browser started profile=%s headless=%s via=%s",
            self.user_data_dir,
            self.headless,
            self._browser_info,
        )

    def _wait_cdp(self, port: int) -> None:
        deadline = time.time() + 20
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline:
            if self._chrome_proc is not None and self._chrome_proc.poll() is not None:
                raise RuntimeError("doubao chrome exited before the debugging port opened")
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError(f"doubao chrome debugging port {port} did not open")

    def _launch_playwright(self) -> None:
        assert self._playwright is not None
        playwright = self._playwright
        common: dict[str, Any] = {
            "headless": self.headless,
            "viewport": {"width": 1280, "height": 900},
            "locale": "zh-CN",
            "user_agent": _browser_user_agent(),
            "args": [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        user_data = str(self.user_data_dir)
        context = playwright.chromium.launch_persistent_context(user_data, **common)
        self._browser_info = "persistent-bundled:chromium"
        self._context = context
        page = context.pages[0] if context.pages else context.new_page()
        self._prepare_page(page)
        logger.info(
            "doubao browser started profile=%s headless=%s via=%s",
            user_data,
            self.headless,
            self._browser_info,
        )

    def _prepare_page(self, page: Page) -> None:
        page.set_default_timeout(self.timeout_ms)
        # Patching webdriver or aborting logout from Playwright makes Doubao show a verify captcha.
        # Chrome started outside Playwright already has webdriver unset.
        page.on("response", self._on_response)
        self._page = page

    def _close_browser(self) -> None:
        context = self._context
        browser = self._browser
        playwright = self._playwright
        proc = self._chrome_proc
        self._page = None
        self._context = None
        self._browser = None
        self._chrome_proc = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                logger.warning("doubao context close failed: %s", exc)
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                logger.warning("doubao browser close failed: %s", exc)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                logger.warning("doubao playwright stop failed: %s", exc)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception as exc:
                logger.warning("doubao chrome stop failed: %s", exc)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
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
            dialog = page.locator('[role="dialog"]')
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
            close = page.get_by_role("button", name="关闭")
            if close.count() > 0 and close.first.is_visible():
                close.first.click(timeout=2_000)
                logger.info("doubao: dismissed dialog")
        except Exception as exc:
            logger.debug("doubao promo dismiss skipped: %s", exc)

    def _logged_in(self, page: Page) -> bool:
        login = page.locator('[data-testid="to_login_button"]')
        try:
            # Hidden still counts. The login dialog hides the button while the user is logged out.
            if login.count() > 0:
                return False
        except Exception:
            return False
        return self._composer_ready(page)

    def _composer_ready(self, page: Page) -> bool:
        composer = page.locator('[data-testid="chat_input"] .ProseMirror')
        try:
            return composer.count() > 0 and composer.first.is_visible()
        except Exception:
            return False

    @staticmethod
    def _abort_logout(route) -> None:
        logger.info("doubao: blocked passport logout")
        route.abort()

    def _on_response(self, response) -> None:
        # Reading the body here deadlocks the sync dispatcher until the stream ends.
        if "/chat/completion" not in response.url:
            return
        self._pending_completion = response

    def _drain_completion(self) -> None:
        response = self._pending_completion
        if response is None:
            return
        self._pending_completion = None
        try:
            payload = response.body().decode("utf-8", "replace")
        except Exception:
            logger.debug("doubao completion body unreadable", exc_info=True)
            return
        self._consume_completion(payload)

    def _reset_stream(self) -> None:
        self._stream_error = ""
        self._stream_ended = False
        self._pending_completion = None

    def _consume_completion(self, payload: str) -> None:
        event = ""
        for line in payload.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line.split(":", 1)[1].strip()
            if event == "STREAM_ERROR":
                self._stream_error = _stream_error_message(raw)
                logger.info("doubao: completion error %s", self._stream_error)
            elif event == "SSE_REPLY_END":
                self._stream_ended = True

    def _reply_error(self, message: str) -> DoubaoReplyError:
        text = (message or "").strip() or "doubao reply failed"
        lowered = text.lower()
        retryable = "频繁" in text or "稍后重试" in text or "rate limit" in lowered
        status = 429 if retryable else 502
        return DoubaoReplyError(text, status=status, retryable=retryable)

    def _wait_until_logged_in(self, page: Page, *, timeout_s: int) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._logged_in(page):
                return True
            time.sleep(0.5)
        return False

    def _restore_saved_session(self) -> None:
        state = db_mgr.get_browser_session(PROVIDER)
        context = self._context
        page = self._page
        if not state or context is None or page is None:
            return
        cookies = state.get("cookies") or []
        try:
            if cookies:
                context.add_cookies(cookies)
            self._goto(page, CHAT_URL)
            self._apply_local_storage(page, state)
            self._goto(page, CHAT_URL)
            self._dismiss_promo(page)
            logger.info("doubao: applied saved session cookies=%s", len(cookies))
        except Exception as exc:
            logger.warning("doubao restore session failed: %s", exc)

    def _apply_local_storage(self, page: Page, state: dict[str, Any]) -> None:
        items: list[dict[str, Any]] = []
        for origin in state.get("origins") or []:
            if origin.get("origin") != "https://www.doubao.com":
                continue
            items.extend(origin.get("localStorage") or [])
        if not items:
            return
        page.evaluate(
            """(entries) => {
                for (const item of entries) {
                    if (!item || !item.name) continue;
                    localStorage.setItem(item.name, item.value || '');
                }
            }""",
            items,
        )

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
        return self._ask_once(
            page,
            question=question,
            conversation_id=conversation_id,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
        )

    def _ask_once(
        self,
        page: Page,
        *,
        question: str,
        conversation_id: str | None,
        mode: str,
        deep_thinking: bool,
        search: bool,
        timeout_s: int,
    ) -> dict[str, Any]:
        self._open_conversation(page, conversation_id)
        if search:
            logger.info("doubao: search follows deep thinking; no separate toggle")
        self._set_deep_thinking(page, deep_thinking)
        before = self._read_answer(page)
        self._reset_stream()
        self._send(page, question)
        self._drain_completion()
        if self._stream_error:
            raise self._reply_error(self._stream_error)
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
        if self._logged_in(page):
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
            self._goto(page, CHAT_URL)
            self._dismiss_promo(page)
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
        self._dismiss_promo(page)
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
        if self._stream_error or self._stream_ended:
            return
        if not self._wait_send_started(page):
            raise RuntimeError(f"doubao send did not start url={page.url}")

    def _wait_send_started(self, page: Page) -> bool:
        deadline = time.time() + 8
        while time.time() < deadline:
            self._drain_completion()
            if self._stream_error or self._stream_ended:
                return True
            try:
                url = page.url
            except Exception:
                page.wait_for_timeout(200)
                continue
            if self._conversation_id_from_url(url):
                return True
            page.wait_for_timeout(200)
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
        ended_since = 0.0
        while time.time() < deadline:
            if self._stop_event.is_set():
                break
            self._drain_completion()
            if self._stream_error:
                raise self._reply_error(self._stream_error)
            if self._stream_ended and ended_since == 0.0:
                ended_since = time.time()
            current = self._read_answer(page)
            if current and current != before and current.strip() != question.strip():
                if "频繁" in current or "稍后重试" in current:
                    raise self._reply_error(current)
                if current == stable:
                    settled = time.time() - stable_since >= 1.0 and not self._generating(page)
                    if settled or self._stream_ended:
                        return current
                else:
                    stable = current
                    stable_since = time.time()
            elif self._stream_ended and time.time() - ended_since >= 2.0:
                raise DoubaoReplyError(f"doubao reply ended without text url={page.url}")
            page.wait_for_timeout(300)
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
