"""Manual analysis trigger and status endpoint."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes._state import _is_analyzing as _analyzing_flag
from app.routes.helpers import _row_list

logger = logging.getLogger(__name__)

bp = Blueprint("analyze", __name__)

# Module-level flag — imported from _state but shadowed here for write access
import app.routes._state as _state


@bp.route(URL_PREFIX + "/api/analyze/trigger", methods=["POST"])
@login_required
def api_analyze_trigger():
    if _state._is_analyzing:
        return jsonify({"status": "already_running"})

    def _run():
        _state._is_analyzing = True
        try:
            from app.paperless_client import get_all_document_ids, get_document, apply_tags
            from app.categorizer import categorize
            from app.extractor import extract
            from app.state import is_analyzed, mark_analyzed
            from app.vector_store import index_document

            db.log_activity("analysis_started", "Manual trigger")
            doc_ids = get_all_document_ids()
            new_ids = [d for d in doc_ids if not is_analyzed(d)]
            analyzed = 0

            for doc_id in new_ids[:20]:
                try:
                    doc = get_document(doc_id)
                    content = doc.get("content", "")
                    title = doc.get("title", f"Document {doc_id}")
                    if not content or len(content.strip()) < 10:
                        mark_analyzed(doc_id, {"doc_id": doc_id, "title": title,
                                               "skipped": True, "reason": "no_content"})
                        continue
                    cat = categorize(content, title)
                    ext = extract(content)
                    result = {
                        "doc_id": doc_id, "title": title,
                        "analyzed_at": datetime.utcnow().isoformat(),
                        **cat,
                        **{k: v for k, v in ext.items()
                           if v is not None and k not in cat},
                    }
                    from app.main import _apply_business_rules
                    result = _apply_business_rules(result, content, title)
                    entity_tag = cat.get("entity") or "personal"
                    year_tag = str(cat.get("tax_year") or "unknown")
                    tags = [t for t in cat.get("tags", []) if t] + [
                        f"tax-{entity_tag}", f"year-{year_tag}"]
                    try:
                        apply_tags(doc_id, tags)
                    except Exception:
                        pass
                    try:
                        index_document(doc_id, title, content, {
                            "doc_type": cat.get("doc_type"),
                            "category": cat.get("category"),
                            "entity": cat.get("entity"),
                            "tax_year": cat.get("tax_year"),
                        })
                    except Exception:
                        pass
                    mark_analyzed(doc_id, result)
                    entity_row = db.get_entity(slug=entity_tag)
                    db.mark_document_analyzed(
                        paperless_doc_id=doc_id,
                        entity_id=entity_row["id"] if entity_row else None,
                        tax_year=year_tag,
                        doc_type=cat.get("doc_type", "other"),
                        category=cat.get("category", "other"),
                        vendor=cat.get("vendor") or "",
                        amount=float(cat.get("amount") or 0),
                        date=ext.get("date") or "",
                        confidence=float(cat.get("confidence") or 0),
                        extracted_json=json.dumps(ext),
                    )
                    analyzed += 1
                    db.log_activity("doc_analyzed",
                                    f"Doc {doc_id}: {cat.get('doc_type')} / "
                                    f"{entity_tag} / ${cat.get('amount') or 0}")
                except Exception as e:
                    logger.error("Error analyzing doc %d: %s", doc_id, e)
                    mark_analyzed(doc_id, {"doc_id": doc_id, "error": str(e),
                                           "analyzed_at": datetime.utcnow().isoformat()})
            db.log_activity("analysis_complete", f"Analyzed {analyzed} docs")
        except Exception as e:
            db.log_activity("analysis_error", str(e))
        finally:
            _state._is_analyzing = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@bp.route(URL_PREFIX + "/api/analyze/status")
@login_required
def api_analyze_status():
    recent = _row_list(db.get_recent_activity(10))
    return jsonify({"is_analyzing": _state._is_analyzing, "recent_log": recent})


@bp.route(URL_PREFIX + "/api/analyze", methods=["POST"])
@login_required
def api_analyze_alias():
    return api_analyze_trigger()
