"""Gmail OAuth import routes."""
import json
import logging
import os
import threading
from datetime import datetime

from flask import (
    Blueprint, Response, flash, jsonify, redirect, render_template,
    request, session as flask_session, stream_with_context,
)
from flask_login import current_user, login_required

from app import db
from app.config import (
    URL_PREFIX, CONSUME_PATH,
    GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES, GMAIL_YEARS,
)
from app.routes._state import (
    _job_logs, _job_stop_events, _job_stop_lock, append_job_log,
)
from app.routes.helpers import _url, admin_required, setup_chat_stream

logger = logging.getLogger(__name__)

bp = Blueprint("import_gmail", __name__)

def _gmail_callback_url() -> str:
    """Derive callback URL from the current request host so OAuth works on any deployment."""
    try:
        from flask import request as _req
        base = _req.host_url.rstrip("/")
        return f"{base}{URL_PREFIX}/import/gmail/auth/callback"
    except RuntimeError:
        # Outside request context — derive from APP_HOST_URL env var (no default)
        host = os.environ.get("APP_HOST_URL", "").rstrip("/")
        if not host:
            raise RuntimeError(
                "Gmail OAuth callback URL cannot be determined outside a request context. "
                "Set APP_HOST_URL env var (e.g. https://your-domain.com)."
            )
        return f"{host}{URL_PREFIX}/import/gmail/auth/callback"

def _gmail_setup_system_prompt(callback_url: str) -> str:
    return f"""You are a friendly setup assistant helping the user connect their personal Gmail account to a self-hosted tax document organizer app. This app automatically imports tax-related emails (receipts, invoices, 1099s, W-2s, etc.) into a local document management system.

KEY FACTS:
- Works with personal @gmail.com accounts (NOT just Google Workspace)
- Select "External" user type on the OAuth consent screen (for personal Gmail)
- App stays in "Testing" mode — that is fine and expected
- Must add themselves as a test user on the consent screen

CRITICAL — REDIRECT URI:
The app callback URL is: {callback_url}
This MUST be added as an Authorized Redirect URI in the Google Cloud credential.
Without this, Google redirects to localhost and the token is never received.

CREDENTIAL TYPE: Must be "Web application" (NOT Desktop app).
Desktop app credentials use localhost redirect which won't work for a server-hosted app.

SETUP STEPS:
1. Go to console.cloud.google.com — sign in with the Google account to scan
2. Create a new project (name it e.g. "tax-gmail-collector")
3. Enable Gmail API: APIs & Services → Library → search "Gmail API" → Enable
4. Configure OAuth consent screen: APIs & Services → OAuth consent screen → External → fill app name, emails → Save
5. Add scope: Scopes step → Add or Remove Scopes → search "gmail.readonly" → select → Update → Save
6. Add test user: Test Users step → Add Users → add their @gmail.com → Save
7. Create credential: APIs & Services → Credentials → Create Credentials → OAuth client ID
   → Select type: "Web application" (NOT Desktop app)
   → Under "Authorized redirect URIs" click Add URI
   → Enter: {callback_url}
   → Click Create
8. Download JSON: click download icon on the new credential
9. Upload here: use the upload section below the chat

If the user already has a Desktop app credential: tell them to either edit it and add the redirect URI, or delete it and create a new "Web application" credential.

Google's UI changes frequently. Adapt guidance if the user describes a different layout. Be concise and step-by-step."""


def _make_flow(redirect_uri=None):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(
        GMAIL_CREDENTIALS_FILE,
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri or _gmail_callback_url(),
    )


@bp.route(URL_PREFIX + "/import/gmail/setup")
@login_required
def gmail_setup_page():
    return render_template("gmail_setup.html",
                           has_credentials=os.path.exists(GMAIL_CREDENTIALS_FILE),
                           has_token=os.path.exists(GMAIL_TOKEN_FILE),
                           url_prefix=URL_PREFIX)


