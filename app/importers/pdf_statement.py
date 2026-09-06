"""Generic PDF bank / credit-card statement → transactions. LLM-free.

Why this exists (2026-09-06, 2023 tax-prep push):
  ofxstatement only helps if a per-bank plugin exists, and for this user's
  banks (US Bank, US Alliance FCU, Capital One, Chime, Merrick, Amex,
  Synchrony, TD) there are none on PyPI. What every one of those banks DOES
  offer is a PDF statement download going back years. This module turns
  those PDFs into transactions with no AI, no browser automation and no
  per-bank code, so March–December 2023 can be filled in from the bank
  portals in an afternoon.

How:
  `pdftotext -layout` (poppler — already in the image for invoice2data)
  gives one transaction per line in the shape every US statement uses:

      <post date> [<trans date>] <description> <amount> [<running balance>]

  A single line regex handles that. Sign, category and the year for
  year-less "MM/DD" dates are inferred from the statement itself.

Scanned (image-only) PDFs have no text layer; those should go into the
Paperless consume folder for OCR instead — we raise a clear error.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 25 * 1024 * 1024
_PDFTOTEXT_TIMEOUT_S = 90

# ── line grammar ─────────────────────────────────────────────────────────────
_DATE_TOKEN = r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
_AMT_TOKEN = r"-?\(?\$?\s?[\d,]{1,12}\.\d{2}\)?(?:\s?CR)?-?"
_LINE_RE = re.compile(
    rf"^\s*(?P<d1>{_DATE_TOKEN})\s+(?:(?P<d2>{_DATE_TOKEN})\s+)?"
    rf"(?P<desc>\S.*?\S|\S)"
    rf"(?P<amts>(?:\s+{_AMT_TOKEN})+)\s*$",
    re.IGNORECASE,
)
_AMT_RE = re.compile(_AMT_TOKEN, re.IGNORECASE)

# Summary rows that look like transactions but aren't. Full phrases only —
# a bare "new" here once swallowed "NEW YEAR STORE".
_SKIP_DESC = re.compile(
    r"^(?:previous balance|new balance|statement balance|beginning balance|ending balance|"
    r"closing balance|opening balance|balance forward|balance\s*$|total\b|sub-?total\b|"
    r"minimum payment|payment due|credit limit|available credit|interest charge calculation|"
    r"annual percentage|days in billing|purchases?\s*$|fees charged|interest charged|"
    r"cash advances?\s*$|late payment warning|amount due)",
    re.IGNORECASE,
)

_CARD_HINTS = ("minimum payment", "credit limit", "new balance", "payment due date",
               "statement closing date", "credit line", "cash advance", "purchases and adjustments")
_BANK_HINTS = ("beginning balance", "ending balance", "checks paid", "deposits and",
               "withdrawals", "account summary", "daily balance", "available balance")

# Card statements: a credit is a payment TO the card or a refund. Anchored at
# the start of the description on purpose — "VERIZON WIRELESS PAYMENT" is a
# charge, "PAYMENT - THANK YOU" is a credit. Most banks also print credits
# negative / "CR", which is checked first.
_CARD_CREDIT = re.compile(
    r"^(?:(?:auto|online|mobile|electronic|internet|phone|web)\s*)?payment\b(?!.*\bto\b)"
    r"|thank you"
    r"|^(?:refund|return|credit|reversal|statement credit|cashback|cash back|reward|rebate)\b",
    re.IGNORECASE,
)
# Bank statements: money IN is a deposit-like description; everything else is out.
_BANK_DEPOSIT = re.compile(
    r"\b(?:deposit|direct ?dep|dir dep|payroll|\bdd\b|interest (?:paid|earned|credit)|dividend|"
    r"transfer from|xfer from|zelle from|venmo from|paypal transfer|refund|reversal|"
    r"\bcredit\b|cashback|cash back|rebate|mobile deposit|atm deposit|ach credit)",
    re.IGNORECASE,
)

_PERIOD_NUMERIC = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|–|—|to|through|thru)\s*(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_LONG_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_CLOSING = re.compile(
    r"(?:statement|closing|billing)\s+(?:date|period end|end date)[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


# ── pdf → text ────────────────────────────────────────────────────────────────

def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def pdf_to_text(data: bytes) -> str:
    """Run `pdftotext -layout` and return the text. Raises RuntimeError with an
    actionable message for the three real-world failures (tool missing,
    corrupt file, scanned image without a text layer)."""
    if not data:
        raise RuntimeError("empty upload")
    if len(data) > MAX_PDF_BYTES:
        raise RuntimeError(f"PDF exceeds {MAX_PDF_BYTES // 1_000_000} MB cap")
    if not pdftotext_available():
        raise RuntimeError("pdftotext (poppler-utils) not installed in this image")
    with tempfile.TemporaryDirectory(prefix="pdfstmt_") as td:
        src = os.path.join(td, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", src, "-"],
                capture_output=True, text=True, timeout=_PDFTOTEXT_TIMEOUT_S, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"pdftotext timed out after {_PDFTOTEXT_TIMEOUT_S}s")
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed (rc={proc.returncode}): {(proc.stderr or '').strip()[-300:]}")
    text = proc.stdout or ""
    if len(text.strip()) < 40:
        raise RuntimeError(
            "PDF has no text layer (scanned image). Drop it into the Paperless "
            "consume folder instead so it gets OCR'd and analyzed as a document."
        )
    return text


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_amount(tok: str) -> Optional[float]:
    t = tok.strip()
    neg = False
    if t.endswith("CR") or t.endswith("cr"):
        neg = True
        t = t[:-2].strip()
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1]
    if t.startswith("-"):
        neg = True
        t = t[1:]
    if t.endswith("-"):
        neg = True
        t = t[:-1]
    t = t.replace("$", "").replace(",", "").strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _to_date(tok: str, year_for_short: int) -> Optional[date]:
    parts = tok.split("/")
    try:
        mm, dd = int(parts[0]), int(parts[1])
        if len(parts) == 3:
            yy = int(parts[2])
            if yy < 100:
                yy += 2000
        else:
            yy = year_for_short
        return date(yy, mm, dd)
    except (ValueError, IndexError):
        return None


def _any_date(tok: str) -> Optional[date]:
    """Parse a MM/DD/YY(YY) token that MUST carry a year."""
    if tok.count("/") != 2:
        return None
    return _to_date(tok, 1900)


def detect_period(text: str) -> tuple[Optional[date], Optional[date]]:
    """(start, end) of the statement period when the PDF states one.
    Numeric form first ('03/01/2023 - 03/31/2023'), then long form
    ('March 1, 2023 through March 31, 2023'), then a closing/statement date."""
    m = _PERIOD_NUMERIC.search(text)
    if m:
        s, e = _any_date(m.group(1)), _any_date(m.group(2))
        if s and e and s <= e:
            return s, e
    longs = _LONG_DATE.findall(text)
    if len(longs) >= 2:
        try:
            ds = [date(int(y), _MONTHS.index(mo[:3].lower()) + 1, int(d)) for mo, d, y in longs[:2]]
            if ds[0] <= ds[1] and (ds[1] - ds[0]).days <= 100:
                return ds[0], ds[1]
        except ValueError:
            pass
    m = _CLOSING.search(text)
    if m:
        e = _any_date(m.group(1))
        if e:
            return None, e
    return None, None


def detect_kind(text: str) -> str:
    """'card' or 'bank' from the statement's own vocabulary."""
    tl = text.lower()
    card = sum(1 for h in _CARD_HINTS if h in tl)
    bank = sum(1 for h in _BANK_HINTS if h in tl)
    return "card" if card >= bank else "bank"


