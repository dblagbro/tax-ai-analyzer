"""Per-month worker + ``run_import`` orchestrator.

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
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from app.importers.gmail.auth import get_credentials
from app.importers.gmail.fetch import (
    _build_service,
    _fast_prefilter,
    _fetch_month_message_ids,
    get_message_detail,
    parse_headers,
    get_pdf_attachments,
    decode_body,
)
from app.importers.gmail.parse import (
    _sanitize_html_for_pdf,
    _html_to_pdf,
    _text_to_pdf,
    _safe_filename,
    _infer_year,
    _is_known_pdf,
)
from app.importers.gmail.ai_review import _ai_review_email
from app.importers.gmail.transactions import upsert_transaction

logger = logging.getLogger(__name__)


def _process_month(
    year: int,
    month: int,
    creds,
    search_terms: list[str],
    entity_id,
    entity_slug: str,
    consume_path: str,
    stop_event: threading.Event,
    stats: dict,
    stats_lock: threading.Lock,
    write_lock: threading.Lock,
    log: Callable,
) -> None:
    """Fetch and process all emails for a single calendar month."""
    from app import db
    from app.importers.entity_router import get_entity_slug
    _, _, build = _google_imports()

    month_label = f"{year}-{month:02d}"
    try:
        msgs = _fetch_month_message_ids(creds, year, month, search_terms)
    except Exception as e:
        log(f"[{month_label}] Gmail fetch error: {e}")
        return

    if not msgs:
        log(f"[{month_label}] 0 emails found")
        return

    log(f"[{month_label}] {len(msgs)} emails — starting processing…")
    # Build one service per thread with timeout
    try:
        service = _build_service(creds)
    except Exception as e:
        log(f"[{month_label}] Service build error: {e}")
        return

    ai_calls_this_month = 0
    for msg_stub in msgs:
        if stop_event.is_set():
            log(f"[{month_label}] Stopped by cancel signal.")
            return

        msg_id = msg_stub["id"]
        try:
            msg = get_message_detail(service, msg_id)
            headers = parse_headers(msg)
            message_id_header = headers["message_id"] or msg_id
            subject = headers["subject"]
            sender = headers["from"]
            date_str = headers["date"]
            msg_year = _infer_year(date_str)

            # ── Fast O(1) dedup via DB ────────────────────────────────────────
            if db.is_gmail_message_processed(message_id_header):
                with stats_lock:
                    stats["skipped"] += 1
                continue

            # ── Fast pre-filter (no AI call) ────────────────────────────────
            if _fast_prefilter(subject, sender):
                log(f"  [{month_label}] SKIP (pre-filter) {subject[:60]}")
                db.record_gmail_message(
                    message_id_header, msg_id, "ai_filtered", entity_slug, msg_year, subject, sender
                )
                with stats_lock:
                    stats["ai_filtered"] += 1
                continue

            # Safety cap on AI calls per month
            ai_calls_this_month += 1
            if ai_calls_this_month > _MAX_AI_CALLS_PER_MONTH:
                log(f"  [{month_label}] AI call limit ({_MAX_AI_CALLS_PER_MONTH}) reached — deferring remaining emails")
                break

            # ── AI review ────────────────────────────────────────────────────
            payload = msg.get("payload", {})
            html_body, text_body = decode_body(payload)
            body_snippet = re.sub(r"<[^>]+>", " ", html_body or text_body or "")
            body_snippet = re.sub(r"\s+", " ", body_snippet).strip()[:900]

            review = _ai_review_email(subject, sender, body_snippet, date_str, log_fn=log)

            if not review.get("relevant", True):
                log(f"  [{month_label}] FILTERED {subject[:50]} — {review.get('reason','')[:60]}")
                db.record_gmail_message(
                    message_id_header, msg_id, "ai_filtered", entity_slug, msg_year, subject, sender
                )
                with stats_lock:
                    stats["ai_filtered"] += 1
                continue

            log(f"  [{month_label}] ✓ {review.get('doc_type','email')} | "
                f"{review.get('vendor',sender[:30])} | ${review.get('amount') or '?'} | {subject[:40]}")

            routed_slug = get_entity_slug(sender=sender, subject=subject)
            slug = routed_slug or entity_slug
            ai_vendor = review.get("vendor", "")
            ai_amount = review.get("amount")
            ai_desc = review.get("description") or subject

            pdf_files: list[tuple[str, bytes]] = []

            # ── PDF attachments ───────────────────────────────────────────────
            for fname, data_or_id in get_pdf_attachments(payload):
                # get_pdf_attachments returns bytes for inline data, str for attachment IDs
                if isinstance(data_or_id, str):
                    try:
                        att = service.users().messages().attachments().get(
                            userId="me", messageId=msg_id, id=data_or_id
                        ).execute()
                        raw = base64.urlsafe_b64decode(att.get("data", "") + "==")
                    except Exception as e:
                        log(f"    [attachment fetch error: {e}]")
                        continue
                else:
                    raw = data_or_id  # already bytes
                if not isinstance(raw, bytes):
                    raw = raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)
                pdf_files.append((_safe_filename(date_str, ai_desc, ai_vendor, ai_amount), raw))

            # ── Body → PDF ────────────────────────────────────────────────────
            _pdf_fail_reason = None
            if not pdf_files:
                body_for_pdf = html_body or text_body
                if body_for_pdf:
                    try:
                        if html_body:
                            pdf_bytes = _html_to_pdf(html_body)
                        else:
                            pdf_bytes = _text_to_pdf(text_body, subject)
                        pdf_files.append((_safe_filename(date_str, ai_desc, ai_vendor, ai_amount), pdf_bytes))
                    except Exception as e:
                        _pdf_fail_reason = str(e)
                        log(f"    [PDF gen failed ({date_str}): {e}]")
                        if html_body and text_body:
                            try:
                                pdf_bytes = _text_to_pdf(text_body, subject)
                                pdf_files.append((_safe_filename(date_str, ai_desc, ai_vendor, ai_amount), pdf_bytes))
                                _pdf_fail_reason = None  # text fallback succeeded
                            except Exception as e2:
                                log(f"    [Text fallback also failed ({date_str}): {e2}]")
                else:
                    _pdf_fail_reason = "no body content"

            if not pdf_files:
                if _pdf_fail_reason and _pdf_fail_reason != "no body content":
                    log(f"    → ({date_str}) PDF generation failed — skipping. Reason: {_pdf_fail_reason}")
                else:
                    log(f"    → ({date_str}) email has no extractable body/attachments — skipping")
                db.record_gmail_message(
                    message_id_header, msg_id, "skipped", slug, msg_year, subject, sender
                )
                with stats_lock:
                    stats["skipped"] += 1
                continue

            # ── Write files ───────────────────────────────────────────────────
            dest_dir = os.path.join(consume_path, slug, msg_year)
            saved = False
            os.makedirs(dest_dir, exist_ok=True)
            deduped_pdfs = []
            for fname, pdf_bytes in pdf_files:
                if _is_known_pdf(pdf_bytes, source="gmail", filename=fname,
                                 entity_slug=slug, year=msg_year):
                    log(f"    → SKIP (content already imported: {fname})")
                    with stats_lock:
                        stats["skipped"] += 1
                else:
                    deduped_pdfs.append((fname, pdf_bytes))
            pdf_files = deduped_pdfs
            with write_lock:
                for fname, pdf_bytes in pdf_files:
                    dest_path = os.path.join(dest_dir, fname)
                    if os.path.exists(dest_path):
                        base, ext = os.path.splitext(fname)
                        dest_path = os.path.join(dest_dir, f"{base}_{secrets.token_hex(3)}{ext}")
                    with open(dest_path, "wb") as f:
                        f.write(pdf_bytes)
                    log(f"    ✓ {os.path.basename(dest_path)}")
                    saved = True

            if saved:
                upsert_transaction({
                    "source": "gmail", "source_id": message_id_header,
                    "entity_id": entity_id, "entity_slug": slug,
                    "year": msg_year, "date": date_str,
                    "description": subject, "vendor": ai_vendor or re.sub(r"<.*?>", "", sender).strip(),
                    "amount": ai_amount, "doc_type": review.get("doc_type", "email"),
                    "category": "imported", "imported_at": datetime.utcnow().isoformat(),
                })
                db.record_gmail_message(
                    message_id_header, msg_id, "imported", slug, msg_year, subject, sender
                )
                with stats_lock:
                    stats["imported"] += 1

        except Exception as e:
            logger.error(f"Error processing Gmail message {msg_id}: {e}", exc_info=True)
            log(f"  [{month_label}] ERROR on {msg_id}: {e}")
            with stats_lock:
                stats["errors"] += 1


# ── main entry point ───────────────────────────────────────────────────────────


def run_import(
    entity_id,
    years: list[str],
    consume_path: str,
    entity_slug: str,
    log_fn: Callable = None,
    stop_event: threading.Event = None,
    max_workers: int = 12,
    progress_fn: Callable = None,
) -> dict:
    """
    Run a full Gmail import using month-parallel threads.

    Each year gets 12 concurrent threads (one per month), so a full year
    completes in ~1/12th the serial time. All 12 threads share a single
    stop_event so cancellation is near-instant.

    Dedup is O(1) via the gmail_processed_messages DB table — Message-IDs
    that have been seen before are skipped without any AI call.
    """
    from app import db

    log = log_fn or logger.info
    stop_event = stop_event or threading.Event()
    stats = {"imported": 0, "skipped": 0, "ai_filtered": 0, "errors": 0}
    stats_lock = threading.Lock()
    write_lock = threading.Lock()

    log("Checking Gmail credentials…")
    creds = get_credentials()
    if not creds:
        log("ERROR: Gmail not authenticated.")
        return stats

    from app import config as _cfg
    settings = db.get_settings()
    default_terms = " ".join(_cfg.GMAIL_SEARCH_TERMS)
    raw_terms = settings.get("gmail_search_terms", default_terms)
    search_terms = raw_terms.split() if raw_terms else list(_cfg.GMAIL_SEARCH_TERMS)
    # Always include accountant email domain if configured
    acct_domain = getattr(_cfg, "ACCOUNTANT_EMAIL_DOMAIN", "")
    if acct_domain:
        acct_term = f"from:{acct_domain}"
        if acct_term not in search_terms:
            search_terms.append(acct_term)
    log(f"Search terms ({len(search_terms)}): {' | '.join(search_terms[:8])}{'…' if len(search_terms)>8 else ''}")

    # Show existing dedup stats
    dedup_stats = db.gmail_processed_stats()
    if dedup_stats:
        log(f"Dedup DB: {dedup_stats}")

    for year_str in years:
        if stop_event.is_set():
            log("Cancelled before starting year.")
            break
        try:
            year = int(year_str)
        except ValueError:
            continue

        log(f"━━━ Year {year}: launching {max_workers} parallel month-workers ━━━")

        months = list(range(1, 13))
        with ThreadPoolExecutor(max_workers=min(max_workers, len(months)),
                                thread_name_prefix=f"gmail-{year}") as pool:
            futures = {
                pool.submit(
                    _process_month,
                    year, month, creds, search_terms,
                    entity_id, entity_slug, consume_path,
                    stop_event, stats, stats_lock, write_lock, log,
                ): month
                for month in months
            }
            # Use per-future timeout so a single hung thread can't block the whole year.
            # Each month worker should complete in well under 30 minutes.
            WORKER_TIMEOUT = 1800  # 30 min per month-worker
            pending = dict(futures)  # future -> month copy
            done_set = set()
            import concurrent.futures as _cf
            try:
                for future in _cf.as_completed(pending.keys(), timeout=WORKER_TIMEOUT):
                    month_n = pending[future]
                    done_set.add(future)
                    try:
                        future.result(timeout=10)
                    except Exception as e:
                        log(f"[{year}-{month_n:02d}] Worker error: {e}")
                        with stats_lock:
                            stats["errors"] += 1
            except _cf.TimeoutError:
                log(f"[{year}] Year-level timeout ({WORKER_TIMEOUT}s) — some month workers hung")
                with stats_lock:
                    stats["errors"] += 1
            # Cancel any still-pending futures
            for f in list(pending.keys()):
                if f not in done_set:
                    f.cancel()
                    log(f"[{year}-{pending[f]:02d}] Worker cancelled after year timeout")

        if not stop_event.is_set():
            log(f"━━━ Year {year} done: "
                f"{stats['imported']} imported, {stats['ai_filtered']} AI-filtered, "
                f"{stats['skipped']} skipped, {stats['errors']} errors ━━━")
        # Flush running counts to DB after each year so they survive a restart
        if progress_fn:
            try:
                progress_fn(stats["imported"], stats["skipped"])
            except Exception:
                pass

    log(
        f"━━━ Import {'CANCELLED' if stop_event.is_set() else 'complete'}: "
        f"{stats['imported']} imported, {stats['ai_filtered']} AI-filtered, "
        f"{stats['skipped']} skipped, {stats['errors']} errors ━━━"
    )
    return stats
