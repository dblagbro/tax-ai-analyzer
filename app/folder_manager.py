"""
Tax folder scanner, standardizer, and Paperless coverage checker.

Works on the source archive: /mnt/s/documents/doc_backup/devin_backup/devin_personal/tax/
Paperless consume queue:      /mnt/s/documents/tax-organizer/consume/

Standardization rules
---------------------
Folder names are normalized to canonical CamelCase identifiers.
Files are optionally renamed to YYYY_MM_DD_description[-cost].ext
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Canonical folder name mappings ───────────────────────────────────────────
# Each entry: (regex_pattern, canonical_name)
# Patterns are matched case-insensitively against the folder basename.
FOLDER_RULES: list[tuple[str, str]] = [
    # Bank statements
    (r"^usalliance[_\s\-]?statements?$",  "USAlliance"),
    (r"^usalliance$",                      "USAlliance"),
    (r"^us[\s_\-]alliance.*",             "USAlliance"),
    # Phone / cell
    (r"^verizon[\s_\-]cell$",             "VerizonCell"),
    (r"^verizon[\s_\-]?wireless$",        "VerizonCell"),
    # Credit cards — top-level folder
    (r"^credit[\s_\-]?cards?$",           "CreditCards"),
    # Credit card issuers — sub-folders
    (r"^discover[\s_\-]?card$",           "DiscoverCard"),
    (r"^citi[\s_\-]?card$",               "CitiCard"),
    (r"^home[\s_\-]?depot$",              "HomeDepot"),
    (r"^lowe['e]?s$",                     "Lowes"),
    (r"^capital[\s_\-]?one$",             "CapitalOne"),
    (r"^amex|american[\s_\-]?express",    "AmericanExpress"),
    # Receipts / expenses
    (r"^receipts?$",                      "Receipts"),
    (r"^amazon[\s_\-]?orders?$",          "AmazonOrders"),
    (r"^central[\s_\-]?hudson$",          "CentralHudson"),
    (r"^spectrum$",                        "Spectrum"),
    # Other
    (r"^professional[\s_\-]?cert.*",      "ProfessionalCertifications"),
    (r"^etrade$",                          "ETrade"),
    (r"^vehicles?$",                       "Vehicles"),
    (r"^hsa[\s_\-]?expense$",             "HSA_Expense"),
    (r"^ebay$",                            "eBay"),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), canon) for pat, canon in FOLDER_RULES]


def canonical_name(name: str) -> Optional[str]:
    """Return canonical folder name if a rule matches, else None."""
    for pattern, canon in _COMPILED:
        if pattern.match(name):
            return canon
    return None


def has_inconsistency(name: str) -> bool:
    """True if name has spaces, leading/trailing whitespace, or matches a
    known pattern but under a non-canonical spelling."""
    if name != name.strip():
        return True
    if " " in name:
        return True
    cn = canonical_name(name)
    if cn and cn != name:
        return True
    return False


# ── Folder scanning ───────────────────────────────────────────────────────────

def scan_tree(root: str, max_depth: int = 4) -> dict:
    """
    Walk root and return a nested tree of folders with metadata.

    Each node:
        {
          "path": str,          # absolute path
          "name": str,          # basename
          "canonical": str|None, # what this should be renamed to (or None if ok)
          "issue": str|None,    # human-readable reason for inconsistency
          "pdf_count": int,
          "other_count": int,
          "children": [...],    # sub-nodes
        }
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return {"error": f"Not a directory: {root}"}

    def _node(p: Path, depth: int) -> dict:
        name = p.name
        cn = canonical_name(name)
        issue = None
        if " " in name:
            suggestion = cn or "".join(w[0].upper() + w[1:] if w else "" for w in name.split())
            issue = f'Has spaces — suggest "{suggestion}"'
        elif cn and cn != name:
            issue = f'Inconsistent spelling — canonical is "{cn}"'

        pdf_count = sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".tiff"))
        other_count = sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg", ".tiff"))
        children = []
        if depth < max_depth:
            for child in sorted(p.iterdir()):
                if child.is_dir():
                    children.append(_node(child, depth + 1))

        return {
            "path": str(p),
            "name": name,
            "canonical": cn if cn != name else None,
            "issue": issue,
            "pdf_count": pdf_count,
            "other_count": other_count,
            "children": children,
        }

    return _node(root_path, 0)


