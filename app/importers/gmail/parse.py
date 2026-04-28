"""HTML/text rendering, filename safety, amount/date normalization, PDF dedup.

Extracted from the original 843-line ``app/importers/gmail_importer.py``
during Phase 11H refactor. The public API (``run_import``, ``get_auth_url``,
``complete_auth``, ``is_authenticated``) and the helpers IMAP imports
(``_ai_review_email``, ``_fast_prefilter``, ``_is_known_pdf``,
``_text_to_pdf``, ``upsert_transaction``) are re-exported by the package
``__init__`` so existing callers don't change.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _sanitize_html_for_pdf(html: str) -> str:
    """Strip complex/broken CSS that causes WeasyPrint to crash."""
    # Remove style blocks with complex font rules that break WeasyPrint
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Keep it simple
    return html


def _html_to_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=_sanitize_html_for_pdf(html)).write_pdf()


def _text_to_pdf(text: str, subject: str = "") -> bytes:
    import html as html_lib
    escaped = html_lib.escape(text).replace("\n", "<br>")
    html_doc = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:monospace;font-size:11px;margin:20px}"
        "h2{font-size:13px;margin-bottom:8px}</style></head>"
        f"<body><h2>{html_lib.escape(subject)}</h2><p>{escaped}</p></body></html>"
    )
    return _html_to_pdf(html_doc)


def _safe_filename(date_str: str, description: str,
                   vendor: str = "", amount=None) -> str:
    try:
        dt = parsedate_to_datetime(date_str)
        prefix = dt.strftime("%Y_%m_%d")
    except Exception:
        prefix = datetime.utcnow().strftime("%Y_%m_%d")
    parts = []
    if vendor:
        v = re.sub(r"[^\w]", "_", vendor.strip())[:25].strip("_")
        if v:
            parts.append(v)
    desc = re.sub(r"[^\w\s-]", "", description or "")
    desc = re.sub(r"\s+", "_", desc.strip())[:45]
    if desc:
        parts.append(desc)
    name = "_".join(parts) if parts else "email"
    if amount is not None:
        try:
            return f"{prefix}_{name}-{float(amount):.2f}.pdf"
        except (TypeError, ValueError):
            pass
    return f"{prefix}_{name}.pdf"


def _infer_year(date_str: str) -> str:
    try:
        return str(parsedate_to_datetime(date_str).year)
    except Exception:
        return str(datetime.utcnow().year)


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_known_pdf(data: bytes, source: str = "gmail", filename: str = "",
                  entity_slug: str = "", year: str = "") -> bool:
    """
    Check whether we've seen this exact PDF content before (persistent DB store).
    If not seen, record it so future imports are deduplicated.
    Returns True if it's a duplicate (already known), False if it's new.

    Replaces the old _hash_exists_in_dir() which only scanned the consume directory
    and became useless after Paperless cleared the files it ingested.
    """
    from app import db as _db
    h = _file_hash(data)
    is_new = _db.record_pdf_hash(h, source=source, filename=filename,
                                  entity_slug=entity_slug, year=year)
    return not is_new  # True = duplicate


def _normalize_gmail_date(date_str: str) -> tuple[str, str]:
    """Convert a Gmail Date header (RFC 2822 or already-ISO) to (iso_date, year_str).

    Returns ("", "") if the input can't be parsed.
    """
    if not date_str:
        return "", ""
    s = date_str.strip()
    # Already ISO?
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        iso = s[:10]
        return iso, iso[:4]
    try:
        dt = parsedate_to_datetime(s)
        return dt.strftime("%Y-%m-%d"), str(dt.year)
    except Exception:
        return "", ""


def _coerce_amount(value) -> Optional[float]:
    """Return a float for valid amounts, None otherwise."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
        return f
    except (TypeError, ValueError):
        return None
