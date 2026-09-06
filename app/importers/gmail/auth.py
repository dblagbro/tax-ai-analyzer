"""Gmail OAuth credential storage + token lifecycle.

Extracted from the original 843-line ``app/importers/gmail_importer.py``
during Phase 11H refactor. The public API (``run_import``, ``get_auth_url``,
``complete_auth``, ``is_authenticated``) and the helpers IMAP imports
(``_ai_review_email``, ``_fast_prefilter``, ``_is_known_pdf``,
``_text_to_pdf``, ``upsert_transaction``) are re-exported by the package
``__init__`` so existing callers don't change.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from app.db import settings as db_settings
# 2026-09-06: GMAIL_SCOPES was referenced 5× below but never imported after the
# Phase 11H split → get_credentials() raised NameError → every Gmail import
# died before fetching a single message (while /api/import/gmail/status kept
# reporting authenticated=true because it only checks that a token exists).
from app.config import GMAIL_SCOPES
from app.importers.gmail.fetch import _google_imports

logger = logging.getLogger(__name__)


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
            global _LAST_REFRESH_ERROR
            _LAST_REFRESH_ERROR = str(e)[:200]
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


# 2026-09-06: the status endpoint used to report authenticated=true whenever
# a token ROW existed. The stored refresh token had been dead (invalid_grant)
# for months and nothing in the UI said so. token_health() actually exercises
# the token (refreshing if needed) and is cached so the Import tab can poll it.
_LAST_REFRESH_ERROR = ""
_TOKEN_HEALTH_CACHE: dict = {"ts": 0.0, "result": None}


def token_health(max_age_s: int = 60, force: bool = False) -> dict:
    """{"has_token": bool, "valid": bool, "error": str}. Network call at most
    once per max_age_s; never raises."""
    import time
    global _LAST_REFRESH_ERROR
    now = time.time()
    cached = _TOKEN_HEALTH_CACHE["result"]
    if cached is not None and not force and now - _TOKEN_HEALTH_CACHE["ts"] < max_age_s:
        return dict(cached)
    if not _load_token_from_db():
        result = {"has_token": False, "valid": False, "error": "no Gmail token stored — connect Gmail"}
    else:
        _LAST_REFRESH_ERROR = ""
        try:
            creds = get_credentials()
            if creds is not None:
                result = {"has_token": True, "valid": True, "error": ""}
            else:
                why = _LAST_REFRESH_ERROR or "token invalid"
                hint = " — Google expired the refresh token; reconnect Gmail" if "invalid_grant" in why else ""
                result = {"has_token": True, "valid": False, "error": f"{why}{hint}"}
        except Exception as e:
            result = {"has_token": True, "valid": False, "error": str(e)[:200]}
    _TOKEN_HEALTH_CACHE["ts"] = now
    _TOKEN_HEALTH_CACHE["result"] = dict(result)
    return result


# ── search / fetch helpers ─────────────────────────────────────────────────────
