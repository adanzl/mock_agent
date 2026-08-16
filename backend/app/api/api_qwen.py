import base64
import logging
import mimetypes
from typing import Any

from flask import Blueprint, Response, jsonify, request

from app.config import ROOT_DIR, config
from app.services.chat_jobs.job_mgr import chat_job_mgr
from app.services.qwen.qwen_mgr import qwen_mgr

bp = Blueprint("qwen", __name__)
logger = logging.getLogger(__name__)
_DOC_PATH = ROOT_DIR / "docs" / "qwen-api.md"
_MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


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
                "error": "qwen disabled; set QWEN_ENABLED=1 in .env",
            }
        ),
        503,
    )


def _file_to_data_url(filename: str, raw: bytes) -> str:
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(f"image too large (max {_MAX_IMAGE_BYTES} bytes)")
    mime = mimetypes.guess_type(filename or "")[0] or "image/png"
    if not str(mime).startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _collect_image_sources(payload: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    raw = payload.get("images")
    if raw is None:
        raw = payload.get("image")
    if raw is None:
        raw = payload.get("image_url")
    if raw is None:
        raw = payload.get("image_base64")
    if raw is None:
        return sources
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("images must be a string or array of strings")
    for item in items:
        text = str(item or "").strip()
        if text:
            sources.append(text)
    return sources


def _parse_chat_request() -> tuple[str, str | None, str, bool, bool, int | None, list[str]]:
    """Parse JSON or multipart chat body into ask() args."""
    images: list[str] = []
    if request.files:
        form = request.form
        question = form.get("question") or form.get("prompt") or form.get("message") or ""
        conversation_id = form.get("conversation_id") or form.get("chat_id")
        mode = form.get("mode") or form.get("model") or "auto"
        deep_thinking = _as_bool(
            form.get("deep_thinking", form.get("think", form.get("deep_think"))),
            False,
        )
        search = _as_bool(
            form.get("search", form.get("web_search", form.get("smart_search"))),
            False,
        )
        timeout_raw = form.get("timeout")
        timeout_s = int(timeout_raw) if timeout_raw not in (None, "") else None
        for key in ("image", "images", "file", "files"):
            for uploaded in request.files.getlist(key):
                if not uploaded or not uploaded.filename:
                    continue
                raw = uploaded.read()
                if not raw:
                    continue
                images.append(_file_to_data_url(uploaded.filename, raw))
        # Also allow JSON-like image URLs in multipart form fields.
        images.extend(_collect_image_sources(dict(form)))
    else:
        payload = request.get_json(silent=True) or {}
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
        timeout_raw = payload.get("timeout")
        timeout_s = int(timeout_raw) if timeout_raw is not None else None
        images = _collect_image_sources(payload)

    # Dedupe while preserving order.
    deduped: list[str] = []
    seen: set[str] = set()
    for src in images:
        if src in seen:
            continue
        seen.add(src)
        deduped.append(src)
    if len(deduped) > _MAX_IMAGES:
        raise ValueError(f"too many images (max {_MAX_IMAGES})")

    question_text = str(question or "").strip()
    if not question_text and not deduped:
        raise ValueError("question is required (or provide image/images)")
    return (
        question_text,
        str(conversation_id) if conversation_id else None,
        str(mode),
        deep_thinking,
        search,
        timeout_s,
        deduped,
    )


@bp.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "qwen",
            "enabled": bool(config.qwen_enabled),
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
    if not config.qwen_enabled:
        return jsonify(
            {
                "ok": True,
                "enabled": False,
                "ready": False,
                "state": "disabled",
            }
        )
    try:
        data = qwen_mgr.status()
        logger.info("status state=%s ready=%s", data.get("state"), data.get("ready"))
        return jsonify({"ok": True, "enabled": True, **data})
    except Exception as exc:
        logger.exception("status failed: %s", exc)
        return jsonify({"ok": False, "enabled": True, "error": str(exc)}), 500


@bp.get("/conversations")
def conversations():
    if not config.qwen_enabled:
        return _disabled_response()
    limit = request.args.get("limit", 50)
    try:
        items = qwen_mgr.list_conversations(provider="qwen", limit=int(limit))
        return jsonify({"ok": True, "items": items})
    except Exception as exc:
        logger.exception("list conversations failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/conversations/<conversation_id>")
def conversation_detail(conversation_id: str):
    if not config.qwen_enabled:
        return _disabled_response()
    try:
        item = qwen_mgr.get_conversation(conversation_id)
        if item is None:
            return jsonify({"ok": False, "error": "conversation not found"}), 404
        messages = qwen_mgr.list_conversation_messages(conversation_id)
        return jsonify({"ok": True, "conversation": item, "messages": messages})
    except Exception as exc:
        logger.exception("conversation detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.post("/chat")
def chat():
    if not config.qwen_enabled:
        return _disabled_response()

    try:
        question, conversation_id, mode, deep_thinking, search, timeout_s, images = (
            _parse_chat_request()
        )
    except ValueError as exc:
        logger.warning("chat bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400

    logger.info(
        "chat request mode=%s think=%s search=%s conv=%s images=%s chars=%s question=%s",
        mode,
        deep_thinking,
        search,
        conversation_id,
        len(images),
        len(str(question)),
        question,
    )
    try:
        result = qwen_mgr.ask(
            str(question),
            conversation_id=conversation_id,
            mode=str(mode),
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
            images=images or None,
        )
        answer = result.get("answer") or ""
        logger.info(
            "chat ok answer_chars=%s mode=%s conv=%s worker=%s images=%s answer=%s",
            len(answer),
            result.get("mode"),
            result.get("conversation_id"),
            result.get("worker_id"),
            result.get("image_count", len(images)),
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
    if not config.qwen_enabled:
        return _disabled_response()

    try:
        question, conversation_id, mode, deep_thinking, search, timeout_s, images = (
            _parse_chat_request()
        )
    except ValueError as exc:
        logger.warning("chat async bad request: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        job = chat_job_mgr.create(
            provider="qwen",
            question=str(question),
            conversation_id=conversation_id,
            mode=str(mode),
            deep_thinking=deep_thinking,
            search=search,
            timeout_s=timeout_s,
            images=images or None,
        )
        return (
            jsonify(
                {
                    "ok": True,
                    "job_id": job.get("id"),
                    "status": job.get("status"),
                    "provider": "qwen",
                    "image_count": len(images),
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
    if not config.qwen_enabled:
        return _disabled_response()
    try:
        job = chat_job_mgr.get(job_id, provider="qwen")
        if job is None:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify(chat_job_mgr.to_api_payload(job))
    except Exception as exc:
        logger.exception("chat job detail failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
