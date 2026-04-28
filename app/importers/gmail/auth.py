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
