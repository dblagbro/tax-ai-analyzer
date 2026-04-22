"""
Gmail importer — fetches emails matching financial search terms and saves them
(PDF attachments or body-to-PDF) into the paperless consume path.

Key features:
  - Persistent Message-ID dedup via gmail_processed_messages DB table (O(1) checks)
  - Stop-event support: pass a threading.Event to cancel mid-run
  - Month-parallel processing: 12 concurrent threads (one per month) per year,
    so a full year completes in ~1/12th the serial time
  - AI review: Claude screens each email for tax relevance before converting
  - Content-hash dedup against files already in the consume directory

Dependencies:
    google-auth google-auth-oauthlib google-auth-httplib2
    google-api-python-client weasyprint anthropic
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ── lazy imports ───────────────────────────────────────────────────────────────

def _google_imports():
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    return Flow, Credentials, build


# ── credential helpers ─────────────────────────────────────────────────────────

def _get_client_config() -> Optional[dict]:
    from app import db, config
    settings = db.get_settings()
    raw = settings.get("gmail_client_config", "")
    if raw:
        import json
        try:
            return json.loads(raw)
        except Exception:
            pass
    creds_file = config.GMAIL_CREDENTIALS_FILE
    if os.path.exists(creds_file):
        import json
        with open(creds_file) as f:
            return json.load(f)
    return None


def _load_token_from_db() -> Optional[dict]:
    from app import db
    settings = db.get_settings()
    raw = settings.get("gmail_oauth_token", "")
    if not raw:
        return None
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_token_to_db(token_data: dict):
    from app import db
    import json
    db.save_settings({"gmail_oauth_token": json.dumps(token_data)})


def get_credentials() -> Optional["google.oauth2.credentials.Credentials"]:
    token = _load_token_from_db()
    if not token:
        return None
    _, Credentials, _ = _google_imports()
    from google.auth.transport.requests import Request
    creds = Credentials(
        token=token.get("token"),
        refresh_token=token.get("refresh_token"),
        token_uri=token.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token.get("client_id"),
        client_secret=token.get("client_secret"),
        scopes=token.get("scopes", GMAIL_SCOPES),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token_to_db({
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes or GMAIL_SCOPES),
            })
        except Exception as e:
            logger.warning(f"Gmail token refresh failed: {e}")
            return None
    return creds if creds.valid else None


def get_auth_url(redirect_uri: str) -> tuple[str, str]:
    client_config = _get_client_config()
    if not client_config:
        raise RuntimeError("Gmail credentials not configured. Upload credentials.json in Settings.")
    Flow, _, _ = _google_imports()
    flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
    state = secrets.token_urlsafe(16)
    auth_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true",
        prompt="consent", state=state,
    )
    return auth_url, state


def complete_auth(code: str, redirect_uri: str):
    client_config = _get_client_config()
    if not client_config:
        raise RuntimeError("Gmail credentials not configured.")
    Flow, _, _ = _google_imports()
    flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_token_to_db({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or GMAIL_SCOPES),
    })
    return creds


def is_authenticated() -> bool:
    try:
        return get_credentials() is not None
    except Exception:
        return False


# ── search / fetch helpers ─────────────────────────────────────────────────────

def _month_query(year: int, month: int, search_terms: list[str]) -> str:
    """Build a Gmail search query for exactly one calendar month."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    after = f"{year}/{month:02d}/01"
    before = f"{year}/{month:02d}/{last_day+1:02d}" if last_day < 28 else (
        f"{year+1}/01/01" if month == 12 else f"{year}/{month+1:02d}/01"
    )
    terms = " OR ".join(search_terms)
    return f"({terms}) after:{after} before:{before}"


_GMAIL_TIMEOUT = 45   # seconds for any single Gmail API call
_AI_TIMEOUT    = 60   # seconds for Anthropic API call

# Obvious non-financial patterns — skip AI call entirely for these
_SKIP_SUBJECTS = re.compile(
    r"(unsubscribe|newsletter|weekly digest|daily digest|no.reply|noreply"
    r"|your account has been|sign.?in attempt|security alert|verification code"
    r"|confirm your email|email confirmation|welcome to|thanks for signing up"
    r"|new message from|missed call|voicemail|you have a new|notification"
    r"|someone liked|commented on|mentioned you|follow request|friend request"
    r"|shipping update|your order has shipped|out for delivery|delivered"
    r"|tracking number|your package|has been delivered"
    r"|password reset|reset your password|forgot your password"
    r"|news:?letter|breaking news|top stories|today's headlines)",
    re.IGNORECASE,
)
_SKIP_SENDERS = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|notifications?@|alerts?@"
    r"|newsletter@|updates?@|news@|digest@|mailer@|postmaster@"
    r"|marketing@|promo@|deals@|offers@|info@(?!.*(?:invoice|bill|receipt)))",
    re.IGNORECASE,
)

