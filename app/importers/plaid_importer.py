"""Plaid transaction importer.

Flow:
  1. Server creates a link_token via Plaid API → given to the frontend.
  2. User runs the Plaid Link widget (client-side JS), authenticates with their
     bank, and returns a short-lived public_token.
  3. Server exchanges public_token → permanent access_token + item_id, stores
     them in the plaid_items table.
  4. run_import() calls /transactions/sync (incremental cursor-based pull)
     and upserts each transaction via db.upsert_transaction().

Settings (all stored in `settings` table via db.set_setting/get_setting):
  - plaid_client_id
  - plaid_secret
  - plaid_env: "sandbox" (default), "development", or "production"

Compared with SimpleFIN, Plaid uses OAuth-like per-institution access tokens and
supports richer data (pending flags, merchant categorization, counterparties).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SOURCE = "plaid"


# ── Plaid client construction ─────────────────────────────────────────────────

def _plaid_host(env: str) -> str:
    env = (env or "sandbox").lower()
    return {
        "sandbox":     "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production":  "https://production.plaid.com",
    }.get(env, "https://sandbox.plaid.com")


def _plaid_client():
    """Return a plaid_api.PlaidApi client built from stored settings.

    Raises RuntimeError if the plaid-python package or credentials are missing.
    """
    from app import db
    client_id = db.get_setting("plaid_client_id") or ""
    secret = db.get_setting("plaid_secret") or ""
    env = db.get_setting("plaid_env") or "sandbox"
    if not client_id or not secret:
        raise RuntimeError("Plaid not configured — set plaid_client_id and plaid_secret in Settings.")

    try:
        import plaid
        from plaid.api import plaid_api
        from plaid.configuration import Configuration
        from plaid.api_client import ApiClient
    except ImportError as e:
        raise RuntimeError(
            "plaid-python package is not installed. Add `plaid-python>=24.0.0` to requirements.txt and rebuild."
        ) from e

    config = Configuration(
        host=_plaid_host(env),
        api_key={"clientId": client_id, "secret": secret, "plaidVersion": "2020-09-14"},
    )
    return plaid_api.PlaidApi(ApiClient(config))


def is_configured() -> bool:
    from app import db
    return bool(db.get_setting("plaid_client_id")) and bool(db.get_setting("plaid_secret"))


# ── Link + exchange ───────────────────────────────────────────────────────────

def create_link_token(user_id: str) -> dict:
    """Create a link_token for the Plaid Link widget.

    Returns {"link_token": str, "expiration": str}.
    """
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products
    from plaid.model.country_code import CountryCode

    client = _plaid_client()
    req = LinkTokenCreateRequest(
        products=[Products("transactions")],
        client_name="Tax Organizer",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=str(user_id)),
    )
    resp = client.link_token_create(req)
    return {"link_token": resp["link_token"], "expiration": str(resp["expiration"])}


def exchange_public_token(public_token: str,
                          institution_id: Optional[str] = None,
                          institution_name: Optional[str] = None,
                          entity_id: Optional[int] = None) -> dict:
    """Exchange a short-lived public_token for a long-lived access_token,
    persist in plaid_items, and return the new DB row.
    """
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from app.db.core import get_connection

    client = _plaid_client()
    resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    access_token = resp["access_token"]
    item_id = resp["item_id"]

    # Fetch institution metadata if we only got the ID from the frontend
    if institution_id and not institution_name:
        try:
            from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
            from plaid.model.country_code import CountryCode
            ireq = InstitutionsGetByIdRequest(
                institution_id=institution_id,
                country_codes=[CountryCode("US")],
            )
            iresp = client.institutions_get_by_id(ireq)
            institution_name = iresp["institution"]["name"]
        except Exception as e:
            logger.warning(f"Plaid: failed to fetch institution name: {e}")

    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO plaid_items
               (item_id, institution_id, institution_name, access_token, entity_id, status)
               VALUES(?, ?, ?, ?, ?, 'active')
               ON CONFLICT(item_id) DO UPDATE SET
                 access_token=excluded.access_token,
                 entity_id=excluded.entity_id,
                 institution_name=excluded.institution_name,
                 status='active'""",
            (item_id, institution_id or "", institution_name or "", access_token, entity_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM plaid_items WHERE item_id=?", (item_id,)
        ).fetchone()
        return dict(row) if row else {"item_id": item_id}
    finally:
        conn.close()


