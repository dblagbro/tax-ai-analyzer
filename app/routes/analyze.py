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
    """Manual "analyze now": same per-document behaviour as the daemon
    (app.analysis_core.process_document) — including the rules-only
    provisional fallback when no LLM is reachable."""
    if _state._is_analyzing:
        return jsonify({"status": "already_running"})

    def _run():
        _state._is_analyzing = True
        try:
            from app import config
            from app.paperless_client import PaperlessClient
            from app.llm_client import LLMClient, has_llm_capability
            from app.analysis_core import process_document

            llm_provider = db.get_setting("llm_provider") or config.LLM_PROVIDER
            llm_api_key = db.get_setting("llm_api_key") or config.LLM_API_KEY
            llm_model = db.get_setting("llm_model") or config.LLM_MODEL
            paperless_token = db.get_setting("paperless_api_token") or config.PAPERLESS_API_TOKEN

            # 2026-09-05: proxy-aware capability check, not the raw key.
            # 2026-09-06: no LLM → rules-only provisional pass, not a silent return.
            llm_ok = has_llm_capability(llm_api_key)
            client = PaperlessClient(token=paperless_token)
            llm = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model) if llm_ok else None

            db.log_activity("analysis_started", f"Manual trigger ({'llm' if llm_ok else 'rules-only'})")
            all_ids = client.get_all_document_ids()
            analyzed_ids = db.get_analyzed_doc_ids()
            new_ids = [d for d in all_ids if d not in analyzed_ids]
            analyzed = 0
            for doc_id in new_ids[:20]:
                try:
                    doc = client.get_document(doc_id)
                    out = process_document(doc_id, doc, llm=llm, llm_ok=llm_ok,
                                           client=client, log=logger.info)
                    if out["status"] != "empty":
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
