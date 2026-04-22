"""Runtime key-value settings store."""
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_settings() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def delete_setting(key: str):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()


# Compatibility aliases
def get_settings() -> dict:
    return get_all_settings()


def save_settings(updates: dict):
    for key, value in updates.items():
        set_setting(key, str(value) if value is not None else "")
