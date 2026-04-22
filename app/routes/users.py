"""User admin: list, create, update, delete, reset password."""
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import auth, db
from app.config import URL_PREFIX
from app.routes.helpers import admin_required

bp = Blueprint("users", __name__)


@bp.route(URL_PREFIX + "/api/users", methods=["GET"])
@login_required
@admin_required
def api_users_list():
    return jsonify([u.to_dict() for u in auth.list_users()])


@bp.route(URL_PREFIX + "/api/users", methods=["POST"])
@login_required
@admin_required
def api_users_create():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    try:
        role = "admin" if data.get("is_admin") else "standard"
        uid = auth.create_user(username=username, password=password,
                               email=data.get("email", ""), role=role)
        db.log_activity("user_created", f"Username: {username}", user_id=current_user.id)
        return jsonify({"id": uid, "username": username, "role": role}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route(URL_PREFIX + "/api/users/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def api_users_update(user_id):
    data = request.get_json() or {}
    if user_id == current_user.id:
        data.pop("role", None)
    data.pop("password", None)
    auth.update_user(user_id, **data)
    db.log_activity("user_updated", f"User ID: {user_id}", user_id=current_user.id)
    return jsonify({"status": "updated", "id": user_id})


@bp.route(URL_PREFIX + "/api/users/<int:user_id>", methods=["DELETE"])
@login_required
@admin_required
def api_users_delete(user_id):
    if user_id == current_user.id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    auth.delete_user(user_id)
    db.log_activity("user_deleted", f"User ID: {user_id}", user_id=current_user.id)
    return jsonify({"status": "deleted"})


@bp.route(URL_PREFIX + "/api/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def api_users_reset_password(user_id):
    data = request.get_json() or {}
    new_pw = data.get("password", "").strip()
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    auth.update_user(user_id, password=new_pw)
    db.log_activity("password_reset", f"User ID: {user_id}", user_id=current_user.id)
    return jsonify({"status": "ok"})


@bp.route(URL_PREFIX + "/api/users/<int:user_id>/entity-access", methods=["GET"])
@login_required
@admin_required
def api_user_entity_access_list(user_id):
    return jsonify(db.get_user_entity_access(user_id))


@bp.route(URL_PREFIX + "/api/users/<int:user_id>/entity-access", methods=["POST"])
@login_required
@admin_required
def api_user_entity_access_grant(user_id):
    data = request.get_json() or {}
    entity_id = data.get("entity_id")
    level = data.get("access_level", "read")
    if not entity_id:
        return jsonify({"error": "entity_id required"}), 400
    db.set_user_entity_access(user_id, entity_id, level, current_user.id)
    return jsonify({"status": "granted"})


@bp.route(URL_PREFIX + "/api/users/<int:user_id>/entity-access/<int:entity_id>",
           methods=["DELETE"])
@login_required
@admin_required
def api_user_entity_access_revoke(user_id, entity_id):
    db.revoke_user_entity_access(user_id, entity_id)
    return jsonify({"status": "revoked"})