def list_items() -> list[dict]:
    """Return all connected Plaid items (without exposing access_token)."""
    from app.db.core import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, item_id, institution_id, institution_name, entity_id, "
            "last_sync, status, created_at FROM plaid_items ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def remove_item(item_id: str) -> bool:
    """Disconnect an item — removes it from Plaid AND deletes our DB row."""
    from plaid.model.item_remove_request import ItemRemoveRequest
    from app.db.core import get_connection

    conn = get_connection()
    row = None
    try:
        row = conn.execute(
            "SELECT access_token FROM plaid_items WHERE item_id=?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False

    try:
        client = _plaid_client()
        client.item_remove(ItemRemoveRequest(access_token=row["access_token"]))
    except Exception as e:
        logger.warning(f"Plaid item_remove API call failed (still deleting local row): {e}")

    conn = get_connection()
    try:
        conn.execute("DELETE FROM plaid_items WHERE item_id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()
    return True


# ── Transaction sync ──────────────────────────────────────────────────────────

def _sync_transactions(client, access_token: str, cursor: str = ""):
    """Pull all new transactions since cursor. Returns (added, modified, removed, new_cursor)."""
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    added = []
    modified = []
    removed = []
    has_more = True
    while has_more:
        req = TransactionsSyncRequest(access_token=access_token, cursor=cursor)
        resp = client.transactions_sync(req)
        added.extend(resp["added"])
        modified.extend(resp["modified"])
        removed.extend(resp["removed"])
        cursor = resp["next_cursor"]
        has_more = resp["has_more"]
    return added, modified, removed, cursor


def _category_from_plaid(txn) -> str:
    """Map Plaid transaction to our category ('income' | 'expense')."""
    # Plaid amounts: positive = outflow (expense); negative = inflow (income)
    try:
        amt = float(txn["amount"])
        return "income" if amt < 0 else "expense"
    except Exception:
        return "expense"


def run_import(
    item_id: Optional[str] = None,
    entity_id: Optional[int] = None,
    log: Callable[[str], None] = logger.info,
    stop_event=None,
) -> dict:
    """Sync transactions for one item (or all items if item_id is None).

    Returns {"imported": int, "modified": int, "removed": int, "items": int}.
    """
    from app import db
    from app.db.core import get_connection

    imported = modified = removed = 0
    items_synced = 0

    client = _plaid_client()

    conn = get_connection()
    try:
        if item_id:
            rows = conn.execute(
                "SELECT * FROM plaid_items WHERE item_id=? AND status='active'", (item_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM plaid_items WHERE status='active'"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        log("No active Plaid items to sync.")
        return {"imported": 0, "modified": 0, "removed": 0, "items": 0}

    for item_row in rows:
        if stop_event and stop_event.is_set():
            log("Stop requested — breaking out of item loop.")
            break
        iid = item_row["item_id"]
        iname = item_row["institution_name"] or iid
        log(f"── {iname} ({iid}) ──")

        try:
            added, mods, rms, new_cursor = _sync_transactions(
                client, item_row["access_token"], item_row["cursor"] or ""
            )
        except Exception as e:
            log(f"  Sync failed: {e}")
            continue

        log(f"  {len(added)} added, {len(mods)} modified, {len(rms)} removed")

        item_entity_id = item_row["entity_id"] or entity_id

        for txn in added:
            try:
                amt = -float(txn["amount"])  # flip sign: Plaid positive=outflow → we use negative for expense
                date_str = str(txn.get("date") or "")[:10]
                year = date_str[:4] if date_str else ""
                vendor = (txn.get("merchant_name") or txn.get("name") or "")[:255]
                description = (txn.get("name") or "")[:255]
                db.upsert_transaction(
                    source=SOURCE,
                    source_id=txn["transaction_id"],
                    entity_id=item_entity_id,
                    tax_year=year,
                    date=date_str,
                    amount=amt,
                    vendor=vendor,
                    description=description,
                    category="income" if amt > 0 else "expense",
                    doc_type="bank_statement",
                )
                imported += 1
            except Exception as e:
                log(f"  upsert error: {e}")

        # Modified/removed handling: for MVP we just log — handling these would require
        # re-upserting by source_id (already supported) and a soft-delete path for removed.
        modified += len(mods)
        removed += len(rms)

        # Persist new cursor + last_sync
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE plaid_items SET cursor=?, last_sync=? WHERE item_id=?",
                (new_cursor, datetime.utcnow().isoformat(), iid),
            )
            conn.commit()
        finally:
            conn.close()

        items_synced += 1

    log(f"Plaid sync complete — imported {imported}, modified {modified}, removed {removed}, items {items_synced}")
    return {"imported": imported, "modified": modified, "removed": removed, "items": items_synced}
