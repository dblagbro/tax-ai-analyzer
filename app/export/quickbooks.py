"""Export to QuickBooks formats.

- IIF (classic, QuickBooks Desktop): tab-delimited, deprecated for QB Online (2021+)
- QBO (Web Connect): OFX-based, accepted by both QB Desktop and QB Online
"""
import os
from datetime import datetime

from app import db
from app.config import EXPORT_PATH


# ── shared helpers ─────────────────────────────────────────────────────────────

def _load_rows(year: str, entity_slug: str) -> list[dict]:
    """Pull analyzed docs + transactions for a year/entity into one normalized list."""
    entity = db.get_entity(slug=entity_slug)
    entity_id = entity["id"] if entity else None
    db_docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000)
    txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=10000)

    rows: list[dict] = []
    for d in db_docs:
        rows.append({
            "date": d.get("date"),
            "vendor": d.get("vendor"),
            "amount": d.get("amount"),
            "doc_type": d.get("doc_type"),
            "category": d.get("category"),
            "description": d.get("title") or "",
            "source": "document",
        })
    for t in txns:
        rows.append({
            "date": t.get("date"),
            "vendor": t.get("vendor"),
            "amount": t.get("amount"),
            "doc_type": t.get("doc_type"),
            "category": t.get("category"),
            "description": t.get("description"),
            "source": f"txn:{t.get('source', '')}",
        })
    return rows


def _is_income(row: dict) -> bool:
    """Decide income vs expense from category and doc_type."""
    cat = (row.get("category") or "").lower()
    if cat in ("income", "revenue"):
        return True
    if cat in ("expense", "expenses", "deduction", "deductions"):
        return False
    dt = (row.get("doc_type") or "").lower()
    income_doc_types = {"w-2", "1099-nec", "1099-k", "1099-int", "1099-div",
                        "1099-misc", "invoice"}
    if dt in income_doc_types:
        return True
    # Fall back to sign: positive amount = income, negative = expense
    try:
        return float(row.get("amount") or 0) > 0
    except (TypeError, ValueError):
        return False


def _fmt_iif_date(date, year_fallback: str) -> str:
    try:
        dt = datetime.strptime(str(date)[:10], "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return f"01/01/{year_fallback}"


def _fmt_qbo_date(date, year_fallback: str) -> str:
    """OFX/QBO uses YYYYMMDD[HHMMSS] format."""
    try:
        dt = datetime.strptime(str(date)[:10], "%Y-%m-%d")
        return dt.strftime("%Y%m%d")
    except Exception:
        return f"{year_fallback}0101"


def _map_account(doc_type: str, category: str) -> str:
    mapping = {
        "W-2": "Wages Income",
        "1099-NEC": "Consulting Income",
        "1099-K": "eBay Sales Income",
        "1099-INT": "Interest Income",
        "1099-DIV": "Dividend Income",
        "1099-MISC": "Other Income",
        "utility_bill": "Utilities",
        "subscription": "Subscriptions",
        "equipment": "Equipment",
        "mortgage_statement": "Mortgage Interest",
        "property_tax": "Property Taxes",
        "vehicle": "Vehicle Expense",
        "farm_expense": "Farm Expense",
        "medical": "Medical Expense",
        "charitable_donation": "Charitable Contributions",
        "invoice": "Consulting Income",
        "business_expense": "Business Expense",
        "bank_statement": "Bank Account",
        "bill": "Utilities",
        "receipt": "Miscellaneous Expense",
    }
    if doc_type in mapping:
        return mapping[doc_type]
    if (category or "").lower() in ("income", "revenue"):
        return "Other Income"
    return "Miscellaneous Expense"


# ── IIF (classic, QuickBooks Desktop) ──────────────────────────────────────────

def export_iif(year: str, entity_slug: str, documents: list = None) -> str:
    """Generate QuickBooks IIF file. Reads from DB unless documents list passed.

    Note: IIF was deprecated for QuickBooks Online in 2021 — use export_qbo() for
    modern QB Online/Desktop Web Connect imports.
    """
    filename = f"export_{year}_{entity_slug}.iif"
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)

    rows = documents if documents is not None else _load_rows(year, entity_slug)

    lines = [
        "!TRNS\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tMEMO",
        "!SPL\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tMEMO",
        "!ENDTRNS",
    ]

    for row in rows:
        amt = abs(float(row.get("amount") or 0))
        if amt == 0:
            continue
        is_income = _is_income(row)
        txn_type = "DEPOSIT" if is_income else "CHECK"
        date_str = _fmt_iif_date(row.get("date"), year)
        vendor = (row.get("vendor") or "Unknown").replace("\t", " ")
        memo = (row.get("description") or "").replace("\t", " ")[:64]
        category_acct = _map_account(row.get("doc_type", "other"), row.get("category", ""))

        # TRNS: money lands in "Checking"; SPL: the offsetting income/expense account
        if is_income:
            # DEPOSIT: debit Checking (+), credit income account (-)
            lines.append(f"TRNS\t{txn_type}\t{date_str}\tChecking\t{vendor}\t{amt:.2f}\t{memo}")
            lines.append(f"SPL\t{txn_type}\t{date_str}\t{category_acct}\t{vendor}\t{-amt:.2f}\t{memo}")
        else:
            # CHECK: credit Checking (-), debit expense account (+)
            lines.append(f"TRNS\t{txn_type}\t{date_str}\tChecking\t{vendor}\t{-amt:.2f}\t{memo}")
            lines.append(f"SPL\t{txn_type}\t{date_str}\t{category_acct}\t{vendor}\t{amt:.2f}\t{memo}")
        lines.append("ENDTRNS")

    with open(dest, "w") as f:
        f.write("\n".join(lines) + "\n")
    return dest


