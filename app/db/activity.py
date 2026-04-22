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


def ensure_default_data():
    from app.config import DEFAULT_ENTITIES, DEFAULT_TAX_YEARS
    from app.db.users import user_count, create_user
    from app.db.entities import get_entity, create_entity, ensure_tax_year, update_entity

    if user_count() == 0:
        create_user("admin", "admin", "admin@localhost", "admin")
        logger.info("Created default admin user (password: admin) — CHANGE THIS IMMEDIATELY")

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