@bp.route(URL_PREFIX + "/import/gmail/credentials", methods=["POST"])
@login_required
@admin_required
def gmail_upload_credentials():
    f = request.files.get("credentials")
    if f:
        try:
            content = f.read()
            json.loads(content)
            os.makedirs(os.path.dirname(GMAIL_CREDENTIALS_FILE), exist_ok=True)
            with open(GMAIL_CREDENTIALS_FILE, "wb") as out:
                out.write(content)
            flash("credentials.json saved.", "success")
        except json.JSONDecodeError:
            flash("Invalid JSON file.", "danger")
        except Exception as e:
            flash(f"Error: {e}", "danger")
    return redirect(_url("/import/gmail/setup"))


@bp.route(URL_PREFIX + "/import/gmail/auth")
@login_required
def gmail_oauth_start():
    try:
        flow = _make_flow(redirect_uri=_gmail_callback_url())
        auth_url, state = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true")
        flask_session["gmail_oauth_state"] = state
        return redirect(auth_url)
    except FileNotFoundError:
        flash("credentials.json not found.", "danger")
        return redirect(_url("/import"))
    except ImportError:
        flash("google-auth-oauthlib not installed.", "danger")
        return redirect(_url("/import"))
    except Exception as e:
        logger.error("Gmail OAuth start error: %s", e)
        flash(f"OAuth error: {e}", "danger")
        return redirect(_url("/import"))


@bp.route(URL_PREFIX + "/import/gmail/auth/callback")
@login_required
def gmail_oauth_callback():
    try:
        flow = _make_flow(redirect_uri=_gmail_callback_url())
        cb = _gmail_callback_url()
        auth_response = cb + "?" + request.query_string.decode()
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else GMAIL_SCOPES,
        }
        os.makedirs(os.path.dirname(GMAIL_TOKEN_FILE), exist_ok=True)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)
        db.set_setting("gmail_oauth_token", json.dumps(token_data))
        db.log_activity("gmail_oauth_complete", "Token saved", user_id=current_user.id)
        return """<!doctype html><html><head><title>Gmail Connected</title>
<style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f0fdf4}
.box{text-align:center;padding:40px;background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.1)}
h2{color:#16a34a;margin:0 0 8px}p{color:#555;margin:0 0 20px}button{padding:8px 20px;background:#16a34a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:1rem}</style>
</head><body><div class="box"><h2>&#10003; Gmail Connected!</h2>
<p>Authorization complete. You can close this tab and return to the Tax Organizer.</p>
<button onclick="window.close()">Close Tab</button></div>
<script>setTimeout(function(){window.close();},3000);</script>
</body></html>"""
    except ImportError:
        flash("google-auth-oauthlib not installed.", "danger")
    except Exception as e:
        logger.error("Gmail OAuth callback error: %s", e)
        flash(f"OAuth callback error: {e}", "danger")
    return redirect(_url("/import"))


