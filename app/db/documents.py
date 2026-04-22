"""Analyzed documents, filed tax returns, and duplicate/hash detection."""
import logging

from app.db.core import get_connection

logger = logging.getLogger(__name__)


# ── Analyzed documents ────────────────────────────────────────────────────────

def mark_document_analyzed(
    paperless_doc_id: int,
    entity_id: int,
    tax_year: str,
    doc_type: str,
    category: str,
    vendor: str,
    amount: float,
    date: str,
    confidence: float,
    extracted_json: str = "{}",
    title: str = None,
    is_duplicate: int = 0,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO analyzed_documents
                (paperless_doc_id,entity_id,tax_year,title,doc_type,category,vendor,
                 amount,date,confidence,extracted_json,is_duplicate)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(paperless_doc_id) DO UPDATE SET
                entity_id=excluded.entity_id,
                tax_year=excluded.tax_year,
                title=excluded.title,
                doc_type=excluded.doc_type,
                category=excluded.category,
                vendor=excluded.vendor,
                amount=excluded.amount,
                date=excluded.date,
                confidence=excluded.confidence,
                extracted_json=excluded.extracted_json,
                is_duplicate=excluded.is_duplicate,
                analyzed_at=datetime('now')
            """,
            (
                paperless_doc_id, entity_id, tax_year, title, doc_type, category,
                vendor, amount, date, confidence, extracted_json, is_duplicate,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_analyzed_doc_ids() -> set:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT paperless_doc_id FROM analyzed_documents").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def get_analyzed_documents(
    entity_id: int = None,
    tax_year: str = None,
    category: str = None,
    include_duplicates: bool = False,
    limit: int = 500,
):
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("d.entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("d.tax_year=?")
            params.append(tax_year)
        if category:
            where.append("d.category=?")
            params.append(category)
        if not include_duplicates:
            where.append("(d.is_duplicate IS NULL OR d.is_duplicate=0)")
        w = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT d.*, e.name as entity_name, e.slug as entity_slug, e.color as entity_color
            FROM analyzed_documents d
            LEFT JOIN entities e ON e.id=d.entity_id
            {w} ORDER BY d.date DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_financial_summary(entity_id: int = None, tax_year: str = None) -> dict:
    """Income/expense/deduction totals with dedup logic to avoid inflated counts."""
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("tax_year=?")
            params.append(tax_year)
        where.append(
            "doc_type NOT IN ('credit_card_statement','bank_statement','mortgage_statement')"
        )
        w = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT category, COUNT(*) as count, COALESCE(SUM(amount),0) as total
            FROM (
                SELECT category,
                       MAX(amount) as amount
                FROM analyzed_documents {w}
                  AND amount IS NOT NULL AND amount > 0
                GROUP BY
                    COALESCE(vendor,''),
                    amount,
                    strftime('%Y-%m', COALESCE(date,'2000-01')),
                    category
            ) deduped
            GROUP BY category
            """,
            params,
        ).fetchall()
        result = {
            "income": 0.0,
            "expense": 0.0,
            "deduction": 0.0,
            "asset": 0.0,
            "other": 0.0,
            "counts": {},
        }
        for r in rows:
            cat = r["category"] or "other"
            result[cat] = result.get(cat, 0.0) + (r["total"] or 0.0)
            result["counts"][cat] = r["count"]
        result["net"] = result["income"] - result["expense"] - result["deduction"]
        return result
    finally:
        conn.close()


