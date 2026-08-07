"""Diagnose fully-headless password login on chat.deepseek.com.

No storage_state is loaded (fresh context), so this exercises the real
headless login path the Linux/headless deployment would take. Reports what
happens after login submit: reaches chat textarea, captcha wall, or error.

用法:
  cd backend
  python -m scripts.inspect_headless_login
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from playwright.sync_api import sync_playwright

from app.config import config


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=config.chrome_path,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-notifications",
                "--lang=zh-CN",
            ],
            ignore_default_args=["--enable-automation"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="zh-CN"
        )
        page = ctx.new_page()
        page.set_default_timeout(15_000)
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            """
        )

        page.goto("https://chat.deepseek.com/sign_in", wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)
        print("URL after goto:", page.url)
        page.screenshot(path="headless_login_1.png")

        # Default UI is phone+SMS; switch to password login.
        pw_mode = page.locator("div.ds-button:visible:has-text('密码登录')")
        print("pw mode control count:", pw_mode.count())
        if pw_mode.count() == 0:
            pw_mode = page.locator(
                "div.ds-button:visible:has-text('Password'), "
                "button:visible:has-text('密码登录')"
            )
            print("alt pw mode control count:", pw_mode.count())
        if pw_mode.count() > 0:
            pw_mode.first.click(timeout=8_000)
            print("clicked password-login control")
        time.sleep(1.0)
        page.wait_for_selector("input[type='password']", timeout=20_000)
        print("password input visible")

        account = page.locator(
            "input[placeholder*='手机号/邮箱'], input[placeholder*='手机号'], "
            "input[placeholder*='邮箱'], input[type='email'], "
            "input[autocomplete='username'], input[type='tel']"
        )
        if account.count() == 0:
            account = page.locator("input[type='text']")
        print("account input count:", account.count())
        account.first.fill(config.deepseek_username)
        page.locator("input[type='password']").first.fill(config.deepseek_password)
        print("filled credentials")

        login_button = page.locator("div.ds-button--filled:visible:has-text('登录')")
        if login_button.count() == 0:
            login_button = page.locator(
                "div.ds-button--filled:has-text('Log in'), "
                "button:has-text('登录'), button:has-text('Log in')"
            )
        print("login button count:", login_button.count())
        login_button.first.click(timeout=10_000)
        print("clicked login submit")

        deadline = time.time() + 60
        reached_chat = False
        while time.time() < deadline:
            has_textarea = page.locator("textarea").count() > 0
            captcha = page.locator(
                "iframe[src*='captcha'], iframe[src*='verify'], "
                "[id*='captcha'], [id*='verify'], [class*='captcha'], "
                ".ds-captcha, .captcha"
            ).count()
            verify = page.locator(
                "text=验证码, text=安全验证, text=Please verify, text=are you a human"
            ).count()
            url = page.url
            print(
                f"  t={int(time.time()) % 100:02d} url={url[:70]} "
                f"textarea={has_textarea} captcha={captcha} verify_text={verify}"
            )
            if has_textarea:
                reached_chat = True
                break
            time.sleep(1)

        page.screenshot(path="headless_login_2.png")
        browser.close()
        print("REACHED_CHAT" if reached_chat else "DID_NOT_REACH_CHAT")


if __name__ == "__main__":
    main()
