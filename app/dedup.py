"""Cross-source deduplication and transaction ↔ document matching.

Layer 3 dedup: the same real-world payment can appear as:
  - A bank debit    (transactions table,      source=usbank/chime/etc.)
  - A Gmail email   (analyzed_documents table, source=gmail)
  - An uploaded PDF (analyzed_documents table, source=upload/paperless)
  - A Verizon bill  (transactions table,       source=verizon)

This module:
  1. Normalizes vendor names for fuzzy matching
  2. Scans for cross-source matches (vendor + amount ±5% + date ±14 days)
  3. Writes links to transaction_links table
  4. Flags secondary occurrences as cross_source_duplicate
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Vendor normalization table ────────────────────────────────────────────────
# Maps regex patterns to canonical vendor names.
# Patterns are applied in order; first match wins.
_VENDOR_RULES: list[tuple[re.Pattern, str]] = [
    # Amazon
    (re.compile(r"amzn|amazon", re.I), "Amazon"),
    # Apple
    (re.compile(r"\bapple\b|apple\.com|itunes|app store", re.I), "Apple"),
    # Google
    (re.compile(r"\bgoogle\b|google\.com|google pay|google store", re.I), "Google"),
    # Verizon (VZW = Verizon Wireless abbreviation; VZN is another variant seen in-the-wild)
    (re.compile(r"verizon|\bvzw\b|\bvzn\b|vzw\*|verizon wireless", re.I), "Verizon"),
    # AT&T
    (re.compile(r"\bat&t\b|att\*|atnt", re.I), "AT&T"),
    # T-Mobile
    (re.compile(r"t-mobile|tmobile|t mobile", re.I), "T-Mobile"),
    # Comcast / Xfinity
    (re.compile(r"comcast|xfinity", re.I), "Comcast/Xfinity"),
    # PayPal
    (re.compile(r"paypal", re.I), "PayPal"),
    # Venmo
    (re.compile(r"\bvenmo\b", re.I), "Venmo"),
    # Uber / Lyft
    (re.compile(r"\buber\b", re.I), "Uber"),
    (re.compile(r"\blyft\b", re.I), "Lyft"),
    # Netflix / Hulu / Spotify / Disney
    (re.compile(r"\bnetflix\b", re.I), "Netflix"),
    (re.compile(r"\bhulu\b", re.I), "Hulu"),
    (re.compile(r"\bspotify\b", re.I), "Spotify"),
    (re.compile(r"\bdisney\b", re.I), "Disney"),
    # Walmart / Target / Costco
    (re.compile(r"\bwalmart\b|wal-mart|wal mart", re.I), "Walmart"),
    (re.compile(r"\btarget\b", re.I), "Target"),
    (re.compile(r"\bcostco\b", re.I), "Costco"),
    # Gas stations
    (re.compile(r"\bshell\b", re.I), "Shell"),
    (re.compile(r"\bchevron\b", re.I), "Chevron"),
    (re.compile(r"\bbp\b|british petroleum", re.I), "BP"),
    (re.compile(r"\bexxon\b|\bmobil\b", re.I), "Exxon/Mobil"),
    # Groceries
    (re.compile(r"whole foods|wholefoods", re.I), "Whole Foods"),
    (re.compile(r"trader joe", re.I), "Trader Joe's"),
    (re.compile(r"\bkroger\b", re.I), "Kroger"),
    # Insurance
    (re.compile(r"\bgeico\b", re.I), "GEICO"),
    (re.compile(r"\bstatefarm\b|state farm", re.I), "State Farm"),
    (re.compile(r"\ballstate\b", re.I), "Allstate"),
    # Banks / financial (for ACH references)
    (re.compile(r"\bchase\b", re.I), "Chase"),
    (re.compile(r"capital one|cap one", re.I), "Capital One"),
    (re.compile(r"\bchime\b", re.I), "Chime"),
]


def normalize_vendor(raw: str) -> str:
    """Return a canonical vendor name from a raw bank description or vendor field.

    Falls back to a cleaned version of the input if no rule matches.
    """
    if not raw:
        return ""
    raw = raw.strip()

    for pattern, canonical in _VENDOR_RULES:
        if pattern.search(raw):
            return canonical

    # Generic cleanup: strip transaction IDs, dates, card numbers
    cleaned = re.sub(r"\b\d{4,}\b", "", raw)           # long numbers
    cleaned = re.sub(r"\*\S+", "", cleaned)              # *XXXXXXXX suffixes
    cleaned = re.sub(r"#\S+", "", cleaned)               # #ref numbers
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if not cleaned:
        return raw[:60]
    # Title-case, then fix possessive apostrophes mangled by .title() ("Bob'S" → "Bob's")
    result = cleaned[:60].title()
    result = re.sub(r"([A-Za-z])'S\b", r"\1's", result)
    return result


# ── Amount matching ───────────────────────────────────────────────────────────

def _amounts_match(a: float, b: float, tolerance: float = 0.05) -> bool:
    """True if two amounts are within tolerance fraction of each other."""
    if a == 0 and b == 0:
        return True
    if a == 0 or b == 0:
        return False
    return abs(a - b) / max(abs(a), abs(b)) <= tolerance


# ── Date matching ─────────────────────────────────────────────────────────────

def _dates_close(d1: str, d2: str, window_days: int = 14) -> bool:
    """True if two ISO date strings are within window_days of each other."""
    try:
        dt1 = datetime.strptime(d1[:10], "%Y-%m-%d")
        dt2 = datetime.strptime(d2[:10], "%Y-%m-%d")
        return abs((dt1 - dt2).days) <= window_days
    except Exception:
        return False


# ── DB helpers ────────────────────────────────────────────────────────────────

def _write_link(conn, txn_id: int, doc_id: int, link_type: str, confidence: float) -> bool:
    """Insert or update a transaction_link. Returns True if new."""
    existing = conn.execute(
        "SELECT id FROM transaction_links WHERE txn_id=? AND doc_id=?",
        (txn_id, doc_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE transaction_links SET confidence=?, link_type=? WHERE id=?",
            (confidence, link_type, existing["id"]),
        )
        return False
    conn.execute(
        "INSERT INTO transaction_links(txn_id, doc_id, link_type, confidence) VALUES(?,?,?,?)",
        (txn_id, doc_id, link_type, confidence),
    )
    return True


# ── Main reconciliation scan ──────────────────────────────────────────────────

def scan_cross_source_matches(
    lookback_days: Optional[int] = None,
    date_window: int = 14,
    amount_tolerance: float = 0.05,
) -> dict:
    """
    Scan transactions and analyzed_documents for cross-source matches.

    A match is: same normalized vendor + amount within tolerance + date within window.

    For each matched pair, write a transaction_link and optionally flag the
    analyzed_document as cross_source_duplicate if a higher-confidence bank
    transaction already covers it.

    lookback_days=None (default) scans every dated record — appropriate for a
    tax app where multi-year reconciliation is normal.

    Returns {"links_created": int, "links_updated": int, "scanned": int}.
    """
    from app.db.core import get_connection

    conn = get_connection()
    links_created = links_updated = 0

    try:
        cutoff = None
        if lookback_days is not None:
            cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        txn_query = (
            "SELECT id, date, amount, vendor, vendor_normalized, source, entity_id, tax_year "
            "FROM transactions "
            "WHERE vendor IS NOT NULL AND vendor != ''"
        )
        doc_query = (
            "SELECT id, date, amount, vendor, entity_id, tax_year, doc_type "
            "FROM analyzed_documents "
            "WHERE vendor IS NOT NULL AND vendor != '' "
            "AND amount IS NOT NULL AND amount > 0 "
            "AND (is_duplicate = 0 OR is_duplicate IS NULL)"
        )
        txn_params: list = []
        doc_params: list = []
        if cutoff:
            txn_query += " AND date >= ?"
            doc_query += " AND date >= ?"
            txn_params.append(cutoff)
            doc_params.append(cutoff)
        txn_query += " ORDER BY date"
        doc_query += " ORDER BY date"

        txns = conn.execute(txn_query, tuple(txn_params)).fetchall()
        docs = conn.execute(doc_query, tuple(doc_params)).fetchall()

        logger.info(
            "Cross-source scan: %d transactions × %d documents", len(txns), len(docs)
        )

        # Normalize + index documents by (vendor_normalized, year)
        doc_by_vendor: dict[str, list] = {}
        for doc in docs:
            vn = normalize_vendor(doc["vendor"] or "")
            key = vn.lower()
            doc_by_vendor.setdefault(key, []).append(dict(doc))

        scanned = 0
        for txn in txns:
            txn_vendor_raw = txn["vendor_normalized"] or txn["vendor"] or ""
            txn_vn = normalize_vendor(txn_vendor_raw)
            txn_amount = abs(txn["amount"] or 0)
            txn_date = txn["date"] or ""
            if not txn_vn or not txn_amount or not txn_date:
                continue

            scanned += 1
            candidates = doc_by_vendor.get(txn_vn.lower(), [])

            for doc in candidates:
                doc_amount = abs(doc["amount"] or 0)
                doc_date = doc["date"] or ""

                if not _amounts_match(txn_amount, doc_amount, amount_tolerance):
                    continue
                if not _dates_close(txn_date, doc_date, date_window):
                    continue

                # Compute confidence: exact amount = higher, tighter date = higher
                amount_score = 1.0 - abs(txn_amount - doc_amount) / max(txn_amount, doc_amount)
                date_diff = abs(
                    (datetime.strptime(txn_date[:10], "%Y-%m-%d")
                     - datetime.strptime(doc_date[:10], "%Y-%m-%d")).days
                )
                date_score = max(0.0, 1.0 - date_diff / date_window)
                confidence = round((amount_score + date_score) / 2, 3)

                is_new = _write_link(conn, txn["id"], doc["id"], "match", confidence)
                if is_new:
                    links_created += 1
                else:
                    links_updated += 1

        conn.commit()

    finally:
        conn.close()

    logger.info(
        "Cross-source scan complete: %d links created, %d updated, %d txns scanned",
        links_created, links_updated, scanned,
    )
    return {"links_created": links_created, "links_updated": links_updated, "scanned": scanned}


def backfill_vendor_normalized(force: bool = False) -> int:
    """Normalize vendor_normalized for transactions.

    With force=False (default): only rows with empty/NULL vendor_normalized.
    With force=True: every row with a non-empty vendor (useful after changing
    the normalization rules). Only writes when the new value differs.

    Returns count of rows actually updated.
    """
    from app.db.core import get_connection

    conn = get_connection()
    try:
        if force:
            rows = conn.execute(
                "SELECT id, vendor, vendor_normalized FROM transactions WHERE vendor IS NOT NULL AND vendor != ''"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, vendor, vendor_normalized FROM transactions WHERE (vendor_normalized IS NULL OR vendor_normalized='') AND vendor IS NOT NULL"
            ).fetchall()
        updated = 0
        for row in rows:
            vn = normalize_vendor(row["vendor"] or "")
            if force and vn == (row["vendor_normalized"] or ""):
                continue  # already correct, skip write
            conn.execute(
                "UPDATE transactions SET vendor_normalized=? WHERE id=?",
                (vn, row["id"]),
            )
            updated += 1
        if updated:
            conn.commit()
        return updated
    finally:
        conn.close()


def get_transaction_links(txn_id: int) -> list[dict]:
    """Return all documents linked to a transaction."""
    from app.db.core import get_connection

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT tl.*, ad.vendor, ad.amount, ad.date, ad.doc_type,
                      ad.title, ad.paperless_doc_id
               FROM transaction_links tl
               JOIN analyzed_documents ad ON ad.id = tl.doc_id
               WHERE tl.txn_id = ?
               ORDER BY tl.confidence DESC""",
            (txn_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_document_links(doc_id: int) -> list[dict]:
    """Return all transactions linked to a document."""
    from app.db.core import get_connection

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT tl.*, t.source, t.vendor, t.amount, t.date, t.description,
                      t.tax_year, t.entity_id
               FROM transaction_links tl
               JOIN transactions t ON t.id = tl.txn_id
               WHERE tl.doc_id = ?
               ORDER BY tl.confidence DESC""",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_unmatched_transactions(
    entity_id: Optional[int] = None,
    tax_year: Optional[str] = None,
    limit: int = 500,
    min_abs_amount: Optional[float] = None,
    categories: Optional[list[str]] = None,
) -> list[dict]:
    """Transactions that have no matching document link.

    Useful for "paid but no receipt" reconciliation. Skips rows with no usable
    vendor/date/amount (they can never match).

    min_abs_amount filters to rows where abs(amount) >= threshold (used for
    audit-risk view: IRS requires receipts for business expenses ≥ $75).
    categories, if given, filters to those category values (e.g. ['expense','deduction']).
    """
    from app.db.core import get_connection

    conn = get_connection()
    try:
        where = [
            "NOT EXISTS (SELECT 1 FROM transaction_links tl WHERE tl.txn_id = t.id)",
            "t.vendor IS NOT NULL AND t.vendor != ''",
            "t.amount IS NOT NULL AND t.amount != 0",
            "t.date IS NOT NULL AND t.date != ''",
        ]
        params: list = []
        if entity_id is not None:
            where.append("t.entity_id = ?")
            params.append(entity_id)
        if tax_year:
            where.append("t.tax_year = ?")
            params.append(tax_year)
        if min_abs_amount is not None:
            where.append("ABS(t.amount) >= ?")
            params.append(float(min_abs_amount))
        if categories:
            placeholders = ",".join("?" for _ in categories)
            where.append(f"t.category IN ({placeholders})")
            params.extend(categories)
        params.append(limit)
        rows = conn.execute(
            f"""SELECT t.*, e.name as entity_name
               FROM transactions t
               LEFT JOIN entities e ON e.id = t.entity_id
               WHERE {' AND '.join(where)}
               ORDER BY ABS(t.amount) DESC, t.date DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# IRS requires receipts for business expenses ≥ $75 (IRS Pub 463).
AUDIT_RISK_THRESHOLD = 75.0


def audit_risk_summary(
    entity_id: Optional[int] = None,
    tax_year: Optional[str] = None,
    threshold: float = AUDIT_RISK_THRESHOLD,
) -> dict:
    """Count + dollar total of expense/deduction transactions ≥ threshold with no
    linked document. These are the IRS-audit-risk items that most need receipts.

    Returns {count, total_amount, threshold, worst_offenders}.
    """
    from app.db.core import get_connection

    conn = get_connection()
    try:
        where = [
            "NOT EXISTS (SELECT 1 FROM transaction_links tl WHERE tl.txn_id = t.id)",
            "t.vendor IS NOT NULL AND t.vendor != ''",
            "t.amount IS NOT NULL",
            "ABS(t.amount) >= ?",
            "t.category IN ('expense','deduction')",
        ]
        params: list = [float(threshold)]
        if entity_id is not None:
            where.append("t.entity_id = ?")
            params.append(entity_id)
        if tax_year:
            where.append("t.tax_year = ?")
            params.append(tax_year)

        row = conn.execute(
            f"""SELECT COUNT(*) as count,
                      COALESCE(SUM(ABS(t.amount)), 0) as total_amount,
                      COALESCE(MAX(ABS(t.amount)), 0) as max_amount
               FROM transactions t
               WHERE {' AND '.join(where)}""",
            tuple(params),
        ).fetchone()

        worst = conn.execute(
            f"""SELECT t.id, t.date, t.vendor, t.amount, t.description,
                      t.tax_year, t.source, e.name as entity_name
               FROM transactions t
               LEFT JOIN entities e ON e.id = t.entity_id
               WHERE {' AND '.join(where)}
               ORDER BY ABS(t.amount) DESC LIMIT 5""",
            tuple(params),
        ).fetchall()

        return {
            "count": row["count"],
            "total_amount": round(row["total_amount"] or 0, 2),
            "max_amount": round(row["max_amount"] or 0, 2),
            "threshold": threshold,
            "worst_offenders": [dict(r) for r in worst],
        }
    finally:
        conn.close()


def list_orphan_documents(
    entity_id: Optional[int] = None,
    tax_year: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """Analyzed documents that have no matching transaction link.

    Useful for "have receipt but no bank record" reconciliation.
    """
    from app.db.core import get_connection

    conn = get_connection()
    try:
        where = [
            "NOT EXISTS (SELECT 1 FROM transaction_links tl WHERE tl.doc_id = ad.id)",
            "ad.vendor IS NOT NULL AND ad.vendor != ''",
            "ad.amount IS NOT NULL AND ad.amount > 0",
            "ad.date IS NOT NULL AND ad.date != ''",
            "(ad.is_duplicate = 0 OR ad.is_duplicate IS NULL)",
        ]
        params: list = []
        if entity_id is not None:
            where.append("ad.entity_id = ?")
            params.append(entity_id)
        if tax_year:
            where.append("ad.tax_year = ?")
            params.append(tax_year)
        params.append(limit)
        rows = conn.execute(
            f"""SELECT ad.*, e.name as entity_name
               FROM analyzed_documents ad
               LEFT JOIN entities e ON e.id = ad.entity_id
               WHERE {' AND '.join(where)}
               ORDER BY ad.date DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def manual_link(txn_id: int, doc_id: int, confidence: float = 1.0) -> dict:
    """Create a manual transaction↔document link (user-asserted match).

    Returns {"status": "created"|"updated", "link_id": int}.
    """
    from app.db.core import get_connection
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM transaction_links WHERE txn_id=? AND doc_id=?",
            (txn_id, doc_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE transaction_links SET link_type='manual', confidence=? WHERE id=?",
                (confidence, existing["id"]),
            )
            conn.commit()
            return {"status": "updated", "link_id": existing["id"]}
        cur = conn.execute(
            "INSERT INTO transaction_links(txn_id, doc_id, link_type, confidence) VALUES(?,?,?,?)",
            (txn_id, doc_id, "manual", confidence),
        )
        conn.commit()
        return {"status": "created", "link_id": cur.lastrowid}
    finally:
        conn.close()


def unlink(txn_id: int, doc_id: int) -> bool:
    """Remove a transaction↔document link. Returns True if a row was deleted."""
    from app.db.core import get_connection
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM transaction_links WHERE txn_id=? AND doc_id=?",
            (txn_id, doc_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
