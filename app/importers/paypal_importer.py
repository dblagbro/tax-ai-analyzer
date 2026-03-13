"""
PayPal CSV importer and API stub.

PayPal CSV column order (standard export):
  Date, Time, TimeZone, Name, Type, Status, Currency, Gross, Fee, Net,
  From Email Address, To Email Address, Transaction ID, Item Title, Note

Usage:
    from app.importers.paypal_importer import import_csv, parse_paypal_csv
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── PayPal Type → (doc_type, category) ────────────────────────────────────────

_TYPE_MAP: dict[str, tuple[str, str]] = {
    "general payment":      ("receipt",  "expense"),
    "payment received":     ("invoice",  "income"),
    "transfer":             ("",         ""),          # ignore
    "subscription payment": ("receipt",  "subscription"),
    "ebay payment":         ("receipt",  "expense"),
    "ebay sale":            ("invoice",  "income"),
    "refund":               ("receipt",  "refund"),
    "payment refund":       ("receipt",  "refund"),
    "reversal":             ("receipt",  "refund"),
    "general withdrawal":   ("",         ""),          # ignore
    "general deposit":      ("",         ""),          # ignore
    "donation received":    ("invoice",  "income"),
    "donation":             ("receipt",  "expense"),
}

# ── amount parsing ─────────────────────────────────────────────────────────────

def _parse_amount(raw: str) -> Optional[float]:
    """Strip currency symbols, commas, spaces and return float or None."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_date(raw: str) -> str:
    """
    Normalize PayPal date strings to ISO-8601 (YYYY-MM-DD).
    PayPal exports dates as MM/DD/YYYY.
    """
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # return as-is if we cannot parse


# ── CSV parser ─────────────────────────────────────────────────────────────────

def parse_paypal_csv(
    file_content: str,
    entity_id: str,
    tax_year: str,
) -> list[dict]:
    """
    Parse a PayPal CSV export into a list of transaction dicts.

    Filtering:
      - Status must be "Completed"
      - Type "Transfer", "General Withdrawal", "General Deposit" are skipped
      - Only transactions whose date year matches tax_year are included

    Returns:
        List of transaction dicts ready for upsert_transaction().
    """
    reader = csv.DictReader(io.StringIO(file_content))

    # Normalize header names (strip BOM, whitespace, quotes)
    if reader.fieldnames:
        reader.fieldnames = [f.strip().strip("\ufeff\"'") for f in reader.fieldnames]

    transactions = []
    seen_ids: set[str] = set()

    for row in reader:
        # Normalize keys
        row = {k.strip(): v.strip() for k, v in row.items() if k}

        status = row.get("Status", "").strip().lower()
        if status != "completed":
            continue

        txn_type_raw = row.get("Type", "").strip()
        txn_type_key = txn_type_raw.lower()
        doc_type, category = _TYPE_MAP.get(txn_type_key, ("receipt", "expense"))

        # Skip ignored types
        if doc_type == "" and category == "":
            continue

        txn_id = row.get("Transaction ID", "").strip()
        if not txn_id:
            continue

        # Dedup by Transaction ID
        if txn_id in seen_ids:
            continue
        seen_ids.add(txn_id)

        raw_date = row.get("Date", "")
        date_iso = _normalize_date(raw_date)

        # Year filter
        year_of_txn = date_iso[:4] if len(date_iso) >= 4 else ""
        if year_of_txn and str(tax_year) and year_of_txn != str(tax_year):
            continue

        gross = _parse_amount(row.get("Gross", ""))
        fee = _parse_amount(row.get("Fee", ""))
        net = _parse_amount(row.get("Net", ""))
        amount = net if net is not None else gross

        name = row.get("Name", "")
        from_email = row.get("From Email Address", "")
        to_email = row.get("To Email Address", "")
        item_title = row.get("Item Title", "")
        note = row.get("Note", "")
        currency = row.get("Currency", "USD")

        description_parts = [p for p in [txn_type_raw, item_title, note, name] if p]
        description = " | ".join(description_parts) or f"PayPal {txn_type_raw}"

        transactions.append({
            "source":      "paypal",
            "source_id":   txn_id,
            "entity_id":   entity_id,
            "year":        year_of_txn or str(tax_year),
            "date":        date_iso,
            "description": description,
            "vendor":      name or from_email or to_email,
            "amount":      amount,
            "gross":       gross,
            "fee":         fee,
            "currency":    currency,
            "doc_type":    doc_type,
            "category":    category,
            "paypal_type": txn_type_raw,
            "from_email":  from_email,
            "to_email":    to_email,
            "item_title":  item_title,
            "note":        note,
            "imported_at": datetime.utcnow().isoformat(),
        })

    logger.info(f"PayPal CSV parsed: {len(transactions)} qualifying transactions")
    return transactions


