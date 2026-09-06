"""The single implementation of "analyze one Paperless document and persist it".

Used by BOTH the background analysis daemon (app.main.analysis_daemon) and
the manual /api/analyze/trigger route. Before 2026-09-06 those were two
copy-pasted ~100-line loops that drifted: the trigger route kept storing
"Analysis failed" rows and returned silently without an LLM after the daemon
had been fixed. One function, one behaviour.

Behaviour:
  * empty / near-empty content → stored as low-confidence "other" (status "empty")
  * LLM reachable and succeeds  → normal result (status "analyzed")
  * LLM reachable but the call fails, or no LLM at all
                                → app.rules_analyzer provisional result
                                  (status "provisional"; NO Paperless tags, NO
                                  vector embed; re-analyzed when the LLM is back)
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def llm_analysis_failed(result: dict) -> bool:
    """True when LLMClient.analyze_document returned its error sentinel
    (confidence 0.0 + 'Analysis failed: …') rather than a real classification."""
    try:
        return (float(result.get("confidence", 0) or 0) <= 0.0
                and str(result.get("description", "")).startswith("Analysis failed"))
    except (TypeError, ValueError, AttributeError):
        return False


def hints_from_tags(tags) -> tuple[str, Optional[str]]:
    """Paperless tags 'tax-<entity>' / 'year-<yyyy>' → (entity_hint, year_hint)."""
    entity_hint, year_hint = "personal", None
    for tag_name in tags or []:
        if isinstance(tag_name, str):
            if tag_name.startswith("tax-"):
                entity_hint = tag_name[4:]
            elif tag_name.startswith("year-"):
                year_hint = tag_name[5:]
    return entity_hint, year_hint


def derive_tax_year(result: dict, year_hint: Optional[str]) -> Optional[str]:
    """Explicit result → Paperless year tag → year of the document date.
    (334 documents once sat with NULL tax_year although 330 had a date.)"""
    tax_year = result.get("tax_year") or year_hint
    if tax_year:
        return str(tax_year)
    d = str(result.get("date") or "")
    if len(d) >= 4 and d[:4].isdigit() and 2015 <= int(d[:4]) <= 2030:
        return d[:4]
    return None


def process_document(
    doc_id: int,
    doc: dict,
    *,
    llm,
    llm_ok: bool,
    client,
    log: Optional[Callable[[str], None]] = None,
    embed: bool = True,
    apply_tags: bool = True,
) -> dict:
    """Analyze one Paperless document dict and persist the outcome.

    Returns {"status": "empty"|"analyzed"|"provisional", "result": dict,
             "provisional": bool, "llm_failed": bool, "doc_type": str,
             "amount": float|None, "entity": str, "tax_year": str|None}
    """
    from app import db, rules_analyzer
    from app.checks.financial_rules import apply_business_rules, validate_document

    log = log or logger.info
    content = doc.get("content", "") or ""
    title = doc.get("title") or f"Document {doc_id}"
    entity_hint, year_hint = hints_from_tags(doc.get("tags", []))

    if len(content.strip()) < 10:
        # Mark as analyzed with minimal data so we don't retry forever
        db.mark_document_analyzed(doc_id, None, year_hint, "other", "other",
                                  "", None, None, 0.1, "{}")
        return {"status": "empty", "result": {}, "provisional": False, "llm_failed": False,
                "doc_type": "other", "amount": None, "entity": entity_hint, "tax_year": year_hint}

    provisional = False
    llm_failed = False
    if llm_ok and llm is not None:
        result = llm.analyze_document(content, title, entity_hint, year_hint, doc_id=doc_id)
        if llm_analysis_failed(result):
            # Never persist "Analysis failed" as a final answer — that is exactly
            # what left documents unclassified forever. Store a provisional row
            # instead so the document is retried when the LLM works again.
            llm_failed = True
            log(f"LLM analysis failed for doc {doc_id} "
                f"({str(result.get('description', ''))[:90]}) — storing provisional rules-only result")
            result = rules_analyzer.analyze_rules_only(content, title, entity_hint, year_hint)
            provisional = True
    else:
        result = rules_analyzer.analyze_rules_only(content, title, entity_hint, year_hint)
        provisional = True

    result = apply_business_rules(result, content, title)
    if provisional:
        result["provisional"] = True
        result.setdefault("method", "rules")

    entity_slug = result.get("entity", entity_hint) or entity_hint
    entity = db.get_entity(slug=entity_slug)
    entity_id = entity["id"] if entity else None
    tax_year = derive_tax_year(result, year_hint)

    validation = validate_document(
        result.get("doc_type", "other"),
        result.get("category", "other"),
        result.get("amount"),
        result.get("date"),
        tax_year,
        result,
    )
    confidence = max(0.0, (float(result.get("confidence", 0.7) or 0.0)
                           - float(validation.get("confidence_penalty", 0) or 0.0)))

    # Title: prefer AI-generated, else construct from fields, else Paperless title
    ai_title = str(result.get("title", "") or "").strip()
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
        if is_dup:
            log(f"Doc {doc_id} flagged as duplicate: {result.get('vendor')} "
                f"${result.get('amount')} {result.get('date')}")

    db.mark_document_analyzed(
        paperless_doc_id=doc_id,
        entity_id=entity_id,
        tax_year=str(tax_year) if tax_year else None,
        doc_type=result.get("doc_type", "other"),
        category=result.get("category", "other"),
        vendor=result.get("vendor", "") or "",
        amount=result.get("amount"),
        date=result.get("date"),
        confidence=confidence,
        extracted_json=json.dumps(result),
        title=ai_title,
        is_duplicate=1 if is_dup else 0,
    )

    # Provisional results are guesses: keep them out of the vector store and
    # off Paperless (a wrong year-/tax- tag would feed the LLM a bad hint on
    # re-analysis).
    if not provisional:
        if embed:
            try:
                from app import vector_store as vs
                vs.embed_document(
                    doc_id=str(doc_id),
                    title=ai_title,
                    content=content[:4000],
                    metadata={
                        "entity_slug": entity_slug,
                        "tax_year": str(tax_year) if tax_year else "",
                        "doc_type": result.get("doc_type", "other"),
                        "category": result.get("category", "other"),
                        "vendor": result.get("vendor", "") or "",
                        "amount": str(result.get("amount") or ""),
                    },
                )
            except Exception as ve:
                log(f"Vector embed failed for {doc_id}: {ve}")
        if apply_tags:
            tag_year = str(tax_year) if tax_year else "unknown"
            tags_to_apply = [f"tax-{entity_slug}", f"year-{tag_year}", result.get("doc_type", "other")]
            try:
                client.apply_tags(doc_id, [t for t in tags_to_apply if t])
            except Exception as e:
                log(f"Tag apply failed for {doc_id}: {e}")

    marker = " [provisional]" if provisional else ""
    amount = result.get("amount") or 0
    db.log_activity("document_analyzed",
                    f"Doc {doc_id} ({result.get('doc_type')}) → {entity_slug}/{tax_year} ${amount:.2f}{marker}")
    log(f"Doc {doc_id} → {result.get('doc_type')}/{result.get('category')} ${amount:.2f}{marker}")

    return {
        "status": "provisional" if provisional else "analyzed",
        "result": result,
        "provisional": provisional,
        "llm_failed": llm_failed,
        "doc_type": result.get("doc_type", "other"),
        "amount": result.get("amount"),
        "entity": entity_slug,
        "tax_year": tax_year,
    }
