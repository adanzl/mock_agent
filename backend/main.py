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
from app.services.deepseek.client import get_client

log = app_logger
app = create_app()


def _bootstrap_deepseek() -> None:
    try:
        result = get_client().ensure_ready()
        log.info(
            "deepseek ready state=%s session_saved=%s",
            result.get("state"),
            result.get("session_saved"),
        )
    except Exception:
        log.exception(
            "deepseek auto login failed at startup; chat will retry on first request"
        )


# Warm up the DeepSeek browser session in a background thread so the web
# server starts immediately even if auto-login is slow or fails. Login is
# retried lazily on the first chat request either way.
threading.Thread(
    target=_bootstrap_deepseek,
    name="deepseek-bootstrap",
    daemon=True,
).start()


@atexit.register
def _shutdown_browser() -> None:
    try:
        get_client().stop()
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
