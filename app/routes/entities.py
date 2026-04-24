"""Entity CRUD, tax year management, user access control, and user profile."""
import json
import logging
import re

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import _row_list, admin_required

logger = logging.getLogger(__name__)

bp = Blueprint("entities", __name__)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _validate_color(color: str) -> tuple[bool, str]:
    """Return (is_valid, normalized_value). Empty/None falls back to the default."""
    c = (color or "").strip()
    if not c:
        return True, "#1a3c5e"
    if _HEX_COLOR_RE.match(c):
        return True, c
    return False, c


@bp.route(URL_PREFIX + "/api/entities", methods=["GET"])
@login_required
def api_entities_list():
    return jsonify(_row_list(db.list_entities()))


@bp.route(URL_PREFIX + "/api/entities", methods=["POST"])
@login_required
@admin_required
def api_entities_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok, color = _validate_color(data.get("color"))
    if not ok:
        return jsonify({"error": "color must be hex (#abc or #aabbcc[aa])"}), 400
    slug = re.sub(r"[^\w]", "_", name.lower())
    try:
        row = db.create_entity(
            name=name, slug=slug,
            entity_type=data.get("type", "personal"),
            description=data.get("description", ""),
            tax_id=data.get("tax_id", ""),
            color=color,
            parent_entity_id=data.get("parent_entity_id") or None,
            display_name=data.get("display_name") or name,
            metadata_json=json.dumps(data.get("metadata", {})),
            sort_order=data.get("sort_order", 0),
        )
        db.log_activity("entity_created", f"Entity: {name}", user_id=current_user.id)
        # db.create_entity returns the full row dict; extract the id so API
        # clients see {"id": <int>, ...} as they expect.
        return jsonify({"id": row["id"], "name": name, "slug": slug}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route(URL_PREFIX + "/api/entities/<int:entity_id>", methods=["POST"])
@login_required
@admin_required
def api_entities_update(entity_id):
    data = request.get_json() or {}
    if "metadata" in data:
        data["metadata_json"] = json.dumps(data.pop("metadata"))
    if "color" in data:
        ok, color = _validate_color(data["color"])
        if not ok:
            return jsonify({"error": "color must be hex (#abc or #aabbcc[aa])"}), 400
        data["color"] = color
    db.update_entity(entity_id, **data)
    row = db.get_entity(entity_id=entity_id)
    db.log_activity("entity_updated", f"ID: {entity_id}", user_id=current_user.id)
    return jsonify(dict(row) if row else {})


@bp.route(URL_PREFIX + "/api/entities/<int:entity_id>/archive", methods=["POST"])
@login_required
@admin_required
def api_entities_archive(entity_id):
    db.update_entity(entity_id, archived=1)
    db.log_activity("entity_archived", f"ID: {entity_id}", user_id=current_user.id)
    return jsonify({"status": "archived"})


@bp.route(URL_PREFIX + "/api/entities/tree")
@login_required
def api_entities_tree():
    return jsonify(db.get_entity_tree())


@bp.route(URL_PREFIX + "/api/entities/<int:entity_id>/merge", methods=["POST"])
@login_required
@admin_required
def api_entity_merge(entity_id):
    data = request.get_json() or {}
    target_id = data.get("target_entity_id")
    if not target_id:
        return jsonify({"error": "target_entity_id required"}), 400
    if int(target_id) == entity_id:
        return jsonify({"error": "source and target must differ"}), 400
    source = db.get_entity(entity_id=entity_id)
    target = db.get_entity(entity_id=int(target_id))
    if not source or not target:
        return jsonify({"error": "entity not found"}), 404
    counts = db.merge_entities(entity_id, int(target_id))
    db.log_activity(
        "entity_merged",
        f"Merged '{source['name']}' → '{target['name']}': {counts}",
        user_id=current_user.id,
    )
    return jsonify({"status": "merged", "moved": counts})


@bp.route(URL_PREFIX + "/api/entities/<int:entity_id>/transfer-docs", methods=["POST"])
@login_required
@admin_required
def api_entity_transfer_docs(entity_id):
    data = request.get_json() or {}
    target_id = data.get("target_entity_id")
    doc_ids = data.get("doc_ids") or []
    txn_ids = data.get("txn_ids") or []
    if not target_id:
        return jsonify({"error": "target_entity_id required"}), 400
    conn = db.get_connection()
    moved = {"documents": 0, "transactions": 0}
    try:
        for did in doc_ids:
            conn.execute("UPDATE analyzed_documents SET entity_id=? WHERE id=? AND entity_id=?",
                         (target_id, did, entity_id))
            moved["documents"] += conn.execute("SELECT changes()").fetchone()[0]
        for tid in txn_ids:
            conn.execute("UPDATE transactions SET entity_id=? WHERE id=? AND entity_id=?",
                         (target_id, tid, entity_id))
            moved["transactions"] += conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    db.log_activity("entity_transfer", f"Transferred {moved} from {entity_id} → {target_id}",
                    user_id=current_user.id)
    return jsonify({"status": "ok", "moved": moved})


@bp.route(URL_PREFIX + "/api/entities/<int:entity_id>/stats")
@login_required
def api_entity_stats(entity_id):
    row = db.get_entity(entity_id=entity_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    summary = db.get_financial_summary(entity_id=entity_id)
    txn_summary = db.get_transaction_summary(entity_id=entity_id)
    years = _row_list(db.list_tax_years(entity_id=entity_id))
    return jsonify({
        "entity": dict(row),
        "income": round(summary["income"], 2),
        "expenses": round(summary["expense"] + summary["deduction"], 2),
        "net": round(summary["net"], 2),
        "doc_count": sum(summary["counts"].values()),
        "txn_count": sum(v["count"] for v in txn_summary.values()),
        "years": years,
    })


@bp.route(URL_PREFIX + "/api/entities/<int:entity_id>/years", methods=["POST"])
@login_required
@admin_required
def api_entity_add_year(entity_id):
    data = request.get_json() or {}
    year = data.get("year", "").strip()
    if not year or not re.match(r"^\d{4}$", year):
        return jsonify({"error": "valid 4-digit year required"}), 400
    if not db.get_entity(entity_id=entity_id):
        return jsonify({"error": "entity not found"}), 404
    ty_id = db.ensure_tax_year(entity_id, year)
    return jsonify({"status": "ok", "tax_year_id": ty_id, "year": year})



@bp.route(URL_PREFIX + "/api/user/profile", methods=["GET"])
@login_required
def api_user_profile_get():
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (current_user.id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        d = dict(row)
        d.pop("password_hash", None)
        try:
            d["profile"] = json.loads(d.get("profile_json") or "{}")
        except Exception:
            d["profile"] = {}
        return jsonify(d)
    finally:
        conn.close()


@bp.route(URL_PREFIX + "/api/user/profile", methods=["POST"])
@login_required
def api_user_profile_save():
    data = request.get_json() or {}
    conn = db.get_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "profile_json" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN profile_json TEXT DEFAULT '{}'")
            conn.commit()
        profile = {
            "full_name": data.get("full_name", ""),
            "email": data.get("email", ""),
            "phone": data.get("phone", ""),
            "address": data.get("address", ""),
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "zip": data.get("zip", ""),
            "notify_email": data.get("notify_email", False),
            "notify_import_complete": data.get("notify_import_complete", False),
        }
        conn.execute("UPDATE users SET profile_json=? WHERE id=?",
                     (json.dumps(profile), current_user.id))
        conn.commit()
        return jsonify({"status": "saved"})
    finally:
        conn.close()
