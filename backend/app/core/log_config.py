"""CLI / Web 日志：控制台 + 文件。仿造 video_factory。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname_fixed)s] %(name)s:%(lineno)d: %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_CONFIGURED = False
_SERVER_CONFIGURED = False


class _AppLogFormatter(logging.Formatter):
    """levelname 固定 4 字符：过长截断，不足右侧补空格。"""

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_fixed = record.levelname[:4].ljust(4)
        return super().format(record)


def _formatter() -> logging.Formatter:
    return _AppLogFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)


def _rotating_file_handler(log_file: Path, *, retention_days: int) -> TimedRotatingFileHandler:
    """按自然日切割；retention_days 含当天，共保留最近 N 天。"""
    handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=max(0, retention_days - 1),
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(_formatter())
    return handler


def setup_logging(*, log_dir: Path, retention_days: int = 3) -> Path:
    """初始化根 logger：stderr + log_dir/worker.log（按天滚动）。"""
    global _CONFIGURED
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "worker.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if _CONFIGURED:
        return log_file

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_formatter())
    root.addHandler(console)

    file_handler = _rotating_file_handler(log_file, retention_days=retention_days)
    root.addHandler(file_handler)

    _CONFIGURED = True
    return log_file


def setup_server_logging(
    *,
    log_dir: Path,
    is_production: bool = False,
    retention_days: int = 3,
) -> tuple[logging.Logger, logging.Logger]:
    """初始化 Web 服务日志：app + app.access（HTTP 访问日志）。"""
    global _SERVER_CONFIGURED
    del is_production  # 与 video_factory 签名对齐，预留环境差异

    app_logger = logging.getLogger("app")
    access_logger = logging.getLogger("app.access")

    # Prevent repeated init (same idea as setup_logging / DbMgr).
    if _SERVER_CONFIGURED:
        return app_logger, access_logger

    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = _formatter()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    file_handler = _rotating_file_handler(log_dir / "app.log", retention_days=retention_days)

    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Flask/Werkzeug default access lines (GET /foo 404) are noisy in console.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app_logger.addHandler(console)
    app_logger.addHandler(file_handler)

    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    access_handler = _rotating_file_handler(log_dir / "access.log", retention_days=retention_days)
    access_handler.setFormatter(
        logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"),
    )
    access_logger.addHandler(access_handler)

    _SERVER_CONFIGURED = True
    return app_logger, access_logger
