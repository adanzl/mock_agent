import atexit
import os
import sys
import threading
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.chdir(BACKEND_DIR)

from app import app_logger, create_app
from app.config import config
from app.services.agnes.agnes_mgr import agnes_mgr
from app.services.chatgpt.chatgpt_mgr import chatgpt_mgr
from app.services.chat_jobs.runner import chat_job_runner
from app.services.deepseek.deepseek_mgr import deepseek_mgr
from app.services.qwen.qwen_mgr import qwen_mgr

log = app_logger

log.info("========== mock_agent ==========")
log.info(
    "env=%s host=%s:%s log=%s sqlite=%s",
    config.env,
    config.host,
    config.port,
    config.log_dir / "app.log",
    config.sqlite_path,
)

app = create_app()

# Async chat job worker (shared across providers).
chat_job_runner.start()


def _bool_env(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _bootstrap_deepseek() -> None:
    if not config.deepseek_enabled:
        log.info("deepseek: skipped (disabled)")
        return
    if not _bool_env("BOOTSTRAP_DEEPSEEK", True):
        log.info("deepseek: skipped (BOOTSTRAP_DEEPSEEK=0)")
        return
    try:
        result = deepseek_mgr.ensure_ready()
        workers = result.get("workers")
        ready_workers = result.get("ready_workers")
        log.info(
            "deepseek: ready state=%s workers=%s/%s session_saved=%s",
            result.get("state"),
            ready_workers if ready_workers is not None else "?",
            workers if workers is not None else "?",
            result.get("session_saved"),
        )
    except Exception:
        log.exception(
            "deepseek: startup login failed; chat will retry on first request"
        )


def _bootstrap_chatgpt() -> None:
    if not config.chatgpt_enabled:
        log.info("chatgpt: skipped (disabled)")
        return
    # Optional warm-up; default on when GPT is enabled.
    if not _bool_env("BOOTSTRAP_CHATGPT", True):
        log.info("chatgpt: skipped (BOOTSTRAP_CHATGPT=0)")
        return
    if not config.chatgpt_manual_login:
        if not config.chatgpt_username or not config.chatgpt_password:
            log.info("chatgpt: skipped (USERNAME/PASSWORD not set)")
            return
    else:
        log.info("chatgpt: manual login mode (headed browser)")
    try:
        result = chatgpt_mgr.ensure_ready()
        log.info(
            "chatgpt: ready state=%s session_saved=%s proxy=%s manual=%s",
            result.get("state"),
            result.get("session_saved"),
            result.get("proxy"),
            result.get("manual_login"),
        )
    except Exception:
        log.exception(
            "chatgpt: startup login failed; chat will retry on first request"
        )


def _bootstrap_qwen() -> None:
    if not config.qwen_enabled:
        log.info("qwen: skipped (disabled)")
        return
    if not _bool_env("BOOTSTRAP_QWEN", True):
        log.info("qwen: skipped (BOOTSTRAP_QWEN=0)")
        return
    if config.qwen_auto_login and (
        not config.qwen_username or not config.qwen_password
    ):
        log.info("qwen: skipped (USERNAME/PASSWORD not set)")
        return
    try:
        result = qwen_mgr.ensure_ready()
        workers = result.get("workers")
        ready_workers = result.get("ready_workers")
        log.info(
            "qwen: ready state=%s workers=%s/%s session_saved=%s",
            result.get("state"),
            ready_workers if ready_workers is not None else "?",
            workers if workers is not None else "?",
            result.get("session_saved"),
        )
    except Exception:
        log.exception(
            "qwen: startup login failed; chat will retry on first request"
        )


def _bootstrap_agnes() -> None:
    if not config.agnes_enabled:
        log.info("agnes: skipped (disabled)")
        return
    if not _bool_env("BOOTSTRAP_AGNES", True):
        log.info("agnes: skipped (BOOTSTRAP_AGNES=0)")
        return
    if config.agnes_auto_login and (
        not config.agnes_username or not config.agnes_password
    ):
        log.info("agnes: skipped (USERNAME/PASSWORD not set)")
        return
    log.info("agnes: warming up...")
    attempts = 3
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = agnes_mgr.ensure_ready()
            workers = result.get("workers")
            ready_workers = result.get("ready_workers")
            log.info(
                "agnes: ready state=%s workers=%s/%s session_saved=%s proxy=%s",
                result.get("state"),
                ready_workers if ready_workers is not None else "?",
                workers if workers is not None else "?",
                result.get("session_saved"),
                result.get("proxy"),
            )
            return
        except Exception as exc:
            last_error = exc
            log.warning(
                "agnes: startup login attempt=%s/%s failed: %s",
                attempt,
                attempts,
                exc,
            )
    log.warning(
        "agnes: startup login failed; chat will retry on first request: %s",
        last_error,
    )


# Warm up browser sessions in background threads so the web server starts
# immediately even if auto-login is slow or fails. Login is retried lazily
# on the first chat request either way.
# Tip: local ChatGPT debug with headed browser —
#   HEADLESS=0 BOOTSTRAP_DEEPSEEK=0 python main.py
_bootstrap_targets = (
    ("deepseek-bootstrap", _bootstrap_deepseek),
    ("chatgpt-bootstrap", _bootstrap_chatgpt),
    ("qwen-bootstrap", _bootstrap_qwen),
    ("agnes-bootstrap", _bootstrap_agnes),
)
_bootstrap_pending = len(_bootstrap_targets)
_bootstrap_lock = threading.Lock()


def _run_bootstrap(name: str, fn) -> None:
    global _bootstrap_pending
    try:
        fn()
    finally:
        with _bootstrap_lock:
            _bootstrap_pending -= 1
            done = _bootstrap_pending <= 0
        if done:
            log.info("========== mock_agent ready ==========")


for name, fn in _bootstrap_targets:
    threading.Thread(
        target=_run_bootstrap,
        args=(name, fn),
        name=name,
        daemon=True,
    ).start()


@atexit.register
def _shutdown_browser() -> None:
    try:
        chat_job_runner.stop()
    except Exception:
        pass
    try:
        deepseek_mgr.stop()
    except Exception:
        pass
    try:
        chatgpt_mgr.stop()
    except Exception:
        pass
    try:
        qwen_mgr.stop()
    except Exception:
        pass
    try:
        agnes_mgr.stop()
    except Exception:
        pass


if __name__ == "__main__":
    log.info(
        "listening http://%s:%s (flask, debug=%s)",
        config.host,
        config.port,
        config.flask_debug,
    )
    app.run(
        host=config.host,
        port=config.port,
        debug=config.flask_debug,
        threaded=True,
    )
