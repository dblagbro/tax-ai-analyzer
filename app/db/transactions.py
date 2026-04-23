"""Transaction CRUD and summary."""
import json
import secrets
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


def upsert_transaction(
    source: str,
    source_id: str,
    entity_id: int,
    tax_year: str,
    date: str,
    amount: float,
    vendor: str,
    description: str,
    category: str = "",
    doc_type: str = "",
    pdf_path: str = "",
    metadata_json: str = "{}",
) -> int:
    from app.dedup import normalize_vendor
    vendor_normalized = normalize_vendor(vendor or "")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM transactions WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO transactions
                (source,source_id,entity_id,tax_year,date,amount,vendor,description,
                 category,doc_type,pdf_path,metadata_json,vendor_normalized)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source, source_id, entity_id, tax_year, date, amount, vendor,
                description, category, doc_type, pdf_path, metadata_json,
                vendor_normalized,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_transactions(
    entity_id: int = None,
    tax_year: str = None,
    source: str = None,
    limit: int = 500,
):
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("t.entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("t.tax_year=?")
            params.append(tax_year)
        if source:
            where.append("t.source=?")
            params.append(source)
        w = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT t.*, e.name as entity_name, e.color as entity_color,
                   COUNT(tl.id) as link_count
            FROM transactions t
            LEFT JOIN entities e ON e.id=t.entity_id
            LEFT JOIN transaction_links tl ON tl.txn_id=t.id
            {w} GROUP BY t.id ORDER BY t.date DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_transaction(transaction_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT t.*, e.name as entity_name FROM transactions t "
            "LEFT JOIN entities e ON e.id=t.entity_id WHERE t.id=?",
            (transaction_id,),
        ).fetchone()
    finally:
        conn.close()


def update_transaction(transaction_id: int, **kwargs):
    conn = get_connection()
    try:
        allowed = {
            "entity_id", "tax_year", "date", "amount", "vendor", "description",
            "category", "doc_type", "pdf_path", "paperless_doc_id", "metadata_json",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE transactions SET {sets} WHERE id=?", (*fields.values(), transaction_id)
        )
        conn.commit()
    finally:
        conn.close()


def update_many_transactions(ids: list[int], **changes) -> int:
    """Apply the same field changes to many transactions at once.

    Returns the number of rows actually updated. Only fields in the allowed
    whitelist are written; unknown keys are silently dropped.
    """
    if not ids:
        return 0
    allowed = {
        "entity_id", "tax_year", "vendor", "description",
        "category", "doc_type", "pdf_path", "paperless_doc_id", "metadata_json",
    }
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return 0

    # sanitize ids to plain ints to avoid SQL injection via str ids
    safe_ids = [int(i) for i in ids]
    placeholders = ",".join("?" for _ in safe_ids)
    sets = ", ".join(f"{k}=?" for k in fields)

    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE transactions SET {sets} WHERE id IN ({placeholders})",
            (*fields.values(), *safe_ids),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_many_transactions(ids: list[int]) -> int:
    """Delete many transactions (and their cross-source links). Returns rowcount."""
    if not ids:
        return 0
    safe_ids = [int(i) for i in ids]
    placeholders = ",".join("?" for _ in safe_ids)

    conn = get_connection()
    try:
        # Remove links first (schema has ON DELETE CASCADE but be explicit in case FKs off)
        conn.execute(
            f"DELETE FROM transaction_links WHERE txn_id IN ({placeholders})",
            safe_ids,
        )
        cur = conn.execute(
            f"DELETE FROM transactions WHERE id IN ({placeholders})",
            safe_ids,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_transaction_summary(entity_id: int = None, tax_year: str = None) -> dict:
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("tax_year=?")
            params.append(tax_year)
        w = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT source, COUNT(*) as count, COALESCE(SUM(amount),0) as total
            FROM transactions {w}
            GROUP BY source
            """,
            params,
        ).fetchall()
        return {r["source"]: {"count": r["count"], "total": r["total"]} for r in rows}
    finally:
        conn.close()


# ── Compatibility wrappers ────────────────────────────────────────────────────

def get_transactions(entity_id=None, year=None, source=None, category=None,
                     limit: int = 100, offset: int = 0) -> list:
    eid = int(entity_id) if entity_id else None
    rows = list_transactions(entity_id=eid, tax_year=year, source=source, limit=limit + offset)
    if category:
        rows = [r for r in rows if r.get("category") == category]
    return rows[offset:offset + limit]


def count_transactions(entity_id=None, year=None, source=None, category=None) -> int:
    conn = get_connection()
    conditions, params = [], []
    if entity_id:
        conditions.append("entity_id=?")
        params.append(int(entity_id))
    if year:
        conditions.append("tax_year=?")
        params.append(str(year))
    if source:
        conditions.append("source=?")
        params.append(source)
    if category:
        conditions.append("category=?")
        params.append(category)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    row = conn.execute(f"SELECT COUNT(*) FROM transactions {where}", params).fetchone()
    conn.close()
    return row[0] if row else 0


def add_transaction(data: dict) -> dict:
    conn = get_connection()
    try:
        entity_id = int(data.get("entity_id")) if data.get("entity_id") else None
        cur = conn.execute(
            """INSERT INTO transactions
               (source,source_id,entity_id,tax_year,date,amount,vendor,description,
                category,doc_type,pdf_path,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("source", "manual"),
                data.get("source_id") or secrets.token_hex(8),
                entity_id,
                data.get("year") or data.get("tax_year", ""),
                data.get("date", ""),
                float(data.get("amount", 0)),
                data.get("vendor") or data.get("description", "")[:50],
                data.get("description", ""),
                data.get("category", ""),
                data.get("doc_type", ""),
                data.get("pdf_path", ""),
                json.dumps(data.get("raw") or data.get("metadata") or {}),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()
