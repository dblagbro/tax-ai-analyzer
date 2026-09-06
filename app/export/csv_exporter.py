"""Export financial data to CSV from database."""
import csv
import os
from app import db
from app.config import EXPORT_PATH


def export_transactions_csv(year: str, entity_slug: str) -> str:
    entity = db.get_entity(slug=entity_slug)
    entity_id = entity["id"] if entity else None
    docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000)
    txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=10000)

    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"transactions_{year}_{entity_slug}.csv"
    dest = os.path.join(dest_dir, filename)

    fieldnames = ["date", "vendor", "amount", "category", "doc_type", "source",
                  "description", "tax_year", "entity", "confidence", "paperless_doc_id"]

    rows = []
    for doc in docs:
        rows.append({
            "date": doc["date"] or "",
            "vendor": doc["vendor"] or "",
            "amount": doc["amount"] or "",
            "category": doc["category"] or "",
            "doc_type": doc["doc_type"] or "",
            "source": "paperless",
            "description": "",
            "tax_year": doc["tax_year"] or year,
            "entity": entity_slug,
            "confidence": f"{(doc['confidence'] or 0):.2f}",
            "paperless_doc_id": doc["paperless_doc_id"] or "",
        })
    for txn in txns:
        rows.append({
            "date": txn["date"] or "",
            "vendor": txn["vendor"] or "",
            "amount": txn["amount"] or "",
            "category": txn["category"] or "",
            "doc_type": txn["doc_type"] or "",
            "source": txn["source"],
            "description": txn["description"] or "",
            "tax_year": txn["tax_year"] or year,
            "entity": entity_slug,
            "confidence": "",
            "paperless_doc_id": txn["paperless_doc_id"] or "",
        })

    rows.sort(key=lambda x: x["date"] or "")
    with open(dest, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return dest


# Legacy shim: old callers pass (year, entity, documents_list)
def export_csv(year: str, entity: str, documents: list = None) -> str:
    if documents is not None:
        # Legacy path: write from in-memory list
        dest_dir = os.path.join(EXPORT_PATH, year)
        os.makedirs(dest_dir, exist_ok=True)
        filename = f"transactions_{year}_{entity}.csv"
        dest = os.path.join(dest_dir, filename)
        fieldnames = ["doc_id", "title", "date", "vendor", "amount", "doc_type",
                      "category", "entity", "tax_year", "description", "tags"]
        with open(dest, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for doc in documents:
                writer.writerow({k: doc.get(k, "") for k in fieldnames})
        return dest
    return export_transactions_csv(year, entity)
