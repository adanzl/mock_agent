"""统一配置：从项目根目录 .env 读取环境变量。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


def _bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _opt(key: str) -> str | None:
    val = os.getenv(key)
    return val.strip() if val and val.strip() else None


def _path(key: str, default: Path, *, root: Path = ROOT_DIR) -> Path:
    raw = os.getenv(key)
    if not raw:
        return default.resolve()
    path = Path(raw)
    return path if path.is_absolute() else (root / path).resolve()


class Config:
    """应用配置。"""

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        # override=False: process env (e.g. systemd Environment=) is
        # authoritative; .env only fills in keys that are not already set.
        # With override=True, a leftover .env PORT/HOST/ENV would silently
        # clobber the systemd values on the server.
        load_dotenv(ROOT_DIR / ".env")

        self.env: str = os.getenv("ENV", "development").strip().lower()
        self.is_production: bool = self.env == "production"
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = int(os.getenv("PORT", "8765"))
        self.flask_debug: bool = _bool("FLASK_DEBUG", False)

        self.root_dir: Path = ROOT_DIR
        self.backend_dir: Path = BACKEND_DIR
        self.log_dir: Path = _path("LOG_DIR", BACKEND_DIR / "logs", root=BACKEND_DIR)
        self.log_retention_days: int = int(os.getenv("LOG_RETENTION_DAYS", "3"))
        self.sqlite_path: Path = _path(
            "SQLITE_PATH",
            BACKEND_DIR / "data" / "data.db",
            root=BACKEND_DIR,
        )

        # Browser (shared)
        self.headless: bool = _bool("HEADLESS", True)
        self.browser_channel: str = (
            os.getenv("BROWSER_CHANNEL", "chrome").strip() or "chrome"
        )
        self.chrome_path: str | None = _opt("CHROME_PATH")

        # DeepSeek account / session
        # Master switch — on by default.
        self.deepseek_enabled: bool = _bool("DEEPSEEK_ENABLED", True)
        # Playwright UI action timeout (login/fill/click).
        self.deepseek_timeout_ms: int = int(os.getenv("DEEPSEEK_TIMEOUT_MS", "120000"))
        # Default wait for a normal chat reply (seconds).
        self.deepseek_chat_timeout_s: int = int(os.getenv("DEEPSEEK_CHAT_TIMEOUT_S", "300"))
        # Wait for expert / deep-thinking replies (seconds).
        self.deepseek_think_timeout_s: int = int(
            os.getenv("DEEPSEEK_THINK_TIMEOUT_S", "600")
        )
        # Parallel Playwright workers (each owns a browser page). Clamp 1..4.
        self.deepseek_workers: int = max(
            1, min(4, int(os.getenv("DEEPSEEK_WORKERS", "2")))
        )
        self.deepseek_username: str | None = _opt("DEEPSEEK_USERNAME") or _opt(
            "DEEPSEEK_EMAIL"
        )
        self.deepseek_password: str | None = _opt("DEEPSEEK_PASSWORD")
        self.deepseek_auto_login: bool = _bool("DEEPSEEK_AUTO_LOGIN", True)

        # ChatGPT account / session (browser via local proxy)
        # Master switch — off by default; set CHATGPT_ENABLED=1 to use GPT.
        self.chatgpt_enabled: bool = _bool("CHATGPT_ENABLED", False)
        self.chatgpt_proxy: str = (
            os.getenv("CHATGPT_PROXY", "http://127.0.0.1:7890").strip()
            or "http://127.0.0.1:7890"
        )
        self.chatgpt_timeout_ms: int = int(os.getenv("CHATGPT_TIMEOUT_MS", "120000"))
        self.chatgpt_chat_timeout_s: int = int(
            os.getenv("CHATGPT_CHAT_TIMEOUT_S", "300")
        )
        self.chatgpt_username: str | None = _opt("CHATGPT_USERNAME") or _opt(
            "CHATGPT_EMAIL"
        )
        self.chatgpt_password: str | None = _opt("CHATGPT_PASSWORD")
        # Manual login (headed browser, user completes auth). Default on.
        self.chatgpt_manual_login: bool = _bool("CHATGPT_MANUAL_LOGIN", True)
        # Password auto-fill only when CHATGPT_MANUAL_LOGIN=0.
        self.chatgpt_auto_login: bool = _bool("CHATGPT_AUTO_LOGIN", False)
        # Wait for manual login / Cloudflare / 真人验证 (seconds).
        self.chatgpt_captcha_timeout_s: int = int(
            os.getenv("CHATGPT_CAPTCHA_TIMEOUT_S", "600")
        )
        # Persistent Chrome profile reduces repeated human checks.
        self.chatgpt_user_data_dir: Path = _path(
            "CHATGPT_USER_DATA_DIR",
            BACKEND_DIR / "data" / "chatgpt-browser",
            root=BACKEND_DIR,
        )
        # Attach to a real Chrome via CDP (recommended for manual login).
        # Example: http://127.0.0.1:9222
        self.chatgpt_cdp_url: str | None = _opt("CHATGPT_CDP_URL")

        # Qwen account / session (chat.qwen.ai)
        # Master switch — off by default; set QWEN_ENABLED=1 to use.
        self.qwen_enabled: bool = _bool("QWEN_ENABLED", False)
        self.qwen_timeout_ms: int = int(os.getenv("QWEN_TIMEOUT_MS", "120000"))
        self.qwen_chat_timeout_s: int = int(os.getenv("QWEN_CHAT_TIMEOUT_S", "300"))
        self.qwen_think_timeout_s: int = int(os.getenv("QWEN_THINK_TIMEOUT_S", "600"))
        # Parallel Playwright workers (each owns a browser page). Clamp 1..4.
        self.qwen_workers: int = max(
            1, min(4, int(os.getenv("QWEN_WORKERS", "2")))
        )
        self.qwen_username: str | None = _opt("QWEN_USERNAME") or _opt("QWEN_EMAIL")
        self.qwen_password: str | None = _opt("QWEN_PASSWORD")
        self.qwen_auto_login: bool = _bool("QWEN_AUTO_LOGIN", True)


config = Config()
