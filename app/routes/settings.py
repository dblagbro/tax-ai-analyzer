"""Runtime settings CRUD and connection test endpoints."""
import logging
import os

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX, LLM_MODEL, PAPERLESS_API_BASE_URL
from app.routes.helpers import admin_required

logger = logging.getLogger(__name__)

bp = Blueprint("settings", __name__)


@bp.route(URL_PREFIX + "/api/settings", methods=["GET"])
@login_required
@admin_required
def api_settings_get():
    raw = db.get_all_settings()
    masked = dict(raw)
    for key in ("llm_api_key", "paperless_token", "smtp_pass",
                "dropbox_token", "s3_secret_key"):
        if masked.get(key):
            masked[key] = "***" + str(masked[key])[-4:]
    masked.setdefault("llm_model", LLM_MODEL)
    masked.setdefault("paperless_url", PAPERLESS_API_BASE_URL)
    return jsonify(masked)


@bp.route(URL_PREFIX + "/api/settings", methods=["POST"])
@login_required
@admin_required
def api_settings_save():
    data = request.get_json() or {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith("***"):
            continue
        db.set_setting(key, str(value))
    db.log_activity("settings_updated", f"{len(data)} keys", user_id=current_user.id)
    return jsonify({"status": "saved"})


@bp.route(URL_PREFIX + "/api/settings/test-llm", methods=["POST"])
@login_required
@admin_required
def api_settings_test_llm():
    try:
        import anthropic
        settings = db.get_all_settings()
        api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
        model = settings.get("llm_model") or LLM_MODEL
        if not api_key:
            return jsonify({"status": "error", "message": "No API key configured"}), 400
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model, max_tokens=20,
            messages=[{"role": "user", "content": "Say OK"}])
        return jsonify({"status": "ok", "model": model,
                        "response": msg.content[0].text if msg.content else ""})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route(URL_PREFIX + "/api/settings/test-paperless", methods=["POST"])
@login_required
@admin_required
def api_settings_test_paperless():
    try:
        import httpx
        settings = db.get_all_settings()
        base = settings.get("paperless_url") or PAPERLESS_API_BASE_URL
        token = settings.get("paperless_token") or os.environ.get("PAPERLESS_API_TOKEN", "")
        headers = {"Authorization": f"Token {token}"} if token else {}
        r = httpx.get(f"{base}/api/", headers=headers, timeout=10)
        return jsonify({"status": "ok" if r.status_code == 200 else "auth_error",
                        "code": r.status_code, "url": base})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route(URL_PREFIX + "/api/settings/llm-models")
@login_required
def api_llm_models():
    models = {
        "anthropic": [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-haiku-20240307",
        ],
        "openai": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-4",
            "gpt-3.5-turbo",
        ],
    }
    return jsonify(models)
