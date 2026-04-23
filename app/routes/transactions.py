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


@bp.route(URL_PREFIX + "/api/transactions/<int:txn_id>/links")
@login_required
def api_transaction_links(txn_id):
    """Return documents linked to this transaction by cross-source dedup."""
    from app.dedup import get_transaction_links
    links = get_transaction_links(txn_id)
    return jsonify({"txn_id": txn_id, "links": links})


@bp.route(URL_PREFIX + "/api/transactions/dedup/scan", methods=["POST"])
@login_required
def api_dedup_scan():
    """Manually trigger a cross-source dedup scan."""
    from app.dedup import scan_cross_source_matches, backfill_vendor_normalized
    try:
        updated = backfill_vendor_normalized()
        result = scan_cross_source_matches()
        result["vendor_normalized_backfilled"] = updated
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/documents/<int:doc_id>/links")
@login_required
def api_document_links(doc_id):
    """Return transactions linked to this analyzed document."""
    from app.dedup import get_document_links
    links = get_document_links(doc_id)
    return jsonify({"doc_id": doc_id, "links": links})


@bp.route(URL_PREFIX + "/api/transactions/unmatched")
@login_required
def api_transactions_unmatched():
    """Transactions with no linked document (paid but no receipt).

    Optional query params:
      min_amount   — only include abs(amount) >= threshold (default: no floor)
      category     — filter to one or more categories (comma-separated)
    """
    from app.dedup import list_unmatched_transactions
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    limit = min(int(request.args.get("limit", 200)), 1000)

    min_amount_raw = request.args.get("min_amount")
    min_amount = None
    if min_amount_raw:
        try:
            min_amount = float(min_amount_raw)
        except ValueError:
            return jsonify({"error": "min_amount must be numeric"}), 400

    category_raw = request.args.get("category")
    categories = None
    if category_raw:
        categories = [c.strip() for c in category_raw.split(",") if c.strip()]

    rows = list_unmatched_transactions(
        entity_id=entity_id, tax_year=year, limit=limit,
        min_abs_amount=min_amount, categories=categories,
    )
    return jsonify({"count": len(rows), "transactions": rows})


@bp.route(URL_PREFIX + "/api/transactions/audit-risk")
@login_required
def api_audit_risk():
    """Summary of expense/deduction transactions ≥ threshold with no receipt.

    IRS (Pub 463) requires receipts for business expenses ≥ $75. This endpoint
    surfaces how many of those your records are missing.
    """
    from app.dedup import audit_risk_summary, AUDIT_RISK_THRESHOLD
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")

    threshold = AUDIT_RISK_THRESHOLD
    th_raw = request.args.get("threshold")
    if th_raw:
        try:
            threshold = float(th_raw)
            if threshold < 0:
                raise ValueError
        except ValueError:
            return jsonify({"error": "threshold must be non-negative numeric"}), 400

    result = audit_risk_summary(entity_id=entity_id, tax_year=year, threshold=threshold)
    return jsonify(result)


@bp.route(URL_PREFIX + "/api/documents/unmatched")
@login_required
def api_documents_unmatched():
    """Analyzed documents with no linked transaction (have receipt but no bank record)."""
    from app.dedup import list_orphan_documents
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    limit = min(int(request.args.get("limit", 200)), 1000)
    rows = list_orphan_documents(entity_id=entity_id, tax_year=year, limit=limit)
    return jsonify({"count": len(rows), "documents": rows})


