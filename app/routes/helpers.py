"""Shared decorators and helper functions used across all Blueprint modules."""
import json
import os
from functools import wraps

from flask import jsonify, make_response, redirect, request
from flask_login import current_user

from app import db
from app.config import URL_PREFIX, LLM_MODEL


def _url(path: str) -> str:
    return URL_PREFIX + path


def admin_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            if request.path.startswith(URL_PREFIX + "/api/"):
                return jsonify({"error": "Admin access required"}), 403
            from flask import flash
            flash("Admin access required.", "danger")
            return redirect(_url("/"))
        return f(*args, **kwargs)
    return _wrap


def superuser_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_superuser:
            if request.path.startswith(URL_PREFIX + "/api/"):
                return jsonify({"error": "Superuser access required"}), 403
            from flask import flash
            flash("Superuser access required.", "danger")
            return redirect(_url("/"))
        return f(*args, **kwargs)
    return _wrap


def _row_list(rows) -> list:
    return [dict(r) for r in rows] if rows else []


def _no_cache_page(html_response):
    resp = make_response(html_response)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _user_can_access_session(sess) -> bool:
    if not sess:
        return False
    if current_user.is_admin:
        return True
    if sess["user_id"] == current_user.id:
        return True
    shares = db.get_chat_shares(sess["id"])
    return any(s["shared_with_user_id"] == current_user.id for s in shares)


def _user_can_write_session(sess) -> bool:
    if not sess:
        return False
    if current_user.is_admin or sess["user_id"] == current_user.id:
        return True
    shares = db.get_chat_shares(sess["id"])
    return any(
        s["shared_with_user_id"] == current_user.id and s["can_write"]
        for s in shares
    )


def setup_chat_stream(system_prompt: str, history: list, user_message: str):
    """SSE generator factory for guided setup chat endpoints (Gmail, PayPal, etc.)."""
    settings = db.get_all_settings()
    api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    model = settings.get("llm_model") or LLM_MODEL

    messages = []
    for h in history[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    def _generate():
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model=model, max_tokens=1024,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return _generate
