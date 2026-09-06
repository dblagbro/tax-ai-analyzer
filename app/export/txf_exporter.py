"""Export to TurboTax TXF format — data sourced from database."""
import os
from datetime import datetime
from app import db
from app.config import EXPORT_PATH

# TXF category codes (simplified subset)
TXF_CODES = {
    "W-2": "N10",                  # Wages
    "1099-NEC": "N11",             # Self-employment income
    "1099-K": "N785",              # Payment card/3rd party network
    "1099-INT": "N11",             # Interest income (simplified)
    "1099-DIV": "N68",             # Dividends
    "mortgage_statement": "N32",   # Mortgage interest
    "charitable_donation": "N57",  # Charitable contributions
    "medical": "N52",              # Medical expenses
    "property_tax": "N36",         # Real estate taxes
    "vehicle": "N542",             # Vehicle/transportation
    "utility_bill": "N276",        # Utilities (Schedule C)
    "equipment": "N542",           # Equipment / depreciation
    "subscription": "N276",        # Subscriptions / misc
    "business_expense": "N276",    # General business expense
    "farm_expense": "N276",        # Farm expense
    "other": "N276",
}


def export_txf(year: str, entity_slug: str, documents: list = None) -> str:
    """Generate TurboTax TXF file. Reads from DB unless documents list passed."""
    filename = f"export_{year}_{entity_slug}.txf"
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)

    if documents is None:
        entity = db.get_entity(slug=entity_slug)
        entity_id = entity["id"] if entity else None
        db_docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000)
        txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=10000)
        documents = []
        for d in db_docs:
            documents.append({
                "date": d.get("date"),
                "vendor": d.get("vendor"),
                "amount": d.get("amount"),
                "doc_type": d.get("doc_type"),
                "description": "",
            })
        for t in txns:
            documents.append({
                "date": t.get("date"),
                "vendor": t.get("vendor"),
                "amount": t.get("amount"),
                "doc_type": t.get("doc_type"),
                "description": t.get("description"),
            })

    lines = [
        "V042",
        "ATax Organizer Export",
        f"D{datetime.utcnow().strftime('%m/%d/%Y')}",
        "^",
    ]

    for doc in documents:
        amount = float(doc.get("amount") or 0)
        date = doc.get("date") or f"{year}-01-01"
        try:
            dt = datetime.strptime(str(date)[:10], "%Y-%m-%d")
            date_str = dt.strftime("%m/%d/%Y")
        except Exception:
            date_str = f"01/01/{year}"

        code = TXF_CODES.get(doc.get("doc_type", "other"), "N276")
        vendor = (doc.get("vendor") or "Unknown")[:40]

        lines += [
            f"T{code}",
            "N1",
            "C1",
            "L1",
            f"P{vendor}",
            f"D{date_str}",
            f"${amount:.2f}",
            "^",
        ]

    with open(dest, "w") as f:
        f.write("\n".join(lines))
    return dest