def _fast_prefilter(subject: str, sender: str) -> bool:
    """Return True if email should be skipped WITHOUT calling AI.
    Only skips emails that are CLEARLY non-financial by pattern."""
    if _SKIP_SUBJECTS.search(subject):
        return True
    # Only skip on sender pattern if subject also lacks financial keywords
    if _SKIP_SENDERS.search(sender):
        fin_keywords = re.compile(
            r"(invoice|receipt|bill|statement|payment|charge|refund"
            r"|subscription|renewal|1099|W-2|tax|amount due|balance|total due"
            r"|order confirm|purchase confirm|transaction)",
            re.IGNORECASE,
        )
        if not fin_keywords.search(subject):
            return True
    return False

_MAX_AI_CALLS_PER_MONTH = 2000  # Safety cap — avoids runaway processing


def _build_service(creds):
    """Build a Gmail service with a connection timeout."""
    import httplib2
    import google_auth_httplib2
    _, _, build = _google_imports()
    http = httplib2.Http(timeout=_GMAIL_TIMEOUT)
    authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    return build("gmail", "v1", http=authorized_http)


def _fetch_month_message_ids(creds, year: int, month: int,
                              search_terms: list[str]) -> list[dict]:
    """Return [{id, threadId}] for one month. Thread-safe (builds own service)."""
    service = _build_service(creds)
    query = _month_query(year, month, search_terms)
    messages = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute(num_retries=2)
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_detail(service, msg_id: str) -> dict:
    """Fetch full message using an already-built service object."""
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute(num_retries=2)


def parse_headers(msg: dict) -> dict:
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        name = h.get("name", "").lower()
        if name in ("subject", "from", "date", "message-id"):
            headers[name.replace("-", "_")] = h.get("value", "")
    return {
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message_id", ""),
    }


def get_pdf_attachments(payload: dict) -> list[tuple[str, bytes | str]]:
    results = []

    def _walk(part):
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {})
        if filename and mime == "application/pdf":
            data = body.get("data")
            if data:
                raw = base64.urlsafe_b64decode(data + "==")
                results.append((filename, raw))
            else:
                attach_id = body.get("attachmentId")
                if attach_id:
                    results.append((filename, attach_id))
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(payload)
    return results


def decode_body(payload: dict) -> tuple[str, str]:
    html_content = ""
    text_content = ""

    def _walk(part):
        nonlocal html_content, text_content
        mime = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data", "")
        if body_data:
            decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
            if mime == "text/html" and not html_content:
                html_content = decoded
            elif mime == "text/plain" and not text_content:
                text_content = decoded
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(payload)
    return html_content, text_content


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


def upsert_transaction(txn: dict) -> dict:
    """Insert transaction, skipping if source_id already exists OR if a
    near-duplicate (same vendor + amount within 7 days) is already present.
    This prevents multiple payment-notification emails about the same payment
    from each creating a separate transaction record.
    """
    from app import db
    from email.utils import parsedate_to_datetime

    vendor = txn.get("vendor", "")
    amount = txn.get("amount")
    date_str = txn.get("date", "")

    # Near-duplicate check: same vendor + amount within 7 days
    if vendor and amount is not None:
        try:
            new_dt = parsedate_to_datetime(date_str)
            conn = db.get_connection()
            try:
                existing = conn.execute(
                    "SELECT id, date FROM transactions WHERE source='gmail' AND vendor=? AND amount=?",
                    (vendor, float(amount)),
                ).fetchall()
                for row in existing:
                    try:
                        existing_dt = parsedate_to_datetime(row["date"])
                        if abs((new_dt - existing_dt).days) <= 7:
                            return {"id": row["id"], "skipped": "near_duplicate"}
                    except Exception:
                        pass
            finally:
                conn.close()
        except Exception:
            pass  # date parse failed — fall through to normal insert

    try:
        txn_id = db.upsert_transaction(
            source=txn.get("source", "gmail"),
            source_id=txn.get("source_id", ""),
            entity_id=txn.get("entity_id"),
            tax_year=txn.get("year", ""),
            date=txn.get("date", ""),
            amount=txn.get("amount"),
            vendor=txn.get("vendor", ""),
            description=txn.get("description", ""),
            category=txn.get("category", "imported"),
            doc_type=txn.get("doc_type", "email"),
        )
        return {"id": txn_id}
    except Exception:
        return db.add_transaction(txn)


# ── per-month worker ───────────────────────────────────────────────────────────

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
