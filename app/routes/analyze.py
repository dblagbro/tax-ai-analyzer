"""Manual analysis trigger and status endpoint."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import _row_list

logger = logging.getLogger(__name__)

bp = Blueprint("analyze", __name__)

import app.routes._state as _state


@bp.route(URL_PREFIX + "/api/analyze/trigger", methods=["POST"])
@login_required
def api_analyze_trigger():
    if _state._is_analyzing:
        return jsonify({"status": "already_running"})

    def _run():
        _state._is_analyzing = True
        try:
            from app import config
            from app.paperless_client import PaperlessClient
            from app.llm_client import LLMClient
            from app.checks.financial_rules import apply_business_rules, validate_document

            llm_provider = db.get_setting("llm_provider") or config.LLM_PROVIDER
            llm_api_key = db.get_setting("llm_api_key") or config.LLM_API_KEY
            llm_model = db.get_setting("llm_model") or config.LLM_MODEL
            paperless_token = db.get_setting("paperless_api_token") or config.PAPERLESS_API_TOKEN

            if not llm_api_key:
                db.log_activity("analysis_error", "LLM API key not configured")
                return

            client = PaperlessClient(token=paperless_token)
            llm = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)

            db.log_activity("analysis_started", "Manual trigger")
            all_ids = client.get_all_document_ids()
            analyzed_ids = db.get_analyzed_doc_ids()
            new_ids = [d for d in all_ids if d not in analyzed_ids]
            analyzed = 0

            for doc_id in new_ids[:20]:
                try:
                    doc = client.get_document(doc_id)
                    content = doc.get("content", "")
                    title = doc.get("title", f"Document {doc_id}")
                    tags = [t for t in doc.get("tags", [])]

                    entity_hint = "personal"
                    year_hint = None
                    for tag_name in tags:
                        if isinstance(tag_name, str):
                            if tag_name.startswith("tax-"):
                                entity_hint = tag_name[4:]
                            elif tag_name.startswith("year-"):
                                year_hint = tag_name[5:]

                    if not content or len(content.strip()) < 10:
                        db.mark_document_analyzed(
                            doc_id, None, year_hint, "other", "other",
                            "", None, None, 0.1, "{}"
                        )
                        continue

                    result = llm.analyze_document(content, title, entity_hint, year_hint)
                    result = apply_business_rules(result, content, title)

                    entity = db.get_entity(slug=result.get("entity", entity_hint))
                    entity_id = entity["id"] if entity else None
                    tax_year = result.get("tax_year") or year_hint

                    validation = validate_document(
                        result.get("doc_type", "other"),
                        result.get("category", "other"),
                        result.get("amount"),
                        result.get("date"),
                        tax_year,
                        result,
                    )
                    confidence = max(0.0, (result.get("confidence", 0.7)
                                          - validation.get("confidence_penalty", 0)))

                    ai_title = result.get("title", "").strip()
                    if not ai_title:
                        parts = [result.get("doc_type", "")]
                        if result.get("vendor"):
                            parts.append(f"— {result['vendor']}")
                        if tax_year:
                            parts.append(f"({tax_year})")
                        ai_title = " ".join(p for p in parts if p) or title

                    is_dup = False
                    if result.get("vendor") and result.get("amount") and result.get("date"):
                        is_dup = db.is_near_duplicate_analyzed_doc(
                            vendor=result.get("vendor", ""),
                            amount=result.get("amount"),
                            date=result.get("date"),
                            doc_type=result.get("doc_type", "other"),
                            paperless_doc_id=doc_id,
                        )

                    db.mark_document_analyzed(
                        paperless_doc_id=doc_id,
                        entity_id=entity_id,
                        tax_year=str(tax_year) if tax_year else None,
                        doc_type=result.get("doc_type", "other"),
                        category=result.get("category", "other"),
                        vendor=result.get("vendor", ""),
                        amount=result.get("amount"),
                        date=result.get("date"),
                        confidence=confidence,
                        extracted_json=json.dumps(result),
                        title=ai_title,
                        is_duplicate=1 if is_dup else 0,
                    )

                    try:
                        from app import vector_store as vs
                        vs.embed_document(
                            doc_id=str(doc_id),
                            title=ai_title,
                            content=content[:4000],
                            metadata={
                                "entity_slug": result.get("entity", entity_hint),
                                "tax_year": str(tax_year) if tax_year else "",
                                "doc_type": result.get("doc_type", "other"),
                                "category": result.get("category", "other"),
                                "vendor": result.get("vendor", ""),
                                "amount": str(result.get("amount") or ""),
                            },
                        )
                    except Exception:
                        pass

                    entity_slug = result.get("entity", entity_hint)
                    tag_year = str(tax_year) if tax_year else "unknown"
                    tags_to_apply = [f"tax-{entity_slug}", f"year-{tag_year}",
                                     result.get("doc_type", "other")]
                    try:
                        client.apply_tags(doc_id, [t for t in tags_to_apply if t])
                    except Exception:
                        pass

                    db.log_activity("doc_analyzed",
                                    f"Doc {doc_id}: {result.get('doc_type')} / "
                                    f"{entity_slug} / ${result.get('amount') or 0}")
                    analyzed += 1
                except Exception as e:
                    logger.error("Error analyzing doc %d: %s", doc_id, e)

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
