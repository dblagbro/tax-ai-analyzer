"""AI-assisted email triage — keep / discard / extract amount + date.

Extracted from the original 843-line ``app/importers/gmail_importer.py``
during Phase 11H refactor. The public API (``run_import``, ``get_auth_url``,
``complete_auth``, ``is_authenticated``) and the helpers IMAP imports
(``_ai_review_email``, ``_fast_prefilter``, ``_is_known_pdf``,
``_text_to_pdf``, ``upsert_transaction``) are re-exported by the package
``__init__`` so existing callers don't change.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)


def _ai_review_email(subject: str, sender: str, body_snippet: str,
                     date_str: str, log_fn=None) -> dict:
    log = log_fn or logger.info
    try:
        from app import db, config as cfg
        import anthropic, json as _json
        api_key = db.get_setting("llm_api_key") or cfg.LLM_API_KEY
        model = db.get_setting("llm_model") or cfg.LLM_MODEL
        if not api_key:
            return {"relevant": True, "doc_type": "email", "vendor": "",
                    "amount": None, "description": subject[:60], "reason": "no API key"}
        prompt = (
            "Is this email a tax-relevant financial document worth keeping "
            "(receipt, invoice, bill, statement, 1099, W-2, payment confirmation, "
            "utility bill, subscription, bank notice)?\n\n"
            f"From: {sender}\nDate: {date_str}\nSubject: {subject}\n"
            f"Body:\n{body_snippet[:900]}\n\n"
            "JSON only, no fences:\n"
            '{"relevant":true,"reason":"","doc_type":"receipt|invoice|bill|statement|'
            '1099|W-2|tax_notice|bank_statement|other","vendor":"","amount":null,'
            '"description":"short filename-safe description max 40 chars"}\n'
            "relevant=false for: marketing, newsletters, shipping-only no cost, "
            "general chat, order status no dollar amount."
        )
        client = anthropic.Anthropic(api_key=api_key, timeout=_AI_TIMEOUT)
        resp = client.messages.create(
            model=model, max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text).rstrip("`")
        result = _json.loads(text)
        log(f"  AI: {result.get('relevant')} — {result.get('reason','')[:80]}")
        return result
    except Exception as e:
        log(f"  [AI review error: {e}] — defaulting to import")
        return {"relevant": True, "doc_type": "email", "vendor": "",
                "amount": None, "description": subject[:60], "reason": "error"}
