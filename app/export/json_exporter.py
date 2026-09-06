"""Export transactions and analyzed documents to JSON — data sourced from database."""
import json
import os
from datetime import datetime
from app import db
from app.config import EXPORT_PATH


def export_json(year: str, entity_slug: str, documents: list = None) -> str:
    """Generate structured JSON export. Reads from DB unless documents list passed."""
    filename = f"export_{year}_{entity_slug}.json"
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)

    entity = db.get_entity(slug=entity_slug)
    entity_id = entity["id"] if entity else None

    if documents is None:
        db_docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000)
        txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=10000)
    else:
        db_docs = documents
        txns = []

    # Compute summary totals
    income_total = sum(float(d.get("amount") or 0) for d in db_docs if d.get("category") == "income")
    expense_total = sum(float(d.get("amount") or 0) for d in db_docs if d.get("category") in ("expense", "deduction"))
    txn_total = sum(float(t.get("amount") or 0) for t in txns)

    payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "tax_year": year,
        "entity": entity_slug,
        "entity_name": entity["name"] if entity else entity_slug,
        "summary": {
            "total_income": income_total,
            "total_expenses": expense_total,
            "net": income_total - expense_total,
            "analyzed_document_count": len(db_docs),
            "transaction_count": len(txns),
            "transaction_total": txn_total,
        },
        "analyzed_documents": db_docs,
        "transactions": txns,
    }

    with open(dest, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return dest
