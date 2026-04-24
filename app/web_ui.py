"""
Tax AI Analyzer — Flask application factory.
Registers all Blueprints from app.routes and wires up auth, DB bootstrap,
context processors, error handlers, and the health endpoint.
"""
import logging
import os
from datetime import datetime

from flask import Flask, jsonify, redirect, request
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

from app import auth, db
from app.config import (
    URL_PREFIX, WEB_PORT, get_flask_secret_key, EXPORT_PATH,
)
from app.routes.helpers import _url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static",
            static_url_path=URL_PREFIX + "/static")
app.secret_key = get_flask_secret_key()
app.config["APPLICATION_ROOT"] = URL_PREFIX
app.config["PREFERRED_URL_SCHEME"] = "https"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # HIGH-1 mitigation: blocks cross-site CSRF via form posts
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

# ProxyFix is only safe when there IS a trusted reverse proxy appending
# X-Forwarded-For; in direct access (dev, LAN, curl) any client can spoof
# XFF and fool ProxyFix(x_for=1) into treating the spoofed value as the real
# remote_addr — this is the CRIT-PASS2-1 attack. Gate the XFF-trusting fields
# behind TRUST_PROXY_HEADERS=1. Always keep proto/host/port so url_for()
# generates correct https:// URLs behind nginx.
_trust_xff = os.environ.get("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1 if _trust_xff else 0,
    x_proto=1, x_host=1, x_port=1,
)


# MED-4: security headers
@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # Conservative CSP — allow same-origin assets + inline styles/scripts already used extensively
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "frame-ancestors 'self'",
    )
    return resp

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def _user_loader(user_id: str):
    return auth.load_user(user_id)


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

with app.app_context():
    try:
        db.init_db()
        db.ensure_default_data()
        _stuck = db.get_connection()
        try:
            _stuck.execute(
                "UPDATE import_jobs SET status='interrupted', completed_at=? "
                "WHERE status='running'",
                (datetime.utcnow().isoformat(),)
            )
            _stuck.commit()
        finally:
            _stuck.close()
    except Exception as _boot_err:
        logger.warning("DB bootstrap deferred: %s", _boot_err)

# ---------------------------------------------------------------------------
# Context processor
# ---------------------------------------------------------------------------

@app.context_processor
def _inject():
    entities = []
    try:
        from app.routes.helpers import _row_list
        entities = _row_list(db.list_entities())
    except Exception:
        pass
    from app.config import TAX_YEARS, PAPERLESS_WEB_URL
    from flask_login import current_user
    return {
        "url_prefix": URL_PREFIX,
        "entities": entities,
        "tax_years": TAX_YEARS,
        "paperless_web_url": PAPERLESS_WEB_URL,
        "app_name": "Financial AI Analyzer",
        "current_user": current_user,
    }


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------

from app.routes import register_blueprints
register_blueprints(app)

# ---------------------------------------------------------------------------
# Health endpoint (no auth needed)
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/health")
def health():
    return jsonify({"status": "ok", "service": "tax-ai-analyzer"})


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def err_404(e):
    if request.path.startswith(URL_PREFIX + "/api/"):
        return jsonify({"error": "not found"}), 404
    return redirect(_url("/"))


@app.errorhandler(500)
def err_500(e):
    logger.error("500: %s", e)
    if request.path.startswith(URL_PREFIX + "/api/"):
        return jsonify({"error": "internal server error"}), 500
    return redirect(_url("/"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(EXPORT_PATH, exist_ok=True)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)
