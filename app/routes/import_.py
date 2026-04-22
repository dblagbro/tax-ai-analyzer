"""Import jobs, credentials, URL pollers, cloud adapters, Gmail/PayPal/US Alliance/OFX."""
import json
import logging
import os
import threading
from datetime import datetime

from flask import (
    Blueprint, Response, flash, jsonify, redirect, render_template,
    request, session as flask_session, stream_with_context, url_for,
)
from flask_login import current_user, login_required

from app import db
from app.config import (
    URL_PREFIX, CONSUME_PATH,
    GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES, GMAIL_YEARS,
    LLM_MODEL,
)
from app.routes._state import (
    _job_logs, _job_logs_lock,
    _job_stop_events, _job_stop_lock,
    append_job_log,
)
from app.routes.helpers import _row_list, _url, admin_required
from app.routes.transactions import _run_csv_job

logger = logging.getLogger(__name__)

bp = Blueprint("import_", __name__)


GMAIL_CALLBACK_URL = f"https://www.voipguru.org{URL_PREFIX}/import/gmail/auth/callback"

GMAIL_SETUP_SYSTEM_PROMPT = """You are a friendly setup assistant helping the user connect their personal Gmail account to a self-hosted tax document organizer app. This app automatically imports tax-related emails (receipts, invoices, 1099s, W-2s, etc.) into a local document management system.

KEY FACTS:
- Works with personal @gmail.com accounts (NOT just Google Workspace)
- Select "External" user type on the OAuth consent screen (for personal Gmail)
- Create a "Desktop app" credential (NOT Web application)
- App stays in "Testing" mode — that is fine and expected
- Must add themselves as a test user on the consent screen

CRITICAL — REDIRECT URI:
The app callback URL is: https://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback
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
   → Enter: https://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback
   → Click Create
8. Download JSON: click download icon on the new credential
9. Upload here: use the upload section below the chat

If the user already has a Desktop app credential: tell them to either edit it and add the redirect URI, or delete it and create a new "Web application" credential.

Google's UI changes frequently. Adapt guidance if the user describes a different layout. Be concise and step-by-step."""

PAYPAL_SETUP_SYSTEM_PROMPT = """You are a friendly setup assistant helping the user connect their PayPal account to a self-hosted tax document organizer app. This app pulls transaction history directly from PayPal's Transactions API to categorize income and expenses for tax purposes.

KEY FACTS:
- Requires a PayPal Developer account (free, at developer.paypal.com)
- Uses REST API credentials: Client ID + Client Secret
- Must create a "Live" app (not Sandbox) to pull real transactions
- Sandbox mode only works for test transactions, not real PayPal history
- The app only READS transactions — it never initiates payments or transfers

SETUP STEPS:
1. Go to developer.paypal.com and log in with your PayPal business or personal account
2. Click "My Apps & Credentials" in the top navigation
3. Make sure the toggle at the top-right is set to "Live" (not Sandbox)
4. Click "Create App" button
5. App name: use something like "Tax Organizer" — the name is just for your reference
6. App Type: select "Merchant" (gives access to Transactions API)
7. Click "Create App"
8. On the app detail page, copy the "Client ID" (starts with A)
9. Click "Show" under "Secret" and copy the Client Secret (starts with E)
10. Paste both into the fields on the Import Hub → PayPal tab and click "Save & Test Credentials"

TROUBLESHOOTING:
- "401 Unauthorized": double-check you copied Live credentials, not Sandbox
- "403 Forbidden": the app may not have Transactions API permission — check App Features
- If you see "App Features" section, enable "Transaction Search" if available
- Sandbox mode: only use this if you want to test with PayPal's test environment

Be concise and step-by-step. Ask clarifying questions if the user seems stuck."""


def _make_flow(redirect_uri=None):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(
        GMAIL_CREDENTIALS_FILE,
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri or GMAIL_CALLBACK_URL,
    )


