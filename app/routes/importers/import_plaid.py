"""Plaid import routes."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.routes._state import _job_logs, append_job_log

logger = logging.getLogger(__name__)
bp = Blueprint("import_plaid", __name__)


def _plaid_unavail(reason: str):
    return jsonify({"error": reason, "configured": False}), 503


@bp.route(URL_PREFIX + "/api/import/plaid/status", methods=["GET"])
@login_required
def api_plaid_status():
    """Return current configuration + connected items."""
    try:
        from app.importers.plaid_importer import is_configured, list_items
    except Exception as e:
        return jsonify({"configured": False, "items": [], "error": str(e)})

    configured = is_configured()
    items = list_items() if configured else []
    return jsonify({
        "configured": configured,
        "env": db.get_setting("plaid_env") or "sandbox",
        "items": items,
        "item_count": len(items),
    })


@bp.route(URL_PREFIX + "/api/import/plaid/settings", methods=["POST"])
@login_required
def api_plaid_save_settings():
    data = request.get_json() or {}
    client_id = (data.get("client_id") or "").strip()
    secret = (data.get("secret") or "").strip()
    env = (data.get("env") or "sandbox").strip().lower()
    if env not in ("sandbox", "development", "production"):
        return jsonify({"error": "invalid env (must be sandbox, development, or production)"}), 400
    if not client_id or not secret:
        return jsonify({"error": "client_id and secret required"}), 400
    db.set_setting("plaid_client_id", client_id)
    db.set_setting("plaid_secret", secret)
    db.set_setting("plaid_env", env)
    db.log_activity("plaid_settings_saved", f"env={env}", user_id=current_user.id)
    return jsonify({"status": "saved", "env": env})


@bp.route(URL_PREFIX + "/api/import/plaid/link-token", methods=["POST"])
@login_required
def api_plaid_link_token():
    """Create a link_token for the frontend Plaid Link widget."""
    try:
        from app.importers.plaid_importer import create_link_token
        result = create_link_token(str(current_user.id))
        return jsonify(result)
    except RuntimeError as e:
        return _plaid_unavail(str(e))
    except Exception as e:
        logger.exception("Plaid link-token error")
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/import/plaid/exchange", methods=["POST"])
@login_required
def api_plaid_exchange():
    """Exchange a public_token returned by Plaid Link for a stored access_token."""
    data = request.get_json() or {}
    public_token = (data.get("public_token") or "").strip()
    if not public_token:
        return jsonify({"error": "public_token required"}), 400
    institution_id = data.get("institution_id") or None
    institution_name = data.get("institution_name") or None
    entity_id = data.get("entity_id") or None

    try:
        from app.importers.plaid_importer import exchange_public_token
        item = exchange_public_token(
            public_token=public_token,
            institution_id=institution_id,
            institution_name=institution_name,
            entity_id=entity_id,
        )
        db.log_activity("plaid_connected", f"{item.get('institution_name','')} ({item.get('item_id','')})",
                        user_id=current_user.id)
        # Don't echo access_token to the client
        item.pop("access_token", None)
        return jsonify({"status": "ok", "item": item})
    except RuntimeError as e:
        return _plaid_unavail(str(e))
    except Exception as e:
        logger.exception("Plaid exchange error")
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/import/plaid/items/<item_id>", methods=["DELETE"])
@login_required
def api_plaid_remove_item(item_id):
    try:
        from app.importers.plaid_importer import remove_item
        ok = remove_item(item_id)
        if not ok:
            return jsonify({"error": "item not found"}), 404
        db.log_activity("plaid_disconnected", item_id, user_id=current_user.id)
        return jsonify({"status": "removed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/import/plaid/start", methods=["POST"])
@login_required
def api_plaid_start():
    """Start an import job for one Plaid item or all items."""
    data = request.get_json() or {}
    item_id = data.get("item_id") or None
    entity_id = data.get("entity_id") or None

    try:
        from app.importers.plaid_importer import is_configured
        if not is_configured():
            return _plaid_unavail("Plaid not configured — set client_id and secret in Settings.")
    except Exception as e:
        return _plaid_unavail(str(e))

    job_id = db.create_import_job(
        "plaid", entity_id=entity_id,
        config_json=json.dumps({"item_id": item_id}),
    )
    _job_logs[job_id] = []

    def _run(jid, iid, eid):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.plaid_importer import run_import
            result = run_import(item_id=iid, entity_id=eid, log=log)
            total = result.get("imported", 0)
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity(
                "import_complete",
                f"Plaid: {total} imported, {result.get('modified',0)} modified, "
                f"{result.get('removed',0)} removed across {result.get('items',0)} items",
            )
        except Exception as e:
            import traceback
            log(f"Fatal error: {e}")
            log(traceback.format_exc()[:600])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(
        target=_run, args=(job_id, item_id, entity_id),
        daemon=True, name=f"plaid-{job_id}",
    ).start()
    return jsonify({"status": "started", "job_id": job_id})
