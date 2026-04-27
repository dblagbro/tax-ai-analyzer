"""Admin routes for the bank-onboarding queue (Phase 11A/C/D).

Lets admins curate user-submitted bank-import requests, upload HAR
recordings + narration, view AI-generated Playwright importers, and
approve them before they go live.
"""
import json
import logging
import os

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import DATA_DIR, URL_PREFIX
from app.routes.helpers import admin_required

logger = logging.getLogger(__name__)
bp = Blueprint("bank_onboarding", __name__)

# HAR + narration archive lives outside the SQLite DB (HARs can be 10+ MB).
ONBOARDING_DIR = os.path.join(DATA_DIR, "onboarding")
os.makedirs(ONBOARDING_DIR, exist_ok=True)

# Bound HAR upload size to keep memory usage sane. Real bank-session HARs
# are typically 1-20 MB. 50 MB lets the user record a longer flow.
MAX_HAR_BYTES = 50 * 1024 * 1024


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


# ── recording uploads (Phase 11C-lite) ────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>/recordings",
          methods=["POST"])
@login_required
@admin_required
def api_upload_recording(bank_id):
    """Upload a HAR file + narration text against a pending bank.

    Multipart form fields:
      - har          : .har or .json file (the captured network trace)
      - narration    : free-text user description of what they did
      - dom_snapshot : OPTIONAL .html or .json file (page DOM at end of flow)
    """
    bank = db.get_pending_bank(bank_id)
    if not bank:
        return jsonify({"error": "bank not found"}), 404

    har_file = request.files.get("har")
    narration = (request.form.get("narration") or "").strip()
    dom_file = request.files.get("dom_snapshot")

    if not har_file and not narration:
        return jsonify({"error": "either 'har' file or 'narration' text required"}), 400

    # Validate + persist HAR
    har_path = None
    byte_size = 0
    if har_file:
        har_bytes = har_file.read()
        if len(har_bytes) > MAX_HAR_BYTES:
            return jsonify({
                "error": f"HAR exceeds size cap ({MAX_HAR_BYTES // 1_000_000} MB)"
            }), 413
        # Sanity-check it's actually a HAR (or at least JSON).
        try:
            har_json = json.loads(har_bytes)
            if not isinstance(har_json, dict) or "log" not in har_json:
                return jsonify({"error": "file does not look like a HAR (missing top-level 'log')"}), 400
        except Exception as e:
            return jsonify({"error": f"HAR is not valid JSON: {e}"}), 400
        bank_dir = os.path.join(ONBOARDING_DIR, bank["slug"])
        os.makedirs(bank_dir, exist_ok=True)
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        har_path = os.path.join(bank_dir, f"recording_{ts}.har")
        with open(har_path, "wb") as f:
            f.write(har_bytes)
        byte_size = len(har_bytes)

    # Persist DOM snapshot if provided
    dom_path = None
    if dom_file:
        dom_bytes = dom_file.read()
        if len(dom_bytes) > MAX_HAR_BYTES:
            return jsonify({"error": "DOM snapshot too large"}), 413
        bank_dir = os.path.join(ONBOARDING_DIR, bank["slug"])
        os.makedirs(bank_dir, exist_ok=True)
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        # Preserve extension if recognizable
        ext = ".html" if dom_file.filename.lower().endswith(".html") else ".json"
        dom_path = os.path.join(bank_dir, f"dom_{ts}{ext}")
        with open(dom_path, "wb") as f:
            f.write(dom_bytes)

    rec_id = db.add_recording(
        pending_bank_id=bank_id,
        har_path=har_path,
        narration_text=narration,
        dom_snapshot_path=dom_path,
        byte_size=byte_size,
    )
    db.update_pending_bank(bank_id, status="recorded")
    db.log_activity("bank_recording_uploaded",
                    f"bank={bank_id} rec={rec_id} har_bytes={byte_size}",
                    user_id=current_user.id)
    return jsonify({
        "id": rec_id,
        "byte_size": byte_size,
        "har_path": har_path,
        "status": "recorded",
    }), 201


@bp.route(URL_PREFIX + "/api/admin/banks/<int:bank_id>/recordings/<int:rec_id>",
          methods=["GET"])
@login_required
@admin_required
def api_recording_get(bank_id, rec_id):
    """Return recording metadata. The HAR file itself is NOT inlined here
    (could be tens of MB) — use ?download=1 to get the raw bytes."""
    rec = db.get_recording(rec_id)
    if not rec or rec.get("pending_bank_id") != bank_id:
        return jsonify({"error": "not found"}), 404
    if request.args.get("download") == "1":
        from flask import send_file
        if not rec.get("har_path") or not os.path.exists(rec["har_path"]):
            return jsonify({"error": "HAR file not on disk"}), 404
        return send_file(rec["har_path"], as_attachment=True,
                         download_name=os.path.basename(rec["har_path"]))
    return jsonify(rec)
