"""/api/import/gmail/status must report whether the token WORKS, not whether
a token row exists. google-auth calls a stored access token "valid" until it
is used, so token_health() makes a real (cheap) getProfile call.
(2026-09-06 — the stored refresh token was dead for months while the endpoint
said authenticated=true.)"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.importers.gmail import auth  # noqa: E402


def _reset_cache():
    auth._TOKEN_HEALTH_CACHE["ts"] = 0.0
    auth._TOKEN_HEALTH_CACHE["result"] = None


def _fake_build(profile=None, error=None):
    svc = MagicMock()
    if error:
        svc.users.return_value.getProfile.return_value.execute.side_effect = error
    else:
        svc.users.return_value.getProfile.return_value.execute.return_value = profile or {"emailAddress": "me@example.com"}
    return lambda *a, **k: svc


def test_no_token_stored():
    _reset_cache()
    with patch.object(auth, "_load_token_from_db", return_value=None):
        th = auth.token_health(force=True)
    assert th == {"has_token": False, "valid": False, "error": "no Gmail token stored — connect Gmail"}


def test_refresh_failure_reports_invalid_with_hint():
    _reset_cache()

    def fake_get_credentials():
        auth._LAST_REFRESH_ERROR = "invalid_grant: Bad Request"
        return None

    with patch.object(auth, "_load_token_from_db", return_value={"refresh_token": "x"}), \
         patch.object(auth, "get_credentials", side_effect=fake_get_credentials):
        th = auth.token_health(force=True)
    assert th["has_token"] is True and th["valid"] is False
    assert "invalid_grant" in th["error"] and "reconnect" in th["error"]


def test_dead_token_only_fails_on_real_call():
    """The exact production shape: creds look valid, the API call raises."""
    _reset_cache()
    with patch.object(auth, "_load_token_from_db", return_value={"refresh_token": "x"}), \
         patch.object(auth, "get_credentials", return_value=object()), \
         patch.object(auth, "_google_imports",
                      return_value=(None, None, _fake_build(error=RuntimeError("invalid_grant: Bad Request")))):
        th = auth.token_health(force=True)
    assert th["valid"] is False and "invalid_grant" in th["error"] and "reconnect" in th["error"]


def test_valid_token_and_cache():
    _reset_cache()
    calls = {"n": 0}

    def fake_get_credentials():
        calls["n"] += 1
        return object()

    with patch.object(auth, "_load_token_from_db", return_value={"refresh_token": "x"}), \
         patch.object(auth, "get_credentials", side_effect=fake_get_credentials), \
         patch.object(auth, "_google_imports", return_value=(None, None, _fake_build())):
        th = auth.token_health(force=True)
        assert th["valid"] is True and th["email"] == "me@example.com"
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
