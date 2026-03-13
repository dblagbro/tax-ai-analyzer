"""Flask-Login user model and loader backed by the SQLite database."""
from flask_login import UserMixin
from app import db


class User(UserMixin):
    """Wraps a sqlite3.Row from the users table."""

    def __init__(self, row):
        self._row = row

    # ── Flask-Login required ──────────────────────────────────────────────────

    def get_id(self) -> str:
        return str(self._row["id"])

    @property
    def is_active(self) -> bool:
        return bool(self._row["active"])

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def id(self) -> int:
        return self._row["id"]

    @property
    def username(self) -> str:
        return self._row["username"]

    @property
    def email(self) -> str:
        return self._row["email"] or ""

    @property
    def role(self) -> str:
        return self._row["role"]

    @property
    def is_admin(self) -> bool:
        return self._row["role"] in ("admin", "superuser")

    @property
    def is_superuser(self) -> bool:
        return self._row["role"] == "superuser"

    @property
    def display_name(self) -> str:
        """Friendly display name — falls back to username."""
        return self._row["username"]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "is_admin": self.is_admin,
            "is_superuser": self.is_superuser,
            "active": self.is_active,
        }


# ── Flask-Login loader ────────────────────────────────────────────────────────

def load_user(user_id: str):
    """Called by Flask-Login to reload a user from the session cookie."""
    try:
        row = db.get_user_by_id(int(user_id))
        if row:
            return User(row)
    except (ValueError, TypeError):
        pass
    return None


# ── Convenience wrappers ──────────────────────────────────────────────────────

def authenticate(username: str, password: str):
    """Return a User if credentials are valid, else None."""
    row = db.authenticate_user(username, password)
    return User(row) if row else None


def get_user_by_username(username: str):
    row = db.get_user_by_username(username)
    return User(row) if row else None


def get_user_by_id(user_id: int):
    row = db.get_user_by_id(user_id)
    return User(row) if row else None


def list_users():
    """Return list of User objects for all active users."""
    return [User(row) for row in db.list_users()]


def create_user(username: str, password: str, email: str = "", role: str = "standard") -> int:
    """Create a new user and return the new row ID."""
    return db.create_user(username, password, email, role)


def update_user(user_id: int, **kwargs):
    """Update mutable user fields. Pass password=... to change password."""
    db.update_user(user_id, **kwargs)


def delete_user(user_id: int):
    """Soft-delete (deactivate) a user."""
    db.delete_user(user_id)
