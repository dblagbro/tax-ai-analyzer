"""Rules-only (no LLM) document analysis — provisional results.

Why (2026-09-06): the analysis daemon used to do nothing at all when no LLM
was reachable (and, worse, when the LLM call *failed* it saved the document
as doc_type="other", confidence 0.0, description "Analysis failed: …" — and
never looked at it again). During the proxy-key reissue that meant every
new statement the user uploaded for 2023 sat unclassified.

This module produces a *provisional* classification from regex + keyword
rules (plus invoice2data templates when one matches). The result has the
exact shape of ``LLMClient.analyze_document`` so the daemon's downstream
code is unchanged, with three extra keys:

    provisional=True   → daemon re-analyzes with the LLM as soon as it is back
    method="rules" | "rules+invoice2data"
    confidence ≤ 0.35  (0.6 when an invoice2data template matched)

Tags always include "provisional" and "needs_review" so the UI and the
accountant PDF show these as unverified.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import Optional

from app.extractor import extract_amounts
from app.llm_client.vocab import VALID_DOC_TYPES, VALID_ENTITIES

logger = logging.getLogger(__name__)
logging.getLogger("invoice2data").setLevel(logging.ERROR)

PROVISIONAL_CONFIDENCE_CAP = 0.35
INVOICE2DATA_CONFIDENCE = 0.6
INVOICE_TEMPLATES_DIR = os.environ.get("INVOICE_TEMPLATES_DIR", "/app/profiles/invoice_templates")

# Ordered: most specific first. First hit wins.
_DOC_TYPE_RULES: list[tuple[str, str]] = [
    ("W-2",               r"wage and tax statement|\bform w-?2\b|\bw-?2\b.{0,40}\bwages\b"),
    ("1099-NEC",          r"1099-?\s?nec\b|nonemployee compensation"),
    ("1099-K",            r"1099-?\s?k\b|payment card and third[- ]party network"),
    ("1099-DIV",          r"1099-?\s?div\b|dividends and distributions"),
    ("1099-MISC",         r"1099-?\s?misc\b|miscellaneous (?:income|information)"),
    ("mortgage_statement", r"\bform 1098\b|mortgage interest statement|\bescrow\b|mortgage statement|principal balance|mortgage payment"),
    ("1099-INT",          r"1099-?\s?int\b|\binterest income\b"),
    ("property_tax",      r"property tax|real estate tax|tax collector|\bassessor\b|\bparcel\b.{0,60}\btax\b"),
    ("credit_card_statement", r"minimum payment(?: due)?|credit limit|statement closing date|payment due date|\bnew balance\b"),
    ("bank_statement",    r"beginning balance|ending balance|checks paid|deposits and (?:other )?credits|withdrawals and|account statement"),
    ("medical",           r"\bpatient\b|\bco-?pay\b|explanation of benefits|\beob\b|\bpharmacy\b|\bclinic\b|\bhospital\b|\bdental\b|\boncolog"),
    ("charitable_donation", r"\bdonation\b|tax-deductible|501\(c\)|\bcharitable\b|thank you for your (?:gift|generous)"),
    ("insurance",         r"\bpolicy (?:number|no\.?|#)|\bpremium\b|\binsurance\b"),
    ("utility_bill",      r"\bkwh\b|electric(?:ity)? (?:bill|service)|water (?:bill|service)|natural gas|\bverizon\b|\bxfinity\b|\bcomcast\b|\bspectrum\b|internet service|wireless bill|usage charges"),
    ("paypal_transaction", r"\bpaypal\b"),
    ("venmo_transaction", r"\bvenmo\b"),
    ("vehicle",           r"\bvin\b|\bdmv\b|registration renewal|vehicle registration|\bauto loan\b|\bodometer\b"),
    ("farm_expense",      r"\blivestock\b|\btractor\b|\bfertiliz|\bfeed store\b|\bhay\b|\bfarm\b|\branch supply\b"),
    ("subscription",      r"\bsubscription\b|\brenewal\b|monthly plan|your plan|\bmembership\b"),
    ("receipt",           r"\breceipt\b|order total|thank you for your (?:order|purchase)|order confirmation|\border #|\border number"),
    ("invoice",           r"\binvoice\b|amount due|\bbill to\b|\bdue date\b"),
]
_DOC_TYPE_RULES_C = [(dt, re.compile(p, re.IGNORECASE)) for dt, p in _DOC_TYPE_RULES]

_CATEGORY_FOR = {
    **{k: "income" for k in ("W-2", "1099-NEC", "1099-K", "1099-INT", "1099-DIV", "1099-MISC")},
    **{k: "deduction" for k in ("mortgage_statement", "property_tax", "charitable_donation", "medical")},
    **{k: "expense" for k in ("invoice", "receipt", "utility_bill", "subscription", "insurance",
                              "vehicle", "equipment", "farm_expense", "paypal_transaction", "venmo_transaction")},
    "capital_improvement": "asset",
}

# Labelled-amount patterns by doc_type. Generic list is tried after these.
_AMOUNT_LABELS = {
    "W-2": ["wages, tips, other comp(?:ensation)?", "wages tips other comp"],
    "1099-NEC": ["nonemployee compensation"],
    "1099-INT": ["interest income"],
    "1099-DIV": ["total ordinary dividends"],
    "1099-K": ["gross amount"],
    "1099-MISC": ["rents", "royalties", "other income"],
    "mortgage_statement": ["mortgage interest received", "interest paid", "total interest"],
    "property_tax": ["total (?:tax )?(?:due|amount)", "amount due", "total tax"],
    "credit_card_statement": ["new balance", "statement balance"],
    "bank_statement": ["ending balance", "closing balance"],
}
_GENERIC_LABELS = ["amount due", "total due", "order total", "grand total", "total charged",
                   "total amount", "total paid", "amount paid", "you paid", "payment amount",
                   "total"]
_STATEMENT_TYPES = {"credit_card_statement", "bank_statement", "mortgage_statement"}

_ISO_DATE = re.compile(r"\b(20\d{2})[-_/](\d{2})[-_/](\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_LONG_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.IGNORECASE)
_LABELLED_DATE = re.compile(
    r"(?:statement date|invoice date|order date|closing date|date of service|payment date|transaction date|\bdate)\s*[:\-]?\s*"
    r"((?:20\d{2}[-/]\d{2}[-/]\d{2})|(?:\d{1,2}/\d{1,2}/(?:20\d{2}|\d{2}))|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20\d{2}))",
    re.IGNORECASE,
)
_TAX_YEAR = re.compile(r"(?:tax year|for tax year|calendar year|for the year)\s*:?\s*(20\d{2})|\b(20\d{2})\s+(?:form\s+)?(?:w-?2|1099|1098)\b", re.IGNORECASE)


# ── field detectors ──────────────────────────────────────────────────────────

def detect_doc_type(text: str, title: str = "") -> str:
    hay = f"{title}\n{text[:8000]}"
    for dt, rx in _DOC_TYPE_RULES_C:
        if rx.search(hay):
            return dt if dt in VALID_DOC_TYPES else "other"
    return "other"


def _norm_date(s: str) -> Optional[str]:
    m = _ISO_DATE.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _US_DATE.search(s)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        except ValueError:
            return None
    m = _LONG_DATE.search(s)
    if m:
        try:
            return f"{int(m.group(3)):04d}-{_MONTHS.index(m.group(1)[:3].lower()) + 1:02d}-{int(m.group(2)):02d}"
        except ValueError:
            return None
    return None


def detect_date(text: str, title: str = "") -> Optional[str]:
    """Title first (Gmail-import filenames start with the email date), then a
    labelled date in the body, then the first date of any shape."""
    for src in (title, ):
        d = _norm_date(src or "")
        if d:
            return d
    m = _LABELLED_DATE.search(text[:6000])
    if m:
        d = _norm_date(m.group(1))
        if d:
            return d
    return _norm_date(text[:6000])


def detect_tax_year(text: str, title: str, date_str: Optional[str], year_hint: Optional[str]) -> Optional[str]:
    if year_hint and str(year_hint).strip()[:4].isdigit():
        return str(year_hint).strip()[:4]
    m = _TAX_YEAR.search(text[:8000])
    if m:
        return m.group(1) or m.group(2)
    if date_str:
        return date_str[:4]
    years = [int(y) for y in re.findall(r"\b(20[12]\d)\b", text[:8000])]
    if years:
        return str(Counter(years).most_common(1)[0][0])
    return None


def _labelled_amount(text: str, labels: list[str]) -> Optional[float]:
    for lab in labels:
        rx = re.compile(rf"(?:{lab})[^\d$\n\-]{{0,40}}\$?\s*(-?[\d,]{{1,12}}\.\d{{2}})", re.IGNORECASE)
        m = rx.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def detect_amount(text: str, doc_type: str) -> Optional[float]:
    body = text[:12000]
    amt = _labelled_amount(body, _AMOUNT_LABELS.get(doc_type, []))
    if amt is not None:
        return amt
    if doc_type in _STATEMENT_TYPES or doc_type.startswith(("W-2", "1099")):
        return None  # never guess a statement balance or a tax-form box from "max $"
    amt = _labelled_amount(body, _GENERIC_LABELS)
    if amt is not None:
        return amt
    amounts = [a for a in extract_amounts(body) if 0 < a < 1_000_000]
    if 0 < len(amounts) <= 40:
        return max(amounts)
    return None


_TITLE_DATE_PREFIX = re.compile(r"^\s*20\d{2}[-_]\d{2}[-_]\d{2}[-_\s]*")
_TITLE_DATE_SUFFIX = re.compile(r"[-_\s]*20\d{2}[-_]\d{2}[-_]\d{2}(?:[-_][0-9a-f]{6})?\s*$")
_GENERIC_TITLE = re.compile(r"^(document|scan|img|image|file|untitled)[\s_\-]*\d*$", re.IGNORECASE)


def detect_vendor(title: str, text: str = "") -> Optional[str]:
    t = os.path.splitext(title or "")[0]
    t = _TITLE_DATE_PREFIX.sub("", t)
    t = _TITLE_DATE_SUFFIX.sub("", t)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–")
    if t and not _GENERIC_TITLE.match(t):
        return t[:60]
    for line in (text or "").splitlines()[:8]:
        s = line.strip()
        if 2 < len(s) <= 60 and re.search(r"[A-Za-z]{3,}", s) and not re.search(r"\d{3,}", s):
            return s
    return None


def detect_entity(title: str, text: str, entity_hint: str) -> str:
    try:
        from app.importers.entity_router import get_entity_slug
        slug = get_entity_slug(sender="", subject=title or "", description=(text or "")[:3000])
    except Exception:
        slug = ""
    if slug in VALID_ENTITIES:
        return slug
    return entity_hint if entity_hint in VALID_ENTITIES else "personal"


# ── invoice2data (optional) ──────────────────────────────────────────────────

_TEMPLATES = None


def _templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        try:
            from invoice2data.extract.loader import read_templates
            tpls = list(read_templates())
            if os.path.isdir(INVOICE_TEMPLATES_DIR):
                tpls = list(read_templates(INVOICE_TEMPLATES_DIR)) + tpls  # custom first
            _TEMPLATES = tpls
            logger.info(f"rules_analyzer: {len(tpls)} invoice2data templates loaded")
        except Exception as e:  # not installed / broken template dir
            logger.info(f"rules_analyzer: invoice2data unavailable ({e!r})")
            _TEMPLATES = []
    return _TEMPLATES


def invoice2data_match(text: str) -> dict:
    """Return the fields of the first invoice2data template that matches, or {}.
    Custom YAML templates in INVOICE_TEMPLATES_DIR (bind-mounted profiles/) let
    the user teach recurring vendors without touching code."""
    for t in _templates():
        try:
            optimized = t.prepare_input(text)
            if not t.matches_input(optimized):
                continue
            fields = t.extract(optimized, "", None) or {}
            if fields:
                fields = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in fields.items()}
                fields.setdefault("template", t.get("template_name", ""))
                return fields
        except Exception as e:
            logger.debug(f"invoice2data template error: {e!r}")
    return {}


# ── entry point ──────────────────────────────────────────────────────────────

def analyze_rules_only(content: str, title: str = "", entity_hint: str = "personal",
                       year_hint: Optional[str] = None) -> dict:
    """Provisional analysis with the same shape as LLMClient.analyze_document."""
    text = content or ""
    doc_type = detect_doc_type(text, title)
    date_str = detect_date(text, title)
    tax_year = detect_tax_year(text, title, date_str, year_hint)
    amount = detect_amount(text, doc_type)
    vendor = detect_vendor(title, text)
    entity = detect_entity(title, text, entity_hint)
    method = "rules"
    extracted: dict = {}

    conf = 0.15
    if doc_type != "other":
        conf += 0.10
    if amount is not None:
        conf += 0.05
    if date_str:
        conf += 0.05
    conf = min(conf, PROVISIONAL_CONFIDENCE_CAP)

    i2d = invoice2data_match(text) if text.strip() else {}
    if i2d:
        method = "rules+invoice2data"
        extracted = i2d
        if i2d.get("issuer"):
            vendor = str(i2d["issuer"])[:60]
        if i2d.get("amount") is not None:
            try:
                amount = float(i2d["amount"])
            except (TypeError, ValueError):
                pass
        if i2d.get("date"):
            d = _norm_date(str(i2d["date"]))
            if d:
                date_str = d
                tax_year = tax_year or d[:4]
        if doc_type == "other":
            doc_type = "invoice"
        conf = INVOICE2DATA_CONFIDENCE

    return {
        "doc_type": doc_type,
        "category": _CATEGORY_FOR.get(doc_type, "other"),
        "entity": entity,
        "tax_year": tax_year,
        "vendor": vendor,
        "amount": amount,
        "date": date_str,
        "confidence": round(conf, 2),
        "description": f"Provisional ({method}) — {doc_type}" + (f" from {vendor}" if vendor else ""),
        "tags": ["provisional", "needs_review"],
        "extracted_fields": extracted,
        "provisional": True,
        "method": method,
    }
