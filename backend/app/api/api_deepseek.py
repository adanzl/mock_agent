import logging

from flask import Blueprint, jsonify, request

from app.repositories.database import (
    get_conversation,
    list_conversation_messages,
    list_conversations,
)
from app.services.deepseek.client import get_client

bp = Blueprint("deepseek", __name__)
logger = logging.getLogger(__name__)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "deepseek"})


@bp.get("/status")
def status():
    client = get_client()
    try:
        data = client.status()
        logger.info("status state=%s ready=%s", data.get("state"), data.get("ready"))
        return jsonify({"ok": True, **data})
    except Exception as exc:
        logger.exception("status failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/conversations")
def conversations():
    limit = request.args.get("limit", 50)
    try:
        items = list_conversations(provider="deepseek", limit=int(limit))
        return jsonify({"ok": True, "items": items})
    except Exception as exc:
        logger.exception("list conversations failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/conversations/<conversation_id>")
def conversation_detail(conversation_id: str):
    try:
        item = get_conversation(conversation_id)
        if item is None:
            return jsonify({"ok": False, "error": "conversation not found"}), 404
        messages = list_conversation_messages(conversation_id)
        return jsonify({"ok": True, "conversation": item, "messages": messages})
    except Exception as exc:
        logger.exception("conversation detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.post("/chat")
def chat():
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
    preview = str(question).replace("\n", " ")[:80]
    logger.info(
        "chat request mode=%s think=%s search=%s conv=%s chars=%s preview=%r",
        mode,
        deep_thinking,
        search,
        conversation_id,
        len(str(question)),
        preview,
    )
    client = get_client()
    try:
        result = client.ask(
            str(question),
            conversation_id=str(conversation_id) if conversation_id else None,
            mode=str(mode),
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=int(timeout_s) if timeout_s is not None else None,
        )
        answer = result.get("answer") or ""
        logger.info(
            "chat ok answer_chars=%s mode=%s conv=%s",
            len(answer),
            result.get("mode"),
            result.get("conversation_id"),
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
