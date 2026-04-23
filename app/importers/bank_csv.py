"""
Generic bank CSV importer with auto-format detection.

Supported formats (auto-detected from headers):
  - Chase
  - Bank of America (BofA)
  - Wells Fargo
  - Ally Bank
  - Generic (best-effort column detection)

Usage:
    from app.importers.bank_csv import import_csv, parse_csv
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Format definitions ─────────────────────────────────────────────────────────

FORMAT_CHASE      = "chase"
FORMAT_BOFA       = "bofa"
FORMAT_WELLSFARGO = "wellsfargo"
FORMAT_ALLY       = "ally"
FORMAT_USBANK     = "usbank"
FORMAT_MERRICK    = "merrick"
FORMAT_CAPITALONE = "capitalone"
FORMAT_GENERIC    = "generic"

# Signature header sets for detection (lowercase, stripped)
_FORMAT_SIGNATURES: dict[str, list[set]] = {
    FORMAT_CHASE: [
        {"date", "description", "amount", "running bal."},
        {"date", "description", "amount", "balance"},
        {"transaction date", "post date", "description", "category", "type", "amount"},
    ],
    FORMAT_BOFA: [
        {"date", "description", "amount", "running bal.", "balance"},
        {"posted date", "reference number", "payee", "address", "amount"},
    ],
    FORMAT_WELLSFARGO: [
        # Wells Fargo CSV has no header row — detected differently
        {"date", "amount", "*", "check number", "description"},
    ],
    FORMAT_ALLY: [
        {"date", "time", "amount", "type", "description"},
        {"date", "time (et)", "amount", "type", "description"},
    ],
    FORMAT_USBANK: [
        {"date", "transaction", "name", "memo", "amount"},
        {"date", "transaction type", "name", "memo", "amount"},
    ],
    FORMAT_MERRICK: [
        {"date", "description", "category", "amount", "type"},
        {"transaction date", "posting date", "description", "amount"},
    ],
    FORMAT_CAPITALONE: [
        {"transaction date", "posted date", "card no.", "description", "category", "debit", "credit"},
        {"date", "description", "debit", "credit"},
    ],
}


# ── Amount parsing ─────────────────────────────────────────────────────────────

def _parse_amount(raw: str) -> Optional[float]:
    """Return float from a currency string; None if empty/unparseable."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_date(raw: str) -> str:
    """Return YYYY-MM-DD from common bank date formats."""
    raw = raw.strip()
    for fmt in (
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%b %d, %Y",
        "%m-%d-%Y",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return raw


def _row_hash(date: str, amount: str, description: str) -> str:
    """Stable dedup key based on date + amount + description."""
    payload = f"{date}|{amount}|{description}".lower().strip()
    return hashlib.sha1(payload.encode()).hexdigest()


# ── Format detection ───────────────────────────────────────────────────────────

def detect_format(headers: list[str]) -> str:
    """
    Determine the bank CSV format from the list of header column names.

    Matching is case-insensitive. Returns one of the FORMAT_* constants.
    Falls back to FORMAT_GENERIC.
    """
    normalized = {h.lower().strip() for h in headers}

    for fmt, signature_sets in _FORMAT_SIGNATURES.items():
        for sig in signature_sets:
            if sig.issubset(normalized):
                return fmt

    # Wells Fargo typically has no header — 5 columns in a specific order
    if len(headers) == 5 and re.match(r"\d{2}/\d{2}/\d{4}", headers[0]):
        return FORMAT_WELLSFARGO

    return FORMAT_GENERIC


# ── Format-specific row parsers ────────────────────────────────────────────────

def _parse_chase_row(row: dict) -> Optional[dict]:
    """Parse one Chase CSV row."""
    # Chase exports use "Transaction Date" or "Date"
    date_raw = row.get("Transaction Date") or row.get("Date", "")
    desc = row.get("Description", "")
    amount_raw = row.get("Amount", "")

    date_iso = _normalize_date(date_raw)
    amount = _parse_amount(amount_raw)

    if not date_iso or amount is None:
        return None

    return {
        "date": date_iso,
        "description": desc,
        "amount": amount,   # Chase: negatives are debits
        "category_hint": row.get("Category", ""),
        "memo": row.get("Memo", ""),
    }


def _parse_bofa_row(row: dict) -> Optional[dict]:
    """Parse one Bank of America CSV row."""
    date_raw = row.get("Date") or row.get("Posted Date", "")
    desc = row.get("Description") or row.get("Payee", "")
    amount_raw = row.get("Amount", "")

    date_iso = _normalize_date(date_raw)
    amount = _parse_amount(amount_raw)

    if not date_iso or amount is None:
        return None

    return {
        "date": date_iso,
        "description": desc,
        "amount": amount,
        "reference": row.get("Reference Number", ""),
        "memo": "",
    }


def _parse_wellsfargo_row(row: dict | list) -> Optional[dict]:
    """
    Parse one Wells Fargo CSV row.

    Wells Fargo exports typically have NO header row:
      col0=Date, col1=Amount, col2=*(unused), col3=Check Number, col4=Description
    The csv.DictReader may assign auto-generated keys if no header is given.
    """
    if isinstance(row, list):
        if len(row) < 5:
            return None
        date_raw, amount_raw, _, check_num, desc = row[0], row[1], row[2], row[3], row[4]
    else:
        keys = list(row.keys())
        if len(keys) < 5:
            return None
        date_raw  = row.get("Date")      or row.get(keys[0], "")
        amount_raw = row.get("Amount")   or row.get(keys[1], "")
        check_num  = row.get("Check Number") or row.get(keys[3], "")
        desc       = row.get("Description") or row.get(keys[4], "")

    date_iso = _normalize_date(date_raw)
    amount   = _parse_amount(amount_raw)

    if not date_iso or amount is None:
        return None

    return {
        "date": date_iso,
        "description": desc,
        "amount": amount,
        "check_number": check_num,
        "memo": "",
    }


def _parse_ally_row(row: dict) -> Optional[dict]:
    """Parse one Ally Bank CSV row."""
    date_raw   = row.get("Date", "")
    desc       = row.get("Description", "")
    amount_raw = row.get("Amount", "")
    txn_type   = row.get("Type", "")

    date_iso = _normalize_date(date_raw)
    amount   = _parse_amount(amount_raw)

    if not date_iso or amount is None:
        return None

    return {
        "date": date_iso,
        "description": desc,
        "amount": amount,
        "txn_type": txn_type,
        "memo": "",
    }


def _parse_generic_row(row: dict, col_map: dict) -> Optional[dict]:
    """
    Parse one row using intelligently detected column names.

    col_map: {"date": actual_col, "amount": actual_col, "description": actual_col}
    """
    date_raw   = row.get(col_map.get("date", ""), "")
    amount_raw = row.get(col_map.get("amount", ""), "")
    desc       = row.get(col_map.get("description", ""), "")

    date_iso = _normalize_date(date_raw)
    amount   = _parse_amount(amount_raw)

    if not date_iso or amount is None:
        return None

    return {
        "date": date_iso,
        "description": desc,
        "amount": amount,
        "memo": "",
    }


def _detect_generic_columns(headers: list[str]) -> dict:
    """
    For generic CSVs, heuristically find date, amount, and description columns.

    Returns a mapping like {"date": "Trans. Date", "amount": "Debit", "description": "Memo"}.
    """
    col_map: dict[str, str] = {}
    date_candidates    = ["date", "trans date", "transaction date", "posted date", "trans. date"]
    amount_candidates  = ["amount", "debit", "credit", "net amount", "transaction amount"]
    desc_candidates    = ["description", "memo", "payee", "narrative", "details", "transaction"]

    normalized = {h.lower().strip(): h for h in headers}

    for c in date_candidates:
        if c in normalized:
            col_map["date"] = normalized[c]
            break

    for c in amount_candidates:
        if c in normalized:
            col_map["amount"] = normalized[c]
            break

    for c in desc_candidates:
        if c in normalized:
            col_map["description"] = normalized[c]
            break

    # Last resort: positional guesses
    if "date" not in col_map and headers:
        col_map["date"] = headers[0]
    if "amount" not in col_map and len(headers) > 1:
        col_map["amount"] = headers[1]
    if "description" not in col_map and len(headers) > 2:
        col_map["description"] = headers[2]

    return col_map


# ── Wells Fargo headerless handling ───────────────────────────────────────────

def _is_wellsfargo_headerless(first_line: str) -> bool:
    """True if the first CSV line looks like a Wells Fargo data row (no header)."""
    parts = first_line.split(",")
    if len(parts) >= 2 and re.match(r'"?\d{2}/\d{2}/\d{4}"?', parts[0].strip()):
        return True
    return False


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_csv(
    file_content: str,
    entity_id: str,
    tax_year: str,
    bank_name: str = "auto",
) -> list[dict]:
    """
    Parse a bank CSV export into a list of transaction dicts.

    Args:
        file_content: Raw CSV text.
        entity_id:    ID of the entity these transactions belong to.
        tax_year:     Only include transactions from this year (if provided).
        bank_name:    One of "auto", "chase", "bofa", "wellsfargo", "ally", "generic".

    Returns:
        List of transaction dicts ready for upsert.
    """
    lines = file_content.splitlines()
    if not lines:
        return []

    # Wells Fargo headerless detection
    wellsfargo_headerless = False
    if bank_name in ("auto", "wellsfargo") and _is_wellsfargo_headerless(lines[0]):
        wellsfargo_headerless = True
        fmt = FORMAT_WELLSFARGO
        reader = csv.reader(io.StringIO(file_content))
    else:
        reader = csv.DictReader(io.StringIO(file_content))
        if reader.fieldnames:
            reader.fieldnames = [f.strip().strip("\ufeff\"'") for f in reader.fieldnames]

        if bank_name == "auto":
            fmt = detect_format(reader.fieldnames or [])
        else:
            fmt = bank_name.lower()

    logger.info(f"Bank CSV format detected/selected: {fmt}")

    generic_col_map: dict = {}
    if fmt == FORMAT_GENERIC and not wellsfargo_headerless:
        generic_col_map = _detect_generic_columns(
            [f.strip() for f in (reader.fieldnames or [])]
        )
        logger.debug(f"Generic column map: {generic_col_map}")

    transactions = []
    seen_hashes: set[str] = set()

    for row in reader:
        try:
            if wellsfargo_headerless:
                parsed = _parse_wellsfargo_row(list(row))
            elif fmt == FORMAT_CHASE:
                parsed = _parse_chase_row(row)
            elif fmt == FORMAT_BOFA:
                parsed = _parse_bofa_row(row)
            elif fmt == FORMAT_WELLSFARGO:
                parsed = _parse_wellsfargo_row(row)
            elif fmt == FORMAT_ALLY:
                parsed = _parse_ally_row(row)
            else:
                parsed = _parse_generic_row(row, generic_col_map)

            if parsed is None:
                continue

            date_iso    = parsed["date"]
            amount      = parsed["amount"]
            description = parsed["description"]

            # Year filter
            year_of_txn = date_iso[:4] if len(date_iso) >= 4 else ""
            if year_of_txn and str(tax_year) and year_of_txn != str(tax_year):
                continue

            # Dedup by hash of (date, amount, description)
            dedup_key = _row_hash(date_iso, str(amount), description)
            if dedup_key in seen_hashes:
                continue
            seen_hashes.add(dedup_key)

            # Sign convention: credits positive, debits negative
            # Most bank exports already follow this; ensure it.
            # (no additional flip needed — use raw sign from parser)

            category = "income" if amount > 0 else "expense"
            doc_type = "bank_statement"

            txn = {
                "source":       "bank_csv",
                "source_id":    dedup_key,
                "entity_id":    entity_id,
                "year":         year_of_txn or str(tax_year),
                "date":         date_iso,
                "description":  description,
                "vendor":       description,  # best we can do without merchant data
                "amount":       amount,
                "doc_type":     doc_type,
                "category":     category,
                "bank_format":  fmt,
                "memo":         parsed.get("memo", ""),
                "imported_at":  datetime.utcnow().isoformat(),
            }

            # Carry through format-specific extras
            for extra in ("category_hint", "check_number", "txn_type", "reference"):
                if parsed.get(extra):
                    txn[extra] = parsed[extra]

            transactions.append(txn)

        except Exception as e:
            logger.warning(f"Bank CSV: error parsing row: {e}")
            continue

    logger.info(f"Bank CSV parsed: {len(transactions)} qualifying transactions (format={fmt})")
    return transactions


# ── DB upsert helper ───────────────────────────────────────────────────────────

def _upsert_transaction(txn: dict) -> tuple[dict, bool]:
    """
    Insert transaction if source_id is new.

    Returns:
        (transaction_dict, is_new)
    """
    from app import db
    source_id = txn.get("source_id", "")
    if source_id:
        existing = db.get_transactions(limit=100000)
        for t in existing:
            if t.get("source") == "bank_csv" and t.get("source_id") == source_id:
                return t, False
    return db.add_transaction(txn), True


# ── Import entry point ─────────────────────────────────────────────────────────

def import_csv(
    file_content: str,
    entity_id: str,
    tax_year: str,
    bank_name: str = "auto",
) -> dict:
    """
    Parse a bank CSV and upsert all qualifying transactions.

    Returns:
        {"count": int, "format_detected": str}
    """
    transactions = parse_csv(file_content, entity_id, tax_year, bank_name)
    inserted = 0
    fmt = transactions[0].get("bank_format", "generic") if transactions else "generic"

    for txn in transactions:
        _, is_new = _upsert_transaction(txn)
        if is_new:
            inserted += 1

    logger.info(
        f"Bank CSV import: {inserted} new transactions inserted "
        f"(of {len(transactions)} parsed, format={fmt})"
    )
    return {"count": inserted, "format_detected": fmt}
