"""Generic dispatcher for auto-deployed bank importers (Phase 11E).

Instead of generating per-bank route files (which would require mutating
`routes/__init__.py` whenever a new bank ships), we expose a single set of
endpoints under `/api/import/auto/<slug>/...` that look up the importer
module by slug at call time.

Endpoints (mirror the per-bank importer pattern):
  POST   /api/import/auto/<slug>/credentials   — save username/password
  POST   /api/import/auto/<slug>/cookies       — save a cookie array
  DELETE /api/import/auto/<slug>/cookies       — clear cookies
  GET    /api/import/auto/<slug>/status        — credential + cookie status
  POST   /api/import/auto/<slug>/mfa           — submit MFA code for a job
  POST   /api/import/auto/<slug>/start         — kick off an import

Slug must:
  - Be a known auto-deployed bank (deployed_path set, file exists)
  - Match the safe-identifier regex (no path traversal etc.)
"""
from __future__ import annotations

import importlib
import json
import logging
import re
import threading
from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX, CONSUME_PATH
from app.routes._state import _job_logs, append_job_log

logger = logging.getLogger(__name__)
bp = Blueprint("import_auto", __name__)

_SAFE_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _resolve_importer(slug: str):
    """Return the importer module for a deployed bank slug, or (None, errstr)."""
    if not _SAFE_SLUG_RE.match(slug):
        return None, "invalid slug"
    bank = db.get_pending_bank_by_slug(slug)
    if not bank:
        return None, "bank not found"
    deployed = [g for g in db.list_generated_importers(bank["id"])
                if g.get("deployed_path") and g.get("deployed_at")]
    if not deployed:
        return None, "bank has no deployed importer"
    try:
        mod = importlib.import_module(f"app.importers.{slug}_importer")
    except Exception as e:
        return None, f"import failed: {e}"
    return mod, ""


def _setting_keys(slug: str) -> dict:
    return {
        "username": f"{slug}_username",
        "password": f"{slug}_password",
        "cookies":  f"{slug}_cookies",
    }


@bp.route(URL_PREFIX + "/api/import/auto/<slug>/credentials", methods=["POST"])
@login_required
def api_auto_credentials(slug):
    mod, err = _resolve_importer(slug)
    if err:
        return jsonify({"error": err}), 404
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    keys = _setting_keys(slug)
    db.set_setting(keys["username"], username)
    db.set_setting(keys["password"], password)
    return jsonify({"status": "saved", "slug": slug})


@bp.route(URL_PREFIX + "/api/import/auto/<slug>/cookies", methods=["POST"])
@login_required
def api_auto_cookies_save(slug):
    mod, err = _resolve_importer(slug)
    if err:
        return jsonify({"error": err}), 404
    data = request.get_json() or {}
    raw = data.get("cookies")
    if not raw:
        return jsonify({"error": "cookies field required"}), 400
    if isinstance(raw, str):
        try:
            cookies = json.loads(raw)
        except Exception:
            return jsonify({"error": "cookies must be valid JSON"}), 400
    elif isinstance(raw, list):
        cookies = raw
    else:
        return jsonify({"error": "cookies must be a JSON array"}), 400
    if not isinstance(cookies, list) or not cookies:
        return jsonify({"error": "cookies must be a non-empty JSON array"}), 400
    db.set_setting(_setting_keys(slug)["cookies"], json.dumps(cookies))
    return jsonify({"status": "saved", "count": len(cookies)})


@bp.route(URL_PREFIX + "/api/import/auto/<slug>/cookies", methods=["DELETE"])
@login_required
def api_auto_cookies_clear(slug):
    mod, err = _resolve_importer(slug)
    if err:
        return jsonify({"error": err}), 404
    db.set_setting(_setting_keys(slug)["cookies"], "")
    return jsonify({"status": "cleared"})


@bp.route(URL_PREFIX + "/api/import/auto/<slug>/status", methods=["GET"])
@login_required
def api_auto_status(slug):
    mod, err = _resolve_importer(slug)
    if err:
        return jsonify({"error": err}), 404
    keys = _setting_keys(slug)
    user = db.get_setting(keys["username"]) or ""
    cookies_raw = db.get_setting(keys["cookies"]) or ""
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


@bp.route(URL_PREFIX + "/api/import/auto/<slug>/mfa", methods=["POST"])
@login_required
def api_auto_mfa(slug):
    mod, err = _resolve_importer(slug)
    if err:
        return jsonify({"error": err}), 404
    data = request.get_json() or {}
    job_id = data.get("job_id")
    code = (data.get("code") or "").strip()
    if not job_id or not code:
        return jsonify({"error": "job_id and code required"}), 400
    set_mfa = getattr(mod, "set_mfa_code", None)
    if not callable(set_mfa):
        return jsonify({"error": "importer does not implement set_mfa_code"}), 500
    set_mfa(int(job_id), code)
    return jsonify({"status": "ok"})


@bp.route(URL_PREFIX + "/api/import/auto/<slug>/start", methods=["POST"])
@login_required
def api_auto_start(slug):
    mod, err = _resolve_importer(slug)
    if err:
        return jsonify({"error": err}), 404
    run_import = getattr(mod, "run_import", None)
    if not callable(run_import):
        return jsonify({"error": "importer does not implement run_import"}), 500

    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or ["2022", "2023", "2024", "2025"]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]

    keys = _setting_keys(slug)
    username = db.get_setting(keys["username"])
    password = db.get_setting(keys["password"])
    if not username or not password:
        return jsonify({"error": f"{slug} credentials not configured"}), 400

    cookies: Optional[list] = None
    cookies_raw = db.get_setting(keys["cookies"]) or ""
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
        slug, entity_id=entity_id,
        config_json=json.dumps({"years": years, "cookie_auth": cookies is not None}),
    )
    _job_logs[job_id] = []

    def _run(jid, uname, pw, yrs, eid, eslug, ckies):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            result = run_import(
                username=uname, password=pw, years=yrs,
                consume_path=CONSUME_PATH, entity_slug=eslug,
                job_id=jid, log=log, cookies=ckies, entity_id=eid,
            )
            total = (result or {}).get("imported", 0)
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"{slug}: {total} transactions for {yrs}")
        except Exception as e:
            import traceback
            log(f"Fatal error: {e}")
            log(traceback.format_exc()[:600])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(
        target=_run,
        args=(job_id, username, password, years, entity_id, entity_slug, cookies),
        daemon=True, name=f"{slug}-{job_id}",
    ).start()
    return jsonify({"status": "started", "job_id": job_id})
