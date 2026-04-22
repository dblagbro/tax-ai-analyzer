"""Authentication routes: login, logout."""
from flask import Blueprint, flash, redirect, render_template, request
from flask_login import current_user, login_required, login_user, logout_user

from app import auth, db
from app.config import URL_PREFIX
from app.routes.helpers import _url

bp = Blueprint("auth", __name__)


@bp.route(URL_PREFIX + "/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_url("/"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.authenticate(username, password)
        if user:
            login_user(user, remember=request.form.get("remember") == "on")
            db.log_activity("login", f"User '{username}' logged in", user_id=user.id)
            return redirect(request.args.get("next") or _url("/"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, url_prefix=URL_PREFIX)


@bp.route(URL_PREFIX + "/logout")
@login_required
def logout():
    db.log_activity("logout", f"User '{current_user.username}' logged out",
                    user_id=current_user.id)
    logout_user()
    return redirect(_url("/login"))
