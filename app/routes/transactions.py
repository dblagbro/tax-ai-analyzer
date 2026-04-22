"""Transaction CRUD and CSV import helper."""
import csv
import io
import logging
import re
import threading
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


# ---------------------------------------------------------------------------
# CSV helpers (used by multiple import routes)
# ---------------------------------------------------------------------------

def _parse_csv(csv_bytes: bytes, source: str, entity_id, year: str, col_map: dict):
    txns, errors = [], []
    try:
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for i, row in enumerate(reader):
            try:
                date_val = row.get(col_map.get("date", "Date"), "").strip()
                desc_val = row.get(col_map.get("description", "Description"), "").strip()
                raw_amt = row.get(col_map.get("amount", "Amount"), "0").strip()
                amount_val = float(re.sub(r"[,$\s]", "", raw_amt or "0") or "0")
                if not date_val and not desc_val:
                    continue
                row_year = year
                if not row_year:
                    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
                        try:
                            row_year = str(datetime.strptime(date_val, fmt).year)
                            break
                        except ValueError:
                            pass
                txns.append({
                    "source": source,
                    "source_id": f"{source}_{i}_{date_val}_{amount_val}",
                    "entity_id": entity_id,
                    "tax_year": row_year or "",
                    "date": date_val,
                    "amount": abs(amount_val),
                    "vendor": "",
                    "description": desc_val,
                    "category": "expense" if amount_val < 0 else "income",
                })
            except Exception as e:
                errors.append(f"Row {i+2}: {e}")
    except Exception as e:
        return [], str(e)
    return txns, ("; ".join(errors[:5]) if errors else None)


def _run_csv_job(job_id, csv_bytes, source, entity_id, year, col_map):
    db.update_import_job(job_id, status="running",
                         started_at=datetime.utcnow().isoformat())
    txns, err = _parse_csv(csv_bytes, source, entity_id, year, col_map)
    if err and not txns:
        db.update_import_job(job_id, status="error", error_msg=err,
                             completed_at=datetime.utcnow().isoformat())
        return
    saved = 0
    for t in txns:
        try:
            db.upsert_transaction(**t)
            saved += 1
        except Exception:
            pass
    db.update_import_job(job_id, status="completed",
                         count_imported=saved,
                         completed_at=datetime.utcnow().isoformat())
    db.log_activity("import_complete", f"{source}: {saved} transactions")