def get_years_with_docs() -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT tax_year, COUNT(*) as doc_count,
                   COALESCE(SUM(CASE WHEN category='income' THEN amount ELSE 0 END), 0) as income,
                   COALESCE(SUM(CASE WHEN category='expense' THEN amount ELSE 0 END), 0) as expense
            FROM analyzed_documents
            WHERE tax_year IS NOT NULL AND tax_year != ''
            GROUP BY tax_year
            ORDER BY tax_year DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_paperless_tags_applied(paperless_doc_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE analyzed_documents SET paperless_tags_applied=1 WHERE paperless_doc_id=?",
            (paperless_doc_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── Filed tax returns ─────────────────────────────────────────────────────────

def upsert_filed_return(entity_id: int, tax_year: str, **kwargs) -> dict:
    conn = get_connection()
    try:
        fields = [
            "filing_status", "agi", "wages_income", "business_income", "other_income",
            "total_income", "total_deductions", "taxable_income", "total_tax",
            "refund_amount", "amount_owed", "preparer_name", "preparer_firm",
            "filed_date", "notes", "form_data_json",
        ]
        updates = {k: kwargs[k] for k in fields if k in kwargs}
        existing = conn.execute(
            "SELECT id FROM filed_tax_returns WHERE entity_id=? AND tax_year=?",
            (entity_id, tax_year)
        ).fetchone()
        if existing:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            set_clause += ", updated_at=datetime('now')"
            conn.execute(
                f"UPDATE filed_tax_returns SET {set_clause} WHERE entity_id=? AND tax_year=?",
                list(updates.values()) + [entity_id, tax_year]
            )
        else:
            cols = ["entity_id", "tax_year"] + list(updates.keys())
            vals = [entity_id, tax_year] + list(updates.values())
            placeholders = ",".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO filed_tax_returns ({','.join(cols)}) VALUES ({placeholders})",
                vals
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM filed_tax_returns WHERE entity_id=? AND tax_year=?",
            (entity_id, tax_year)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def list_filed_returns(entity_id: int = None) -> list:
    conn = get_connection()
    try:
        if entity_id:
            rows = conn.execute(
                "SELECT f.*, e.name as entity_name FROM filed_tax_returns f "
                "LEFT JOIN entities e ON e.id=f.entity_id "
                "WHERE f.entity_id=? ORDER BY f.tax_year DESC",
                (entity_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT f.*, e.name as entity_name FROM filed_tax_returns f "
                "LEFT JOIN entities e ON e.id=f.entity_id "
                "ORDER BY f.tax_year DESC, e.name"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Duplicate detection ───────────────────────────────────────────────────────

def find_duplicate_analyzed_docs() -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT vendor, amount, date, doc_type,
                   COUNT(*) as n,
                   GROUP_CONCAT(id ORDER BY confidence DESC, id ASC) as id_list
            FROM analyzed_documents
            WHERE vendor != '' AND amount IS NOT NULL AND amount > 0 AND date IS NOT NULL
            GROUP BY vendor, amount, date, doc_type
            HAVING n > 1
        """).fetchall()
        results = []
        for r in rows:
            ids = [int(i) for i in r["id_list"].split(",")]
            results.append({
                "canonical_id": ids[0],
                "duplicate_ids": ids[1:],
                "vendor": r["vendor"],
                "amount": r["amount"],
                "date": r["date"],
                "doc_type": r["doc_type"],
                "count": r["n"],
            })
        return results
    finally:
        conn.close()


def flag_duplicate_analyzed_docs() -> dict:
    conn = get_connection()
    try:
        groups = find_duplicate_analyzed_docs()
        flagged = 0
        already = 0
        for g in groups:
            for dup_id in g["duplicate_ids"]:
                row = conn.execute(
                    "SELECT is_duplicate FROM analyzed_documents WHERE id=?", (dup_id,)
                ).fetchone()
                if row and row["is_duplicate"]:
                    already += 1
                else:
                    conn.execute(
                        "UPDATE analyzed_documents SET is_duplicate=1 WHERE id=?", (dup_id,)
                    )
                    flagged += 1
        canonical_ids = [g["canonical_id"] for g in groups]
        if canonical_ids:
            conn.execute(
                f"UPDATE analyzed_documents SET is_duplicate=0 WHERE id IN "
                f"({','.join('?' for _ in canonical_ids)})",
                canonical_ids,
            )
        conn.commit()
        return {"flagged": flagged, "already_flagged": already, "groups": len(groups)}
    finally:
        conn.close()


def is_near_duplicate_analyzed_doc(
    vendor: str, amount: float, date: str, doc_type: str, paperless_doc_id: int
) -> bool:
    if not vendor or not amount or not date:
        return False
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT 1 FROM analyzed_documents
               WHERE vendor=? AND ABS(amount - ?) < 0.01
                 AND date=? AND doc_type=?
                 AND paperless_doc_id != ?
                 AND (is_duplicate IS NULL OR is_duplicate=0)
               LIMIT 1""",
            (vendor, float(amount), date, doc_type, paperless_doc_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── PDF content-hash store ────────────────────────────────────────────────────

def pdf_hash_exists(sha256: str) -> bool:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM pdf_content_hashes WHERE sha256=? LIMIT 1", (sha256,)
        ).fetchone() is not None
    finally:
        conn.close()


def record_pdf_hash(sha256: str, source: str = "", filename: str = "",
                    entity_slug: str = "", year: str = "") -> bool:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pdf_content_hashes(sha256,source,filename,entity_slug,year) "
            "VALUES(?,?,?,?,?)",
            (sha256, source, filename[:255] if filename else "", entity_slug, year),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def pdf_hash_stats() -> dict:
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM pdf_content_hashes").fetchone()[0]
        rows = conn.execute(
            "SELECT source, COUNT(*) as n FROM pdf_content_hashes GROUP BY source"
        ).fetchall()
        return {"total": total, "by_source": {r["source"]: r["n"] for r in rows}}
    finally:
        conn.close()
