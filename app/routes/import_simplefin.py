"""SimpleFIN Bridge import routes.

SimpleFIN Bridge connects to MX Financial's data network (16,000+ institutions).
Users obtain a one-time setup token from their bank's SimpleFIN connection page
and paste it here to link their account.

Endpoints:
  POST /api/import/simplefin/claim    — claim a setup token, store access URL
  GET  /api/import/simplefin/status   — check connection status
  POST /api/import/simplefin/start    — start an import job
"""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes._state import _job_logs, append_job_log

logger = logging.getLogger(__name__)
bp = Blueprint("import_simplefin", __name__)

_SETTING_KEY = "simplefin_access_url"


@bp.route(URL_PREFIX + "/api/import/simplefin/claim", methods=["POST"])
@login_required
def api_simplefin_claim():
    data = request.get_json() or {}
    setup_url = (data.get("setup_url") or "").strip()
    if not setup_url:
        return jsonify({"error": "setup_url is required"}), 400

    # setup_url can be a raw token string OR a full claim URL
    if not setup_url.startswith("http"):
        setup_url = f"https://beta-bridge.simplefin.org/simplefin/claim/{setup_url}"

    try:
        from app.importers.simplefin_importer import claim_token
        access_url = claim_token(setup_url)
    except Exception as e:
        logger.error(f"SimpleFIN claim failed: {e}")
        return jsonify({"error": f"Claim failed: {str(e)[:200]}"}), 400

    db.set_setting(_SETTING_KEY, access_url)
    return jsonify({"status": "connected", "message": "SimpleFIN Bridge connected."})


@bp.route(URL_PREFIX + "/api/import/simplefin/status", methods=["GET"])
@login_required
def api_simplefin_status():
    access_url = db.get_setting(_SETTING_KEY) or ""
    connected = bool(access_url)
    preview = ""
    if connected:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(access_url)
            preview = f"{parsed.hostname}" if parsed.hostname else "connected"
        except Exception:
            preview = "connected"
    return jsonify({"connected": connected, "preview": preview})


@bp.route(URL_PREFIX + "/api/import/simplefin/start", methods=["POST"])
@login_required
def api_simplefin_start():
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or ["2022", "2023", "2024", "2025"]
    account_filter = data.get("account_filter") or None

    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]

    access_url = db.get_setting(_SETTING_KEY) or ""
    if not access_url:
        return jsonify({"error": "SimpleFIN not connected. Claim a setup token first."}), 400

    entity_slug = "personal"
    if entity_id:
        ent = db.get_entity(entity_id=entity_id)
        if ent:
            entity_slug = ent.get("slug", "personal")

    job_id = db.create_import_job(
        "simplefin", entity_id=entity_id,
        config_json=json.dumps({"years": years}),
    )
    _job_logs[job_id] = []

    def _run(jid, url, yrs, eid, eslug, acct_filter):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.simplefin_importer import run_import
            result = run_import(
                access_url=url,
                years=yrs,
                entity_id=eid,
                entity_slug=eslug,
                job_id=jid,
                log=log,
                account_filter=acct_filter,
            )
            total = result.get("imported", 0)
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"SimpleFIN: {total} transactions for {yrs}")
        except Exception as e:
            import traceback
            log(f"Fatal error: {e}")
            log(traceback.format_exc()[:600])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(
        target=_run,
        args=(job_id, access_url, years, entity_id, entity_slug, account_filter),
        daemon=True, name=f"simplefin-{job_id}",
    ).start()
    return jsonify({"status": "started", "job_id": job_id})
