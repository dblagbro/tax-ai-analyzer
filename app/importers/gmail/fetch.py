"""Gmail API connection + message-list / message-detail fetching.

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
from typing import Optional

logger = logging.getLogger(__name__)


def _google_imports():
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    return Flow, Credentials, build


# ── credential helpers ─────────────────────────────────────────────────────────


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
