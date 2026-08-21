import logging

from flask import Blueprint, Response, jsonify, request

from app.config import ROOT_DIR, config
from app.services.chat_jobs.job_mgr import chat_job_mgr
from app.services.deepseek.deepseek_mgr import deepseek_mgr

bp = Blueprint("deepseek", __name__)
logger = logging.getLogger(__name__)
_DOC_PATH = ROOT_DIR / "docs" / "deepseek-api.md"


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
                "error": "deepseek disabled; set DEEPSEEK_ENABLED=1 in .env",
            }
        ),
        503,
    )


@bp.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "deepseek",
            "enabled": bool(config.deepseek_enabled),
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
    if not config.deepseek_enabled:
        return jsonify(
            {
                "ok": True,
                "enabled": False,
                "ready": False,
                "state": "disabled",
            }
        )
    try:
        data = deepseek_mgr.status()
        logger.info("status state=%s ready=%s", data.get("state"), data.get("ready"))
        return jsonify({"ok": True, "enabled": True, **data})
    except Exception as exc:
        logger.exception("status failed: %s", exc)
        return jsonify({"ok": False, "enabled": True, "error": str(exc)}), 500


@bp.get("/conversations")
def conversations():
    if not config.deepseek_enabled:
        return _disabled_response()
    limit = request.args.get("limit", 50)
    try:
        items = deepseek_mgr.list_conversations(provider="deepseek", limit=int(limit))
        return jsonify({"ok": True, "items": items})
    except Exception as exc:
        logger.exception("list conversations failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/conversations/<conversation_id>")
def conversation_detail(conversation_id: str):
    if not config.deepseek_enabled:
        return _disabled_response()
    sync = request.args.get("sync", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        if sync or deepseek_mgr.get_conversation(conversation_id) is None:
            deepseek_mgr.sync_conversation(conversation_id)
        item = deepseek_mgr.get_conversation(conversation_id)
        if item is None:
            return jsonify({"ok": False, "error": "conversation not found"}), 404
        messages = deepseek_mgr.list_conversation_messages(conversation_id)
        return jsonify({"ok": True, "conversation": item, "messages": messages})
    except ValueError as exc:
        logger.warning("conversation detail bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        logger.warning("conversation sync/runtime: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        logger.error("conversation detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.post("/conversations/<conversation_id>/sync")
def conversation_sync(conversation_id: str):
    if not config.deepseek_enabled:
        return _disabled_response()
    try:
        result = deepseek_mgr.sync_conversation(conversation_id)
        item = deepseek_mgr.get_conversation(conversation_id)
        messages = deepseek_mgr.list_conversation_messages(conversation_id)
        return jsonify(
            {
                **result,
                "conversation": item,
                "messages": messages,
            }
        )
    except ValueError as exc:
        logger.warning("conversation sync bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        logger.warning("conversation sync/runtime: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:
        logger.error("conversation sync failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.post("/chat")
def chat():
    if not config.deepseek_enabled:
        return _disabled_response()

    payload = request.get_json(silent=True) or {}
    question = payload.get("question") or payload.get("prompt") or payload.get("message")
    if not question:
        return jsonify({"ok": False, "error": "question is required"}), 400

    # 无 conversation_id = 新对话；有 conversation_id = 多轮续聊
    conversation_id = payload.get("conversation_id") or payload.get("chat_id")
    mode = payload.get("mode") or payload.get("model") or "instant"
    deep_thinking = _as_bool(
        payload.get("deep_thinking", payload.get("think", payload.get("deep_think"))),
        False,
    )
    search = _as_bool(
        payload.get("search", payload.get("web_search", payload.get("smart_search"))),
        False,
    )
    timeout_s = payload.get("timeout")
    logger.info(
        "chat request mode=%s think=%s search=%s conv=%s chars=%s question=%s",
        mode,
        deep_thinking,
        search,
        conversation_id,
        len(str(question)),
        question,
    )
    try:
        result = deepseek_mgr.ask(
            str(question),
            conversation_id=str(conversation_id) if conversation_id else None,
            mode=str(mode),
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=int(timeout_s) if timeout_s is not None else None,
        )
        answer = result.get("answer") or ""
        logger.info(
            "chat ok answer_chars=%s mode=%s conv=%s worker=%s answer=%s",
            len(answer),
            result.get("mode"),
            result.get("conversation_id"),
            result.get("worker_id"),
            answer,
        )
        return jsonify(result)
    except ValueError as exc:
        logger.warning("chat bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
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
    if not config.deepseek_enabled:
        return _disabled_response()

    payload = request.get_json(silent=True) or {}
    question = payload.get("question") or payload.get("prompt") or payload.get("message")
    if not question:
        return jsonify({"ok": False, "error": "question is required"}), 400

    conversation_id = payload.get("conversation_id") or payload.get("chat_id")
    mode = payload.get("mode") or payload.get("model") or "instant"
    deep_thinking = _as_bool(
        payload.get("deep_thinking", payload.get("think", payload.get("deep_think"))),
        False,
    )
    search = _as_bool(
        payload.get("search", payload.get("web_search", payload.get("smart_search"))),
        False,
    )
    timeout_s = payload.get("timeout")
    try:
        job = chat_job_mgr.create(
            provider="deepseek",
            question=str(question),
            conversation_id=str(conversation_id) if conversation_id else None,
            mode=str(mode),
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=int(timeout_s) if timeout_s is not None else None,
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "job_id": job.get("id"),
                    "status": job.get("status"),
                    "provider": "deepseek",
                }
            ),
            202,
        )
    except ValueError as exc:
        logger.warning("chat async bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("chat async failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/chat/jobs/<job_id>")
def chat_job_detail(job_id: str):
    if not config.deepseek_enabled:
        return _disabled_response()
    try:
        job = chat_job_mgr.get(job_id, provider="deepseek")
        if job is None:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify(chat_job_mgr.to_api_payload(job))
    except Exception as exc:
        logger.exception("chat job detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
