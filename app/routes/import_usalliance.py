"""US Alliance FCU import routes."""
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX, CONSUME_PATH
from app.routes._state import _job_logs, append_job_log

logger = logging.getLogger(__name__)

bp = Blueprint("import_usalliance", __name__)


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
                return jsonify({"status": "ok",
                                "message": "Login page reached. Run a full import to authenticate."})
            elif "dashboard" in current_url or "account" in current_url:
                return jsonify({"status": "ok",
                                "message": "Already authenticated (session active)."})
            else:
                return jsonify({"status": "ok",
                                "message": f"Portal reached at {current_url}."})
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
