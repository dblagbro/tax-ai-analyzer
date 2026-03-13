#!/usr/bin/env python3
"""Financial AI Analyzer — main entry point."""
import logging
import os
import sys
import threading
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Activity log for web UI
_activity_log: list[str] = []
_analysis_status = {"running": False, "last_run": None, "analyzed_this_cycle": 0}


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    logger.info(msg)
    _activity_log.append(entry)
    if len(_activity_log) > 500:
        _activity_log.pop(0)


def get_activity_log() -> list[str]:
    return list(reversed(_activity_log[-100:]))


def get_analysis_status() -> dict:
    return _analysis_status.copy()


def _apply_business_rules(result: dict, content: str, title: str) -> dict:
    """Deterministic post-processing applied after AI classification.

    This is the only place where content, title, AND the AI result are all
    available simultaneously. Rules here are non-negotiable overrides that
    fix systematic AI misclassification patterns.
    """
    combined = ((title or "") + " " + (content or "")[:1000]).lower()

    # Rule 1: Proposals / quotes / bids / estimates are NOT expenses.
    # They describe work to be done, not work invoiced or paid.
    proposal_signals = [
        "proposal", "we are pleased to submit", "scope of work",
        "work to be performed", "price quote", "cost estimate",
        "request for proposal", " bid ", "quotation",
    ]
    if any(sig in combined for sig in proposal_signals):
        if result.get("doc_type") in ("invoice", "receipt", "other"):
            result["doc_type"] = "other"
            result["category"] = "other"
            result["amount"] = None
            tags = result.setdefault("tags", [])
            if "proposal" not in tags:
                tags.append("proposal")
        return result  # no further rules needed for proposals

    # Rule 2: Capital improvements must be capitalized, not expensed (IRS §263).
    capital_signals = [
        "abatement", "asbestos", "demolition", "remediation", "renovation",
        "remodel", "construction", "build-out", "buildout", "structural",
        "foundation", "roofing", "hvac replacement", "lead removal",
        "mold removal", "elevator", "major repair",
    ]
    amount = result.get("amount") or 0
    if result.get("doc_type") in ("invoice", "receipt", "capital_improvement") and (
        any(sig in combined for sig in capital_signals) or amount > 2500
        and any(sig in combined for sig in capital_signals)
    ):
        result["doc_type"] = "capital_improvement"
        result["category"] = "asset"
        tags = result.setdefault("tags", [])
        if "capital_improvement" not in tags:
            tags.append("capital_improvement")

    # Rule 3: Statements are never income or expense — they show balances.
    if result.get("doc_type") in (
        "bank_statement", "credit_card_statement", "mortgage_statement"
    ) and result.get("category") in ("income", "expense"):
        result["category"] = "other"

    return result


