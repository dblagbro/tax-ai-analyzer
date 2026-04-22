"""Import job management: list, status, delete, log polling, cancel."""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes._state import _job_logs, _job_logs_lock, _job_stop_events, _job_stop_lock
from app.routes.helpers import admin_required

logger = logging.getLogger(__name__)

bp = Blueprint("import_jobs", __name__)


@bp.route(URL_PREFIX + "/api/import/jobs")
@login_required
def api_import_jobs():
    return jsonify([dict(r) for r in (db.list_import_jobs(limit=50) or [])])


@bp.route(URL_PREFIX + "/api/import/jobs/<int:job_id>")
@login_required
def api_import_job_status(job_id):
    row = db.get_import_job(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@bp.route(URL_PREFIX + "/api/import/jobs/<int:job_id>", methods=["DELETE"])
@login_required
@admin_required
def api_import_job_delete(job_id):
    job = db.get_import_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job.get("status") in ("running", "pending", "cancelling"):
        return jsonify({"error": "Cannot delete a running job. Cancel it first."}), 400
    db.delete_import_job(job_id)
    with _job_logs_lock:
        _job_logs.pop(job_id, None)
    return jsonify({"status": "deleted"})


@bp.route(URL_PREFIX + "/api/import/jobs/<int:job_id>/logs")
@login_required
def api_job_logs(job_id: int):
    offset = int(request.args.get("offset", 0))
    with _job_logs_lock:
        mem_lines = list((_job_logs.get(job_id) or []))
    if mem_lines:
        return jsonify({"lines": mem_lines[offset:], "total": len(mem_lines), "source": "memory"})
    db_lines, total = db.get_import_job_logs(job_id, offset=offset)
    return jsonify({"lines": db_lines, "total": total, "source": "db"})


@bp.route(URL_PREFIX + "/api/import/jobs/<int:job_id>/cancel", methods=["POST"])
@login_required
def api_import_job_cancel(job_id: int):
    with _job_stop_lock:
        ev = _job_stop_events.get(job_id)
        if ev:
            ev.set()
            db.update_import_job(job_id, status="cancelling")
            return jsonify({"status": "cancelling"})
    job = db.get_import_job(job_id)
    if job and job.get("status") in ("running", "pending", "cancelling"):
        db.update_import_job(job_id, status="cancelled",
                             completed_at=datetime.utcnow().isoformat())
        return jsonify({"status": "cancelled", "note": "Orphaned job marked cancelled"})
    return jsonify({"status": "not_running"})
