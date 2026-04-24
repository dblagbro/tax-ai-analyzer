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
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
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
            return redirect(request.args.get("next") or _url("/"))
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
