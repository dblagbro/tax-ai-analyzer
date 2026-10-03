#!/usr/bin/env python3
"""File the Gmail importer's mirrored PDFs into the tax archive folder.

The Gmail importer (app/importers/gmail/runner.py) mirrors every PDF it saves
to   /mnt/s/documents/tax-organizer/export/gmail_pdfs/<year>/<entity>/
with a _manifest.csv row per file. This host-side script copies them into
    /mnt/s/documents/doc_backup/devin_backup/devin_personal/tax/<year>/
sorted the way the earlier years are organised, and writes an index CSV.
Copy-only and idempotent — safe to run repeatedly while an import is going.

    python3 scripts/file_gmail_mirror.py --year 2023 [--dry-run]

Only emails actually DATED in <year> are filed (a January notice about the
previous year stays out unless its date is in <year>).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
from email.utils import parsedate_to_datetime

ARCHIVE_ROOT = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
MIRROR_ROOT = "/mnt/s/documents/tax-organizer/export/gmail_pdfs"

_ISSUER = re.compile(
    r"capital ?one|citi(?:bank| ?cards?)?\b|discover|amex|american express|synchrony|merrick|td (?:bank|card)|"
    r"\bchase\b|penfed|pentagon federal|citizens|barclay|credit one|paypal credit|\bbhg\b|bankers healthcare|"
    r"lendingclub|lending club|upgrade|home depot credit|lowe'?s (?:credit|advantage)|comenity|cabela|walmart rewards", re.I)
_NOTICE = re.compile(r"statement|payment|due\b|posted|autopay|balance|minimum|scheduled", re.I)
_BANK = re.compile(r"usalliance|us alliance|u\.?s\.? bank|chime|ally bank", re.I)
_MORTGAGE = re.compile(r"wells fargo|mortgage|escrow", re.I)
_UTILITY = re.compile(
    r"verizon|vzw|spectrum|charter comm|central hudson|cenhud|paraco|nyseg|earthlink|net10|tracfone|"
    r"optimum|at&t|t-mobile|suburban propane|heating oil|anderman", re.I)
_DONATION = re.compile(r"donat|gofundme|wikimedia|aclu|red cross|charit|foundation 451|non-?profit", re.I)
_MEDICAL = re.compile(r"medical|health(?!equity)|pharmac|dental|hospital|clinic|acupunct|labcorp|quest diag|\bcvs\b", re.I)
_TAX = re.compile(r"\b1099|\bw-?2\b|\b1098|\b5498|tax (?:document|form|statement|return)|\birs\b|turbotax|h&r block", re.I)
_INVEST = re.compile(r"\be\*?trade\b|morgan stanley|fidelity|schwab|vanguard|robinhood|empower|great[- ]west|401\(?k\)?|"
                     r"healthequity|\bhsa\b|\bubs\b|brokerage|trade confirmation", re.I)   # \b: "squaretrade" is not E*TRADE
_INSURANCE = re.compile(r"travelers|lemonade|allstate|geico|progressive|state farm|insurance|policy (?:renew|document)", re.I)


def folder_for(doc_type: str, vendor: str, subject: str, sender: str) -> str:
    hay = f"{vendor} {sender}"
    text = f"{subject} {hay}"
    if _TAX.search(subject):
        return "TaxForms/EmailNotices"
    if _INVEST.search(hay) or _INVEST.search(subject):
        return "Investments_HSA/EmailNotices"
    if _MORTGAGE.search(hay) and _NOTICE.search(subject):
        return "Mortgage/EmailNotices"
    if _ISSUER.search(hay) and (_NOTICE.search(subject) or doc_type in ("statement", "bill")):
        return "CreditCards/EmailNotices"
    if _BANK.search(hay):
        return "Banking/EmailNotices"
    if _UTILITY.search(text):
        return "Receipts/Utilities"
    if _DONATION.search(text):
        return "Receipts/Donations"
    if _INSURANCE.search(text):
        return "Receipts/Insurance"
    if _MEDICAL.search(text):
        return "Receipts/Medical"
    return "Receipts"


def email_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        m = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", raw or "")
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    mirror = os.path.join(MIRROR_ROOT, a.year)
    year_dir = os.path.join(ARCHIVE_ROOT, a.year)
    man = os.path.join(mirror, "_manifest.csv")
    if not os.path.isfile(man):
        raise SystemExit(f"no manifest at {man} — has the Gmail import run?")
    if not os.path.isdir(year_dir):
        raise SystemExit(f"no such archive folder: {year_dir}")

    with open(man, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # names a person has already filed, renamed or set aside by hand — never re-copy them
    skip_path = os.path.join(year_dir, ".filed_skip")
    skip = set()
    if os.path.isfile(skip_path):
        with open(skip_path, encoding="utf-8") as f:
            skip = {l.strip() for l in f if l.strip()}

    copied = present = wrong_year = missing = 0
    counts: dict[str, int] = {}
    index = []
    seen_files = set()
    for r in rows:
        fname = r["file"]
        if fname in seen_files:
            continue
        seen_files.add(fname)
        if fname in skip:
            continue
        d = email_date(r["email_date"]) or email_date(fname)
        if d[:4] != a.year:
            wrong_year += 1
            continue
        src = os.path.join(mirror, r["entity"] or "personal", fname)
        if not os.path.isfile(src):
            missing += 1
            continue
        folder = folder_for(r["doc_type"], r["vendor"], r["subject"], r["sender"])
        dest = os.path.join(year_dir, folder, fname)
        if os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(src):
            present += 1
        else:
            if not a.dry_run:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o664)
            copied += 1
        counts[folder] = counts.get(folder, 0) + 1
        index.append({"date": d, "vendor": r["vendor"], "type": r["doc_type"], "amount": r["amount"],
                      "entity": r["entity"], "subject": r["subject"], "from": r["sender"],
                      "file": os.path.join(folder, fname)})

    if not a.dry_run and index:
        idx = os.path.join(year_dir, "Receipts", f"_INDEX_{a.year}_gmail.csv")
        os.makedirs(os.path.dirname(idx), exist_ok=True)
        index.sort(key=lambda x: (x["date"], x["vendor"]))
        with open(idx, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(index[0].keys()))
            w.writeheader()
            w.writerows(index)

    months: dict[str, int] = {}
    for i in index:
        months[i["date"][:7]] = months.get(i["date"][:7], 0) + 1
    print(f"{'DRY RUN — ' if a.dry_run else ''}{a.year}: {len(index)} emails filed "
          f"({copied} copied now, {present} already there), {wrong_year} dated outside {a.year}, {missing} file missing")
    for k in sorted(counts):
        print(f"   {counts[k]:>5}  {k}")
    print("   by month:", " ".join(f"{k[5:]}:{v}" for k, v in sorted(months.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
