"""Generic IMAP email importer.

Extends the tax app beyond Gmail: Outlook.com, Yahoo, iCloud, and any
standard IMAP server. Uses app-password authentication (same as Gmail IMAP),
which every major provider supports for 3rd-party clients.

Reuses the Gmail importer's AI review + upsert_transaction pipeline so
extracted transactions land in the transactions table with identical shape
(source='imap:<provider>').

Provider presets:
  - yahoo:   imap.mail.yahoo.com:993    (app password required)
  - icloud:  imap.mail.me.com:993       (app password required)
  - outlook: outlook.office365.com:993  (app password for personal; OAuth for M365 orgs)
  - generic: user-supplied host/port
"""
from __future__ import annotations

import email
import imaplib
import io
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional

# Reuse Gmail importer helpers — they are pure functions operating on strings/bytes
from app.importers.gmail_importer import (
    _ai_review_email,
    _fast_prefilter,
    _is_known_pdf,
    _text_to_pdf,
    upsert_transaction as gmail_upsert,  # date+amount normalizing upsert
)

logger = logging.getLogger(__name__)


PROVIDERS: dict[str, dict] = {
    "yahoo":   {"host": "imap.mail.yahoo.com",   "port": 993, "label": "Yahoo Mail"},
    "icloud":  {"host": "imap.mail.me.com",      "port": 993, "label": "iCloud Mail"},
    "outlook": {"host": "outlook.office365.com", "port": 993, "label": "Outlook.com / Office 365"},
    "aol":     {"host": "imap.aol.com",          "port": 993, "label": "AOL Mail"},
    "generic": {"host": "",                      "port": 993, "label": "Generic IMAP"},
}


DEFAULT_SEARCH_TERMS = [
    "receipt", "invoice", "statement", "payment", "order",
    "confirmation", "1099", "W-2", "tax", "billing",
]


# ── helpers ────────────────────────────────────────────────────────────────────

