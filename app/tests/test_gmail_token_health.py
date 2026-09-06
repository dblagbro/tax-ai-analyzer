"""/api/import/gmail/status must report whether the token WORKS, not whether
a token row exists. (2026-09-06 — the stored refresh token was dead for
months while the endpoint said authenticated=true.)"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.importers.gmail import auth  # noqa: E402


def _reset_cache():
    auth._TOKEN_HEALTH_CACHE["ts"] = 0.0
    auth._TOKEN_HEALTH_CACHE["result"] = None


def test_no_token_stored():
    _reset_cache()
    with patch.object(auth, "_load_token_from_db", return_value=None):
        th = auth.token_health(force=True)
    assert th == {"has_token": False, "valid": False, "error": "no Gmail token stored — connect Gmail"}


def test_dead_refresh_token_reports_invalid_with_hint():
    _reset_cache()

    def fake_get_credentials():
        auth._LAST_REFRESH_ERROR = "invalid_grant: Bad Request"
        return None

    with patch.object(auth, "_load_token_from_db", return_value={"refresh_token": "x"}), \
         patch.object(auth, "get_credentials", side_effect=fake_get_credentials):
        th = auth.token_health(force=True)
    assert th["has_token"] is True and th["valid"] is False
    assert "invalid_grant" in th["error"] and "reconnect" in th["error"]


def test_valid_token_and_cache():
    _reset_cache()
    calls = {"n": 0}

    def fake_get_credentials():
        calls["n"] += 1
        return object()

    with patch.object(auth, "_load_token_from_db", return_value={"refresh_token": "x"}), \
         patch.object(auth, "get_credentials", side_effect=fake_get_credentials):
        assert auth.token_health(force=True)["valid"] is True
        assert auth.token_health()["valid"] is True  # served from cache
    assert calls["n"] == 1


def test_status_route_uses_token_health():
    from app.web_ui import app
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    with patch("app.importers.gmail.auth.token_health",
               return_value={"has_token": True, "valid": False, "error": "invalid_grant — reconnect Gmail"}):
        body = c.get("/tax-ai-analyzer/api/import/gmail/status").get_json()
    assert body["authenticated"] is False and body["token_valid"] is False
    assert "reconnect" in body["token_error"]
