"""SimpleFIN Bridge importer — covers 16,000+ institutions via MX data network.

Auth flow:
  1. User obtains a setup token from their bank's SimpleFIN connection page
     (a URL like https://beta-bridge.simplefin.org/simplefin/claim/<TOKEN>).
  2. App POSTs to that URL (claim step) → response body is the access URL.
  3. Access URL embeds Basic auth: https://user:pass@bridge.simplefin.org/simplefin
  4. GET <access_url>/accounts?start-date=<unix>&end-date=<unix> → JSON

API limits: 90-day window per request, 24 requests/day.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, date
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

SOURCE = "simplefin"
_90_DAYS = 90 * 24 * 3600


def claim_token(setup_url: str) -> str:
    """POST to the setup URL and return the persistent access URL."""
    setup_url = setup_url.strip()
    resp = requests.post(setup_url, timeout=30)
    resp.raise_for_status()
    access_url = resp.text.strip()
    if not access_url.startswith("http"):
        raise ValueError(f"Unexpected claim response: {access_url[:120]}")
    return access_url


def _fetch_accounts(access_url: str, start: int, end: int, log: Callable) -> list[dict]:
    """GET /accounts for a single 90-day window. Returns account list or []."""
    parsed = urlparse(access_url)
    api_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/accounts"
    auth = (parsed.username, parsed.password) if parsed.username else None

    log(f"SimpleFIN: fetching {datetime.utcfromtimestamp(start).date()} → "
        f"{datetime.utcfromtimestamp(end).date()}")

    resp = requests.get(
        api_url,
        params={"start-date": start, "end-date": end},
        auth=auth,
        timeout=30,
    )
    if resp.status_code == 403:
        log("SimpleFIN: 403 — access URL may be expired. Re-claim the token.")
        return []
    resp.raise_for_status()
    data = resp.json()
    errors = data.get("errors", [])
    if errors:
        log(f"SimpleFIN errors: {errors}")
    return data.get("accounts", [])


def _year_windows(year: str) -> list[tuple[int, int]]:
    """Return (start_unix, end_unix) tuples covering the year in 90-day chunks."""
    y = int(year)
    now_date = date.today()
    year_end = date(y, 12, 31)
    end_date = min(year_end, now_date)
    start_date = date(y, 1, 1)

    windows: list[tuple[int, int]] = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(date.fromordinal(cur.toordinal() + 89), end_date)
        windows.append((
            int(datetime(cur.year, cur.month, cur.day).timestamp()),
            int(datetime(chunk_end.year, chunk_end.month, chunk_end.day, 23, 59, 59).timestamp()),
        ))
        cur = date.fromordinal(chunk_end.toordinal() + 1)

    return windows


def run_import(
    access_url: str,
    years: list[str],
    entity_id: Optional[int],
    entity_slug: str,
    job_id: int,
    log: Callable[[str], None] = logger.info,
    account_filter: Optional[list[str]] = None,
) -> dict:
    """
    Import transactions for the requested years from SimpleFIN Bridge.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    from app import db

    imported = skipped = errors = 0

    for year in years:
        windows = _year_windows(year)
        log(f"Year {year}: {len(windows)} window(s) to fetch")
        seen_txn_ids: set[str] = set()

        for start_ts, end_ts in windows:
            try:
                accounts = _fetch_accounts(access_url, start_ts, end_ts, log)
            except Exception as e:
                log(f"Fetch error {year} window: {e}")
                errors += 1
                continue

            if account_filter:
                accounts = [a for a in accounts
                            if any(f.lower() in (a.get("name", "") + a.get("id", "")).lower()
                                   for f in account_filter)]

            for acct in accounts:
                acct_name = acct.get("name", "Unknown")
                org_name = (acct.get("organization") or {}).get("name", "")
                institution = org_name or acct_name

                txns = acct.get("transactions", [])
                log(f"  {institution} / {acct_name}: {len(txns)} transaction(s)")

                for txn in txns:
                    txn_id = txn.get("id") or hashlib.sha1(
                        json.dumps(txn, sort_keys=True).encode()
                    ).hexdigest()[:16]

                    if txn_id in seen_txn_ids:
                        skipped += 1
                        continue
                    seen_txn_ids.add(txn_id)

                    posted_ts = txn.get("posted") or txn.get("transacted_at") or 0
                    try:
                        txn_date = datetime.utcfromtimestamp(int(posted_ts)).strftime("%Y-%m-%d")
                    except Exception:
                        txn_date = f"{year}-01-01"

                    txn_year = txn_date[:4]
                    if txn_year != year:
                        skipped += 1
                        continue

                    try:
                        amount = float(txn.get("amount", 0))
                    except (TypeError, ValueError):
                        amount = 0.0

                    description = txn.get("description") or txn.get("memo") or ""
                    vendor = txn.get("payee") or description[:60]

                    source_id = f"simplefin:{txn_id}"
                    try:
                        db.upsert_transaction(
                            source=SOURCE,
                            source_id=source_id,
                            entity_id=entity_id,
                            tax_year=txn_year,
                            date=txn_date,
                            amount=amount,
                            vendor=vendor[:100],
                            description=description[:500],
                            category=txn.get("category", ""),
                            metadata_json=json.dumps({
                                "account": acct_name,
                                "institution": institution,
                                "account_id": acct.get("id", ""),
                                "simplefin_id": txn_id,
                            }),
                        )
                        imported += 1
                    except Exception as e:
                        log(f"DB insert error for {txn_id}: {e}")
                        errors += 1

            # Respect rate limit — 24 req/day means don't hammer
            time.sleep(1)

    log(f"SimpleFIN done — imported: {imported}, skipped: {skipped}, errors: {errors}")
    return {"imported": imported, "skipped": skipped, "errors": errors}
