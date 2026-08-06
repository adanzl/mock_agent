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
        self.deepseek_timeout_ms: int = int(os.getenv("DEEPSEEK_TIMEOUT_MS", "120000"))
        self.deepseek_username: str | None = _opt("DEEPSEEK_USERNAME") or _opt(
            "DEEPSEEK_EMAIL"
        )
        self.deepseek_password: str | None = _opt("DEEPSEEK_PASSWORD")
        self.deepseek_auto_login: bool = _bool("DEEPSEEK_AUTO_LOGIN", True)


config = Config()