def _setup_chat_stream(system_prompt: str, history: list, user_message: str):
    """Shared SSE generator for guided setup chat endpoints."""
    settings = db.get_all_settings()
    api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    model = settings.get("llm_model") or LLM_MODEL

    messages = []
    for h in history[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    def _generate():
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model=model, max_tokens=1024,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return _generate

# ── CSV importers ────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/paypal/csv", methods=["POST"])
@login_required
def api_import_paypal_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "")
    csv_bytes = f.read()
    col_map = {"date": "Date", "description": "Name", "amount": "Amount"}
    job_id = db.create_import_job("paypal", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename}))
    threading.Thread(target=_run_csv_job,
                     args=(job_id, csv_bytes, "paypal", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@bp.route(URL_PREFIX + "/api/import/venmo/csv", methods=["POST"])
@login_required
def api_import_venmo_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "")
    csv_bytes = f.read()
    col_map = {"date": "Datetime", "description": "Note", "amount": "Amount (total)"}
    job_id = db.create_import_job("venmo", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename}))
    threading.Thread(target=_run_csv_job,
                     args=(job_id, csv_bytes, "venmo", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@bp.route(URL_PREFIX + "/api/import/bank-csv", methods=["POST"])
@login_required
def api_import_bank_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "")
    col_map = {
        "date": request.form.get("date_col", "Date"),
        "description": request.form.get("desc_col", "Description"),
        "amount": request.form.get("amount_col", "Amount"),
    }
    csv_bytes = f.read()
    job_id = db.create_import_job("bank_csv", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename,
                                                          "col_map": col_map}))
    threading.Thread(target=_run_csv_job,
                     args=(job_id, csv_bytes, "bank_csv", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── URL import ───────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/url", methods=["POST"])
@login_required
def api_import_url():
    data = request.get_json() or {}
    import_url = data.get("url", "").strip()
    entity_id = data.get("entity_id")
    if not import_url:
        return jsonify({"error": "url required"}), 400
    job_id = db.create_import_job("url", entity_id=entity_id,
                                  config_json=json.dumps({"url": import_url}))

    def _run(jid, u):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            import httpx
            r = httpx.get(u, follow_redirects=True, timeout=30)
            r.raise_for_status()
            db.update_import_job(jid, status="completed", count_imported=1,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"URL: {u}")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, import_url), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── OFX import ───────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/bank-ofx", methods=["POST"])
@login_required
def api_import_bank_ofx():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "") or None
    content = f.read()
    job_id = db.create_import_job("ofx_import", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename}))

    def _run(jid, data, eid, yr):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.ofx_importer import parse_ofx
            txns = parse_ofx(data, entity_id=eid, default_year=yr)
            total = 0
            for t in txns:
                try:
                    db.add_transaction(t)
                    total += 1
                except Exception:
                    pass
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"OFX: {total} transactions imported")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, content, entity_id, year),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── Gmail ────────────────────────────────────────────────────────────────────

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
        flow = _make_flow(redirect_uri=GMAIL_CALLBACK_URL)
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
        flow = _make_flow(redirect_uri=GMAIL_CALLBACK_URL)
        auth_response = GMAIL_CALLBACK_URL + "?" + request.query_string.decode()
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
        "callback_url": GMAIL_CALLBACK_URL,
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
    gen = _setup_chat_stream(GMAIL_SETUP_SYSTEM_PROMPT, data.get("history", []), user_message)
    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── PayPal ───────────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/paypal/credentials", methods=["POST"])
@login_required
def api_paypal_save_credentials():
    data = request.get_json() or {}
    client_id = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "").strip()
    sandbox = bool(data.get("sandbox", False))
    if not client_id or not client_secret:
        return jsonify({"error": "client_id and client_secret required"}), 400
    db.set_setting("paypal_client_id", client_id)
    db.set_setting("paypal_client_secret", client_secret)
    db.set_setting("paypal_sandbox", "1" if sandbox else "0")
    try:
        from app.importers.paypal_api import get_access_token
        get_access_token(client_id, client_secret, sandbox=sandbox)
        return jsonify({"status": "ok", "message": "Credentials saved and verified."})
    except Exception as e:
        return jsonify({"status": "saved", "message": f"Saved but test failed: {e}"})


@bp.route(URL_PREFIX + "/api/import/paypal/pull", methods=["POST"])
@login_required
def api_import_paypal_pull():
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or [str(datetime.utcnow().year)]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]
    client_id = data.get("client_id") or db.get_setting("paypal_client_id")
    client_secret = data.get("client_secret") or db.get_setting("paypal_client_secret")
    sandbox = (db.get_setting("paypal_sandbox") or "0") == "1"
    if not client_id or not client_secret:
        return jsonify({"error": "PayPal credentials not configured."}), 400
    job_id = db.create_import_job("paypal_api", entity_id=entity_id,
                                  config_json=json.dumps({"years": years, "sandbox": sandbox}))

    def _run(jid, cid, csec, ys, eid, sbx):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.paypal_api import pull_transactions_for_year
            total = 0
            for yr in ys:
                txns = pull_transactions_for_year(cid, csec, yr, entity_id=eid, sandbox=sbx)
                for t in txns:
                    try:
                        db.add_transaction(t)
                        total += 1
                    except Exception:
                        pass
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"PayPal API: {total} transactions for {ys}")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, client_id, client_secret,
                                        years, entity_id, sandbox),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@bp.route(URL_PREFIX + "/api/import/paypal/status", methods=["GET"])
@login_required
def api_paypal_status():
    cid = db.get_setting("paypal_client_id") or ""
    return jsonify({
        "configured": bool(cid),
        "sandbox": (db.get_setting("paypal_sandbox") or "0") == "1",
        "client_id_preview": (cid[:8] + "…") if len(cid) > 8 else cid,
    })


