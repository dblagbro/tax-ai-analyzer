"""After a Gmail reconnect, an import queued in `gmail_autostart_years` starts
by itself (Google testing-mode refresh tokens expire after 7 days, so a
reconnect precedes nearly every run). Everything that would touch the live
token / settings / job tables is patched. (2026-10-01)"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _client():
    from app.web_ui import app
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _run_callback(tmp_path, queued):
    from app.routes.importers import import_gmail as ig
    store = {"gmail_autostart_years": json.dumps(queued)}
    creds = MagicMock(token="t", refresh_token="r", token_uri="u", client_id="c",
                      client_secret="s", scopes=["scope"])
    flow = MagicMock(credentials=creds)
    started = []
    with patch.object(ig, "_make_flow", return_value=flow), \
         patch.object(ig, "GMAIL_TOKEN_FILE", str(tmp_path / "gmail_token.json")), \
         patch.object(ig.db, "set_setting", side_effect=lambda k, v: store.__setitem__(k, v)), \
         patch.object(ig.db, "get_setting", side_effect=lambda k, *a: store.get(k, "")), \
         patch.object(ig.db, "log_activity"), \
         patch.object(ig, "_start_gmail_job", side_effect=lambda eid, yrs: started.append((eid, yrs)) or 123):
        r = _client().get("/tax-ai-analyzer/import/gmail/auth/callback?code=x&state=y")
    return r, store, started


def test_callback_autostarts_queued_years_and_clears_queue(tmp_path):
    r, store, started = _run_callback(tmp_path, ["2023"])
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Gmail Connected" in body and "job #123" in body and "2023" in body
    assert started == [(None, ["2023"])]
    assert store["gmail_autostart_years"] == "[]"            # one-shot
    assert json.loads(store["gmail_oauth_token"])["refresh_token"] == "r"


def test_callback_without_queue_starts_nothing(tmp_path):
    r, store, started = _run_callback(tmp_path, [])
    assert r.status_code == 200
    assert started == []
    assert "started automatically" not in r.get_data(as_text=True)


def test_effective_redirect_uri_is_browsing_host_unless_overridden():
    from app.routes.importers import import_gmail as ig
    www = "https://www.voipguru.org/tax-ai-analyzer/import/gmail/auth/callback"
    # credentials.json may list a stale URI — it must NOT be substituted
    with patch.object(ig, "_registered_redirect_uris", return_value=["https://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback"]), \
         patch.object(ig.db, "get_setting", return_value=""):
        assert ig._effective_redirect_uri(www) == www
    with patch.object(ig.db, "get_setting", return_value="https://example.org/cb"):
        assert ig._effective_redirect_uri(www) == "https://example.org/cb"


def test_callback_restores_pkce_verifier_and_uses_browsing_host_uri(tmp_path):
    from app.routes.importers import import_gmail as ig
    reg = "http://localhost/tax-ai-analyzer/import/gmail/auth/callback"   # test client's host
    creds = MagicMock(token="t", refresh_token="r", token_uri="u", client_id="c", client_secret="s", scopes=["x"])
    flow = MagicMock(credentials=creds)
    seen = {}
    def make_flow(redirect_uri=None):
        seen["redirect_uri"] = redirect_uri
        return flow
    store = {}
    c = _client()
    with c.session_transaction() as s:
        s["gmail_oauth_verifier"] = "VERIFIER123"
    with patch.object(ig, "_make_flow", side_effect=make_flow), \
         patch.object(ig, "GMAIL_TOKEN_FILE", str(tmp_path / "t.json")), \
         patch.object(ig.db, "set_setting", side_effect=lambda k, v: store.__setitem__(k, v)), \
         patch.object(ig.db, "get_setting", side_effect=lambda k, *a: store.get(k, "")), \
         patch.object(ig.db, "log_activity"), \
         patch.object(ig, "_start_gmail_job", return_value=1):
        r = c.get("/tax-ai-analyzer/import/gmail/auth/callback?code=abc&state=xyz")
    assert r.status_code == 200
    assert seen["redirect_uri"].endswith("/tax-ai-analyzer/import/gmail/auth/callback")
    assert "localhost" in seen["redirect_uri"]                 # the host being browsed
    assert flow.code_verifier == "VERIFIER123"
    assert flow.fetch_token.call_args.kwargs["authorization_response"] == seen["redirect_uri"] + "?code=abc&state=xyz"
