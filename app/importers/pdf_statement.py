"""Generic PDF bank / credit-card statement → transactions. LLM-free.

Why this exists (2026-09-06, 2023 tax-prep push):
  ofxstatement only helps if a per-bank plugin exists, and for this user's
  banks (US Bank, US Alliance FCU, Capital One, Discover, Chime, Merrick,
  Amex, Synchrony, TD) there are none on PyPI. What every one of those banks
  DOES offer is a PDF statement download going back years. This module turns
  those PDFs into transactions with no AI, no browser automation and no
  per-bank code.

How:
  `pdftotext -layout` (poppler) gives one transaction per line in the shape
  every US statement uses:

      <date> [<date>] <description> <amount> [<running balance>]

  A single line regex handles that. Sign, category and the year for
  year-less dates are inferred from the statement itself.

Layouts verified against real 2023 statements (2026-10-01):
  * US Alliance FCU     "03-06 03-06 EXT WD AMEX … ($150.00)  $3,107.27"
                        dash dates, withdrawals in parentheses, running balance
  * Discover            "03/14  ELLENVILLE DISCOUNT BEVE …  Merchandise  $47.62"
  * Capital One monthly "Dec 2   Dec 4   WALMART.COM …   $60.32" / "- $150.00"
  * Capital One Year-End Summary  "12/02  THEWOODSTOCKPUB  WOODSTOCK NY  $135.27"
                        grouped under category headings (Dining, Merchandise…)

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
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?"
_NUM_DATE = r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
_DATE_TOKEN = rf"(?:{_NUM_DATE}|{_MON}\s{{1,2}}\d{{1,2}}(?:,\s?\d{{4}})?)"
_AMT_TOKEN = r"-?\s?\(?\$?\s?[\d,]{1,12}\.\d{2}\)?(?:\s?CR)?-?"
_LINE_RE = re.compile(
    rf"^\s*(?P<d1>{_DATE_TOKEN})\s+(?:(?P<d2>{_DATE_TOKEN})\s+)?"
    rf"(?P<desc>\S.*?\S|\S)"
    rf"(?P<amts>(?:\s+{_AMT_TOKEN})+)\s*$",
    re.IGNORECASE,
)
_AMT_RE = re.compile(_AMT_TOKEN, re.IGNORECASE)
# Fallback for multi-column pages where unrelated text shares the line AFTER the
# amount (Discover prints its rewards box to the right of the transactions:
# "09/27  WALMART.COM …  Merchandise  $821.97 maximum when you activate…").
# Only tried when the strict pattern fails; takes the first amount that follows
# the description by 2+ spaces.
_LINE_RELAXED_RE = re.compile(
    rf"^\s*(?P<d1>{_DATE_TOKEN})\s+(?:(?P<d2>{_DATE_TOKEN})\s+)?"
    rf"(?P<desc>\S.*?\S)\s{{2,}}(?P<amts>{_AMT_TOKEN})(?=\s)\s+(?P<tail>\S.*)$",
    re.IGNORECASE,
)
_EMBEDDED_AMT = re.compile(rf"\s{{2,}}{_AMT_TOKEN}\s+[A-Za-z]", re.IGNORECASE)
# "Date  Check #  Amount   Date  Check #  Amount …" — a recap table of checks that
# are ALREADY listed in the detail section; parsing it double-counts them.
_CHECK_RECAP_HEADER = re.compile(r"Date\s+Check\s*#\s+Amount", re.IGNORECASE)
# A recap row's "description" is just a check number ("9446", "9449*") — possibly
# followed by more date/check/amount triples. Page headers can sit between the
# recap header and its rows, so this is matched on its own as well.
_CHECK_RECAP_ROW = re.compile(r"^\d{1,6}\*?(?:\s+\$[\d,]+\.\d{2}\s.*)?$")
# A wrapped description: one short indented fragment on the line after a row
# ("VM -" ⏎ "DIRECT DEP", "…BankToCard From Bank" ⏎ "Account To Card").
_CONTINUATION = re.compile(r"^\s{4,}([A-Za-z][A-Za-z0-9 .&'#*/-]{0,28})\s*$")

# Summary rows that look like transactions but aren't. Full phrases only —
# a bare "new" here once swallowed "NEW YEAR STORE".
_SKIP_DESC = re.compile(
    r"^(?:previous balance|new balance|statement balance|beginning balance|ending balance|"
    r"starting balance|closing balance|opening balance|balance forward|balance\s*$|total\b|sub-?total\b|"
    r"minimum payment|payment due|credit limit|available credit|interest charge calculation|"
    r"annual percentage|days in billing|purchases?\s*$|fees charged|interest charged|"
    r"cash advances?\s*$|late payment warning|amount due)",
    re.IGNORECASE,
)

_CARD_HINTS = ("minimum payment", "credit limit", "new balance", "payment due date",
               "statement closing date", "credit line", "cash advance", "purchases and adjustments",
               "year-end summary", "merchant name", "card ending in")
_BANK_HINTS = ("beginning balance", "ending balance", "starting balance", "checks paid", "deposits and",
               "withdrawals", "withdrawal", "account summary", "daily balance", "available balance",
               "deposit summary", "deposit detail")

# Card statements: a credit is a payment TO the card or a refund. Anchored at
# the start of the description on purpose — "VERIZON WIRELESS PAYMENT" is a
# charge, "PAYMENT - THANK YOU" is a credit. Most banks also print credits
# negative / "CR", which is checked first.
_CARD_CREDIT = re.compile(
    r"^(?:(?:auto|online|mobile|electronic|internet|phone|web)\s*)?payment\b(?!.*\bto\b)"
    r"|thank you"
    r"|\bonline pymt\b"
    r"|^(?:refund|return|credit|reversal|statement credit|cashback|cash back|reward|rebate)\b",
    re.IGNORECASE,
)
# Bank statements WITHOUT explicit signs: money IN is a deposit-like description.
_BANK_DEPOSIT = re.compile(
    r"\b(?:deposit|direct ?dep|dir dep|ext dep|payroll|\bdd\b|interest (?:paid|earned|credit)|dividend|"
    r"transfer from|xfer from|zelle from|venmo from|paypal transfer|refund|reversal|"
    r"\bcredit\b|cashback|cash back|rebate|mobile deposit|atm deposit|ach credit)",
    re.IGNORECASE,
)

# Bank rows that move money between the user's own accounts / debts. These are
# NOT income or expense — counting a card payment as an expense double-counts
# every purchase already on the card statement.
_CARD_ISSUERS = (r"capital one|citi ?card|citibank|amex|american express|discover|home depot|td bank|"
                 r"merrick|citizensbank|pentagon federal|penfed|syncb|synchrony|chase c(?:redit|ard)|"
                 r"barclay|lowes|walmart|cabela|comenity|bhg|paypal credit|apple card|usbank|u\.?s\.? bank")
_BANK_TRANSFER_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    ("balance_transfer", re.compile(r"\bbt deposit\b|balance transfer", re.I)),
    # A lender wiring the loan amount in is debt, not income.
    ("loan_proceeds", re.compile(r"bankers healthcare group|loan proceeds|loan disbursement|\bwire\b.*\b(?:lendingclub|upgrade|sofi|upstart|prosper)\b", re.I)),
    ("internal_transfer", re.compile(r"transfer (?:to|from)\b|internet transfer|xfer (?:to|from)|internal transfer|"
                                     r"overdraft protection", re.I)),
    # Net pay. The W-2 is the tax document for wages; counting deposits as
    # income as well would double-count them.
    ("payroll_net_pay", re.compile(r"\bpayroll\b|direct[ -]?(?:dep|pay)\b|- direct\b|\bdir dep\b", re.I)),
    # Principal + interest + escrow in one number; Form 1098 is authoritative.
    ("mortgage_payment", re.compile(r"wells fargo hom|mortgage|\bmtg\b|loancare|mr\.? cooper|rocket m", re.I)),
    ("card_payment", re.compile(rf"(?:{_CARD_ISSUERS}).*(?:pmt|pymt|payment|e-?payment|epay|trnsfr|autopay|crcardpmt|ach|banktocard)"
                                rf"|(?:pmt|pymt|payment)\b.*(?:{_CARD_ISSUERS})"
                                r"|storecard|ebaymc|banktocard|crcardpmt|credit card pmt", re.I)),
    ("loan_payment", re.compile(r"loan pmt|loan payment|lendingclub|lending club|upstart|sofi\b|prosper|upgrade, inc", re.I)),
    ("p2p_transfer", re.compile(r"venmo|paypal.*(?:transfer|xfer)|\bzel\*|zelle|cash app|cashout", re.I)),
    ("cash", re.compile(r"\batm (?:wd|withdrawal)|cash withdrawal", re.I)),
]

_DATE_WITH_YEAR = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
_PERIOD_NUMERIC = re.compile(
    rf"({_DATE_WITH_YEAR})\s*(?:-|–|—|to|through|thru)\s*({_DATE_WITH_YEAR})",
    re.IGNORECASE,
)
_LONG_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_CLOSING = re.compile(
    rf"(?:statement|closing|billing)\s+(?:date|period end|end date)[:\s]+({_DATE_WITH_YEAR})",
    re.IGNORECASE,
)

_ACCOUNT_PATTERNS = [
    re.compile(r"(?:card|account)(?: number)? ending in\s+(\d{4})", re.I),
    re.compile(r"#(\d{4}):"),
    re.compile(r"(?m)^\s*[A-Za-z][A-Za-z ]{2,40} - \d{2,8}(\d{4})\s"),      # "MyLifeChecking - 1916245540"
    re.compile(r"account (?:number|no\.?|#)[:\s]+[\dXx*\- ]*?(\d{4})\b", re.I),
]
_SECTION_PATTERNS = [
    re.compile(r"^\s*DATE\s+(PAYMENTS AND CREDITS|PURCHASES|BALANCE TRANSFERS|CASH ADVANCES)\b"),
    re.compile(r"#\d{4}:\s*(Payments, Credits and Adjustments|Transactions)\s*$"),
]
_SUMMARY_HEADER = re.compile(r"^\s*Date\s+Merchant Name\b")
_LABEL_LINE = re.compile(r"^\s{2,}([A-Z][A-Za-z/&' \-]{2,40})\s*$")


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
    if t[-2:].upper() == "CR":
        neg = True
        t = t[:-2].strip()
    if t.startswith("-"):
        neg = True
        t = t[1:].strip()
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


_NUM_SPLIT = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
_MON_SPLIT = re.compile(rf"^({_MON})\s+(\d{{1,2}})(?:,\s?(\d{{4}}))?$", re.IGNORECASE)


def _split_date(tok: str) -> Optional[tuple[int, int, Optional[int]]]:
    """Date token → (month, day, year|None) for 'MM/DD', 'MM-DD-YYYY', 'Nov 9'."""
    tok = tok.strip()
    m = _NUM_SPLIT.match(tok)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        yy = int(m.group(3)) if m.group(3) else None
    else:
        m = _MON_SPLIT.match(tok)
        if not m:
            return None
        try:
            mm = _MONTHS.index(m.group(1)[:3].lower()) + 1
        except ValueError:
            return None
        dd = int(m.group(2))
        yy = int(m.group(3)) if m.group(3) else None
    if yy is not None and yy < 100:
        yy += 2000
    return mm, dd, yy


def _mk(yy: int, mm: int, dd: int) -> Optional[date]:
    try:
        return date(yy, mm, dd)
    except ValueError:
        return None


def _any_date(tok: str) -> Optional[date]:
    """Parse a token that MUST carry a year."""
    p = _split_date(tok)
    if not p or p[2] is None:
        return None
    return _mk(p[2], p[0], p[1])


def detect_period(text: str) -> tuple[Optional[date], Optional[date]]:
    """(start, end) of the statement period when the PDF states one.
    Numeric form first ('03/01/2023 - 03/31/2023'), then long form
    ('Nov 08, 2023 - Dec 08, 2023'), then a closing/statement date."""
    m = _PERIOD_NUMERIC.search(text)
    if m:
        s, e = _any_date(m.group(1)), _any_date(m.group(2))
        if s and e and s <= e and (e - s).days <= 100:
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


def detect_account(text: str, filename: str = "") -> str:
    """Last 4 digits of the account/card, from the statement text, else from a
    filename that ends in 4 digits (…-6338.pdf / …_5933.pdf). '' if unknown."""
    for rx in _ACCOUNT_PATTERNS:
        m = rx.search(text)
        if m:
            return m.group(1)
    m = re.search(r"(?<!\d)(\d{4})\.[A-Za-z]{3,4}$", os.path.basename(filename or ""))
    return m.group(1) if m else ""


def _dominant_year(text: str, fallback: int) -> int:
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", text)]
    if not years:
        return fallback
    return Counter(years).most_common(1)[0][0]


def _resolve_date(tok: str, period: tuple[Optional[date], Optional[date]], year_hint: int) -> Optional[date]:
    p = _split_date(tok)
    if not p:
        return None
    mm, dd, yy = p
    if yy is not None:
        return _mk(yy, mm, dd)
    start, end = period
    candidates: list[int] = []
    if start:
        candidates.append(start.year)
    if end:
        for y in (end.year, end.year - 1):
            if y not in candidates:
                candidates.append(y)
    if year_hint not in candidates:
        candidates.append(year_hint)
    for y in candidates:
        d = _mk(y, mm, dd)
        if not d:
            continue
        if start and end and not (start <= d <= end):
            continue
        if end and not start and d > end:
            continue
        return d
    return _mk(year_hint, mm, dd)


def _clean_desc(desc: str) -> str:
    return re.sub(r"\s{2,}", " ", desc).strip()


def _vendor_from(desc: str) -> str:
    v = re.sub(r"^(?:EXT (?:WD|DEP)|POS(?: PURCHASE)?|DEBIT CARD|CHECK CARD|ACH (?:DEBIT|CREDIT))\s+", "", desc, flags=re.I)
    return v[:255]


def classify_bank_row(desc: str) -> Optional[str]:
    """'card_payment' / 'loan_payment' / 'p2p_transfer' / 'cash' /
    'balance_transfer' / 'internal_transfer' for rows that are movements
    between the user's own accounts or debts; None for real income/expense."""
    for kind, rx in _BANK_TRANSFER_RULES:
        if rx.search(desc):
            return kind
    return None


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
      card → charges are money OUT (stored negative, category 'expense');
             payments/credits are IN (category 'payment' — excluded from
             income/expense totals)
      bank → explicit signs win when the statement prints them
             (parentheses / minus = withdrawal); otherwise deposits are
             recognised by keyword. Card/loan payments, P2P transfers and
             ATM cash get category 'transfer' (excluded from totals).
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
    account = detect_account(text, filename)

    # ── pass 1: collect candidate rows with their section ────────────────────
    rows: list[dict] = []
    section = ""
    last_label = ""
    in_check_recap = False
    pending_cont: Optional[dict] = None   # last row, still open for one wrapped line
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if _CHECK_RECAP_HEADER.search(raw_line):
            in_check_recap = True
            pending_cont = None
            continue
        if pending_cont is not None:
            cont = _CONTINUATION.match(raw_line)
            row_, pending_cont = pending_cont, None
            if cont and not re.search(r"\s{3,}", cont.group(1).strip()):
                row_["desc"] = f"{row_['desc']} {cont.group(1).strip()}"
                continue
        if _SUMMARY_HEADER.match(raw_line):          # Capital One year-end summary
            section = last_label
            continue
        hit = next((m for m in (rx.search(raw_line) for rx in _SECTION_PATTERNS) if m), None)
        if hit:
            section = hit.group(1).strip()
            continue
        lab = _LABEL_LINE.match(raw_line)
        if lab and not re.search(r"\d", raw_line):
            last_label = lab.group(1).strip()
        m = _LINE_RE.match(raw_line)
        if m and kind == "card" and _EMBEDDED_AMT.search(m.group("desc")):
            # "VISTAPRINT …  Services   $5.00 CASHBACK BONUS BALANCE   $7.50":
            # the strict pattern swallowed the real amount into the description
            # and took the right-hand column's figure. Prefer the first amount.
            m = _LINE_RELAXED_RE.match(raw_line) or m
        if in_check_recap:
            if m:
                continue          # recap row — the check is already in the detail section
            in_check_recap = False
        if not m:
            m = _LINE_RELAXED_RE.match(raw_line)
            if not m or not re.search(r"[A-Za-z]{3}", m.group("tail")):
                continue
        desc = _clean_desc(m.group("desc"))
        if not desc or _SKIP_DESC.match(desc) or _CHECK_RECAP_ROW.match(desc):
            continue
        amts = [a for a in (_parse_amount(t) for t in _AMT_RE.findall(m.group("amts"))) if a is not None]
        if not amts:
            continue
        d = _resolve_date(m.group("d1"), period, year_hint)
        if not d:
            continue
        rows.append({"date": d, "desc": desc, "amount": amts[0],
                     "balance": amts[-1] if len(amts) >= 2 else None,
                     "d2": m.group("d2") or "", "section": section})
        pending_cont = rows[-1]

    # A bank statement that prints ANY negative amount uses explicit signs.
    explicit_signs = kind == "bank" and any(r["amount"] < 0 for r in rows)

    # ── pass 2: sign, category, ids ──────────────────────────────────────────
    out: list[dict] = []
    seen: Counter = Counter()
    for r in rows:
        desc, amount, sec = r["desc"], r["amount"], r["section"]
        transfer_kind = None
        if kind == "card":
            sec_l = sec.lower()
            credit_section = "payment" in sec_l or "credit" in sec_l
            money_in = amount < 0 or credit_section or bool(_CARD_CREDIT.search(desc))
            signed = abs(amount) if money_in else -abs(amount)
            doc_type, category = ("invoice", "payment") if money_in else ("receipt", "expense")
        else:
            if explicit_signs:
                money_in = amount > 0
            else:
                money_in = amount >= 0 and bool(_BANK_DEPOSIT.search(desc))
            signed = abs(amount) if money_in else -abs(amount)
            transfer_kind = classify_bank_row(desc)
            if transfer_kind:
                doc_type, category = "other", "transfer"
            else:
                doc_type, category = ("invoice", "income") if money_in else ("receipt", "expense")

        d = r["date"]
        key = f"{account}|{d.isoformat()}|{desc.lower()}|{signed:.2f}"
        n = seen[key]
        seen[key] += 1
        sid = hashlib.sha256(f"pdfstmt:{key}|{n}".encode()).hexdigest()[:32]

        out.append({
            "date": d.isoformat(),
            "description": desc[:255],
            "vendor": _vendor_from(desc),
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
                "account_last4": account,
                "source_file": os.path.basename(filename or ""),
                "second_date": r["d2"],          # post date when the line has two
                "running_balance": r["balance"],
                "statement_section": sec,         # e.g. Capital One "Dining", Discover "PURCHASES"
                "transfer_kind": transfer_kind,
                "period": [p.isoformat() if p else None for p in period],
            },
        })
    logger.info(f"pdf_statement: {len(out)} transactions from {filename or '<bytes>'} (kind={kind}, acct={account or '?'})")
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
