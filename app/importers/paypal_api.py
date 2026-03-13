"""
PayPal REST API importer.

Uses PayPal's Transactions API (/v1/reporting/transactions) with OAuth2 client credentials.
Credentials (client_id + secret) are obtained from developer.paypal.com and stored in DB settings.

Reference: https://developer.paypal.com/docs/api/transaction-search/v1/
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

PAYPAL_LIVE_BASE = "https://api-m.paypal.com"
PAYPAL_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"

# PayPal transaction type codes → (doc_type, category)
_TRANSACTION_CODE_MAP: dict[str, tuple[str, str]] = {
    "T0000": ("receipt", "expense"),    # General Payment
    "T0001": ("receipt", "expense"),    # MassPay
    "T0002": ("receipt", "expense"),    # Subscription
    "T0003": ("receipt", "expense"),    # Preapproved
    "T0004": ("receipt", "expense"),    # eBay
    "T0005": ("receipt", "expense"),    # Direct debit
    "T0006": ("receipt", "expense"),    # Express Checkout
    "T0007": ("receipt", "expense"),    # Website Payment
    "T0008": ("receipt", "expense"),    # Flexible Payment
    "T0009": ("receipt", "expense"),    # Gift Certificate
    "T0010": ("receipt", "expense"),    # Pay Later
    "T0011": ("receipt", "expense"),    # Mobile
    "T0012": ("receipt", "expense"),    # Virtual Terminal
    "T0013": ("invoice", "income"),     # Donation
    "T0014": ("invoice", "income"),     # Unilateral
    "T0200": ("", ""),                  # General Currency Conversion — skip
    "T0400": ("", ""),                  # General Bank Deposit/Withdrawal — skip
    "T0600": ("", ""),                  # General Transfer — skip
    "T1100": ("receipt", "refund"),     # Reversal
    "T1101": ("receipt", "refund"),     # ACH Return
    "T1102": ("receipt", "refund"),     # Chargeback
    "T1103": ("receipt", "refund"),     # Guarantee
    "T1104": ("receipt", "refund"),     # Buyer Credit Card Chargeback
    "T1105": ("receipt", "refund"),     # Buyer Reversal
    "T1106": ("receipt", "refund"),     # Payment Reversal
    "T1107": ("receipt", "refund"),     # Payment Refund
    "T1108": ("receipt", "refund"),     # Fee Reversal
    "T1109": ("receipt", "refund"),     # Fee Refund
    "T1200": ("invoice", "income"),     # Payment
    "T1201": ("invoice", "income"),     # Recurring Payment
    "T1202": ("invoice", "income"),     # Recurring Payment Profile
    "T1203": ("invoice", "income"),     # Express Checkout
    "T1204": ("invoice", "income"),     # Handling Fee
    "T1205": ("invoice", "income"),     # Transaction Fee
    "T3000": ("receipt", "expense"),    # eBay Auction Payment
}


def get_access_token(client_id: str, client_secret: str, sandbox: bool = False) -> str:
    """Exchange client credentials for an OAuth2 access token."""
    import urllib.request
    import urllib.parse
    import base64
    import json

    base = PAYPAL_SANDBOX_BASE if sandbox else PAYPAL_LIVE_BASE
    url = f"{base}/v1/oauth2/token"
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def fetch_transactions(
    access_token: str,
    start_date: str,  # ISO-8601 e.g. "2023-01-01T00:00:00-0700"
    end_date: str,
    sandbox: bool = False,
    fields: str = "all",
) -> list[dict]:
    """
    Fetch all transactions between start_date and end_date.
    PayPal limits each request to 31 days, so we page automatically.
    Returns raw transaction_detail dicts from PayPal.
    """
    import urllib.request
    import json

    base = PAYPAL_SANDBOX_BASE if sandbox else PAYPAL_LIVE_BASE
    all_txns: list[dict] = []
    page = 1

    while True:
        params = (
            f"start_date={start_date}&end_date={end_date}"
            f"&fields={fields}&page_size=500&page={page}"
        )
        url = f"{base}/v1/reporting/transactions?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        txns = data.get("transaction_details", [])
        all_txns.extend(txns)

        total_pages = data.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    return all_txns


def parse_paypal_transaction(raw: dict, entity_id: Optional[int] = None,
                              default_year: Optional[str] = None) -> Optional[dict]:
    """Convert a PayPal transaction_detail dict to our internal transaction format."""
    info = raw.get("transaction_info", {})
    payer = raw.get("payer_info", {})
    cart = raw.get("cart_info", {})

    txn_id = info.get("transaction_id", "")
    code = info.get("transaction_event_code", "")
    status = info.get("transaction_status", "")

    # Skip pending/denied/etc
    if status not in ("S", "P", "V"):  # Success, Pending, Reversal
        return None

    # Skip internal transfers (no financial meaning)
    doc_type, category = _TRANSACTION_CODE_MAP.get(code, ("receipt", "expense"))
    if not doc_type:
        return None

    # Amount
    amount_info = info.get("transaction_amount", {})
    try:
        amount = float(amount_info.get("value", 0))
    except (ValueError, TypeError):
        amount = 0.0

    # For reversals/refunds make amount negative
    if code.startswith("T11") and amount > 0:
        amount = -amount

    # Date
    raw_date = info.get("transaction_initiation_date", "") or ""
    txn_date = ""
    tax_year = default_year
    if raw_date:
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            txn_date = dt.strftime("%Y-%m-%d")
            tax_year = str(dt.year)
        except Exception:
            txn_date = raw_date[:10]

    # Vendor / description
    items = cart.get("item_details", [])
    if items:
        item_names = ", ".join(i.get("item_name", "") for i in items if i.get("item_name"))
        description = item_names or info.get("transaction_note", "") or code
    else:
        description = info.get("transaction_note", "") or info.get("transaction_subject", "") or code

    payer_name = payer.get("payer_name", {})
    vendor = (
        payer_name.get("full_name")
        or f"{payer_name.get('given_name','')} {payer_name.get('surname','')}".strip()
        or payer.get("email_address", "")
        or ""
    )

    # Dedup hash
    import hashlib
    dedup = hashlib.sha256(f"paypal:{txn_id}".encode()).hexdigest()[:32]

    return {
        "date": txn_date,
        "description": description[:255],
        "vendor": vendor[:255],
        "amount": round(amount, 2),
        "category": category,
        "doc_type": doc_type,
        "source": "paypal_api",
        "entity_id": entity_id,
        "tax_year": tax_year,
        "external_id": txn_id,
        "dedup_hash": dedup,
        "raw_data": str(raw)[:1000],
    }


def pull_transactions_for_year(
    client_id: str,
    client_secret: str,
    year: str,
    entity_id: Optional[int] = None,
    sandbox: bool = False,
) -> list[dict]:
    """
    Pull all PayPal transactions for a full calendar year.
    Chunks into 31-day windows (PayPal's max per request).
    Returns list of parsed transaction dicts.
    """
    token = get_access_token(client_id, client_secret, sandbox=sandbox)
    results: list[dict] = []
    y = int(year)

    # Build 31-day chunks
    start = datetime(y, 1, 1, tzinfo=timezone.utc)
    end_of_year = datetime(y, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    while start <= end_of_year:
        chunk_end = min(start + timedelta(days=30), end_of_year)
        raw_txns = fetch_transactions(
            token,
            start_date=start.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            end_date=chunk_end.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            sandbox=sandbox,
        )
        for raw in raw_txns:
            parsed = parse_paypal_transaction(raw, entity_id=entity_id, default_year=year)
            if parsed:
                results.append(parsed)
        start = chunk_end + timedelta(seconds=1)

    logger.info(f"PayPal API: pulled {len(results)} transactions for {year}")
    return results
