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
from app.services.chatgpt.chatgpt_mgr import chatgpt_mgr
from app.services.deepseek.deepseek_mgr import deepseek_mgr

log = app_logger
app = create_app()


def _bootstrap_deepseek() -> None:
    if not config.deepseek_enabled:
        log.info("deepseek bootstrap skipped; DEEPSEEK_ENABLED=0")
        return
    if not _bool_env("BOOTSTRAP_DEEPSEEK", True):
        log.info("deepseek bootstrap skipped; BOOTSTRAP_DEEPSEEK=0")
        return
    try:
        result = deepseek_mgr.ensure_ready()
        log.info(
            "deepseek ready state=%s session_saved=%s",
            result.get("state"),
            result.get("session_saved"),
        )
    except Exception:
        log.exception(
            "deepseek auto login failed at startup; chat will retry on first request"
        )


def _bootstrap_chatgpt() -> None:
    if not config.chatgpt_enabled:
        log.info("chatgpt bootstrap skipped; CHATGPT_ENABLED=0")
        return
    # Optional warm-up; default on when GPT is enabled.
    if not _bool_env("BOOTSTRAP_CHATGPT", True):
        log.info("chatgpt bootstrap skipped; BOOTSTRAP_CHATGPT=0")
        return
    if not config.chatgpt_manual_login:
        if not config.chatgpt_username or not config.chatgpt_password:
            log.info("chatgpt bootstrap skipped; CHATGPT_USERNAME/PASSWORD not set")
            return
    else:
        log.info(
            "chatgpt manual login mode: will open headed browser for you to sign in"
        )
    try:
        result = chatgpt_mgr.ensure_ready()
        log.info(
            "chatgpt ready state=%s session_saved=%s proxy=%s manual=%s",
            result.get("state"),
            result.get("session_saved"),
            result.get("proxy"),
            result.get("manual_login"),
        )
    except Exception:
        log.exception(
            "chatgpt login failed at startup; chat will retry on first request"
        )


def _bool_env(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# Warm up browser sessions in background threads so the web server starts
# immediately even if auto-login is slow or fails. Login is retried lazily
# on the first chat request either way.
# Tip: local ChatGPT debug with headed browser —
#   HEADLESS=0 BOOTSTRAP_DEEPSEEK=0 python main.py
threading.Thread(
    target=_bootstrap_deepseek,
    name="deepseek-bootstrap",
    daemon=True,
).start()
threading.Thread(
    target=_bootstrap_chatgpt,
    name="chatgpt-bootstrap",
    daemon=True,
).start()


@atexit.register
def _shutdown_browser() -> None:
    try:
        deepseek_mgr.stop()
    except Exception:
        pass
    try:
        chatgpt_mgr.stop()
    except Exception:
        pass


if __name__ == "__main__":
    log.info(
        "Server started on http://%s:%s (flask, debug=%s)",
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