def find_inconsistencies(root: str) -> list[dict]:
    """
    Return a flat list of all folders under root that have naming issues.
    Each item: { path, name, canonical, issue, pdf_count, depth }
    """
    results = []

    def _walk(p: Path, depth: int):
        for child in sorted(p.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            cn = canonical_name(name)
            issue = None
            if " " in name:
                # Preserve existing capitalisation per word (NACR → NACR, not Nacr)
                suggestion = cn or "".join(w[0].upper() + w[1:] if w else "" for w in name.split())
                issue = f'Has spaces — suggest "{suggestion}"'
            elif cn and cn != name:
                issue = f'Inconsistent — canonical is "{cn}"'

            if issue:
                pdf_count = sum(1 for f in child.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
                # Compute the best suggested name
                if " " in name:
                    best = cn or "".join(w[0].upper() + w[1:] if w else "" for w in name.split())
                else:
                    best = cn or name
                results.append({
                    "path": str(child),
                    "name": name,
                    "canonical": best,
                    "issue": issue,
                    "pdf_count": pdf_count,
                    "depth": depth,
                })
            _walk(child, depth + 1)

    _walk(Path(root), 0)
    return results


# ── Rename operations ─────────────────────────────────────────────────────────

def rename_folder(src: str, new_name: str, dry_run: bool = True) -> dict:
    """
    Rename folder basename to new_name. Returns action result dict.
    Refuses if destination already exists (unless it's a merge situation).
    """
    src_path = Path(src)
    if not src_path.is_dir():
        return {"status": "error", "message": f"Not a directory: {src}"}

    dst_path = src_path.parent / new_name
    if dst_path.exists():
        if dst_path == src_path:
            return {"status": "noop", "message": "Source and destination are identical"}
        # Offer to merge
        return {
            "status": "conflict",
            "message": f'Destination "{dst_path}" already exists. Use merge=true to move contents.',
            "src": str(src_path),
            "dst": str(dst_path),
        }

    if dry_run:
        return {"status": "dry_run", "src": str(src_path), "dst": str(dst_path)}

    try:
        src_path.rename(dst_path)
        logger.info(f"Renamed: {src_path} → {dst_path}")
        return {"status": "renamed", "src": str(src_path), "dst": str(dst_path)}
    except Exception as e:
        return {"status": "error", "message": str(e), "src": str(src_path)}


def merge_folders(src: str, dst: str, dry_run: bool = True) -> dict:
    """
    Move all files/subfolders from src into dst, then remove src if empty.
    Used when renaming would collide with an existing folder.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    if not src_path.is_dir():
        return {"status": "error", "message": f"Source not a directory: {src}"}
    if not dst_path.is_dir():
        return {"status": "error", "message": f"Destination not a directory: {dst}"}

    moved = []
    skipped = []
    conflicts = []

    for item in src_path.iterdir():
        target = dst_path / item.name
        if target.exists():
            conflicts.append(str(item))
        else:
            if not dry_run:
                shutil.move(str(item), str(target))
            moved.append(str(item))

    if not dry_run and not conflicts:
        try:
            src_path.rmdir()
            moved.append(f"[removed empty dir] {src_path}")
        except OSError:
            pass  # not empty, leave it

    return {
        "status": "dry_run" if dry_run else "merged",
        "moved": moved,
        "skipped": skipped,
        "conflicts": conflicts,
    }


def apply_all_auto_renames(root: str, dry_run: bool = True) -> list[dict]:
    """
    Find all folders with canonical name differences and rename/merge them all.
    Returns list of action results.
    """
    issues = find_inconsistencies(root)
    results = []
    for item in issues:
        src = item["path"]
        new_name = item["canonical"]
        dst = str(Path(src).parent / new_name)
        if Path(dst).exists():
            result = merge_folders(src, dst, dry_run=dry_run)
        else:
            result = rename_folder(src, new_name, dry_run=dry_run)
        result["original_name"] = item["name"]
        result["canonical_name"] = new_name
        results.append(result)
    return results


# ── Paperless coverage check ─────────────────────────────────────────────────

def _paperless_doc_titles(paperless_token: str, paperless_url: str) -> set[str]:
    """Fetch all document titles/filenames from the Paperless API."""
    try:
        from app.paperless_client import PaperlessClient
        client = PaperlessClient(token=paperless_token, base_url=paperless_url)
        all_ids = client.get_all_document_ids()
        titles = set()
        # Batch fetch titles (up to first 200 for performance)
        for doc_id in all_ids[:500]:
            try:
                doc = client.get_document(doc_id)
                titles.add(doc.get("title", "").lower().strip())
                # Also add original filename if present
                fn = doc.get("original_file_name", "")
                if fn:
                    titles.add(Path(fn).stem.lower().strip())
            except Exception:
                pass
        return titles
    except Exception as e:
        logger.warning(f"Could not fetch Paperless titles: {e}")
        return set()


def _similarity_key(filename: str) -> str:
    """Reduce filename to a comparable key: strip dates, amounts, extensions."""
    name = Path(filename).stem.lower()
    # Remove date prefixes YYYY_MM_DD or YYYY-MM-DD
    name = re.sub(r"^\d{4}[-_]\d{2}[-_]\d{2}[-_]?", "", name)
    # Remove amounts like _99.99 or -99.99
    name = re.sub(r"[-_]\d+\.\d{2}$", "", name)
    # Normalize separators
    name = re.sub(r"[\s_\-]+", "_", name)
    return name.strip("_")


def check_paperless_coverage(
    tax_root: str,
    paperless_token: str,
    paperless_url: str,
) -> dict:
    """
    For each PDF in tax_root, check if a document with a similar name exists
    in Paperless.

    Returns:
        {
          "total_files": int,
          "in_paperless": int,
          "not_in_paperless": int,
          "files": [{ path, name, year, in_paperless, match }]
        }
    """
    pl_titles = _paperless_doc_titles(paperless_token, paperless_url)
    pl_keys = {_similarity_key(t) for t in pl_titles if t}

    all_files = []
    root_path = Path(tax_root)

    for pdf in sorted(root_path.rglob("*.pdf")):
        name = pdf.name
        key = _similarity_key(name)
        # Determine year from path
        parts = pdf.relative_to(root_path).parts
        year = parts[0] if parts else "unknown"

        # Check exact title match or similarity key match
        in_pl = (
            name.lower() in pl_titles
            or name.lower().rsplit(".", 1)[0] in pl_titles
            or key in pl_keys
        )

        all_files.append({
            "path": str(pdf),
            "name": name,
            "year": year,
            "in_paperless": in_pl,
            "match": key if in_pl else None,
        })

    in_pl = sum(1 for f in all_files if f["in_paperless"])
    return {
        "total_files": len(all_files),
        "in_paperless": in_pl,
        "not_in_paperless": len(all_files) - in_pl,
        "files": all_files,
    }


# ── Consume queue management ──────────────────────────────────────────────────

def queue_for_paperless(
    file_path: str,
    consume_root: str,
    entity_slug: str,
    year: str,
    dry_run: bool = True,
) -> dict:
    """
    Copy a file from the source archive to the Paperless consume queue.
    Uses path: <consume_root>/<entity_slug>/<year>/<filename>
    Paperless CONSUMER_SUBDIRS_AS_TAGS=true will tag it automatically.
    """
    src = Path(file_path)
    if not src.is_file():
        return {"status": "error", "message": f"Not a file: {file_path}"}

    dest_dir = Path(consume_root) / entity_slug / year
    dest_file = dest_dir / src.name

    if dest_file.exists():
        return {"status": "skip", "message": "Already in consume queue", "dest": str(dest_file)}

    if dry_run:
        return {"status": "dry_run", "src": str(src), "dest": str(dest_file)}

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest_file))
    return {"status": "queued", "src": str(src), "dest": str(dest_file)}


def queue_year_for_paperless(
    tax_root: str,
    year: str,
    consume_root: str,
    entity_slug: str = "personal",
    dry_run: bool = True,
) -> dict:
    """Queue all PDFs from a given year directory into the Paperless consume path."""
    year_dir = Path(tax_root) / year
    if not year_dir.is_dir():
        return {"status": "error", "message": f"Year directory not found: {year_dir}"}

    queued = []
    skipped = []
    errors = []

    for pdf in sorted(year_dir.rglob("*.pdf")):
        result = queue_for_paperless(str(pdf), consume_root, entity_slug, year, dry_run=dry_run)
        if result["status"] in ("queued", "dry_run"):
            queued.append(result)
        elif result["status"] == "skip":
            skipped.append(str(pdf))
        else:
            errors.append(result)

    return {
        "status": "dry_run" if dry_run else "done",
        "year": year,
        "queued": queued,
        "skipped_count": len(skipped),
        "errors": errors,
    }
