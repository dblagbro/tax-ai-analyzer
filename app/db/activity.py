"""Activity log and database bootstrap."""
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


def log_activity(
    action: str,
    detail: str = "",
    user_id=None,
    entity_id=None,
):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activity_log(action,detail,user_id,entity_id) VALUES(?,?,?,?)",
            (action, detail, user_id, entity_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_recent_activity(limit: int = 50):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT a.*, u.username, e.name as entity_name FROM activity_log a "
            "LEFT JOIN users u ON u.id=a.user_id "
            "LEFT JOIN entities e ON e.id=a.entity_id "
            "ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def get_activity_log(limit: int = 50) -> list:
    rows = get_recent_activity(limit=limit)
    return [dict(r) for r in rows]


def search_activity(
    action: str = None,
    user_id: int = None,
    entity_id: int = None,
    search: str = None,
    since: str = None,      # YYYY-MM-DD
    until: str = None,      # YYYY-MM-DD (exclusive)
    limit: int = 200,
    offset: int = 0,
) -> tuple[list, int]:
    """Filtered activity log query. Returns (rows, total_count)."""
    where: list[str] = []
    params: list = []
    if action:
        where.append("a.action = ?")
        params.append(action)
    if user_id is not None:
        where.append("a.user_id = ?")
        params.append(user_id)
    if entity_id is not None:
        where.append("a.entity_id = ?")
        params.append(entity_id)
    if search:
        where.append("(a.action LIKE ? OR a.detail LIKE ?)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    if since:
        where.append("a.created_at >= ?")
        params.append(since)
    if until:
        where.append("a.created_at < ?")
        params.append(until)
    w = "WHERE " + " AND ".join(where) if where else ""

    conn = get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM activity_log a {w}", tuple(params)
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT a.*, u.username, e.name as entity_name
                FROM activity_log a
                LEFT JOIN users u ON u.id = a.user_id
                LEFT JOIN entities e ON e.id = a.entity_id
                {w}
                ORDER BY a.created_at DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], total
    finally:
        conn.close()


def distinct_activity_actions() -> list[str]:
    """List all distinct action values (for filter dropdown)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT action, COUNT(*) n FROM activity_log GROUP BY action ORDER BY n DESC"
        ).fetchall()
        return [{"action": r["action"], "count": r["n"]} for r in rows]
    finally:
        conn.close()


def ensure_default_data():
    import os
    from app.config import DEFAULT_ENTITIES, DEFAULT_TAX_YEARS
    from app.db.users import user_count, create_user
    from app.db.entities import get_entity, create_entity, ensure_tax_year, update_entity

    if user_count() == 0:
        initial_pw = os.environ.get("ADMIN_INITIAL_PASSWORD", "").strip()
        if not initial_pw:
            raise RuntimeError(
                "No users exist and ADMIN_INITIAL_PASSWORD env var is not set. "
                "Refusing to seed default admin with a known password. "
                "Set ADMIN_INITIAL_PASSWORD to a strong value in your environment "
                "(e.g. in docker-compose.yml) and restart."
            )
        if len(initial_pw) < 12:
            raise RuntimeError(
                "ADMIN_INITIAL_PASSWORD is too short (minimum 12 chars). "
                "Refusing to seed admin."
            )
        create_user("admin", initial_pw, "admin@localhost", "admin")
        logger.warning("Created default admin user from ADMIN_INITIAL_PASSWORD env var")

    for ent in DEFAULT_ENTITIES:
        existing = get_entity(slug=ent["slug"])
        parent_id = None
        if ent.get("parent_slug"):
            parent_row = get_entity(slug=ent["parent_slug"])
            if parent_row:
                parent_id = parent_row["id"]
        if not existing:
            result = create_entity(
                name=ent["name"],
                slug=ent["slug"],
                entity_type=ent.get("type", "personal"),
                color=ent.get("color", "#1a3c5e"),
                parent_entity_id=parent_id,
                display_name=ent.get("display_name", ent["name"]),
                sort_order=ent.get("sort_order", 0),
            )
            eid = result["id"] if isinstance(result, dict) else result
            for year in DEFAULT_TAX_YEARS:
                ensure_tax_year(eid, year)
            logger.info(f"Created entity: {ent['name']}")
        elif parent_id and not existing.get("parent_entity_id"):
            update_entity(
                existing["id"],
                parent_entity_id=parent_id,
                display_name=ent.get("display_name", existing["name"]),
                sort_order=ent.get("sort_order", 0),
            )
