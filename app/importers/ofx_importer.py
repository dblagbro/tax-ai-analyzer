"""
OFX/QFX file importer for US Alliance FCU and any standard OFX-exporting bank.

Supports both OFX 1.x (SGML) and OFX 2.x (XML) formats.
US Alliance FCU exports standard OFX 1.x via their online banking.

Download path in US Alliance FCU online banking:
  Accounts → [account] → Export Transactions → QuickBooks (OFX)
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ── OFX parsing ────────────────────────────────────────────────────────────────

def _parse_ofx1(text: str) -> list[dict]:
    """Parse OFX 1.x SGML format into raw transaction dicts."""
    # Remove headers (lines before <OFX> or <STMTTRN>)
    txns = []
    # Find all STMTTRN blocks
    blocks = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.DOTALL | re.IGNORECASE)
    for block in blocks:
        t = {}
        for tag, val in re.findall(r"<(\w+)>([^<\r\n]*)", block):
            t[tag.upper()] = val.strip()
        if t:
            txns.append(t)
    return txns


def _parse_ofx2(text: str) -> list[dict]:
    """Parse OFX 2.x XML format into raw transaction dicts."""
    import xml.etree.ElementTree as ET
    txns = []
    try:
        root = ET.fromstring(text)
        for stmttrn in root.iter("STMTTRN"):
            t = {}
            for child in stmttrn:
                t[child.tag.upper()] = (child.text or "").strip()
            if t:
                txns.append(t)
    except ET.ParseError as e:
        logger.warning(f"OFX2 XML parse error: {e}")
    return txns


def _parse_date(raw: str) -> tuple[str, str]:
    """
    Parse OFX date string (YYYYMMDD or YYYYMMDDHHMMSS[.mmm][timezone]) to
    ISO date string and tax year string.
    """
    raw = raw.strip()
    # Strip timezone suffix (e.g. [−5:EST])
    raw = re.sub(r"\[.*\]", "", raw).strip()
    raw = raw[:8]  # Keep YYYYMMDD
    try:
        dt = datetime.strptime(raw, "%Y%m%d")
        return dt.strftime("%Y-%m-%d"), str(dt.year)
    except ValueError:
        return raw, raw[:4] if len(raw) >= 4 else ""


def _trntype_to_category(trntype: str, amount: float) -> tuple[str, str]:
    """
    Map OFX TRNTYPE + amount sign to (doc_type, category).
    Positive = money in (income/deposit), Negative = money out (expense).
    """
    t = (trntype or "").upper()
    if t in ("CREDIT", "DEP", "DIRECTDEP", "DIV", "INT", "REFUND"):
        return "invoice", "income"
    if t in ("DEBIT", "CHECK", "PAYMENT", "XFER"):
        # Negative = expense paid
        return "receipt", "expense"
    if t in ("ATM", "CASH"):
        return "receipt", "expense"
    if t == "FEE":
        return "receipt", "fee"
    if t == "SRVCHG":
        return "receipt", "fee"
    if t == "INT":
        return "invoice", "income"
    # Fall back to sign
    if amount >= 0:
        return "invoice", "income"
    return "receipt", "expense"


def parse_ofx(content: str | bytes, entity_id: Optional[int] = None,
              default_year: Optional[str] = None) -> list[dict]:
    """
    Parse OFX/QFX file content (string or bytes).
    Returns list of internal transaction dicts.
    """
    if isinstance(content, bytes):
        # Try UTF-8 first, fall back to latin-1
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    # Detect OFX version
    if re.search(r"<\?OFX\s", text) or text.strip().startswith("<?xml"):
        raw_txns = _parse_ofx2(text)
    else:
        raw_txns = _parse_ofx1(text)

    if not raw_txns:
        logger.warning("OFX: no transactions found")
        return []

    results = []
    for t in raw_txns:
        try:
            amount_raw = t.get("TRNAMT", "0")
            try:
                amount = float(amount_raw.replace(",", ""))
            except ValueError:
                amount = 0.0

            txn_date, tax_year = _parse_date(t.get("DTPOSTED", "") or t.get("DTUSER", ""))
            if not tax_year:
                tax_year = default_year or ""

            trntype = t.get("TRNTYPE", "")
            doc_type, category = _trntype_to_category(trntype, amount)

            fitid = t.get("FITID", "")
            dedup = hashlib.sha256(f"ofx:{fitid}".encode()).hexdigest()[:32]

            memo = t.get("MEMO", "") or ""
            name = t.get("NAME", "") or ""
            description = name or memo or trntype

            results.append({
                "date": txn_date,
                "description": description[:255],
                "vendor": name[:255],
                "amount": round(amount, 2),
                "category": category,
                "doc_type": doc_type,
                "source": "ofx_import",
                "entity_id": entity_id,
                "tax_year": tax_year,
                "external_id": fitid,
                "dedup_hash": dedup,
            })
        except Exception as e:
            logger.warning(f"OFX: skipping transaction due to error: {e}")

    logger.info(f"OFX: parsed {len(results)} transactions")
    return results