def _decode(value) -> str:
    """Decode a MIME-encoded header safely."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _get_body_text(msg) -> str:
    """Return the first text/plain part of a message (or text/html fallback)."""
    if msg.is_multipart():
        # Prefer text/plain; fall back to text/html
        plain = html = ""
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
        return plain or re.sub(r"<[^>]+>", " ", html)
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""


def _get_pdf_attachments(msg) -> list[tuple[str, bytes]]:
    """Extract all PDF attachments from an email message."""
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = _decode(part.get_filename() or "")
        ctype = (part.get_content_type() or "").lower()
        is_pdf = ctype == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        try:
            data = part.get_payload(decode=True)
            if data:
                out.append((filename or "attachment.pdf", data))
        except Exception as e:
            logger.warning(f"Failed to decode PDF attachment: {e}")
    return out


def _build_search_criteria(year: int, search_terms: list[str]) -> str:
    """Build an IMAP SEARCH string for the given year + keyword list.

    IMAP SEARCH uses OR (term1 OR term2 …) structure with OR being prefix-binary.
    """
    from datetime import date
    since = date(year, 1, 1).strftime("%d-%b-%Y")
    before = date(year + 1, 1, 1).strftime("%d-%b-%Y")

    # Build a chain of OR SUBJECT "x" for each term (IMAP OR is binary: OR a b)
    terms = [t for t in search_terms if t.strip()]
    if not terms:
        return f'SINCE {since} BEFORE {before}'

    # IMAP OR: OR <crit1> <crit2>. Chain multiple: OR a (OR b (OR c d))
    def _or_chain(crits: list[str]) -> str:
        if len(crits) == 1:
            return crits[0]
        if len(crits) == 2:
            return f'OR {crits[0]} {crits[1]}'
        return f'OR {crits[0]} ({_or_chain(crits[1:])})'

    subject_crits = [f'SUBJECT "{t}"' for t in terms]
    body_crits = [f'BODY "{t}"' for t in terms[:5]]  # cap body terms (slower on server)
    all_crits = subject_crits + body_crits
    or_expr = _or_chain(all_crits)
    return f'(SINCE {since}) (BEFORE {before}) ({or_expr})'


# ── main flow ──────────────────────────────────────────────────────────────────

def test_connection(host: str, port: int, username: str, password: str,
                    use_ssl: bool = True) -> dict:
    """Connect, LOGIN, and return {'ok': bool, 'folders': [...], 'error': str}."""
    try:
        imap = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        try:
            imap.login(username, password)
            typ, data = imap.list()
            folders = []
            if typ == "OK":
                for line in data:
                    try:
                        s = line.decode() if isinstance(line, bytes) else str(line)
                        # Very rough: pick out the quoted folder name at the end
                        m = re.search(r'"([^"]+)"\s*$', s)
                        if m:
                            folders.append(m.group(1))
                    except Exception:
                        pass
            return {"ok": True, "folders": folders[:50]}
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": f"IMAP error: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"Connection error: {e}"}


def run_import(
    host: str,
    port: int,
    username: str,
    password: str,
    years: list[str],
    consume_path: str,
    entity_slug: str,
    entity_id: Optional[int] = None,
    search_terms: Optional[list[str]] = None,
    folder: str = "INBOX",
    job_id: Optional[int] = None,
    log: Callable[[str], None] = logger.info,
    stop_event: Optional[threading.Event] = None,
    progress_fn: Optional[Callable[[int, int], None]] = None,
    use_ssl: bool = True,
) -> dict:
    """Scan an IMAP mailbox for tax-relevant messages and import them.

    Returns {"imported": int, "skipped": int, "ai_filtered": int, "errors": int}.
    """
    from app import db

    search_terms = search_terms or DEFAULT_SEARCH_TERMS
    provider_label = host.replace("imap.", "").split(".")[0] or "imap"
    source_tag = f"imap:{provider_label}"

    imported = skipped = ai_filtered = errors = 0

    log(f"Connecting to {host}:{port} as {username}…")
    try:
        imap = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        imap.login(username, password)
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"IMAP login failed: {e}")

    try:
        typ, _ = imap.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Could not select folder {folder!r}")

        for year_str in years:
            if stop_event and stop_event.is_set():
                log("Stop requested — breaking out of year loop.")
                break
            try:
                year = int(year_str)
            except ValueError:
                log(f"  Skipping invalid year: {year_str!r}")
                continue
            log(f"── {year} ──")
            criteria = _build_search_criteria(year, search_terms)
            log(f"  IMAP SEARCH {criteria[:120]}…")
            typ, data = imap.search(None, criteria)
            if typ != "OK":
                log(f"  search failed: {typ}")
                continue
            msg_ids = (data[0] or b"").split()
            log(f"  {len(msg_ids)} messages match")

            dest_dir = Path(consume_path) / entity_slug / str(year)
            dest_dir.mkdir(parents=True, exist_ok=True)

            for idx, mid in enumerate(msg_ids):
                if stop_event and stop_event.is_set():
                    log("  Stop requested.")
                    break
                try:
                    typ, msg_data = imap.fetch(mid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        errors += 1
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    subject = _decode(msg.get("Subject", ""))
                    sender = _decode(msg.get("From", ""))
                    date_hdr = msg.get("Date", "")
                    message_id = _decode(msg.get("Message-ID", "")) or f"imap:{mid.decode()}"

                    if _fast_prefilter(subject, sender):
                        skipped += 1
                        continue

                    body = _get_body_text(msg)
                    body_snippet = (body or "")[:2000]

                    review = _ai_review_email(subject, sender, body_snippet, date_hdr, log)
                    if not review.get("relevant"):
                        ai_filtered += 1
                        continue

                    ai_vendor = review.get("vendor") or re.sub(r"<.*?>", "", sender).strip()
                    ai_amount = review.get("amount")
                    ai_doc_type = review.get("doc_type", "email")

                    # Save PDFs + a text-rendered version of the email itself
                    pdfs = _get_pdf_attachments(msg)
                    pdfs.append((
                        f"{_safe(subject, 60)}_email.pdf",
                        _text_to_pdf(body or "", subject=subject),
                    ))

                    saved = False
                    for fname, pdf_bytes in pdfs:
                        if _is_known_pdf(pdf_bytes, source=source_tag,
                                         filename=fname, entity_slug=entity_slug,
                                         year=str(year)):
                            continue
                        dest_path = dest_dir / _safe(fname, 80)
                        if dest_path.exists():
                            base = dest_path.stem
                            ext = dest_path.suffix
                            dest_path = dest_dir / f"{base}_{secrets.token_hex(3)}{ext}"
                        dest_path.write_bytes(pdf_bytes)
                        log(f"    ✓ {dest_path.name}")
                        saved = True

                    if saved:
                        gmail_upsert({
                            "source": source_tag,
                            "source_id": message_id,
                            "entity_id": entity_id,
                            "entity_slug": entity_slug,
                            "year": str(year),
                            "date": date_hdr,
                            "description": subject,
                            "vendor": ai_vendor,
                            "amount": ai_amount,
                            "doc_type": ai_doc_type,
                            "category": "imported",
                        })
                        imported += 1

                    if progress_fn and idx % 20 == 0:
                        progress_fn(imported, skipped + ai_filtered)

                except Exception as e:
                    logger.error(f"IMAP error on message {mid}: {e}", exc_info=True)
                    log(f"    ERROR: {e}")
                    errors += 1

    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass

    log(f"IMAP done — imported={imported}, skipped={skipped}, ai_filtered={ai_filtered}, errors={errors}")
    return {"imported": imported, "skipped": skipped, "ai_filtered": ai_filtered, "errors": errors}


def _safe(s: str, limit: int) -> str:
    """Filesystem-safe short name."""
    s = re.sub(r"[^A-Za-z0-9._\- ]", "_", s or "")
    return s[:limit] or "file"