def analysis_daemon():
    """Background thread: continuously analyze new Paperless documents."""
    from app import db, config
    from app.paperless_client import PaperlessClient
    from app.llm_client import LLMClient
    from app.checks.financial_rules import validate_document
    import json

    _log("Analysis daemon started")

    while True:
        try:
            _analysis_status["running"] = True

            # Get LLM config from DB (allows runtime override)
            llm_provider = db.get_setting("llm_provider") or config.LLM_PROVIDER
            llm_api_key = db.get_setting("llm_api_key") or config.LLM_API_KEY
            llm_model = db.get_setting("llm_model") or config.LLM_MODEL
            paperless_token = db.get_setting("paperless_api_token") or config.PAPERLESS_API_TOKEN

            if not llm_api_key:
                _log("LLM API key not configured — skipping analysis cycle")
                time.sleep(config.POLL_INTERVAL)
                continue

            client = PaperlessClient(token=paperless_token)
            llm = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)

            # Get all Paperless doc IDs
            all_ids = client.get_all_document_ids()
            analyzed_ids = db.get_analyzed_doc_ids()
            new_ids = [d for d in all_ids if d not in analyzed_ids]

            if new_ids:
                _log(f"Found {len(new_ids)} unanalyzed documents (processing up to 20)")

            analyzed_this_cycle = 0
            for doc_id in new_ids[:20]:
                try:
                    doc = client.get_document(doc_id)
                    content = doc.get("content", "")
                    title = doc.get("title", f"Document {doc_id}")
                    tags = [t for t in doc.get("tags", [])]

                    # Extract entity hint from tags (e.g. "tax-personal", "tax-voipguru")
                    entity_hint = "personal"
                    year_hint = None
                    for tag_name in tags:
                        if isinstance(tag_name, str):
                            if tag_name.startswith("tax-"):
                                entity_hint = tag_name[4:]
                            elif tag_name.startswith("year-"):
                                year_hint = tag_name[5:]

                    if not content or len(content.strip()) < 10:
                        # Mark as analyzed with minimal data so we don't retry
                        db.mark_document_analyzed(
                            doc_id, None, year_hint, "other", "other",
                            "", None, None, 0.1, "{}"
                        )
                        continue

                    _log(f"Analyzing doc {doc_id}: {title[:50]}")
                    result = llm.analyze_document(content, title, entity_hint, year_hint)
                    result = _apply_business_rules(result, content, title)

                    # Look up entity ID
                    entity = db.get_entity(slug=result.get("entity", entity_hint))
                    entity_id = entity["id"] if entity else None
                    tax_year = result.get("tax_year") or year_hint

                    # Validate
                    validation = validate_document(
                        result.get("doc_type", "other"),
                        result.get("category", "other"),
                        result.get("amount"),
                        result.get("date"),
                        tax_year,
                        result,
                    )

                    # Apply confidence penalty from validation
                    confidence = max(0.0, (result.get("confidence", 0.7) - validation.get("confidence_penalty", 0)))

                    # Build title: prefer AI-generated, else construct from fields,
                    # fall back to the Paperless document title
                    ai_title = result.get("title", "").strip()
                    if not ai_title:
                        parts = [result.get("doc_type", "")]
                        if result.get("vendor"):
                            parts.append(f"— {result['vendor']}")
                        if tax_year:
                            parts.append(f"({tax_year})")
                        ai_title = " ".join(p for p in parts if p) or title

                    # Save to DB
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
                    )

                    # Embed into vector store for RAG
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
                    except Exception as ve:
                        _log(f"Vector embed failed for {doc_id}: {ve}")

                    # Apply tags back to Paperless
                    entity_slug = result.get("entity", entity_hint)
                    tag_year = str(tax_year) if tax_year else "unknown"
                    tags_to_apply = [f"tax-{entity_slug}", f"year-{tag_year}", result.get("doc_type", "other")]
                    try:
                        client.apply_tags(doc_id, [t for t in tags_to_apply if t])
                    except Exception as e:
                        _log(f"Tag apply failed for {doc_id}: {e}")

                    db.log_activity("document_analyzed",
                        f"Doc {doc_id} ({result.get('doc_type')}) → {entity_slug}/{tax_year} ${result.get('amount', 0) or 0:.2f}")
                    analyzed_this_cycle += 1
                    _log(f"Doc {doc_id} → {result.get('doc_type')}/{result.get('category')} ${result.get('amount', 0) or 0:.2f}")

                except Exception as e:
                    _log(f"Error analyzing doc {doc_id}: {e}")
                    import traceback
                    _log(traceback.format_exc()[:300])

            _analysis_status["analyzed_this_cycle"] = analyzed_this_cycle
            _analysis_status["last_run"] = datetime.utcnow().isoformat()

        except Exception as e:
            _log(f"Analysis daemon cycle error: {e}")
        finally:
            _analysis_status["running"] = False

        time.sleep(config.POLL_INTERVAL)


def main():
    from app import db, config

    # Initialize
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.EXPORT_PATH, exist_ok=True)
    os.makedirs(config.CONSUME_PATH, exist_ok=True)

    db.init_db()
    db.ensure_default_data()
    _log("Financial AI Analyzer starting...")
    _log(f"Web UI: http://0.0.0.0:{config.WEB_PORT}{config.URL_PREFIX}/")

    # Start analysis daemon
    daemon = threading.Thread(target=analysis_daemon, daemon=True, name="analysis-daemon")
    daemon.start()

    # Start Flask
    from app.web_ui import app as flask_app
    flask_app.run(
        host="0.0.0.0",
        port=config.WEB_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
