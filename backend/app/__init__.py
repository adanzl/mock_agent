from __future__ import annotations

import time

from flask import Flask, request

from app.config import config
from app.core.log_config import setup_server_logging

app_logger, access_logger = setup_server_logging(
    log_dir=config.log_dir,
    is_production=config.is_production,
    retention_days=config.log_retention_days,
)
log = app_logger


def create_app() -> Flask:
    app = Flask(__name__)

    from app.api.api_chatgpt import bp as chatgpt_bp
    from app.api.api_deepseek import bp as deepseek_bp
    from app.repositories.database import db_mgr

    db_mgr.init()
    app.register_blueprint(deepseek_bp, url_prefix="/api/deepseek")
    app.register_blueprint(chatgpt_bp, url_prefix="/api/chatgpt")

    @app.before_request
    def _record_request_start_time():
        request._ma_start_time = time.perf_counter()

    @app.after_request
    def _log_api_access(response):
        start = getattr(request, "_ma_start_time", None)
        if start is None:
            return response
        duration_ms = (time.perf_counter() - start) * 1000
        path = request.full_path if request.query_string else request.path
        access_logger.info(
            "%s %s %s %s %.1fms",
            request.remote_addr or "-",
            request.method,
            path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    log.info(
        "mock_agent app created env=%s log_dir=%s sqlite=%s",
        config.env,
        config.log_dir,
        config.sqlite_path,
    )
    return app


__all__ = ["access_logger", "app_logger", "create_app", "log"]
