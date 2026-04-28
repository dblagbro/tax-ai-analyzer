"""Transaction upsert with date+amount normalization (used by Gmail and IMAP importers).

Extracted from the original 843-line ``app/importers/gmail_importer.py``
during Phase 11H refactor. The public API (``run_import``, ``get_auth_url``,
``complete_auth``, ``is_authenticated``) and the helpers IMAP imports
(``_ai_review_email``, ``_fast_prefilter``, ``_is_known_pdf``,
``_text_to_pdf``, ``upsert_transaction``) are re-exported by the package
``__init__`` so existing callers don't change.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.db import transactions as db_transactions
from app.importers.gmail.parse import _normalize_gmail_date, _coerce_amount

logger = logging.getLogger(__name__)


def upsert_transaction(txn: dict) -> dict:
    """Insert transaction, skipping if source_id already exists OR if a
    near-duplicate (same vendor + amount within 7 days) is already present.
    This prevents multiple payment-notification emails about the same payment
    from each creating a separate transaction record.
    """
    from app import db

    vendor = txn.get("vendor", "")
    amount = _coerce_amount(txn.get("amount"))
    raw_date = txn.get("date", "")
    iso_date, year_from_date = _normalize_gmail_date(raw_date)
    tax_year = txn.get("year") or year_from_date

    # Near-duplicate check: same vendor + amount within 7 days (uses ISO dates)
    if vendor and amount is not None and iso_date:
        try:
            from datetime import datetime as _dt
            new_dt = _dt.strptime(iso_date, "%Y-%m-%d")
            conn = db.get_connection()
            try:
                existing = conn.execute(
                    "SELECT id, date FROM transactions WHERE source='gmail' AND vendor=? AND amount=?",
                    (vendor, amount),
                ).fetchall()
                for row in existing:
                    try:
                        existing_iso, _ = _normalize_gmail_date(row["date"])
                        if not existing_iso:
                            continue
                        existing_dt = _dt.strptime(existing_iso, "%Y-%m-%d")
                        if abs((new_dt - existing_dt).days) <= 7:
                            return {"id": row["id"], "skipped": "near_duplicate"}
                    except Exception:
                        pass
            finally:
                conn.close()
        except Exception:
            pass

    try:
        txn_id = db.upsert_transaction(
            source=txn.get("source", "gmail"),
            source_id=txn.get("source_id", ""),
            entity_id=txn.get("entity_id"),
            tax_year=tax_year,
            date=iso_date or raw_date,
            amount=amount,
            vendor=vendor,
            description=txn.get("description", ""),
            category=txn.get("category", "imported"),
            doc_type=txn.get("doc_type", "email"),
        )
        return {"id": txn_id}
    except Exception:
        # Normalize the fallback payload too so add_transaction doesn't reintroduce bad data
        fallback = dict(txn)
        fallback["date"] = iso_date or raw_date
        fallback["amount"] = amount
        fallback["year"] = tax_year
        fallback["tax_year"] = tax_year
        return db.add_transaction(fallback)


# ── per-month worker ───────────────────────────────────────────────────────────
