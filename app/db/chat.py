"""Chat sessions, messages, and sharing."""
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


def create_chat_session(
    user_id: int,
    entity_id=None,
    year: str = None,
    tax_year: str = None,
    title: str = "New Chat",
) -> dict:
    ty = year or tax_year
    eid = int(entity_id) if entity_id else None
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO chat_sessions(user_id,entity_id,tax_year,title) VALUES(?,?,?,?)",
            (user_id, eid, ty, title),
        )
        conn.commit()
        row = conn.execute(
            "SELECT s.*, e.name as entity_name FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id WHERE s.id=?",
            (cur.lastrowid,),
        ).fetchone()
        d = dict(row) if row else {}
        d["messages"] = []
        return d
    finally:
        conn.close()


def get_chat_session(session_id) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT s.*, e.name as entity_name FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id WHERE s.id=?",
            (int(session_id),),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        msgs = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at",
            (int(session_id),),
        ).fetchall()
        d["messages"] = [dict(m) for m in msgs]
        return d
    finally:
        conn.close()


def update_chat_session_title(session_id: int, title: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE chat_sessions SET title=?,updated_at=datetime('now') WHERE id=?",
            (title, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_chat_session(session_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM chat_session_shares WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def search_chat_sessions(user_id: int, query: str, is_admin: bool = False) -> list:
    conn = get_connection()
    try:
        q = f"%{query}%"
        if is_admin:
            rows = conn.execute(
                "SELECT DISTINCT s.*, e.name as entity_name FROM chat_sessions s "
                "LEFT JOIN entities e ON e.id=s.entity_id "
                "LEFT JOIN chat_messages m ON m.session_id=s.id "
                "WHERE s.deleted=0 AND (s.title LIKE ? OR m.content LIKE ?) "
                "ORDER BY s.updated_at DESC LIMIT 50",
                (q, q),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT s.*, e.name as entity_name FROM chat_sessions s "
                "LEFT JOIN entities e ON e.id=s.entity_id "
                "LEFT JOIN chat_messages m ON m.session_id=s.id "
                "LEFT JOIN chat_session_shares sh ON sh.session_id=s.id AND sh.shared_with_user_id=? "
                "WHERE s.deleted=0 AND (s.user_id=? OR sh.shared_with_user_id IS NOT NULL) "
                "AND (s.title LIKE ? OR m.content LIKE ?) "
                "ORDER BY s.updated_at DESC LIMIT 50",
                (user_id, user_id, q, q),
            ).fetchall()
        return rows
    finally:
        conn.close()


def list_chat_sessions(user_id: int, entity_id: int = None, include_shared: bool = True):
    conn = get_connection()
    try:
        base_select = (
            "SELECT DISTINCT s.*, e.name as entity_name, "
            "CASE WHEN s.user_id=? THEN 0 ELSE 1 END as is_shared "
            "FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id "
        )
        share_join = "LEFT JOIN chat_session_shares sh ON sh.session_id=s.id AND sh.shared_with_user_id=? "
        where = "WHERE s.deleted=0 AND (s.user_id=? OR sh.shared_with_user_id IS NOT NULL) "
        order = "ORDER BY s.updated_at DESC LIMIT 100"
        if entity_id is not None:
            where += "AND s.entity_id=? "
            rows = conn.execute(
                base_select + share_join + where + order,
                (user_id, user_id, user_id, entity_id),
            ).fetchall()
        else:
            rows = conn.execute(
                base_select + share_join + where + order,
                (user_id, user_id, user_id),
            ).fetchall()
        return rows
    finally:
        conn.close()


def get_chat_sessions(user_id: int, entity_id=None) -> list:
    return [dict(r) for r in list_chat_sessions(user_id=user_id, entity_id=entity_id)]


def truncate_messages_from(session_id: int, from_message_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM chat_messages WHERE session_id=? AND id>=?",
            (session_id, from_message_id),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=datetime('now') WHERE id=?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def share_chat_session(session_id: int, shared_with_user_id: int,
                       shared_by_user_id: int, can_write: bool = False):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO chat_session_shares"
            "(session_id, shared_with_user_id, shared_by_user_id, can_write) VALUES(?,?,?,?)",
            (session_id, shared_with_user_id, shared_by_user_id, 1 if can_write else 0),
        )
        conn.commit()
    finally:
        conn.close()


def unshare_chat_session(session_id: int, shared_with_user_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM chat_session_shares WHERE session_id=? AND shared_with_user_id=?",
            (session_id, shared_with_user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_chat_shares(session_id: int) -> list:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT sh.*, u.username, u.email FROM chat_session_shares sh "
            "JOIN users u ON u.id=sh.shared_with_user_id WHERE sh.session_id=?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def get_chat_messages(session_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def append_chat_message(
    session_id: int,
    role: str,
    content: str,
    model_used: str = "",
    tokens_used: int = 0,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO chat_messages(session_id,role,content,model_used,tokens_used) "
            "VALUES(?,?,?,?,?)",
            (session_id, role, content, model_used, tokens_used),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=datetime('now') WHERE id=?", (session_id,)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_chat_message(session_id, role: str, content: str):
    append_chat_message(session_id=int(session_id), role=role, content=content)
