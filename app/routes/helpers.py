"""Shared decorators and helper functions used across all Blueprint modules."""
from functools import wraps

from flask import jsonify, make_response, redirect, request
from flask_login import current_user

from app import db
from app.config import URL_PREFIX


def _url(path: str) -> str:
    return URL_PREFIX + path


def admin_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            if request.path.startswith(URL_PREFIX + "/api/"):
                return jsonify({"error": "Admin access required"}), 403
            from flask import flash
            flash("Admin access required.", "danger")
            return redirect(_url("/"))
        return f(*args, **kwargs)
    return _wrap


def superuser_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_superuser:
            if request.path.startswith(URL_PREFIX + "/api/"):
                return jsonify({"error": "Superuser access required"}), 403
            from flask import flash
            flash("Superuser access required.", "danger")
            return redirect(_url("/"))
        return f(*args, **kwargs)
    return _wrap


def _row_list(rows) -> list:
    return [dict(r) for r in rows] if rows else []


def _no_cache_page(html_response):
    resp = make_response(html_response)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _user_can_access_session(sess) -> bool:
    if not sess:
        return False
    if current_user.is_admin:
        return True
    if sess["user_id"] == current_user.id:
        return True
    shares = db.get_chat_shares(sess["id"])
    return any(s["shared_with_user_id"] == current_user.id for s in shares)


def _user_can_write_session(sess) -> bool:
    if not sess:
        return False
    if current_user.is_admin or sess["user_id"] == current_user.id:
        return True
    shares = db.get_chat_shares(sess["id"])
    return any(
        s["shared_with_user_id"] == current_user.id and s["can_write"]
        for s in shares
    )
