"""Document list, detail, override, recategorize, dedup, and title backfill."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import _row_list, admin_required

logger = logging.getLogger(__name__)

bp = Blueprint("documents", __name__)


@bp.route(URL_PREFIX + "/api/documents")
@login_required
def api_documents_list():
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    category = request.args.get("category")
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = db.get_analyzed_documents(entity_id=entity_id, tax_year=year,
                                     category=category, limit=limit)
    docs = _row_list(rows)
    try:
        from app.state import get_result
        for d in docs:
            if not d.get("title"):
                sr = get_result(d.get("paperless_doc_id") or 0)
                paperless_title = sr.get("title", "")
                if paperless_title:
                    d["title"] = paperless_title
                else:
                    parts = [d.get("doc_type", "")]
                    if d.get("vendor"):
                        parts.append(f"— {d['vendor']}")
                    if d.get("tax_year"):
                        parts.append(f"({d['tax_year']})")
                    d["title"] = " ".join(p for p in parts if p) or f"Document {d.get('paperless_doc_id','?')}"
    except Exception:
        for d in docs:
            if not d.get("title"):
                d["title"] = f"Document {d.get('paperless_doc_id','?')}"
    return jsonify({"total": len(docs), "documents": docs})


@bp.route(URL_PREFIX + "/api/documents/dedup", methods=["POST"])
@login_required
@admin_required
def api_documents_dedup():
    result = db.flag_duplicate_analyzed_docs()
    db.log_activity(
        "dedup_scan",
        f"Flagged {result['flagged']} duplicates in {result['groups']} groups "
        f"({result['already_flagged']} already flagged)",
        user_id=current_user.id,
    )
    return jsonify({"status": "ok", **result})


@bp.route(URL_PREFIX + "/api/documents/backfill-titles", methods=["POST"])
@login_required
def api_backfill_titles():
    from app.paperless_client import get_document
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT paperless_doc_id, doc_type, vendor, tax_year "
            "FROM analyzed_documents WHERE title IS NULL OR title = ''"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify({"status": "ok", "updated": 0, "message": "All titles already populated"})

    updated = 0
    errors = []
    for row in rows:
        doc_id = row["paperless_doc_id"]
        try:
            paperless_doc = get_document(doc_id)
            pl_title = (paperless_doc.get("title") or "").strip()
            if pl_title and pl_title != str(doc_id):
                title = pl_title
            else:
                parts = [row["doc_type"] or ""]
                if row["vendor"]:
                    parts.append(f"— {row['vendor']}")
                if row["tax_year"]:
                    parts.append(f"({row['tax_year']})")
                title = " ".join(p for p in parts if p) or f"Document {doc_id}"
            conn2 = db.get_connection()
            try:
                conn2.execute(
                    "UPDATE analyzed_documents SET title=? WHERE paperless_doc_id=?",
                    (title, doc_id)
                )
                conn2.commit()
            finally:
                conn2.close()
            updated += 1
        except Exception as e:
            errors.append(f"doc {doc_id}: {e}")

    return jsonify({"status": "ok", "updated": updated, "errors": errors[:10]})


@bp.route(URL_PREFIX + "/api/documents/<int:doc_id>")
@login_required
def api_document_detail(doc_id):
    try:
        from app.state import get_result
        state_doc = get_result(doc_id)
        paperless_doc = {}
        try:
            from app.paperless_client import get_document
            paperless_doc = get_document(doc_id)
        except Exception:
            pass
        conn = db.get_connection()
        row = conn.execute(
            "SELECT d.*, e.name as entity_name FROM analyzed_documents d "
            "LEFT JOIN entities e ON e.id=d.entity_id WHERE d.paperless_doc_id=?",
            (doc_id,)).fetchone()
        conn.close()
        db_rec = dict(row) if row else {}
        return jsonify({**paperless_doc, **state_doc, **db_rec, "doc_id": doc_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/documents/<int:doc_id>/recategorize", methods=["POST"])
@login_required
def api_document_recategorize(doc_id):
    def _run():
        try:
            from app.paperless_client import get_document, apply_tags
            from app.categorizer import categorize
            from app.extractor import extract
            from app.state import mark_analyzed
            doc = get_document(doc_id)
            content = doc.get("content", "")
            title = doc.get("title", f"Document {doc_id}")
            cat = categorize(content, title)
            ext = extract(content)
            result = {"doc_id": doc_id, "title": title,
                      "analyzed_at": datetime.utcnow().isoformat(), "recategorized": True,
                      **cat,
                      **{k: v for k, v in ext.items() if v is not None and k not in cat}}
            mark_analyzed(doc_id, result)
            entity_row = db.get_entity(slug=cat.get("entity", "personal"))
            db.mark_document_analyzed(
                paperless_doc_id=doc_id,
                entity_id=entity_row["id"] if entity_row else None,
                tax_year=str(cat.get("tax_year") or ""),
                doc_type=cat.get("doc_type", "other"),
                category=cat.get("category", "other"),
                vendor=cat.get("vendor") or "",
                amount=float(cat.get("amount") or 0),
                date=ext.get("date") or "",
                confidence=float(cat.get("confidence") or 0),
                extracted_json=json.dumps(ext),
            )
            try:
                tags = [t for t in cat.get("tags", []) if t] + [
                    f"tax-{cat.get('entity','personal')}", f"year-{cat.get('tax_year','unknown')}"]
                apply_tags(doc_id, tags)
            except Exception:
                pass
            db.log_activity("doc_recategorized", f"Doc {doc_id}: {cat.get('doc_type')}")
        except Exception as e:
            logger.error("Recategorize doc %d: %s", doc_id, e)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "recategorizing", "doc_id": doc_id})


@bp.route(URL_PREFIX + "/api/documents/<int:doc_id>/override", methods=["POST"])
@login_required
def api_document_override(doc_id):
    from app.llm_client import VALID_DOC_TYPES, VALID_CATEGORIES
    data = request.get_json() or {}
    allowed = {"doc_type", "category", "vendor", "amount", "date", "tax_year", "title"}
    if "doc_type" in data and data["doc_type"] not in VALID_DOC_TYPES:
        return jsonify({"error": f"Invalid doc_type: {data['doc_type']}"}), 400
    if "category" in data and data["category"] not in VALID_CATEGORIES:
        return jsonify({"error": f"Invalid category: {data['category']}"}), 400
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "No valid fields provided"}), 400
    conn = db.get_connection()
    try:
        sets = [f"{k}=?" for k in fields] + ["confidence=1.0"]
        params = list(fields.values()) + [doc_id]
        conn.execute(
            f"UPDATE analyzed_documents SET {', '.join(sets)} WHERE paperless_doc_id=?",
            params,
        )
        conn.commit()
        db.log_activity("doc_override",
                        f"Doc {doc_id} manually overridden: {fields}",
                        user_id=current_user.id)
        return jsonify({"status": "ok", "doc_id": doc_id, "updated": fields})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