# ── QBO / Web Connect (OFX-based, QB Online + Desktop) ─────────────────────────

def export_qbo(year: str, entity_slug: str, documents: list = None) -> str:
    """Generate a QBO (Web Connect) file — OFX 1.0.3 + Intuit extensions.

    This is the format QuickBooks (Desktop + Online) accepts for bank-statement
    imports as of 2024. Each transaction becomes a <STMTTRN> block inside a
    single checking-account statement.
    """
    filename = f"export_{year}_{entity_slug}.qbo"
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)

    rows = documents if documents is not None else _load_rows(year, entity_slug)

    # Filter to rows with dates + amounts (QBO requires both)
    usable = [
        r for r in rows
        if r.get("amount") is not None and float(r.get("amount") or 0) != 0 and r.get("date")
    ]

    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    dt_start = f"{year}0101"
    dt_end = f"{year}1231"

    # Stable account/routing IDs derived from entity_slug so reimports match up
    import hashlib
    acct_hash = hashlib.sha1(entity_slug.encode()).hexdigest()[:12].upper()
    routing_id = "000000000"
    acct_id = f"{entity_slug.upper()}_{year}"[:22]

    lines = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:103",
        "SECURITY:NONE",
        "ENCODING:USASCII",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
        "<OFX>",
        "<SIGNONMSGSRSV1><SONRS>",
        f"<STATUS><CODE>0<SEVERITY>INFO</STATUS>",
        f"<DTSERVER>{now}",
        "<LANGUAGE>ENG",
        "<FI><ORG>TaxOrganizer<FID>9999</FI>",
        "<INTU.BID>9999",
        "</SONRS></SIGNONMSGSRSV1>",
        "<BANKMSGSRSV1><STMTTRNRS>",
        f"<TRNUID>{acct_hash}",
        "<STATUS><CODE>0<SEVERITY>INFO</STATUS>",
        "<STMTRS>",
        "<CURDEF>USD",
        f"<BANKACCTFROM><BANKID>{routing_id}<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{dt_start}",
        f"<DTEND>{dt_end}",
    ]

    for i, row in enumerate(usable):
        amt = float(row.get("amount") or 0)
        is_income = _is_income(row)
        # Normalize amount sign: income positive, expense negative
        signed_amt = abs(amt) if is_income else -abs(amt)
        trntype = "CREDIT" if is_income else "DEBIT"

        posted = _fmt_qbo_date(row.get("date"), year)
        vendor = (row.get("vendor") or "Unknown").replace("<", "").replace(">", "").replace("&", "and")
        memo = (row.get("description") or "").replace("<", "").replace(">", "").replace("&", "and")[:254]
        # FITID must be unique per account — derive stable ID
        fitid_src = f"{entity_slug}|{row.get('date')}|{amt}|{vendor}|{i}"
        fitid = hashlib.sha1(fitid_src.encode()).hexdigest()[:22].upper()

        lines.extend([
            "<STMTTRN>",
            f"<TRNTYPE>{trntype}",
            f"<DTPOSTED>{posted}",
            f"<TRNAMT>{signed_amt:.2f}",
            f"<FITID>{fitid}",
            f"<NAME>{vendor[:32]}",
            f"<MEMO>{memo[:254]}" if memo else "",
            "</STMTTRN>",
        ])

    lines.extend([
        "</BANKTRANLIST>",
        f"<LEDGERBAL><BALAMT>0.00<DTASOF>{dt_end}</LEDGERBAL>",
        "</STMTRS>",
        "</STMTTRNRS>",
        "</BANKMSGSRSV1>",
        "</OFX>",
    ])

    with open(dest, "w") as f:
        f.write("\n".join(l for l in lines if l != "") + "\n")
    return dest
