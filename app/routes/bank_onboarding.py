"""Admin routes for the bank-onboarding queue (Phase 11A).

Lets admins curate user-submitted bank-import requests, view recordings,
and approve generated Playwright importers before they go live.

The actual codegen + recording-upload routes are built on top of this in
Phase 11C/D and live in a sibling module.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import admin_required

logger = logging.getLogger(__name__)
bp = Blueprint("bank_onboarding", __name__)


# ── pending-bank queue ────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/admin/banks/queue", methods=["GET"])
@login_required
@admin_required
def api_banks_queue():
    """List the bank-onboarding queue. Optional ?status=<status> filter."""
    status = request.args.get("status") or None
    return jsonify({"banks": db.list_pending_banks(status=status)})


@bp.route(URL_PREFIX + "/api/admin/banks", methods=["POST"])
@login_required
@admin_required
def api_banks_create():
    """Submit a new bank to the onboarding queue."""
    data = request.get_json(silent=True) or {}
    name = (data.get("display_name") or "").strip()
    url = (data.get("login_url") or "").strip()
    if not name:
        return jsonify({"error": "display_name required"}), 400
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "login_url must be http(s)://..."}), 400
    bank_id = db.create_pending_bank(
        display_name=name,
        login_url=url,
        statements_url=(data.get("statements_url") or "").strip(),
        platform_hint=(data.get("platform_hint") or "").strip(),
        submitted_by=current_user.id,
        notes=(data.get("notes") or "").strip(),
    )
    db.log_activity("bank_submitted", f"{name} ({url})", user_id=current_user.id)
    return jsonify({"id": bank_id, "status": "created"}), 201


@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>", methods=["GET"])
@login_required
@admin_required
def api_banks_get(bank_id):
    bank = db.get_pending_bank(bank_id)
    if not bank:
        return jsonify({"error": "not found"}), 404
    bank["recordings"] = db.list_recordings(bank_id)
    bank["generated"] = db.list_generated_importers(bank_id)
    return jsonify(bank)


@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>", methods=["POST"])
@login_required
@admin_required
def api_banks_update(bank_id):
    """Mutate notes / status / statements_url / platform_hint."""
    bank = db.get_pending_bank(bank_id)
    if not bank:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        ok = db.update_pending_bank(
            bank_id,
            status=data.get("status"),
            notes=data.get("notes"),
            statements_url=data.get("statements_url"),
            platform_hint=data.get("platform_hint"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "no valid fields to update"}), 400
    db.log_activity("bank_updated", f"id={bank_id}", user_id=current_user.id)
    return jsonify(db.get_pending_bank(bank_id))


@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>", methods=["DELETE"])
@login_required
@admin_required
def api_banks_delete(bank_id):
    bank = db.get_pending_bank(bank_id)
    if not bank:
        return jsonify({"error": "not found"}), 404
    db.delete_pending_bank(bank_id)
    db.log_activity("bank_deleted",
                    f"{bank.get('display_name')} (id={bank_id})",
                    user_id=current_user.id)
    return jsonify({"status": "deleted"})


# ── generated-importer review ─────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>/generated/<int:gen_id>",
          methods=["GET"])
@login_required
@admin_required
def api_generated_get(bank_id, gen_id):
    """Return the full source code + metadata for a single generated importer."""
    gen = db.get_generated_importer(gen_id)
    if not gen or gen.get("pending_bank_id") != bank_id:
        return jsonify({"error": "not found"}), 404
    return jsonify(gen)


@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>/generated/<int:gen_id>/approve",
          methods=["POST"])
@login_required
@admin_required
def api_generated_approve(bank_id, gen_id):
    """Mark a generated importer as approved. Does NOT auto-deploy — admin still
    has to copy the source into app/importers/ and register a route. This
    endpoint just records the approval timestamp + user."""
    gen = db.get_generated_importer(gen_id)
    if not gen or gen.get("pending_bank_id") != bank_id:
        return jsonify({"error": "not found"}), 404
    db.approve_generated_importer(gen_id, approved_by=current_user.id)
    db.update_pending_bank(bank_id, status="approved")
    db.log_activity("bank_approved", f"id={bank_id} gen_id={gen_id}",
                    user_id=current_user.id)
    return jsonify({"status": "approved"})
