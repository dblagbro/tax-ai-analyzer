"""Beancount plain-text double-entry ledger export.

Why: accountants who don't use QuickBooks Online (IIF/QBO) want a ledger
they can read, diff, and sanity-check without proprietary software. Beancount
(https://github.com/beancount/beancount) is the de-facto plain-text standard —
`bean-check` validates it, `bean-report`/Fava render it, and it round-trips
cleanly into hledger/ledger-cli if the accountant prefers those.

Output is one `.beancount` file per (entity, year). Every transaction and
categorized document becomes a balanced posting between an asset/liability
account (the source — bank, card, PayPal, etc.) and an expense/income/asset
account derived from our category + doc_type.

Account naming (Beancount requires Capitalized:Colon:Separated):
  Assets:<Source>         — bank/card/PayPal source of the movement
  Income:<DocType>        — income postings
  Expenses:<DocType>      — expense + deduction postings
  Assets:CapitalImprovement — asset category (depreciable)
  Expenses:Uncategorized  — anything we couldn't classify (accountant reviews)

All amounts in USD. We emit `; paperless:<id>` metadata on each posting so
the accountant can trace back to the source document.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from app import db
from app.config import EXPORT_PATH

logger = logging.getLogger(__name__)

_ACCOUNT_SAFE = re.compile(r"[^A-Za-z0-9]")


def _account_leaf(s: str) -> str:
    """Convert an arbitrary string into a valid Beancount account leaf.
    Beancount leaves must start with an uppercase letter or digit and contain
    only letters, digits, and hyphens. We CamelCase and strip."""
    if not s:
        return "Unknown"
    parts = _ACCOUNT_SAFE.sub(" ", str(s)).split()
    leaf = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not leaf:
        return "Unknown"
    if not (leaf[0].isupper() or leaf[0].isdigit()):
        leaf = "X" + leaf
    return leaf[:40]


def _source_account(source: str, vendor: str = "") -> str:
    """Map a transaction source to an Assets:/Liabilities: account."""
    s = (source or "").lower()
    if s in ("gmail", "imap", "paperless"):
        return "Assets:Unreconciled"  # from an email/document, not a bank feed
    if "card" in s or s in ("capitalone", "merrick", "chime"):
        return f"Liabilities:CreditCard:{_account_leaf(source)}"
    if s in ("paypal", "venmo"):
        return f"Assets:{_account_leaf(source)}"
    return f"Assets:Bank:{_account_leaf(source)}"


def _counter_account(category: str, doc_type: str) -> str:
    """Map (category, doc_type) to the Income:/Expenses:/Assets: side."""
    c = (category or "").lower()
    leaf = _account_leaf(doc_type or "Uncategorized")
    if c == "income":
        return f"Income:{leaf}"
    if c in ("expense", "deduction"):
        return f"Expenses:{leaf}"
    if c == "asset":
        return f"Assets:CapitalImprovement:{leaf}"
    return "Expenses:Uncategorized"


def _fmt_date(d) -> str:
    """Beancount wants YYYY-MM-DD. Tolerate ISO strings and partial dates."""
    if not d:
        return "1970-01-01"
    s = str(d)[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return "1970-01-01"


def _esc(s) -> str:
    """Escape for a Beancount quoted string."""
    return str(s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def export_beancount(year: str, entity_slug: str) -> str:
    """Write `<EXPORT_PATH>/<year>/ledger_<year>_<slug>.beancount`. Returns path."""
    entity = db.get_entity(slug=entity_slug)
    entity_id = entity["id"] if entity else None
    entity_name = entity["name"] if entity else entity_slug

    docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000)
    txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=10000)

    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"ledger_{year}_{entity_slug}.beancount")

    accounts_used: set[str] = set()
    entries: list[tuple[str, str]] = []  # (date, text) — sorted by date at the end

    def _add(date, payee, narration, from_acct, to_acct, amount, meta: dict):
        accounts_used.update((from_acct, to_acct))
        meta_lines = "".join(
            f"  {k}: \"{_esc(v)}\"\n" for k, v in meta.items() if v not in (None, "")
        )
        amt = f"{abs(float(amount)):.2f}"
        block = (
            f"{date} * \"{_esc(payee)}\" \"{_esc(narration)}\"\n"
            f"{meta_lines}"
            f"  {to_acct:<45} {amt} USD\n"
            f"  {from_acct:<45} -{amt} USD\n"
        )
        entries.append((date, block))

    # ── analyzed documents (receipts, invoices, 1099s…) ─────────────────────
    for d in docs:
        if d.get("is_duplicate") or d.get("cross_source_duplicate"):
            continue
        amt = d.get("amount")
        if amt in (None, 0):
            continue
        doc_type = d.get("doc_type") or "other"
        # Statements summarize activity that transactions already capture — skip to avoid double-count
        if doc_type in ("credit_card_statement", "bank_statement", "mortgage_statement"):
            continue
        cat = d.get("category") or "other"
        to_acct = _counter_account(cat, doc_type)
        from_acct = "Assets:Unreconciled"
        # Income flows INTO Assets; expenses flow OUT. Swap for income so the
        # Assets side is positive.
        if cat == "income":
            to_acct, from_acct = from_acct, to_acct
        _add(
            _fmt_date(d.get("date")),
            d.get("vendor") or "Unknown",
            d.get("title") or d.get("description") or doc_type,
            from_acct, to_acct, amt,
            {"paperless": d.get("paperless_doc_id"), "doctype": doc_type,
             "confidence": d.get("confidence")},
        )

    # ── transactions (bank/card/PayPal/Venmo/gmail-derived) ────────────────
    for t in txns:
        amt = t.get("amount")
        if amt in (None, 0):
            continue
        src = t.get("source") or "unknown"
        cat = (t.get("category") or "").lower()
        # Infer direction from sign when category is generic
        if cat not in ("income", "expense", "deduction", "asset"):
            cat = "income" if float(amt) > 0 else "expense"
        src_acct = _source_account(src, t.get("vendor"))
        cnt_acct = _counter_account(cat, t.get("doc_type") or "transaction")
        if cat == "income":
            from_acct, to_acct = cnt_acct, src_acct   # money arrives in the source acct
        else:
            from_acct, to_acct = src_acct, cnt_acct   # money leaves the source acct
        _add(
            _fmt_date(t.get("date")),
            t.get("vendor_normalized") or t.get("vendor") or "Unknown",
            t.get("description") or "",
            from_acct, to_acct, amt,
            {"source": src, "source_id": t.get("source_id"),
             "paperless": t.get("paperless_doc_id")},
        )

    entries.sort(key=lambda e: e[0])

    header = (
        f";; Beancount ledger — {entity_name} — tax year {year}\n"
        f";; Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} by tax-ai-analyzer\n"
        f";; Validate with:  bean-check {os.path.basename(dest)}\n"
        f";; Browse with:    fava {os.path.basename(dest)}\n"
        f";;\n"
        f";; Every posting carries a `paperless:` id where a source document\n"
        f";; exists — open https://www.voipguru.org/tax-paperless/documents/<id>/\n"
        f";; to see the original.\n"
        f";;\n"
        f";; Assets:Unreconciled = movements derived from documents/emails that\n"
        f";; have not yet been matched to a bank feed. Review before relying on.\n\n"
        f'option "title" "{_esc(entity_name)} {year}"\n'
        f'option "operating_currency" "USD"\n\n'
    )
    open_lines = "".join(
        f"{year}-01-01 open {acct} USD\n" for acct in sorted(accounts_used)
    )
    body = "\n".join(block for _, block in entries)

    with open(dest, "w", encoding="utf-8") as f:
        f.write(header + open_lines + "\n" + body)

    logger.info(f"beancount export: {len(entries)} postings → {dest}")
    return dest
