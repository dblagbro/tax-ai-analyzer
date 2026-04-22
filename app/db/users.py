"""User account CRUD and authentication."""
import hashlib
import secrets
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False


def create_user(username: str, password: str, email: str = "", role: str = "standard") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, email, password_hash, role) VALUES(?,?,?,?)",
            (username, email or None, _hash_password(password), role),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_by_username(username: str):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def authenticate_user(username: str, password: str):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
        if row and _verify_password(password, row["password_hash"]):
            conn.execute(
                "UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],)
            )
            conn.commit()
            return row
        return None
    finally:
        conn.close()


def list_users():
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id,username,email,role,created_at,last_login,active "
            "FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()


def update_user(user_id: int, **kwargs):
    conn = get_connection()
    try:
        allowed = {"email", "role", "active", "username"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if "password" in kwargs:
            fields["password_hash"] = _hash_password(kwargs["password"])
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id))
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id: int):
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def user_count() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    finally:
        conn.close()
