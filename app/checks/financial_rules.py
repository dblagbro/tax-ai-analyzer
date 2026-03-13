"""
Deterministic financial document validation rules.
Returns a dict with 'issues', 'warnings', and 'confidence_penalty'.

Public API:
  validate_document(...)         — full validation, returns {issues, warnings, confidence_penalty}
  check_amount_reasonable(...)   — (ok, message) tuple
  check_year_consistency(...)    — (ok, message) tuple
  check_required_fields(...)     — list of missing field names
"""
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Reasonable amount ranges per doc_type
AMOUNT_RANGES = {
    "W-2":                (5000.0,   500000.0),
    "1099-NEC":           (100.0,    500000.0),
    "1099-K":             (20000.0,  999999.0),
    "1099-INT":           (0.01,     50000.0),
    "1099-DIV":           (0.01,     100000.0),
    "utility_bill":       (20.0,     5000.0),
    "subscription":       (1.0,      10000.0),
    "equipment":          (50.0,     500000.0),
    "mortgage_statement": (500.0,    50000.0),
    "property_tax":       (200.0,    50000.0),
    "vehicle":            (20.0,     200000.0),
    "farm_expense":       (10.0,     200000.0),
    "medical":            (5.0,      100000.0),
    "charitable_donation":(1.0,      500000.0),
    "invoice":            (1.0,      500000.0),
    "business_expense":   (1.0,      100000.0),
    "bank_statement":     (0.01,     9999999.0),
}

# Valid category values
VALID_CATEGORIES = {"income", "expense", "deduction", "other"}

# Income doc types
INCOME_TYPES = {"W-2", "1099-NEC", "1099-K", "1099-INT", "1099-DIV", "invoice"}

# Expense doc types
EXPENSE_TYPES = {
    "utility_bill", "subscription", "equipment", "mortgage_statement",
    "property_tax", "vehicle", "farm_expense", "medical", "charitable_donation",
    "business_expense",
}


def validate_document(
    doc_type: str,
    category: str,
    amount: Optional[float],
    date: Optional[str],
    tax_year: Optional[str],
    full_result: dict,
) -> dict:
    """
    Run deterministic checks against extracted document data.

    Returns:
        {
            "issues": [...],          # Blocking errors
            "warnings": [...],        # Non-blocking flags
            "confidence_penalty": float  # Reduction to apply to AI confidence (0.0–0.5)
        }
    """
    issues = []
    warnings = []
    penalty = 0.0

    # --- Category consistency check ---
    if doc_type in INCOME_TYPES and category not in ("income", "other"):
        warnings.append(f"doc_type '{doc_type}' is typically income but category is '{category}'")
        penalty += 0.05

    if doc_type in EXPENSE_TYPES and category not in ("expense", "deduction", "other"):
        warnings.append(f"doc_type '{doc_type}' is typically expense but category is '{category}'")
        penalty += 0.05

    if category not in VALID_CATEGORIES:
        issues.append(f"Unknown category '{category}' — expected one of {VALID_CATEGORIES}")
        penalty += 0.15

    # --- Amount checks ---
    if amount is not None:
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            issues.append(f"Amount '{amount}' is not numeric")
            penalty += 0.20
            amt = None

        if amt is not None:
            if amt < 0:
                warnings.append(f"Negative amount {amt:.2f} — verify sign")
                penalty += 0.05

            if amt == 0:
                warnings.append("Amount is zero — may indicate failed extraction")
                penalty += 0.10

            if doc_type in AMOUNT_RANGES:
                lo, hi = AMOUNT_RANGES[doc_type]
                if amt > 0 and not (lo <= amt <= hi):
                    warnings.append(
                        f"Amount ${amt:,.2f} outside typical range ${lo:,.0f}–${hi:,.0f} for {doc_type}"
                    )
                    penalty += 0.05

            if amt > 1_000_000:
                issues.append(f"Suspiciously large amount: ${amt:,.2f}")
                penalty += 0.20

    # --- Date checks ---
    if date:
        parsed_date = _parse_date(date)
        if parsed_date is None:
            warnings.append(f"Could not parse date '{date}'")
            penalty += 0.05
        else:
            year_int = parsed_date.year
            if tax_year:
                try:
                    ty = int(str(tax_year)[:4])
                    # Allow one year of tolerance (e.g. January statements for prior year)
                    if abs(year_int - ty) > 1:
                        warnings.append(
                            f"Document date year {year_int} differs from tax year {ty}"
                        )
                        penalty += 0.10
                except ValueError:
                    pass

            # Sanity: future dates
            if parsed_date > datetime.now():
                issues.append(f"Document date {date} is in the future")
                penalty += 0.15

            # Sanity: very old dates
            if year_int < 2015:
                warnings.append(f"Document date year {year_int} seems very old")
                penalty += 0.05

    elif doc_type not in ("bank_statement", "other"):
        warnings.append("No date extracted — may affect tax year assignment")
        penalty += 0.05

    # --- Vendor / payer check ---
    vendor = full_result.get("vendor") or full_result.get("payer") or ""
    if not vendor and doc_type not in ("bank_statement", "other"):
        warnings.append("No vendor/payer name extracted")
        penalty += 0.05

    # --- Clamp penalty ---
    penalty = min(penalty, 0.50)

    return {
        "issues": issues,
        "warnings": warnings,
        "confidence_penalty": penalty,
    }