def _dominant_year(text: str, fallback: int) -> int:
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", text)]
    if not years:
        return fallback
    return Counter(years).most_common(1)[0][0]


def _resolve_date(tok: str, period: tuple[Optional[date], Optional[date]], year_hint: int) -> Optional[date]:
    if tok.count("/") == 2:
        return _to_date(tok, year_hint)
    start, end = period
    candidates = []
    if end:
        candidates.append(end.year)
        candidates.append(end.year - 1)
    if start and start.year not in candidates:
        candidates.insert(0, start.year)
    if year_hint not in candidates:
        candidates.append(year_hint)
    for y in candidates:
        d = _to_date(tok, y)
        if not d:
            continue
        if start and end and not (start <= d <= end):
            continue
        if end and not start and d > end:
            continue
        return d
    return _to_date(tok, year_hint)


def _clean_desc(desc: str) -> str:
    return re.sub(r"\s{2,}", " ", desc).strip()


# ── core parser ──────────────────────────────────────────────────────────────

def parse_statement_text(
    text: str,
    filename: str = "",
    year: Optional[str] = None,
    entity_id: Optional[int] = None,
    kind: str = "auto",
) -> list[dict]:
    """Parse `pdftotext -layout` output into internal transaction dicts
    (same shape as ofx_importer.parse_ofx, plus a deterministic source_id
    so re-uploading the same statement is a no-op).

    kind: 'card' | 'bank' | 'auto'
      card → charges are money OUT (stored negative), payments/credits IN
      bank → withdrawals OUT, deposits IN (decided per line by keywords)
    """
    if kind not in ("card", "bank", "auto"):
        raise ValueError("kind must be card, bank or auto")
    if kind == "auto":
        kind = detect_kind(text)

    period = detect_period(text)
    try:
        year_hint = int(year) if year else None
    except ValueError:
        year_hint = None
    if not year_hint:
        year_hint = (period[1] or period[0]).year if (period[1] or period[0]) else _dominant_year(text, datetime.now().year)

    out: list[dict] = []
    seen: Counter = Counter()
    for raw_line in text.splitlines():
        m = _LINE_RE.match(raw_line)
        if not m:
            continue
        desc = _clean_desc(m.group("desc"))
        if not desc or _SKIP_DESC.match(desc):
            continue
        amts = [a for a in (_parse_amount(t) for t in _AMT_RE.findall(m.group("amts"))) if a is not None]
        if not amts:
            continue
        amount = amts[0]
        balance = amts[-1] if len(amts) >= 2 else None

        d = _resolve_date(m.group("d1"), period, year_hint)
        if not d:
            continue

        if kind == "card":
            # Statement prints charges positive; a payment/credit is negative,
            # "CR", or described as a payment. Store money-out negative.
            money_in = amount < 0 or bool(_CARD_CREDIT.search(desc))
            signed = abs(amount) if money_in else -abs(amount)
            doc_type, category = ("invoice", "payment") if money_in else ("receipt", "expense")
        else:
            money_in = amount >= 0 and bool(_BANK_DEPOSIT.search(desc))
            signed = abs(amount) if money_in else -abs(amount)
            doc_type, category = ("invoice", "income") if money_in else ("receipt", "expense")

        key = f"{d.isoformat()}|{desc.lower()}|{signed:.2f}"
        n = seen[key]
        seen[key] += 1
        sid = hashlib.sha256(f"pdfstmt:{key}|{n}".encode()).hexdigest()[:32]

        out.append({
            "date": d.isoformat(),
            "description": desc[:255],
            "vendor": desc[:255],
            "amount": round(signed, 2),
            "category": category,
            "doc_type": doc_type,
            "source": "pdf_statement",
            "source_id": sid,
            "dedup_hash": sid,
            "external_id": "",
            "entity_id": entity_id,
            "tax_year": str(d.year),
            "metadata": {
                "statement_kind": kind,
                "source_file": os.path.basename(filename or ""),
                "second_date": m.group("d2") or "",  # post date when the line has two
                "running_balance": balance,
                "period": [p.isoformat() if p else None for p in period],
            },
        })
    logger.info(f"pdf_statement: {len(out)} transactions from {filename or '<bytes>'} (kind={kind})")
    return out


def parse_pdf_statement(
    data: bytes,
    filename: str = "",
    year: Optional[str] = None,
    entity_id: Optional[int] = None,
    kind: str = "auto",
) -> list[dict]:
    """PDF bytes → transactions. See parse_statement_text for semantics."""
    text = pdf_to_text(data)
    return parse_statement_text(text, filename=filename, year=year, entity_id=entity_id, kind=kind)