@bp.route(URL_PREFIX + "/import/gmail/clear-credentials", methods=["POST"])
@login_required
@admin_required
def gmail_clear_credentials():
    try:
        if os.path.exists(GMAIL_CREDENTIALS_FILE):
            os.remove(GMAIL_CREDENTIALS_FILE)
        if os.path.exists(GMAIL_TOKEN_FILE):
            os.remove(GMAIL_TOKEN_FILE)
        db.log_activity("Gmail credentials cleared")
        flash("Gmail credentials and token cleared.", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(_url("/import/gmail/setup"))


@bp.route(URL_PREFIX + "/api/import/gmail/status")
@login_required
def gmail_status_api():
    from app.config import GMAIL_SEARCH_TERMS
    token_in_db = bool(db.get_setting("gmail_oauth_token"))
    return jsonify({
        "has_credentials": os.path.exists(GMAIL_CREDENTIALS_FILE),
        "has_token": os.path.exists(GMAIL_TOKEN_FILE) or token_in_db,
        "authenticated": token_in_db,
        "search_terms": GMAIL_SEARCH_TERMS,
        "callback_url": _gmail_callback_url(),
    })


@bp.route(URL_PREFIX + "/api/import/gmail/search-terms", methods=["POST"])
@login_required
def api_save_gmail_search_terms():
    data = request.get_json(force=True) or {}
    terms = data.get("terms", "")
    db.set_setting("gmail_search_terms", terms.strip())
    return jsonify({"status": "ok", "terms": terms.strip()})


@bp.route(URL_PREFIX + "/api/import/gmail/credentials", methods=["POST"])
@login_required
@admin_required
def api_import_gmail_credentials():
    if "credentials" in request.files:
        f = request.files["credentials"]
        try:
            content = f.read()
            json.loads(content)
            os.makedirs(os.path.dirname(GMAIL_CREDENTIALS_FILE), exist_ok=True)
            with open(GMAIL_CREDENTIALS_FILE, "wb") as out:
                out.write(content)
            db.log_activity("gmail_creds_saved", "credentials.json uploaded",
                            user_id=current_user.id)
            return jsonify({"status": "saved"})
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON file"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    data = request.get_json() or {}
    try:
        parsed = json.loads(data.get("credentials_json", "{}"))
        os.makedirs(os.path.dirname(GMAIL_CREDENTIALS_FILE), exist_ok=True)
        with open(GMAIL_CREDENTIALS_FILE, "w") as out:
            json.dump(parsed, out, indent=2)
        db.log_activity("gmail_creds_saved", "pasted", user_id=current_user.id)
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route(URL_PREFIX + "/api/import/gmail/start", methods=["POST"])
@login_required
def api_import_gmail_start():
    data = request.get_json() or {}
    entity_id = data.get("entity_id")
    years = data.get("years", GMAIL_YEARS)
    if not os.path.exists(GMAIL_CREDENTIALS_FILE) and not db.get_setting("gmail_oauth_token"):
        return jsonify({"error": "Gmail not configured. Use Setup / Credentials first."}), 400
    job_id = db.create_import_job("gmail", entity_id=entity_id,
                                  config_json=json.dumps({"years": years}))
    _job_logs[job_id] = []
    stop_ev = threading.Event()
    with _job_stop_lock:
        _job_stop_events[job_id] = stop_ev

    def _run(jid, eid, yrs, stop):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.gmail_importer import run_import
            entity_slug = "personal"
            if eid:
                e = db.get_entity(entity_id=eid)
                if e:
                    entity_slug = e.get("slug", "personal")

            def _flush(imported, skipped):
                db.update_import_job(jid, count_imported=imported, count_skipped=skipped)

            result = run_import(entity_id=eid, years=yrs,
                                consume_path=CONSUME_PATH, entity_slug=entity_slug,
                                log_fn=log, stop_event=stop, progress_fn=_flush)
            count = result.get("imported", 0) if isinstance(result, dict) else result
            skipped = result.get("skipped", 0) if isinstance(result, dict) else 0
            filtered = result.get("ai_filtered", 0) if isinstance(result, dict) else 0
            final_status = "cancelled" if stop.is_set() else "completed"
            db.update_import_job(jid, status=final_status, count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"Gmail: {count} imported, {filtered} AI-filtered, {skipped} skipped")
        except Exception as e:
            import traceback
            append_job_log(jid, f"FATAL ERROR: {e}")
            append_job_log(jid, traceback.format_exc()[:500])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())
        finally:
            with _job_stop_lock:
                _job_stop_events.pop(jid, None)

    threading.Thread(target=_run, args=(job_id, entity_id, years, stop_ev),
                     daemon=True, name=f"gmail-{job_id}").start()
    return jsonify({"status": "started", "job_id": job_id})


@bp.route(URL_PREFIX + "/api/import/gmail/setup-chat", methods=["POST"])
@login_required
def gmail_setup_chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "message required"}), 400
    gen = setup_chat_stream(_gmail_setup_system_prompt(_gmail_callback_url()), data.get("history", []), user_message)
    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
