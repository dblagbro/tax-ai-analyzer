"""
Venmo CSV importer.

Venmo CSV export format notes:
  - Row 1:  Venmo statement header / account info line (skip)
  - Row 2:  Blank or second header junk line (skip)
  - Row 3:  Blank (skip)
  - Row 4+: Column headers followed by data rows
    Columns:
      ID, Datetime, Type, Status, Note, From, To,
      Amount (total), Amount (tip), Amount (tax), Amount (fee),
      Destination, Beginning Balance, Ending Balance,
      Statement Period Venmo Fees, Terminal Balance

Usage:
    from app.importers.venmo_importer import import_csv, parse_venmo_csv
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Type mapping ───────────────────────────────────────────────────────────────

#  Venmo Type  →  (doc_type, category)
#  "Transfer"  →  skip (moving money in/out of Venmo balance)
_TYPE_MAP: dict[str, tuple[str, str]] = {
    "payment":  ("receipt",  "expense"),
    "charge":   ("invoice",  "income"),
    "transfer": ("",         ""),   # ignored
    "refund":   ("receipt",  "refund"),
    "standard transfer": ("", ""),  # ignored
    "instant transfer": ("", ""),   # ignored
    "top-up":   ("",         ""),   # ignored
}


def _parse_amount(raw: str) -> Optional[float]:
    """
    Parse a Venmo amount string.

    Venmo uses prefixes like "+ $25.00" (credit) or "- $25.00" (debit).
    Returns a signed float, or None if the field is empty or non-numeric.
    """
    if not raw:
        return None
    raw = raw.strip()
    # Detect sign from leading + / -
    negative = raw.startswith("-")
    # Strip everything except digits and decimal point
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned:
        return None
    try:
        value = float(cleaned)
        return -value if negative else value
    except ValueError:
        return None


def _normalize_date(raw: str) -> str:
    """Return YYYY-MM-DD from various Venmo datetime formats."""
    raw = raw.strip()
    # Venmo exports "2023-12-31T14:30:00" or "2023-12-31 14:30:00" or "12/31/2023"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(raw[:len(fmt) + 2], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last-ditch: try extracting first 10 chars if they look like YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return raw


def _find_header_row(lines: list[str]) -> int:
    """
    Find the index of the actual CSV header row.

    Venmo CSVs have 3 leading junk rows before the real header.
    We look for a row that contains "ID" and "Datetime" (or similar).
    Falls back to row index 3 if heuristic fails.
    """
    for i, line in enumerate(lines):
        lower = line.lower()
        if "datetime" in lower and ("from" in lower or "type" in lower):
            return i
    # Default: skip the first 3 rows (Venmo standard export)
    return 3


# ── CSV parser ─────────────────────────────────────────────────────────────────

def parse_venmo_csv(
    file_content: str,
    entity_id: str,
    tax_year: str,
) -> list[dict]:
    """
    Parse a Venmo CSV statement into a list of transaction dicts.

    Skips:
      - Leading junk/header rows (auto-detected)
      - Transfer-type rows
      - Rows whose year does not match tax_year (if tax_year is provided)

    Deduplicates by Venmo transaction ID.

    Returns:
        List of transaction dicts ready for upsert.
    """
    lines = file_content.splitlines()
    header_idx = _find_header_row(lines)

    # Re-join from the real header row onward
    csv_body = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(csv_body))
    if reader.fieldnames:
        reader.fieldnames = [f.strip().strip("\ufeff\"'") for f in reader.fieldnames]

    transactions = []
    seen_ids: set[str] = set()

    for row in reader:
        row = {k.strip(): v.strip() for k, v in row.items() if k}

        # Some rows are balance summary lines with no ID — skip them
        txn_id = row.get("ID", "").strip()
        if not txn_id or not txn_id.lstrip("0123456789"):
            # Also accept purely numeric IDs
            if not re.match(r"^\d+$", txn_id):
                continue

        if txn_id in seen_ids:
            continue
        seen_ids.add(txn_id)

        txn_type_raw = row.get("Type", "").strip()
        txn_type_key = txn_type_raw.lower()
        doc_type, category = _TYPE_MAP.get(txn_type_key, ("receipt", "expense"))

        # Skip transfers and other ignored types
        if doc_type == "" and category == "":
            continue

        raw_datetime = row.get("Datetime", "")
        date_iso = _normalize_date(raw_datetime)
        year_of_txn = date_iso[:4] if len(date_iso) >= 4 else ""

        # Year filter
        if year_of_txn and str(tax_year) and year_of_txn != str(tax_year):
            continue

        amount_total = _parse_amount(row.get("Amount (total)", ""))
        amount_tip   = _parse_amount(row.get("Amount (tip)", ""))
        amount_tax   = _parse_amount(row.get("Amount (tax)", ""))
        amount_fee   = _parse_amount(row.get("Amount (fee)", ""))

        amount = amount_total  # primary amount

        from_person = row.get("From", "")
        to_person   = row.get("To", "")
        note        = row.get("Note", "")
        status      = row.get("Status", "")
        destination = row.get("Destination", "")

        # Build a readable description
        description_parts = [p for p in [note, txn_type_raw] if p]
        if from_person and to_person:
            description_parts.insert(0, f"{from_person} → {to_person}")
        description = " | ".join(description_parts) or f"Venmo {txn_type_raw}"

        # Vendor is the counterparty
        vendor = to_person if category == "expense" else from_person

        transactions.append({
            "source":         "venmo",
            "source_id":      txn_id,
            "entity_id":      entity_id,
            "year":           year_of_txn or str(tax_year),
            "date":           date_iso,
            "description":    description,
            "vendor":         vendor,
            "amount":         amount,
            "amount_tip":     amount_tip,
            "amount_tax":     amount_tax,
            "amount_fee":     amount_fee,
            "doc_type":       doc_type,
            "category":       category,
            "venmo_type":     txn_type_raw,
            "from_person":    from_person,
            "to_person":      to_person,
            "note":           note,
            "status":         status,
            "destination":    destination,
            "imported_at":    datetime.utcnow().isoformat(),
        })

    logger.info(f"Venmo CSV parsed: {len(transactions)} qualifying transactions")
    return transactions


# ── DB upsert helper ───────────────────────────────────────────────────────────

def _upsert_transaction(txn: dict) -> dict:
    """Insert if source_id is new; return existing record if duplicate."""
    from app import db
    source_id = txn.get("source_id", "")
    if source_id:
        existing = db.get_transactions(limit=100000)
        for t in existing:
            if t.get("source") == "venmo" and t.get("source_id") == source_id:
                return t
    return db.add_transaction(txn)


# ── import entry point ─────────────────────────────────────────────────────────

def import_csv(
    file_content: str,
    entity_id: str,
    tax_year: str,
) -> int:
    """
    Parse Venmo CSV and upsert all qualifying transactions.

    Returns:
        Number of new records inserted.
    """
    transactions = parse_venmo_csv(file_content, entity_id, tax_year)
    inserted = 0
    for txn in transactions:
        result = _upsert_transaction(txn)
        if result.get("imported_at") == txn.get("imported_at"):
            inserted += 1

    logger.info(
        f"Venmo import: {inserted} new transactions inserted "
        f"(of {len(transactions)} parsed)"
    )
    return inserted
