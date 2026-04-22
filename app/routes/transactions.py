"""Transaction CRUD routes."""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import _row_list

logger = logging.getLogger(__name__)

bp = Blueprint("transactions", __name__)


@bp.route(URL_PREFIX + "/api/transactions", methods=["GET"])
@login_required
def api_transactions_list():
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    source = request.args.get("source")
    limit = min(int(request.args.get("limit", 100)), 1000)
    rows = db.list_transactions(entity_id=entity_id, tax_year=year,
                                source=source, limit=limit)
    return jsonify({"total": len(rows), "transactions": _row_list(rows)})


@bp.route(URL_PREFIX + "/api/transactions", methods=["POST"])
@login_required
def api_transactions_create():
    data = request.get_json() or {}
    for field in ("date", "amount", "description"):
        if not data.get(field):
            return jsonify({"error": f"{field} required"}), 400
    try:
        tid = db.upsert_transaction(
            source="manual",
            source_id=f"manual_{datetime.utcnow().timestamp()}",
            entity_id=data.get("entity_id"),
            tax_year=data.get("year") or data.get("tax_year", ""),
            date=data["date"],
            amount=float(data["amount"]),
            vendor=data.get("vendor", ""),
            description=data["description"],
            category=data.get("category", ""),
            doc_type=data.get("doc_type", ""),
        )
        db.log_activity("txn_created", data["description"][:80], user_id=current_user.id)
        return jsonify({"id": tid, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route(URL_PREFIX + "/api/transactions/<int:txn_id>/edit", methods=["POST"])
@login_required
def api_transactions_edit(txn_id):
    data = request.get_json() or {}
    if not db.get_transaction(txn_id):
        return jsonify({"error": "not found"}), 404
    db.update_transaction(txn_id, **data)
    db.log_activity("txn_updated", f"ID: {txn_id}", user_id=current_user.id)
    return jsonify({"status": "updated", "id": txn_id})

