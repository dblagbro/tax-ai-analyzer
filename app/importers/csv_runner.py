"""CSV parsing and job runner shared by all CSV-based import routes."""
import csv
import io
import logging
import re
from datetime import datetime

from app import db

logger = logging.getLogger(__name__)


def parse_csv(csv_bytes: bytes, source: str, entity_id, year: str, col_map: dict):
    txns, errors = [], []
    try:
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for i, row in enumerate(reader):
            try:
                date_val = row.get(col_map.get("date", "Date"), "").strip()
                desc_val = row.get(col_map.get("description", "Description"), "").strip()
                raw_amt = row.get(col_map.get("amount", "Amount"), "0").strip()
                amount_val = float(re.sub(r"[,$\s]", "", raw_amt or "0") or "0")
                if not date_val and not desc_val:
                    continue
                row_year = year
                if not row_year:
                    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
                        try:
                            row_year = str(datetime.strptime(date_val, fmt).year)
                            break
                        except ValueError:
                            pass
                txns.append({
                    "source": source,
                    "source_id": f"{source}_{i}_{date_val}_{amount_val}",
                    "entity_id": entity_id,
                    "tax_year": row_year or "",
                    "date": date_val,
                    "amount": abs(amount_val),
                    "vendor": "",
                    "description": desc_val,
                    "category": "expense" if amount_val < 0 else "income",
                })
            except Exception as e:
                errors.append(f"Row {i+2}: {e}")
    except Exception as e:
        return [], str(e)
    return txns, ("; ".join(errors[:5]) if errors else None)


def run_csv_job(job_id, csv_bytes, source, entity_id, year, col_map):
    db.update_import_job(job_id, status="running",
                         started_at=datetime.utcnow().isoformat())
    txns, err = parse_csv(csv_bytes, source, entity_id, year, col_map)
    if err and not txns:
        db.update_import_job(job_id, status="error", error_msg=err,
                             completed_at=datetime.utcnow().isoformat())
        return
    saved = 0
    for t in txns:
        try:
            db.upsert_transaction(**t)
            saved += 1
        except Exception:
            pass
    db.update_import_job(job_id, status="completed",
                         count_imported=saved,
                         completed_at=datetime.utcnow().isoformat())
    db.log_activity("import_complete", f"{source}: {saved} transactions")
