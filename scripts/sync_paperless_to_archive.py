#!/usr/bin/env python3
"""Copy a tax year's documents OUT of Paperless into the tax archive folder.

Why (2026-10-01): the Gmail importer writes receipt PDFs into Paperless's
consume folder; Paperless files them in its own media store. The user's
(and the accountant's) working folder is
    /mnt/s/documents/doc_backup/devin_backup/devin_personal/tax/<year>/
and nothing ever copied the documents back there — so tax/2023/Receipts
looked empty while 274 documents sat in Paperless. The app container mounts
/mnt/s/documents read-only, so this runs on the HOST.

    python3 scripts/sync_paperless_to_archive.py --year 2023 [--dry-run]

What it does
  * reads the analyzed (non-duplicate) documents for the year from the app DB
  * resolves each one's stored file through the Paperless database
  * COPIES it (never moves) into  tax/<year>/<folder by document type>/
  * skips anything whose content is already somewhere under tax/<year>/
    (so documents that came FROM the archive are not copied back)
  * writes  tax/<year>/Receipts/_INDEX_<year>_from_paperless.csv
  * is idempotent: tax/<year>/.paperless_sync.json remembers what was copied
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ARCHIVE_ROOT = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
MEDIA_ORIGINALS = "/mnt/s/documents/tax-organizer/media/documents/originals"
APP_CONTAINER = "tax-ai-analyzer"
PG_CONTAINER = "tax-paperless-postgres"

# document type → folder under tax/<year>/  (follows the existing year folders)
FOLDER_FOR = {
    "receipt": "Receipts", "invoice": "Receipts", "subscription": "Receipts",
    "paypal_transaction": "Receipts", "venmo_transaction": "Receipts", "other": "Receipts",
    "equipment": "Receipts", "vehicle": "Receipts", "farm_expense": "Receipts",
    "capital_improvement": "Receipts",
    "utility_bill": "Receipts/Utilities",
    "charitable_donation": "Receipts/Donations",
    "medical": "Receipts/Medical",
    "insurance": "Receipts/Insurance",
    "credit_card_statement": "CreditCards/EmailNotices",
    "bank_statement": "Banking/EmailNotices",
    "mortgage_statement": "Mortgage",
    "property_tax": "TaxForms",
    "W-2": "TaxForms", "1099-NEC": "TaxForms", "1099-K": "TaxForms", "1099-INT": "TaxForms",
    "1099-DIV": "TaxForms", "1099-MISC": "TaxForms",
}

_APP_QUERY = r'''
import sqlite3, json, sys
c = sqlite3.connect("file:/app/data/financial_analyzer.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
rows = c.execute("""SELECT d.paperless_doc_id AS id, d.doc_type, d.category, d.vendor, d.amount, d.date, d.title,
                           d.confidence, e.slug AS entity,
                           CASE WHEN d.extracted_json LIKE '%"provisional": true%' THEN 1 ELSE 0 END AS provisional
                    FROM analyzed_documents d LEFT JOIN entities e ON e.id = d.entity_id
                    WHERE d.tax_year = ? AND (d.is_duplicate = 0 OR d.is_duplicate IS NULL)
                    ORDER BY d.date""", (sys.argv[1],)).fetchall()
print(json.dumps([dict(r) for r in rows]))
'''


def _run(cmd: list[str], **kw) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd[:4])}…\n{p.stderr[-400:]}")
    return p.stdout


def app_documents(year: str) -> list[dict]:
    out = _run(["sudo", "docker", "exec", APP_CONTAINER, "python3", "-c", _APP_QUERY, year])
    return json.loads(out.strip().splitlines()[-1])


def paperless_files(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    sql = ("select id, coalesce(filename,''), coalesce(original_filename,''), coalesce(checksum,'') "
           f"from documents_document where id in ({','.join(str(int(i)) for i in ids)})")
    out = _run(["sudo", "docker", "exec", PG_CONTAINER, "sh", "-c",
                'psql -U "${POSTGRES_USER:-paperless}" -d "${POSTGRES_DB:-paperless}" -At -F "\t" -c "$0"', sql])
    res = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            res[int(parts[0])] = {"filename": parts[1], "original": parts[2], "md5": parts[3]}
    return res


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip()
    return name[:180] or "document.pdf"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    year_dir = os.path.join(ARCHIVE_ROOT, a.year)
    if not os.path.isdir(year_dir):
        raise SystemExit(f"no such archive folder: {year_dir}")

    docs = app_documents(a.year)
    files = paperless_files([d["id"] for d in docs])

    # content already in the archive (any depth) — never copy those back
    have: dict[str, str] = {}
    for root, _d, fnames in os.walk(year_dir):
        for fn in fnames:
            p = os.path.join(root, fn)
            try:
                have[md5_of(p)] = os.path.relpath(p, year_dir)
            except OSError:
                pass

    # Same document, different bytes: a PDF that had to be re-saved before
    # Paperless would accept it no longer matches by checksum. Fall back to the
    # filename (minus the "<Folder> - " prefix added when it was queued).
    names: dict[str, str] = {}
    for rel in have.values():
        names.setdefault(os.path.basename(rel), rel)

    manifest_path = os.path.join(year_dir, ".paperless_sync.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}

    copied = already = missing = 0
    index_rows = []
    for d in docs:
        info = files.get(d["id"])
        if not info or not info["filename"]:
            missing += 1
            continue
        src = os.path.join(MEDIA_ORIGINALS, info["filename"])
        if not os.path.isfile(src):
            missing += 1
            continue
        folder = FOLDER_FOR.get(d["doc_type"] or "other", "Receipts")
        orig = info["original"] or ""
        by_name = names.get(orig) or (names.get(orig.split(" - ", 1)[1]) if " - " in orig else None)
        if info["md5"] in have:
            rel = have[info["md5"]]
            already += 1
        elif by_name:
            rel = by_name
            already += 1
        else:
            dest_dir = os.path.join(year_dir, folder)
            name = safe_name(info["original"] or os.path.basename(info["filename"]))
            dest = os.path.join(dest_dir, name)
            if os.path.exists(dest):                      # same name, different content
                stem, ext = os.path.splitext(name)
                dest = os.path.join(dest_dir, f"{stem}_p{d['id']}{ext}")
            rel = os.path.relpath(dest, year_dir)
            if not a.dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o664)
            have[info["md5"]] = rel
            manifest[str(d["id"])] = rel
            copied += 1
        index_rows.append({
            "date": d["date"] or "", "vendor": d["vendor"] or "", "type": d["doc_type"] or "",
            "category": d["category"] or "",
            "amount": "" if d["amount"] is None else f"{float(d['amount']):.2f}",
            "entity": d["entity"] or "", "file": rel, "paperless_id": d["id"],
            "verified": "no (keyword rules — check original)" if d["provisional"] else "AI-classified",
            "paperless_link": f"https://www.voipguru.org/tax-paperless/documents/{d['id']}/",
            # A document can legitimately be dated outside its tax year (a
            # January notice about last year's form) — but that is also how a
            # mis-tagged document looks, so make it visible.
            "check": "" if (d["date"] or "")[:4] in (a.year, "") else
                     f"dated {(d['date'] or '')[:4]}, filed under {a.year} — confirm it is about {a.year}",
        })

    if not a.dry_run:
        idx_dir = os.path.join(year_dir, "Receipts")
        os.makedirs(idx_dir, exist_ok=True)
        idx = os.path.join(idx_dir, f"_INDEX_{a.year}_from_paperless.csv")
        with open(idx, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(index_rows[0].keys()) if index_rows else ["date"])
            w.writeheader()
            w.writerows(index_rows)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=1)

    by_folder: dict[str, int] = {}
    for r in index_rows:
        top = os.path.dirname(r["file"]) or "."
        by_folder[top] = by_folder.get(top, 0) + 1
    print(f"{'DRY RUN — ' if a.dry_run else ''}year {a.year}: {len(docs)} documents in Paperless | "
          f"{copied} copied, {already} already in archive, {missing} file missing")
    for k in sorted(by_folder):
        print(f"   {by_folder[k]:>4}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
