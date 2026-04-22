"""PayPal API and CSV import routes."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import setup_chat_stream

logger = logging.getLogger(__name__)

bp = Blueprint("import_paypal", __name__)

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
    gen = setup_chat_stream(PAYPAL_SETUP_SYSTEM_PROMPT, data.get("history", []), user_message)
    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
