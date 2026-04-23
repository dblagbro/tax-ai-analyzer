"""US Bank import routes."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX, CONSUME_PATH
from app.routes._state import _job_logs, append_job_log

logger = logging.getLogger(__name__)
bp = Blueprint("import_usbank", __name__)


@bp.route(URL_PREFIX + "/api/import/usbank/credentials", methods=["POST"])
@login_required
def api_usbank_save_credentials():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    db.set_setting("usbank_username", username)
    db.set_setting("usbank_password", password)
    return jsonify({"status": "saved", "message": "Credentials saved."})


@bp.route(URL_PREFIX + "/api/import/usbank/cookies", methods=["POST"])
@login_required
def api_usbank_save_cookies():
    data = request.get_json() or {}
    cookies_raw = data.get("cookies")
    if not cookies_raw:
        return jsonify({"error": "cookies field is required"}), 400
    if isinstance(cookies_raw, str):
        try:
            cookies_list = json.loads(cookies_raw)
        except Exception:
            return jsonify({"error": "cookies must be valid JSON array"}), 400
    elif isinstance(cookies_raw, list):
        cookies_list = cookies_raw
    else:
        return jsonify({"error": "cookies must be a JSON array"}), 400
    if not isinstance(cookies_list, list) or not cookies_list:
        return jsonify({"error": "cookies must be a non-empty JSON array"}), 400
    db.set_setting("usbank_cookies", json.dumps(cookies_list))
    return jsonify({"status": "saved", "message": f"{len(cookies_list)} cookies saved.",
                    "count": len(cookies_list)})


@bp.route(URL_PREFIX + "/api/import/usbank/cookies", methods=["DELETE"])
@login_required
def api_usbank_clear_cookies():
    db.set_setting("usbank_cookies", "")
    return jsonify({"status": "cleared"})


@bp.route(URL_PREFIX + "/api/import/usbank/status", methods=["GET"])
@login_required
def api_usbank_status():
    user = db.get_setting("usbank_username") or ""
    cookies_raw = db.get_setting("usbank_cookies") or ""
    cookies_count = 0
    if cookies_raw:
        try:
            cookies_count = len(json.loads(cookies_raw))
        except Exception:
            pass
    return jsonify({
        "configured": bool(user),
        "username_preview": (user[:3] + "…") if len(user) > 3 else user,
        "cookies_saved": cookies_count > 0,
        "cookies_count": cookies_count,
    })


@bp.route(URL_PREFIX + "/api/import/usbank/mfa", methods=["POST"])
@login_required
def api_usbank_mfa():
    data = request.get_json() or {}
    job_id = data.get("job_id")
    code = data.get("code", "").strip()
    if not job_id or not code:
        return jsonify({"error": "job_id and code required"}), 400
    from app.importers.usbank_importer import set_mfa_code
    set_mfa_code(int(job_id), code)
    return jsonify({"status": "ok"})


@bp.route(URL_PREFIX + "/api/import/usbank/start", methods=["POST"])
@login_required
def api_import_usbank_start():
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or ["2022", "2023", "2024", "2025"]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]

    username = db.get_setting("usbank_username")
    password = db.get_setting("usbank_password")
    if not username or not password:
        return jsonify({"error": "US Bank credentials not configured."}), 400

    cookies = None
    cookies_raw = db.get_setting("usbank_cookies") or ""
    if cookies_raw:
        try:
            cookies = json.loads(cookies_raw)
        except Exception:
            cookies = None

    entity_slug = "personal"
    if entity_id:
        ent = db.get_entity(entity_id=entity_id)
        if ent:
            entity_slug = ent.get("slug", "personal")

    job_id = db.create_import_job(
        "usbank", entity_id=entity_id,
        config_json=json.dumps({"years": years, "cookie_auth": cookies is not None}),
    )
    _job_logs[job_id] = []

    def _run(jid, uname, pw, yrs, eid, eslug, ckies):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.usbank_importer import run_import
            result = run_import(
                username=uname, password=pw, years=yrs,
                consume_path=CONSUME_PATH, entity_slug=eslug,
                job_id=jid, log=log, cookies=ckies, entity_id=eid,
            )
            total = result.get("imported", 0)
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"US Bank: {total} transactions for {yrs}")
        except Exception as e:
            import traceback
            log(f"Fatal error: {e}")
            log(traceback.format_exc()[:600])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(
        target=_run,
        args=(job_id, username, password, years, entity_id, entity_slug, cookies),
        daemon=True, name=f"usbank-{job_id}",
    ).start()
    return jsonify({"status": "started", "job_id": job_id})