@bp.route(URL_PREFIX + "/api/transactions/<int:txn_id>/attach", methods=["POST"])
@login_required
def api_attach_receipt(txn_id):
    """Upload a receipt for an existing transaction. Writes to consume_path so
    Paperless ingests it, and creates an analyzed_document + link immediately.

    Multipart form fields:
      file: the receipt (PDF / image)
    """
    import hashlib
    import os
    import re as _re
    import secrets
    from app.config import CONSUME_PATH
    from app.db.core import get_connection

    txn_row = db.get_transaction(txn_id)
    if not txn_row:
        return jsonify({"error": "transaction not found"}), 404
    txn = dict(txn_row)

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400

    raw = f.read()
    if not raw or len(raw) < 50:
        return jsonify({"error": "file is empty or too small"}), 400
    if len(raw) > 50 * 1024 * 1024:
        return jsonify({"error": "file too large (max 50MB)"}), 400

    ext = os.path.splitext(f.filename)[1].lower() or ".pdf"
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        return jsonify({"error": "unsupported file type"}), 400

    # Choose a destination path based on entity + year
    entity_slug = "personal"
    if txn.get("entity_id"):
        ent = db.get_entity(entity_id=txn["entity_id"])
        if ent:
            entity_slug = ent.get("slug") or "personal"
    year = txn.get("tax_year") or (txn.get("date") or "")[:4] or "unknown"
    dest_dir = os.path.join(CONSUME_PATH, entity_slug, str(year), "receipts_manual")
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = _re.sub(r"[^A-Za-z0-9._\- ]", "_", f.filename)[:120]
    dest_path = os.path.join(dest_dir, safe_name)
    if os.path.exists(dest_path):
        base, _ext = os.path.splitext(safe_name)
        dest_path = os.path.join(dest_dir, f"{base}_{secrets.token_hex(3)}{_ext}")
    with open(dest_path, "wb") as out:
        out.write(raw)

    # Record the PDF hash so Paperless' re-ingestion doesn't double-count
    h = hashlib.sha256(raw).hexdigest()
    try:
        db.record_pdf_hash(h, source="receipt_manual",
                           filename=os.path.basename(dest_path),
                           entity_slug=entity_slug, year=str(year))
    except Exception:
        pass

    # Insert an analyzed_document placeholder (Paperless will later fill in
    # paperless_doc_id via the normal AI analysis pipeline).
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO analyzed_documents
               (entity_id, tax_year, title, doc_type, category, vendor, amount, date,
                confidence, extracted_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                txn.get("entity_id"), txn.get("tax_year"),
                os.path.basename(dest_path), "receipt",
                txn.get("category"), txn.get("vendor"),
                abs(float(txn.get("amount") or 0)), txn.get("date"),
                1.0,  # user-attached = 100% confidence
                '{"source":"manual_upload"}',
            ),
        )
        doc_id = cur.lastrowid
        conn.execute(
            "INSERT INTO transaction_links(txn_id, doc_id, link_type, confidence) VALUES(?,?,?,?)",
            (txn_id, doc_id, "manual", 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    db.log_activity(
        "txn_receipt_attached",
        f"txn={txn_id} file={os.path.basename(dest_path)}",
        user_id=current_user.id,
    )
    return jsonify({
        "status": "attached",
        "doc_id": doc_id,
        "file": os.path.basename(dest_path),
        "bytes": len(raw),
    })


@bp.route(URL_PREFIX + "/api/transactions/links/manual", methods=["POST"])
@login_required
def api_link_manual():
    """Create a manual link between a transaction and a document."""
    data = request.get_json() or {}
    try:
        txn_id = int(data.get("txn_id"))
        doc_id = int(data.get("doc_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "txn_id and doc_id (int) required"}), 400
    from app.dedup import manual_link
    result = manual_link(txn_id, doc_id, confidence=1.0)
    db.log_activity("link_manual", f"txn={txn_id} doc={doc_id}", user_id=current_user.id)
    return jsonify(result)


@bp.route(URL_PREFIX + "/api/transactions/links/remove", methods=["POST"])
@login_required
def api_link_remove():
    """Remove a transaction↔document link."""
    data = request.get_json() or {}
    try:
        txn_id = int(data.get("txn_id"))
        doc_id = int(data.get("doc_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "txn_id and doc_id (int) required"}), 400
    from app.dedup import unlink
    removed = unlink(txn_id, doc_id)
    if removed:
        db.log_activity("link_remove", f"txn={txn_id} doc={doc_id}", user_id=current_user.id)
    return jsonify({"removed": removed})


_BULK_ALLOWED_FIELDS = {
    "entity_id", "tax_year", "category", "doc_type", "vendor"
}


@bp.route(URL_PREFIX + "/api/transactions/bulk", methods=["POST"])
@login_required
def api_transactions_bulk():
    """Bulk update or delete transactions.

    Body:
      {"action": "update", "ids": [1,2,3], "changes": {"category": "expense", "entity_id": 2}}
      {"action": "delete", "ids": [1,2,3]}
    """
    data = request.get_json() or {}
    action = (data.get("action") or "").lower()
    raw_ids = data.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "ids (non-empty list) required"}), 400
    try:
        ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400

    # Cap batch size to prevent runaway updates
    MAX_BULK = 2000
    if len(ids) > MAX_BULK:
        return jsonify({"error": f"too many ids (max {MAX_BULK})"}), 400

    if action == "update":
        changes = data.get("changes") or {}
        if not isinstance(changes, dict):
            return jsonify({"error": "changes must be an object"}), 400
        # Whitelist filter
        clean = {k: v for k, v in changes.items() if k in _BULK_ALLOWED_FIELDS}
        if not clean:
            return jsonify({"error": f"no editable fields in 'changes' (allowed: {sorted(_BULK_ALLOWED_FIELDS)})"}), 400
        # Coerce numeric entity_id
        if "entity_id" in clean and clean["entity_id"] not in (None, ""):
            try:
                clean["entity_id"] = int(clean["entity_id"])
            except (TypeError, ValueError):
                return jsonify({"error": "entity_id must be int or null"}), 400
        if clean.get("entity_id") == "":
            clean["entity_id"] = None
        try:
            updated = db.update_many_transactions(ids, **clean)
            db.log_activity(
                "txn_bulk_update",
                f"{updated} rows, changes={sorted(clean.keys())}",
                user_id=current_user.id,
            )
            return jsonify({"status": "updated", "count": updated, "changes": clean})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if action == "delete":
        try:
            removed = db.delete_many_transactions(ids)
            db.log_activity("txn_bulk_delete", f"{removed} rows",
                            user_id=current_user.id)
            return jsonify({"status": "deleted", "count": removed})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "action must be 'update' or 'delete'"}), 400

