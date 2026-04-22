"""Import jobs, credentials, URL pollers, and Gmail dedup tracking."""
import json
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


# ── Import jobs ───────────────────────────────────────────────────────────────

def create_import_job(
    source_type: str,
    entity_id=None,
    params: dict = None,
    config_json: str = None,
) -> int:
    eid = int(entity_id) if entity_id else None
    cfg = config_json or json.dumps(params or {})
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO import_jobs(source_type,entity_id,status,config_json) VALUES(?,?,?,?)",
            (source_type, eid, "pending", cfg),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_import_jobs(source_type: str = None, limit: int = 50):
    conn = get_connection()
    try:
        where = "WHERE source_type=?" if source_type else ""
        params = [source_type] if source_type else []
        return conn.execute(
            f"SELECT j.*, e.name as entity_name FROM import_jobs j "
            f"LEFT JOIN entities e ON e.id=j.entity_id "
            f"{where} ORDER BY j.id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()


def get_import_jobs(source_type=None, limit: int = 50) -> list:
    return [dict(r) for r in list_import_jobs(source_type=source_type, limit=limit)]


def update_import_job(job_id: int, **kwargs):
    mapping = {"message": "error_msg", "progress": "count_imported"}
    conn = get_connection()
    try:
        allowed = {
            "status", "started_at", "completed_at",
            "count_imported", "count_skipped", "error_msg",
        }
        fields = {}
        for k, v in kwargs.items():
            key = mapping.get(k, k)
            if key in allowed:
                fields[key] = v
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE import_jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        conn.commit()
    finally:
        conn.close()


def get_import_job(job_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT j.*, e.name as entity_name FROM import_jobs j "
            "LEFT JOIN entities e ON e.id=j.entity_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_import_job(job_id: int) -> bool:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM import_job_logs WHERE job_id=?", (job_id,))
        cur = conn.execute("DELETE FROM import_jobs WHERE id=?", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def append_import_job_log(job_id: int, line: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO import_job_logs (job_id, line) VALUES (?, ?)",
            (job_id, line),
        )
        conn.commit()
    finally:
        conn.close()


def get_import_job_logs(job_id: int, offset: int = 0) -> tuple:
    conn = get_connection()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM import_job_logs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT line FROM import_job_logs WHERE job_id=? ORDER BY id LIMIT -1 OFFSET ?",
            (job_id, offset),
        ).fetchall()
        return [r[0] for r in rows], total
    finally:
        conn.close()


# ── Credentials ───────────────────────────────────────────────────────────────

def save_credential(
    source_type: str,
    name: str,
    credentials_json: str,
    entity_id: int = None,
) -> int:
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM importer_credentials WHERE source_type=? AND name=?",
            (source_type, name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE importer_credentials "
                "SET credentials_json=?,last_used=datetime('now') WHERE id=?",
                (credentials_json, existing["id"]),
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO importer_credentials(source_type,name,credentials_json,entity_id) "
            "VALUES(?,?,?,?)",
            (source_type, name, credentials_json, entity_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_credential(source_type: str, name: str = None):
    conn = get_connection()
    try:
        if name:
            return conn.execute(
                "SELECT * FROM importer_credentials "
                "WHERE source_type=? AND name=? AND active=1",
                (source_type, name),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM importer_credentials "
            "WHERE source_type=? AND active=1 ORDER BY id LIMIT 1",
            (source_type,),
        ).fetchone()
    finally:
        conn.close()


def list_credentials(source_type: str = None):
    conn = get_connection()
    try:
        if source_type:
            return conn.execute(
                "SELECT id,source_type,name,entity_id,created_at,last_used,active "
                "FROM importer_credentials WHERE source_type=? ORDER BY name",
                (source_type,),
            ).fetchall()
        return conn.execute(
            "SELECT id,source_type,name,entity_id,created_at,last_used,active "
            "FROM importer_credentials ORDER BY source_type,name"
        ).fetchall()
    finally:
        conn.close()


def delete_credential(credential_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE importer_credentials SET active=0 WHERE id=?", (credential_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ── Gmail dedup ───────────────────────────────────────────────────────────────

def is_gmail_message_processed(message_id: str) -> bool:
    if not message_id:
        return False
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM gmail_processed_messages WHERE message_id=? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_gmail_message(
    message_id: str,
    gmail_id: str,
    status: str,
    entity_slug: str = "",
    year: str = "",
    subject: str = "",
    sender: str = "",
):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gmail_processed_messages"
            "(message_id, gmail_id, status, entity_slug, year, subject, sender)"
            " VALUES (?,?,?,?,?,?,?)",
            (message_id, gmail_id, status, entity_slug, year,
             subject[:200] if subject else "", sender[:200] if sender else ""),
        )
        conn.commit()
    finally:
        conn.close()


def gmail_processed_stats() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM gmail_processed_messages GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


# ── URL pollers ───────────────────────────────────────────────────────────────

def create_url_poller(
    name: str,
    url: str,
    entity_id: int = None,
    tax_year: str = None,
    auth_type: str = "none",
    auth_config_json: str = "{}",
    poll_interval_hours: int = 24,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO url_pollers(name,url,entity_id,tax_year,auth_type,"
            "auth_config_json,poll_interval_hours) VALUES(?,?,?,?,?,?,?)",
            (name, url, entity_id, tax_year, auth_type, auth_config_json, poll_interval_hours),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_url_pollers(active_only: bool = True):
    conn = get_connection()
    try:
        where = "WHERE p.active=1" if active_only else ""
        return conn.execute(
            f"SELECT p.*, e.name as entity_name FROM url_pollers p "
            f"LEFT JOIN entities e ON e.id=p.entity_id {where} ORDER BY p.name"
        ).fetchall()
    finally:
        conn.close()


def update_url_poller_poll(poller_id: int, checksum: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE url_pollers SET last_polled=datetime('now'),last_checksum=? WHERE id=?",
            (checksum, poller_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_url_poller(poller_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM url_pollers WHERE id=?", (poller_id,))
        conn.commit()
    finally:
        conn.close()
