"""
Local filesystem importer.

Scans a directory path (accessible from the container) for:
  - PDF files → queued for Paperless-ngx consumption (copied to consume dir)
  - CSV files → imported as transactions (auto-format detection)
  - OFX/QFX files → imported as transactions

Naming convention YYYY_MM_DD_description[-cost].ext is auto-detected.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

PDF_EXTENSIONS  = {".pdf"}
CSV_EXTENSIONS  = {".csv"}
OFX_EXTENSIONS  = {".ofx", ".qfx", ".qbo"}
ALL_EXTENSIONS  = PDF_EXTENSIONS | CSV_EXTENSIONS | OFX_EXTENSIONS

# Regex: 2022_01_15_vendor-description-99.99 or 2022_01_15_description
_FNAME_RE = re.compile(
    r"^(\d{4})[-_](\d{2})[-_](\d{2})[-_](.+?)(?:[-_]([\d.]+))?$",
    re.IGNORECASE,
)


def _parse_filename(stem: str) -> dict:
    """
    Parse a filename stem like '2023_03_15_spectrum-internet-89.99' into
    {date, tax_year, description, amount}. Returns {} if no match.
    """
    m = _FNAME_RE.match(stem)
    if not m:
        return {}
    y, mo, d, desc, amt = m.groups()
    date = f"{y}-{mo}-{d}"
    amount = float(amt) if amt else None
    description = desc.replace("-", " ").replace("_", " ").strip()
    return {"date": date, "tax_year": y, "description": description, "amount": amount}


def detect_entity_from_path(path: str, entities: list[dict]) -> dict | None:
    """
    Try to identify which entity a folder path belongs to by matching
    path components against entity slugs, names, and DBA aliases.

    Returns the best-matching entity dict, or None if no match.
    """
    import re
    path_lower = path.lower().replace("\\", "/")
    # Normalise: split path into slug-like tokens
    path_parts = set(re.split(r"[/\-_ ]+", path_lower))

    best = None
    best_score = 0

    for ent in entities:
        score = 0
        slug = (ent.get("slug") or "").lower()
        name = (ent.get("name") or "").lower()
        display = (ent.get("display_name") or "").lower()

        # Direct slug/name match in path components
        if slug and slug in path_lower:
            score += 10
        if name and name in path_lower:
            score += 8
        # Token-level match (handles e.g. "martinfeld" in path for martinfeld_ranch)
        for token in re.split(r"[_\- ]+", slug):
            if len(token) >= 4 and token in path_lower:
                score += 5
        for token in re.split(r"[_\- ]+", name):
            if len(token) >= 4 and token in path_lower:
                score += 3

        if score > best_score:
            best_score = score
            best = ent

    return best if best_score >= 3 else None


def scan_directory(path: str, recursive: bool = True) -> list[dict]:
    """
    Walk directory and return list of file info dicts:
    {path, name, ext, size, mtime}
    """
    results = []
    if not os.path.isdir(path):
        raise ValueError(f"Not a directory: {path}")

    if recursive:
        for root, _dirs, files in os.walk(path):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext in ALL_EXTENSIONS:
                    fpath = os.path.join(root, fname)
                    try:
                        st = os.stat(fpath)
                        results.append({
                            "path": fpath,
                            "name": fname,
                            "ext": ext,
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        })
                    except OSError:
                        pass
    else:
        for fname in sorted(os.listdir(path)):
            fpath = os.path.join(path, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in ALL_EXTENSIONS:
                try:
                    st = os.stat(fpath)
                    results.append({
                        "path": fpath,
                        "name": fname,
                        "ext": ext,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
                except OSError:
                    pass
    return results


def import_directory(
    path: str,
    entity_id: Optional[int] = None,
    default_year: Optional[str] = None,
    consume_path: Optional[str] = None,
    recursive: bool = True,
) -> dict:
    """
    Import all financial files from a directory.

    PDFs: copied to consume_path (for Paperless ingestion) if provided.
    CSVs: parsed as transactions using bank_csv auto-detection.
    OFX/QFX: parsed as transactions using ofx_importer.

    Returns:
    {
        "pdfs_queued": int,
        "transactions": [parsed transaction dicts],
        "errors": [str],
        "scanned": int,
    }
    """
    from app.importers.bank_csv import parse_csv as parse_bank_csv
    from app.importers.ofx_importer import parse_ofx

    files = scan_directory(path, recursive=recursive)
    pdfs_queued = 0
    transactions: list[dict] = []
    errors: list[str] = []

    for fi in files:
        ext = fi["ext"]
        fpath = fi["path"]
        fname = fi["name"]
        stem = os.path.splitext(fname)[0]
        parsed_name = _parse_filename(stem)
        year = parsed_name.get("tax_year") or default_year

        try:
            if ext in PDF_EXTENSIONS:
                if consume_path and os.path.isdir(consume_path):
                    dest = os.path.join(consume_path, fname)
                    if not os.path.exists(dest):
                        shutil.copy2(fpath, dest)
                        pdfs_queued += 1
                    else:
                        # Already copied — still count it
                        pdfs_queued += 1

            elif ext in CSV_EXTENSIONS:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                txns = parse_bank_csv(content, entity_id=entity_id, default_year=year)
                transactions.extend(txns)

            elif ext in OFX_EXTENSIONS:
                with open(fpath, "rb") as f:
                    content = f.read()
                txns = parse_ofx(content, entity_id=entity_id, default_year=year)
                transactions.extend(txns)

        except Exception as e:
            errors.append(f"{fname}: {e}")
            logger.warning(f"local_fs: error on {fpath}: {e}")

    return {
        "pdfs_queued": pdfs_queued,
        "transactions": transactions,
        "errors": errors,
        "scanned": len(files),
    }