@bp.route(URL_PREFIX + "/api/import/paypal/setup-chat", methods=["POST"])
@login_required
def paypal_setup_chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "message required"}), 400
    gen = _setup_chat_stream(PAYPAL_SETUP_SYSTEM_PROMPT, data.get("history", []), user_message)
    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── US Alliance FCU ──────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/usalliance/credentials", methods=["POST"])
@login_required
def api_usalliance_save_credentials():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    db.set_setting("usalliance_username", username)
    db.set_setting("usalliance_password", password)
    return jsonify({"status": "saved", "message": "Credentials saved."})


@bp.route(URL_PREFIX + "/api/import/usalliance/cookies", methods=["POST"])
@login_required
def api_usalliance_save_cookies():
    data = request.get_json() or {}
    cookies_raw = data.get("cookies")
    if not cookies_raw:
        return jsonify({"error": "cookies field is required"}), 400
    if isinstance(cookies_raw, str):
        try:
            cookies_list = json.loads(cookies_raw)
        except Exception:
            return jsonify({"error": "cookies must be a valid JSON array"}), 400
    elif isinstance(cookies_raw, list):
        cookies_list = cookies_raw
    else:
        return jsonify({"error": "cookies must be a JSON array"}), 400
    if not isinstance(cookies_list, list) or len(cookies_list) == 0:
        return jsonify({"error": "cookies must be a non-empty JSON array"}), 400
    db.set_setting("usalliance_cookies", json.dumps(cookies_list))
    return jsonify({"status": "saved", "message": f"{len(cookies_list)} cookies saved.",
                    "count": len(cookies_list)})


@bp.route(URL_PREFIX + "/api/import/usalliance/cookies", methods=["DELETE"])
@login_required
def api_usalliance_clear_cookies():
    db.set_setting("usalliance_cookies", "")
    return jsonify({"status": "cleared"})


@bp.route(URL_PREFIX + "/api/import/usalliance/status", methods=["GET"])
@login_required
def api_usalliance_status():
    user = db.get_setting("usalliance_username") or ""
    cookies_raw = db.get_setting("usalliance_cookies") or ""
    cookies_count = 0
    if cookies_raw:
        try:
            cookies_count = len(json.loads(cookies_raw))
        except Exception:
            pass
    return jsonify({
        "configured": bool(user),
        "username_preview": (user[:3] + "…") if len(user) > 3 else user,
        "cookies_saved": cookies_count > 0,
        "cookies_count": cookies_count,
    })


@bp.route(URL_PREFIX + "/api/import/usalliance/test", methods=["POST"])
@login_required
def api_usalliance_test():
    username = db.get_setting("usalliance_username")
    password = db.get_setting("usalliance_password")
    if not username or not password:
        return jsonify({"error": "Credentials not saved — enter username and password first"}), 400
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return jsonify({"error": "Playwright not installed in this container"}), 500
    try:
        from playwright_stealth import Stealth
        _stealth = Stealth(navigator_webdriver=True, navigator_plugins=True,
                           chrome_app=True, chrome_csi=True, webgl_vendor=True,
                           navigator_platform_override="Win32")
    except ImportError:
        _stealth = None
    try:
        with sync_playwright() as p:
            if _stealth:
                _stealth.hook_playwright_context(p)
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--headless=new", "--disable-blink-features=AutomationControlled",
                      "--window-size=1280,900"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = context.new_page()
            if _stealth:
                _stealth.apply_stealth_sync(page)
            page.goto("https://account.usalliance.org/login",
                      wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            content = page.content().lower()
            current_url = page.url
            browser.close()
            if "404 page not found" in content or "404 - page not found" in content:
                return jsonify({"error": "Portal is blocking this server's browser (bot detection)."})
            if "login" in current_url or "username" in content or "sign in" in content:
                return jsonify({"status": "ok", "message": "Login page reached. Run a full import to authenticate."})
            elif "dashboard" in current_url or "account" in current_url:
                return jsonify({"status": "ok", "message": "Already authenticated (session active)."})
            else:
                return jsonify({"status": "ok", "message": f"Portal reached at {current_url}."})
    except Exception as e:
        return jsonify({"error": f"Test failed: {str(e)[:200]}"})


@bp.route(URL_PREFIX + "/api/import/usalliance/mfa", methods=["POST"])
@login_required
def api_usalliance_mfa():
    data = request.get_json() or {}
    job_id = data.get("job_id")
    code = data.get("code", "").strip()
    if not job_id or not code:
        return jsonify({"error": "job_id and code required"}), 400
    from app.importers.usalliance_importer import set_mfa_code
    set_mfa_code(int(job_id), code)
    return jsonify({"status": "ok"})


@bp.route(URL_PREFIX + "/api/import/usalliance/start", methods=["POST"])
@login_required
def api_import_usalliance_start():
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or ["2022", "2023", "2024", "2025"]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]
    username = db.get_setting("usalliance_username")
    password = db.get_setting("usalliance_password")
    if not username or not password:
        return jsonify({"error": "US Alliance credentials not configured."}), 400
    cookies = None
    cookies_raw = db.get_setting("usalliance_cookies") or ""
    if cookies_raw:
        try:
            cookies = json.loads(cookies_raw)
        except Exception:
            cookies = None
    entity_slug = "personal"
    if entity_id:
        ent = db.get_entity(entity_id=entity_id)
        if ent:
            entity_slug = ent.get("slug", "personal")
    job_id = db.create_import_job("usalliance", entity_id=entity_id,
                                  config_json=json.dumps({"years": years,
                                                          "cookie_auth": cookies is not None}))
    _job_logs[job_id] = []

    def _run(jid, uname, pw, yrs, eid, eslug, ckies):
        log = lambda msg: append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.usalliance_importer import run_import
            result = run_import(username=uname, password=pw, years=yrs,
                                consume_path=CONSUME_PATH, entity_slug=eslug,
                                job_id=jid, log=log, cookies=ckies)
            total = result.get("imported", 0)
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"US Alliance: {total} statements for {yrs}")
        except Exception as e:
            import traceback
            log(f"Fatal error: {e}")
            log(traceback.format_exc()[:600])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run,
                     args=(job_id, username, password, years, entity_id, entity_slug, cookies),
                     daemon=True, name=f"usalliance-{job_id}").start()
    return jsonify({"status": "started", "job_id": job_id})