# ── DB upsert helper ───────────────────────────────────────────────────────────

def _upsert_transaction(txn: dict) -> dict:
    """Insert if source_id is new; return existing record if duplicate."""
    from app import db
    source_id = txn.get("source_id", "")
    if source_id:
        existing = db.get_transactions(limit=100000)
        for t in existing:
            if t.get("source") == "paypal" and t.get("source_id") == source_id:
                return t
    return db.add_transaction(txn)


# ── import entry point ─────────────────────────────────────────────────────────

def import_csv(
    file_content: str,
    entity_id: str,
    tax_year: str,
) -> int:
    """
    Parse PayPal CSV and upsert all qualifying transactions.

    Returns:
        Number of new records inserted.
    """
    transactions = parse_paypal_csv(file_content, entity_id, tax_year)
    inserted = 0
    for txn in transactions:
        result = _upsert_transaction(txn)
        if result.get("imported_at") == txn.get("imported_at"):
            # freshly created record (timestamps match)
            inserted += 1
    logger.info(f"PayPal import: {inserted} new transactions inserted (of {len(transactions)} parsed)")
    return inserted


# ── credentials ───────────────────────────────────────────────────────────────

def get_api_credentials() -> Optional[dict]:
    """
    Read PayPal API credentials from DB settings.

    Returns:
        {"client_id": ..., "client_secret": ..., "mode": "sandbox"|"live"} or None.
    """
    from app import db
    settings = db.get_settings()
    client_id = settings.get("paypal_client_id", "").strip()
    client_secret = settings.get("paypal_client_secret", "").strip()
    if not client_id or not client_secret:
        return None
    return {
        "client_id":     client_id,
        "client_secret": client_secret,
        "mode":          settings.get("paypal_mode", "live"),
    }


# ── API stub ───────────────────────────────────────────────────────────────────

def fetch_api_transactions(
    entity_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """
    Stub: PayPal REST API transaction fetch.

    PayPal's Transactions API requires live API credentials and explicit
    enablement on the developer dashboard.  Until live keys are configured,
    this returns an empty list with an explanatory message.

    To activate:
      1. Create a PayPal developer application at developer.paypal.com
      2. Set paypal_client_id and paypal_client_secret in Settings
      3. Replace this stub with a real implementation using the
         paypalrestsdk or direct OAuth 2 + requests calls against
         https://api-m.paypal.com/v1/reporting/transactions
    """
    creds = get_api_credentials()
    if not creds:
        logger.info(
            "PayPal API credentials not configured. "
            "Use CSV export from paypal.com/activities until live API keys are available."
        )
        return []

    # Placeholder — real implementation would:
    #   1. POST to https://api-m.paypal.com/v1/oauth2/token for bearer token
    #   2. GET  https://api-m.paypal.com/v1/reporting/transactions
    #            ?start_date={start_date}T00:00:00-0700&end_date={end_date}T23:59:59-0700
    #            &fields=all&page_size=100&page=1
    #   3. Page through results, map to our transaction schema

    logger.warning(
        "fetch_api_transactions: PayPal REST API stub called. "
        "Real API integration requires live credentials and is not yet implemented."
    )
    return []
