import logging

from flask import Blueprint, Response, jsonify, request

from app.config import ROOT_DIR, config
from app.services.chat_jobs.job_mgr import chat_job_mgr
from app.services.doubao.client import DoubaoReplyError
from app.services.doubao.doubao_mgr import doubao_mgr

bp = Blueprint("doubao", __name__)
logger = logging.getLogger(__name__)
_DOC_PATH = ROOT_DIR / "docs" / "doubao-api.md"


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _disabled_response():
    return (
        jsonify(
            {
                "ok": False,
                "enabled": False,
                "error": "doubao disabled; set DOUBAO_ENABLED=1 in .env",
            }
        ),
        503,
    )


def _parse_chat_request() -> tuple[str, str | None, str, bool, bool, int | None]:
    payload = request.get_json(silent=True) or {}
    if request.files:
        raise ValueError("doubao image upload is not supported yet")
    question = payload.get("question") or payload.get("prompt") or payload.get("message") or ""
    conversation_id = payload.get("conversation_id") or payload.get("chat_id")
    mode = payload.get("mode") or payload.get("model") or "auto"
    deep_thinking = _as_bool(
        payload.get("deep_thinking", payload.get("think", payload.get("deep_think"))),
        False,
    )
    search = _as_bool(
        payload.get("search", payload.get("web_search", payload.get("smart_search"))),
        False,
    )
    if payload.get("images") or payload.get("image") or payload.get("image_url"):
        raise ValueError("doubao image upload is not supported yet")
    timeout_raw = payload.get("timeout")
    timeout_s = int(timeout_raw) if timeout_raw is not None else None
    question_text = str(question or "").strip()
    if not question_text:
        raise ValueError("question is required")
    return (
        question_text,
        str(conversation_id) if conversation_id else None,
        str(mode),
        deep_thinking,
        search,
        timeout_s,
    )


@bp.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "doubao",
            "enabled": bool(config.doubao_enabled),
        }
    )


@bp.get("/doc")
def doc():
    if not _DOC_PATH.is_file():
        return jsonify({"ok": False, "error": f"doc not found: {_DOC_PATH.name}"}), 404
    return Response(
        _DOC_PATH.read_text(encoding="utf-8"),
        mimetype="text/markdown; charset=utf-8",
    )


@bp.get("/status")
def status():
    if not config.doubao_enabled:
        return jsonify(
            {
                "ok": True,
                "enabled": False,
                "ready": False,
                "state": "disabled",
            }
        )
    try:
        data = doubao_mgr.status()
        return jsonify({"ok": True, "enabled": True, **data})
    except Exception as exc:
        logger.exception("status failed: %s", exc)
        return jsonify({"ok": False, "enabled": True, "error": str(exc)}), 500


@bp.get("/conversations")
def conversations():
    if not config.doubao_enabled:
        return _disabled_response()
    limit = request.args.get("limit", 50)
    try:
        items = doubao_mgr.list_conversations(provider="doubao", limit=int(limit))
        return jsonify({"ok": True, "items": items})
    except Exception as exc:
        logger.exception("list conversations failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/conversations/<conversation_id>")
def conversation_detail(conversation_id: str):
    if not config.doubao_enabled:
        return _disabled_response()
    try:
        item = doubao_mgr.get_conversation(conversation_id)
        if item is None:
            return jsonify({"ok": False, "error": "conversation not found"}), 404
        messages = doubao_mgr.list_conversation_messages(conversation_id)
        return jsonify({"ok": True, "conversation": item, "messages": messages})
    except Exception as exc:
        logger.exception("conversation detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.post("/chat")
def chat():
    if not config.doubao_enabled:
        return _disabled_response()
    try:
        question, conversation_id, mode, deep_thinking, search, timeout_s = _parse_chat_request()
    except ValueError as exc:
        logger.warning("chat bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    logger.info(
        "chat request mode=%s think=%s search=%s conv=%s chars=%s question=%s",
        mode,
        deep_thinking,
        search,
        conversation_id,
        len(question),
        question,
    )
    try:
        result = doubao_mgr.ask(
            question,
            conversation_id=conversation_id,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
        )
        return jsonify(result)
    except ValueError as exc:
        logger.warning("chat bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except DoubaoReplyError as exc:
        logger.warning("chat reply failed status=%s error=%s", exc.status, exc)
        return jsonify({"ok": False, "error": str(exc)}), exc.status
    except RuntimeError as exc:
        logger.warning("chat auth/runtime: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 401
    except TimeoutError as exc:
        logger.error("chat timeout: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 504
    except Exception as exc:
        logger.exception("chat failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.post("/chat/async")
def chat_async():
    if not config.doubao_enabled:
        return _disabled_response()
    try:
        question, conversation_id, mode, deep_thinking, search, timeout_s = _parse_chat_request()
    except ValueError as exc:
        logger.warning("chat async bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    try:
        job = chat_job_mgr.create(
            provider="doubao",
            question=question,
            conversation_id=conversation_id,
            mode=mode,
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "job_id": job.get("id"),
                    "status": job.get("status"),
                    "provider": "doubao",
                }
            ),
            202,
        )
    except Exception as exc:
        logger.exception("chat async failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/chat/jobs/<job_id>")
def chat_job_detail(job_id: str):
    if not config.doubao_enabled:
        return _disabled_response()
    try:
        job = chat_job_mgr.get(job_id, provider="doubao")
        if job is None:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify(chat_job_mgr.to_api_payload(job))
    except Exception as exc:
        logger.exception("chat job detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
