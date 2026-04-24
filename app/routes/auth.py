"""Authentication routes: login, logout."""
import time
from collections import defaultdict, deque
from threading import Lock

from flask import Blueprint, flash, redirect, render_template, request
from flask_login import current_user, login_required, login_user, logout_user

from app import auth, db
from app.config import URL_PREFIX
from app.routes.helpers import _url

bp = Blueprint("auth", __name__)

# MED-5: simple in-memory rate limiter — 10 failed attempts per IP per 5 min.
_LOGIN_WINDOW_SEC = 300
_LOGIN_MAX_FAILS = 10
_login_fail_log: "dict[str, deque[float]]" = defaultdict(deque)
_login_lock = Lock()

def _client_ip() -> str:
    """Return the real client IP for rate-limiting keys.

    CRIT-PASS2-1: a prior version read X-Forwarded-For directly from headers
    and trusted whatever the client supplied — any attacker could rotate
    X-Forwarded-For per request to reset the rate-limiter counter. The fix is
    to rely on Werkzeug's ProxyFix (configured in app/web_ui.py with x_for=1)
    which validates the XFF chain for a known number of trusted proxy hops
    and populates request.remote_addr with the rightmost (proxy-appended)
    entry. We then trust request.remote_addr only.

    For direct (non-proxied) access, request.remote_addr is the raw TCP peer
    address — also attacker-visible but not spoofable without source-IP
    spoofing (very different threat model).
    """
    return request.remote_addr or ""


def _safe_next(next_url: str) -> str | None:
    """Validate a post-login `?next=` redirect target.

    Only accept values that are same-origin paths (start with `/` and not
    `//`, which would be protocol-relative). Prevents open-redirect vectors
    like `?next=https://evil.com/`.
    """
    if not next_url:
        return None
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return None


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        q = _login_fail_log[ip]
        while q and q[0] < now - _LOGIN_WINDOW_SEC:
            q.popleft()
        return len(q) >= _LOGIN_MAX_FAILS


def _record_fail(ip: str) -> None:
    with _login_lock:
        _login_fail_log[ip].append(time.time())


def _clear_fails(ip: str) -> None:
    with _login_lock:
        _login_fail_log.pop(ip, None)


@bp.route(URL_PREFIX + "/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_url("/"))
    error = None
    client_ip = _client_ip()
    if request.method == "POST":
        if _rate_limited(client_ip):
            error = "Too many failed attempts. Wait 5 minutes and try again."
            return render_template("login.html", error=error, url_prefix=URL_PREFIX), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.authenticate(username, password)
        if user:
            _clear_fails(client_ip)
            login_user(user, remember=request.form.get("remember") == "on")
            db.log_activity("login", f"User '{username}' logged in", user_id=user.id)
            nxt = _safe_next(request.args.get("next"))
            return redirect(nxt or _url("/"))
        _record_fail(client_ip)
        error = "Invalid username or password."
    return render_template("login.html", error=error, url_prefix=URL_PREFIX)


@bp.route(URL_PREFIX + "/logout")
@login_required
def logout():
    db.log_activity("logout", f"User '{current_user.username}' logged out",
                    user_id=current_user.id)
    logout_user()
    return redirect(_url("/login"))