# ── Local filesystem ─────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/local/scan", methods=["POST"])
@login_required
def api_import_local_scan():
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Directory not found: {path}"}), 400
    try:
        from app.importers.local_fs import scan_directory, detect_entity_from_path
        files = scan_directory(path, recursive=True)
        counts = {"pdf": 0, "csv": 0, "ofx": 0}
        for fi in files:
            ext = fi["ext"]
            if ext == ".pdf":
                counts["pdf"] += 1
            elif ext == ".csv":
                counts["csv"] += 1
            elif ext in (".ofx", ".qfx", ".qbo"):
                counts["ofx"] += 1
        entities = db.get_entities()
        suggested = detect_entity_from_path(path, entities)
        return jsonify({
            "path": path, "total": len(files), "counts": counts,
            "suggested_entity": {"id": suggested["id"], "name": suggested["name"]}
                                 if suggested else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/import/local/run", methods=["POST"])
@login_required
def api_import_local_run():
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    entity_id = data.get("entity_id") or None
    year = data.get("year", "") or None
    if not path:
        return jsonify({"error": "path required"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Directory not found: {path}"}), 400
    job_id = db.create_import_job("local_fs", entity_id=entity_id,
                                  config_json=json.dumps({"path": path, "year": year}))
    entities_list = [dict(e) for e in db.list_entities()]

    def _run(jid, fpath, eid, yr, cpath, ents):
        def log(msg):
            append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        log(f"Scanning: {fpath}")
        log(f"Consume path: {cpath}")
        try:
            from app.importers.local_fs import import_directory, scan_directory
            all_files = scan_directory(fpath, recursive=True)
            log(f"Found {len(all_files)} files")
            if not cpath or not os.path.isdir(cpath):
                log(f"ERROR: consume path not accessible: {cpath}")
            result = import_directory(fpath, entity_id=eid, default_year=yr,
                                       consume_path=cpath, recursive=True, entities=ents)
            total_txns = 0
            for t in result.get("transactions", []):
                try:
                    db.add_transaction(t)
                    total_txns += 1
                except Exception:
                    pass
            pdfs = result.get("pdfs_queued", 0)
            errors = result.get("errors", [])
            entity_counts = result.get("entity_counts", {})
            if entity_counts:
                log("Entity breakdown: " + ", ".join(
                    f"{slug}: {cnt}" for slug, cnt in sorted(entity_counts.items())))
            for err in errors[:20]:
                log(f"  ERROR: {err}")
            if len(errors) > 20:
                log(f"  ... and {len(errors)-20} more errors")
            log(f"Done: {pdfs} PDFs queued, {total_txns} transactions imported"
                + (f" | {len(errors)} errors" if errors else ""))
            db.update_import_job(jid, status="completed",
                                 count_imported=total_txns + pdfs,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"Local FS: {pdfs} PDFs, {total_txns} txns from {fpath}")
        except Exception as e:
            log(f"FATAL ERROR: {e}")
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run,
                     args=(job_id, path, entity_id, year, CONSUME_PATH, entities_list),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})

