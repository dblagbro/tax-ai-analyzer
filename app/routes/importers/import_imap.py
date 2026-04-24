"""IMAP email import routes (Outlook / Yahoo / iCloud / generic)."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX, CONSUME_PATH
from app.routes._state import _job_logs, _job_stop_events, _job_stop_lock, append_job_log

logger = logging.getLogger(__name__)
bp = Blueprint("import_imap", __name__)


def _load_config() -> dict:
    """Return stored IMAP config (password omitted from response)."""
    return {
        "provider": db.get_setting("imap_provider") or "generic",
        "host":     db.get_setting("imap_host") or "",
        "port":     int(db.get_setting("imap_port") or "993"),
        "username": db.get_setting("imap_username") or "",
        "folder":   db.get_setting("imap_folder") or "INBOX",
        "use_ssl":  (db.get_setting("imap_use_ssl") or "1") == "1",
    }


@bp.route(URL_PREFIX + "/api/import/imap/providers", methods=["GET"])
@login_required
def api_imap_providers():
    from app.importers.imap_importer import PROVIDERS
    return jsonify({"providers": PROVIDERS})


@bp.route(URL_PREFIX + "/api/import/imap/status", methods=["GET"])
@login_required
def api_imap_status():
    cfg = _load_config()
    cfg["configured"] = bool(cfg["host"] and cfg["username"] and db.get_setting("imap_password"))
    cfg["password_set"] = bool(db.get_setting("imap_password"))
    return jsonify(cfg)


@bp.route(URL_PREFIX + "/api/import/imap/settings", methods=["POST"])
@login_required
def api_imap_save_settings():
    from app.importers.imap_importer import PROVIDERS
    data = request.get_json() or {}
    provider = (data.get("provider") or "generic").strip().lower()
    host = (data.get("host") or "").strip()
    port = data.get("port") or 993
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()  # leave empty to keep existing
    folder = (data.get("folder") or "INBOX").strip() or "INBOX"
    use_ssl = bool(data.get("use_ssl", True))

    # Fill host/port from preset if provider is known
    if provider in PROVIDERS and PROVIDERS[provider]["host"]:
        host = host or PROVIDERS[provider]["host"]
        port = port or PROVIDERS[provider]["port"]

    if not host or not username:
        return jsonify({"error": "host and username required"}), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify({"error": "port must be numeric"}), 400

    db.set_setting("imap_provider", provider)
    db.set_setting("imap_host", host)
    db.set_setting("imap_port", str(port))
    db.set_setting("imap_username", username)
    db.set_setting("imap_folder", folder)
    db.set_setting("imap_use_ssl", "1" if use_ssl else "0")
    if password:
        db.set_setting("imap_password", password)
    db.log_activity("imap_settings_saved", f"{provider} {host}", user_id=current_user.id)
    return jsonify({"status": "saved", "password_updated": bool(password)})


@bp.route(URL_PREFIX + "/api/import/imap/test", methods=["POST"])
@login_required
def api_imap_test():
    cfg = _load_config()
    pwd = db.get_setting("imap_password") or ""
    if not cfg["host"] or not cfg["username"] or not pwd:
        return jsonify({"ok": False, "error": "Not fully configured"}), 400
    try:
        from app.importers.imap_importer import test_connection
        result = test_connection(cfg["host"], cfg["port"], cfg["username"], pwd,
                                 use_ssl=cfg["use_ssl"])
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/import/imap/start", methods=["POST"])
@login_required
def api_imap_start():
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or ["2022", "2023", "2024", "2025"]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]
    search_terms_raw = data.get("search_terms") or ""
    search_terms = None
    if search_terms_raw:
        if isinstance(search_terms_raw, list):
            search_terms = search_terms_raw
        else:
            search_terms = [t for t in search_terms_raw.split() if t]

    cfg = _load_config()
    pwd = db.get_setting("imap_password") or ""
    if not cfg["host"] or not cfg["username"] or not pwd:
        return jsonify({"error": "IMAP not configured — save settings first."}), 400

    entity_slug = "personal"
    if entity_id:
        ent = db.get_entity(entity_id=entity_id)
        if ent:
            entity_slug = ent.get("slug") or "personal"

    job_id = db.create_import_job(
        "imap", entity_id=entity_id,
        config_json=json.dumps({
            "provider": cfg["provider"], "host": cfg["host"],
            "username": cfg["username"], "years": years,
        }),
    )
    _job_logs[job_id] = []
    stop_ev = threading.Event()
    with _job_stop_lock:
        _job_stop_events[job_id] = stop_ev

    def _run(jid, eid, yrs, stop):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.imap_importer import run_import
            result = run_import(
                host=cfg["host"], port=cfg["port"],
                username=cfg["username"], password=pwd,
                years=yrs, consume_path=CONSUME_PATH,
                entity_slug=entity_slug, entity_id=eid,
                search_terms=search_terms,
                folder=cfg["folder"], job_id=jid,
                log=log, stop_event=stop,
                use_ssl=cfg["use_ssl"],
            )
            count = result.get("imported", 0)
            final_status = "cancelled" if stop.is_set() else "completed"
            db.update_import_job(jid, status=final_status, count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity(
                "import_complete",
                f"IMAP: {count} imported, {result.get('ai_filtered',0)} AI-filtered, "
                f"{result.get('skipped',0)} skipped",
            )
        except Exception as e:
            import traceback
            append_job_log(jid, f"FATAL: {e}")
            append_job_log(jid, traceback.format_exc()[:500])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())
        finally:
            with _job_stop_lock:
                _job_stop_events.pop(jid, None)

    threading.Thread(target=_run, args=(job_id, entity_id, years, stop_ev),
                     daemon=True, name=f"imap-{job_id}").start()
    return jsonify({"status": "started", "job_id": job_id})
