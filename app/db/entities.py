"""Entity, tax-year, and entity-access CRUD."""
import re
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


# ── Entities ──────────────────────────────────────────────────────────────────

def create_entity(
    name: str,
    slug: str = None,
    entity_type: str = "personal",
    description: str = "",
    tax_id: str = "",
    color: str = "#1a3c5e",
    parent_entity_id: int = None,
    display_name: str = None,
    metadata_json: str = "{}",
    sort_order: int = 0,
) -> dict:
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO entities(name,slug,type,description,tax_id,color,"
            "parent_entity_id,display_name,metadata_json,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, slug, entity_type, description, tax_id, color,
             parent_entity_id, display_name or name, metadata_json, sort_order),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM entities WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_entity(entity_id=None, slug: str = None):
    conn = get_connection()
    try:
        if entity_id is not None:
            row = conn.execute("SELECT * FROM entities WHERE id=?", (int(entity_id),)).fetchone()
        elif slug is not None:
            row = conn.execute("SELECT * FROM entities WHERE slug=?", (slug,)).fetchone()
        else:
            return None
        return dict(row) if row else None
    finally:
        conn.close()


def list_entities(include_archived: bool = False):
    conn = get_connection()
    try:
        if include_archived:
            return conn.execute("SELECT * FROM entities ORDER BY name").fetchall()
        return conn.execute(
            "SELECT * FROM entities WHERE archived=0 ORDER BY name"
        ).fetchall()
    finally:
        conn.close()


def update_entity(entity_id, **kwargs) -> dict:
    conn = get_connection()
    try:
        allowed = {
            "name", "slug", "description", "type", "tax_id", "color", "archived",
            "metadata_json", "years", "parent_entity_id", "display_name", "sort_order",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE entities SET {sets} WHERE id=?", (*fields.values(), int(entity_id)))
            conn.commit()
        row = conn.execute("SELECT * FROM entities WHERE id=?", (int(entity_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def merge_entities(source_id: int, target_id: int) -> dict:
    conn = get_connection()
    try:
        counts = {}
        for table, col in [
            ("transactions", "entity_id"),
            ("analyzed_documents", "entity_id"),
            ("import_jobs", "entity_id"),
            ("chat_sessions", "entity_id"),
            ("tax_years", "entity_id"),
            ("url_pollers", "entity_id"),
            ("importer_credentials", "entity_id"),
        ]:
            cur = conn.execute(
                f"UPDATE {table} SET {col}=? WHERE {col}=?", (target_id, source_id)
            )
            if cur.rowcount:
                counts[table] = cur.rowcount
        conn.execute(
            "UPDATE entities SET parent_entity_id=? WHERE parent_entity_id=?",
            (target_id, source_id),
        )
        conn.execute("UPDATE entities SET archived=1 WHERE id=?", (source_id,))
        conn.commit()
        return counts
    finally:
        conn.close()


def get_entity_tree() -> list:
    rows = list_entities(include_archived=False)
    by_id = {r["id"]: dict(r) | {"children": []} for r in rows}
    roots = []
    for eid, ent in by_id.items():
        pid = ent.get("parent_entity_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(ent)
        else:
            roots.append(ent)
    return roots


def archive_entity(entity_id) -> bool:
    try:
        conn = get_connection()
        conn.execute("UPDATE entities SET archived=1 WHERE id=?", (int(entity_id),))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def get_entities(include_archived: bool = False) -> list:
    return [dict(r) for r in list_entities(include_archived=include_archived)]


def get_entity_dict(entity_id=None, slug: str = None) -> dict:
    return get_entity(entity_id=entity_id, slug=slug)


# ── Tax years ─────────────────────────────────────────────────────────────────

def ensure_tax_year(entity_id: int, year: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tax_years WHERE entity_id=? AND year=?", (entity_id, year)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO tax_years(entity_id,year) VALUES(?,?)", (entity_id, year)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_tax_years(entity_id: int = None):
    conn = get_connection()
    try:
        if entity_id is not None:
            return conn.execute(
                "SELECT * FROM tax_years WHERE entity_id=? ORDER BY year DESC", (entity_id,)
            ).fetchall()
        return conn.execute(
            "SELECT DISTINCT year FROM tax_years ORDER BY year DESC"
        ).fetchall()
    finally:
        conn.close()


def update_tax_year_status(entity_id: int, year: str, status: str, notes: str = ""):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tax_years SET status=?, notes=? WHERE entity_id=? AND year=?",
            (status, notes, entity_id, year),
        )
        conn.commit()
    finally:
        conn.close()


# ── Entity access control ─────────────────────────────────────────────────────

def get_user_entity_access(user_id: int) -> list:
    conn = get_connection()
    try:
        return [r["entity_id"] for r in conn.execute(
            "SELECT entity_id FROM user_entity_access WHERE user_id=?", (user_id,)
        ).fetchall()]
    finally:
        conn.close()


def set_user_entity_access(user_id: int, entity_id: int,
                            access_level: str, granted_by: int):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO user_entity_access"
            "(user_id, entity_id, access_level, granted_by) VALUES(?,?,?,?)",
            (user_id, entity_id, access_level, granted_by),
        )
        conn.commit()
    finally:
        conn.close()


def revoke_user_entity_access(user_id: int, entity_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM user_entity_access WHERE user_id=? AND entity_id=?",
            (user_id, entity_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_entity_access(entity_id: int) -> list:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT ua.*, u.username, u.email, u.role FROM user_entity_access ua "
            "JOIN users u ON u.id=ua.user_id WHERE ua.entity_id=?",
            (entity_id,),
        ).fetchall()
    finally:
        conn.close()
