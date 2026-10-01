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


def test_registered_host_redirect():
    from app.routes.importers import import_gmail as ig
    reg = ["https://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback"]
    with patch.object(ig, "_registered_redirect_uris", return_value=reg):
        # wrong hostname → bounce to the OAuth start route on the registered host
        assert ig._registered_host_redirect(
            "https://www.voipguru.org/tax-ai-analyzer/import/gmail/auth/callback"
        ) == "https://voipguru.org/tax-ai-analyzer/import/gmail/auth"
        # already the registered callback → no redirect
        assert ig._registered_host_redirect(reg[0]) is None
        # same host, different scheme → never loop
        assert ig._registered_host_redirect(
            "http://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback") is None
    with patch.object(ig, "_registered_redirect_uris", return_value=[]):
        assert ig._registered_host_redirect("https://x/y") is None
