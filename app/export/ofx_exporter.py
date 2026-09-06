"""Export to OFX/QFX format (Quicken/bank standard) — data sourced from database."""
import os
from datetime import datetime
from app import db
from app.config import EXPORT_PATH


def export_ofx(year: str, entity_slug: str, documents: list = None) -> str:
    """Generate OFX/QFX SGML file. Reads from DB unless documents list passed."""
    filename = f"export_{year}_{entity_slug}.ofx"
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
                "category": d.get("category"),
                "description": "",
            })
        for t in txns:
            documents.append({
                "date": t.get("date"),
                "vendor": t.get("vendor"),
                "amount": t.get("amount"),
                "category": t.get("category"),
                "description": t.get("description"),
            })

    now = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    start = f"{year}0101"
    end = f"{year}1231"

    lines = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:NONE",
        "ENCODING:USASCII",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
        "<OFX>",
        "<SIGNONMSGSRSV1>",
        "<SONRS>",
        "<STATUS><CODE>0<SEVERITY>INFO</STATUS>",
        f"<DTSERVER>{now}",
        "<LANGUAGE>ENG",
        "</SONRS>",
        "</SIGNONMSGSRSV1>",
        "<BANKMSGSRSV1>",
        "<STMTTRNRS>",
        "<TRNUID>1",
        "<STATUS><CODE>0<SEVERITY>INFO</STATUS>",
        "<STMTRS>",
        "<CURDEF>USD",
        "<BANKACCTFROM><BANKID>999999999<ACCTID>TAX-ORGANIZER<ACCTTYPE>CHECKING</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{start}",
        f"<DTEND>{end}",
    ]

    for i, doc in enumerate(documents):
        amount = float(doc.get("amount") or 0)
        date = doc.get("date") or f"{year}-01-01"
        try:
            dt = datetime.strptime(str(date)[:10], "%Y-%m-%d")
            date_str = dt.strftime("%Y%m%d")
        except Exception:
            date_str = f"{year}0101"
        vendor = (doc.get("vendor") or "Unknown")[:32]
        memo = (doc.get("description") or "")[:64]
        ttype = "DEBIT" if doc.get("category") in ("expense", "deduction") else "CREDIT"

        lines += [
            "<STMTTRN>",
            f"<TRNTYPE>{ttype}",
            f"<DTPOSTED>{date_str}",
            f"<TRNAMT>{-abs(amount):.2f}" if ttype == "DEBIT" else f"<TRNAMT>{abs(amount):.2f}",
            f"<FITID>TAX{year}{i:05d}",
            f"<NAME>{vendor}",
            f"<MEMO>{memo}",
            "</STMTTRN>",
        ]

    lines += [
        "</BANKTRANLIST>",
        "</STMTRS>",
        "</STMTTRNRS>",
        "</BANKMSGSRSV1>",
        "</OFX>",
    ]

    with open(dest, "w") as f:
        f.write("\n".join(lines))
    return dest