# ── Named helper functions (spec-compatible public API) ────────────────────────

def check_amount_reasonable(amount: float, doc_type: str) -> tuple[bool, str]:
    """
    Flag unusually large or small amounts for the given doc type.

    Returns:
        (ok, message) — ok is False when the amount is outside the expected range.
    """
    if amount is None:
        return True, ""
    if doc_type in AMOUNT_RANGES:
        lo, hi = AMOUNT_RANGES[doc_type]
        if not (lo <= float(amount) <= hi):
            return (
                False,
                f"Amount ${float(amount):,.2f} outside expected range for {doc_type}",
            )
    elif float(amount) > 1_000_000:
        return False, f"Amount ${float(amount):,.2f} outside expected range for {doc_type}"
    return True, ""


def check_year_consistency(doc_date: str, tax_year: str) -> tuple[bool, str]:
    """
    Check whether a document's date year matches the claimed tax year.

    One year of tolerance is allowed (e.g. January statement for prior year).

    Returns:
        (ok, message)
    """
    if not doc_date or not tax_year:
        return True, ""
    try:
        year = int(str(doc_date)[:4])
        ty = int(str(tax_year)[:4])
        if abs(year - ty) <= 1:
            return True, ""
        return False, f"Document date {doc_date} doesn't match tax year {tax_year}"
    except Exception:
        return True, ""


def check_required_fields(doc_type: str, extracted: dict) -> list[str]:
    """
    Return the list of required fields that are missing for the given doc type.

    Fields are considered missing when absent or falsy.
    """
    required: dict[str, list[str]] = {
        "W-2":                ["vendor", "amount"],
        "1099-NEC":           ["vendor", "amount"],
        "1099-K":             ["vendor", "amount"],
        "1099-INT":           ["vendor", "amount"],
        "1099-DIV":           ["vendor", "amount"],
        "mortgage_statement": ["vendor", "amount"],
        "utility_bill":       ["vendor", "amount"],
        "invoice":            ["vendor", "amount"],
    }
    missing = []
    for field in required.get(doc_type, []):
        if not extracted.get(field):
            missing.append(field)
    return missing


# ── Internal helpers ───────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> Optional[datetime]:
    """Try common date formats, return datetime or None."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y",
               "%B %d, %Y", "%b %d, %Y", "%Y%m%d"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str[:10], fmt)
        except ValueError:
            continue
    # Try extracting 4-digit year as fallback
    m = re.search(r"\b(20\d{2})\b", date_str)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1)
        except ValueError:
            pass
    return None
