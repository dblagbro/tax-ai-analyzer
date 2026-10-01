"""Tests for the rules-only provisional analyzer (2026-09-06)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app import rules_analyzer as ra  # noqa: E402
from app.llm_client.vocab import VALID_DOC_TYPES, VALID_CATEGORIES  # noqa: E402

W2 = """2023 Form W-2 Wage and Tax Statement
Employer: ACME CORP  EIN 12-3456789
1 Wages, tips, other compensation   55,000.00
2 Federal income tax withheld        6,100.00
"""
NEC = """Form 1099-NEC  Nonemployee Compensation   Tax Year 2023
PAYER'S name: Big Client LLC
1 Nonemployee compensation   $12,345.00
"""
CARD = """Capital One  Statement Closing Date 03/25/2023
New Balance $1,234.56   Minimum Payment Due $25.00   Payment Due Date 04/20/2023
03/02 AMAZON.COM 45.67
"""
RECEIPT = """Your Amazon.com order #113-1675470-3862613
Order Date: March 4, 2023
Item Subtotal: $41.99   Shipping: $0.00   Tax: $3.68
Order Total: $45.67
"""
MORTGAGE = """Form 1098 Mortgage Interest Statement 2023
Mortgage interest received from payer(s)/borrower(s)   $9,876.54
"""


def _shape_ok(r: dict):
    for k in ("doc_type", "category", "entity", "tax_year", "vendor", "amount", "date",
              "confidence", "description", "tags", "extracted_fields"):
        assert k in r, k
    assert r["doc_type"] in VALID_DOC_TYPES
    assert r["category"] in VALID_CATEGORIES
    assert r["provisional"] is True
    assert "provisional" in r["tags"] and "needs_review" in r["tags"]
    assert 0.0 <= r["confidence"] <= ra.INVOICE2DATA_CONFIDENCE


def test_w2():
    r = ra.analyze_rules_only(W2, "acme_w2.pdf")
    _shape_ok(r)
    assert r["doc_type"] == "W-2" and r["category"] == "income"
    assert r["amount"] == 55000.00
    assert r["tax_year"] == "2023"
    assert r["confidence"] <= ra.PROVISIONAL_CONFIDENCE_CAP


def test_1099_nec_with_year_hint_precedence():
    r = ra.analyze_rules_only(NEC, "1099.pdf", year_hint="2022")
    assert r["doc_type"] == "1099-NEC" and r["category"] == "income"
    assert r["amount"] == 12345.00
    assert r["tax_year"] == "2022"  # explicit Paperless year- tag wins
    r2 = ra.analyze_rules_only(NEC, "1099.pdf")
    assert r2["tax_year"] == "2023"  # "Tax Year 2023" in the body


def test_credit_card_statement_uses_labelled_balance_not_max_dollar():
    r = ra.analyze_rules_only(CARD, "capone_statement.pdf")
    assert r["doc_type"] == "credit_card_statement" and r["category"] == "other"
    assert r["amount"] == 1234.56
    assert r["date"] == "2023-03-25"


def test_receipt_date_from_gmail_style_title_and_total():
    r = ra.analyze_rules_only(RECEIPT, "2023_03_04_Amazon_receipt_2023-03-04.pdf")
    assert r["doc_type"] == "receipt" and r["category"] == "expense"
    assert r["amount"] == 45.67
    assert r["date"] == "2023-03-04"
    assert r["vendor"] == "Amazon receipt"
    assert r["tax_year"] == "2023"


def test_mortgage_1098_is_deduction():
    r = ra.analyze_rules_only(MORTGAGE, "1098.pdf")
    assert r["doc_type"] == "mortgage_statement" and r["category"] == "deduction"
    assert r["amount"] == 9876.54


def test_empty_content_is_low_confidence_other():
    r = ra.analyze_rules_only("", "Document 12")
    _shape_ok(r)
    assert r["doc_type"] == "other" and r["amount"] is None
    assert r["confidence"] == 0.15
    assert r["vendor"] is None  # generic title is not a vendor


def test_entity_falls_back_to_hint_when_router_silent():
    r = ra.analyze_rules_only("nothing identifying here", "x.pdf", entity_hint="martinfeld_ranch")
    assert r["entity"] in ("martinfeld_ranch", "personal", "voipguru")


def test_invoice2data_match_never_raises():
    # With or without the package installed this must return a dict.
    assert isinstance(ra.invoice2data_match("random text with no invoice"), dict)


# ── 2026-10-01: lessons from the first real batch (48 docs from the 2023 archive) ──

BANK_WITH_MORTGAGE_PMT = """Deposit Summary
MyLifeChecking   $2,957.27   $3,774.89
Deposit Detail
03-10 03-10 EXT WD WELLS FARGO HOME MORTGAGE - ONLINE PMT   ($2,900.00)   $3,619.02
03-31 03-31 Ending Balance  $3,774.89
"""


def test_title_beats_body_for_user_named_files():
    assert ra.detect_doc_type(BANK_WITH_MORTGAGE_PMT, "USAlliance - March 2023 Regular Statement") == "bank_statement"
    assert ra.detect_doc_type("Insurance $0.00 Dining $40", "CreditCards - DevinB_CapOne_Smry_2023_5933") == "credit_card_statement"
    assert ra.detect_doc_type("whatever", "DevinB_W2_Statement for 2023") == "W-2"
    assert ra.detect_doc_type("whatever", "Mortgage - 2023 1098-MORT MORTGAGE 7951 WellsFargo") == "mortgage_statement"
    assert ra.detect_doc_type("whatever", "Mortgage - 2023 1099-INT-MRT MORTGAGE 7951") == "1099-INT"
    # a random number containing 1098 is not a form
    assert ra.detect_doc_type("thank you for your order", "order_210984.pdf") != "mortgage_statement"


def test_bank_statement_mentioning_a_mortgage_payment_is_a_bank_statement():
    assert ra.detect_doc_type(BANK_WITH_MORTGAGE_PMT, "scan0001") == "bank_statement"


def test_form_year_beats_document_date():
    r = ra.analyze_rules_only("Form 1098 Mortgage Interest Statement\nStatement date 01/03/24\n",
                              "Mortgage - 2023 1098-MORT MORTGAGE 7951 WellsFargo")
    assert r["tax_year"] == "2023"
    r = ra.analyze_rules_only("Deposit Summary\nSTATEMENT DATE 03-31-2023", "USAlliance - March 2023 Regular Statement")
    assert r["doc_type"] == "bank_statement" and r["tax_year"] == "2023"


def test_amount_guess_requires_cents():
    # OCR dropped the decimal point: "$ 4065" must not become $4,065.00
    r = ra.analyze_rules_only("Payment History\n10/24/2023  $ 4065\n11/23/2023  $ 4065\n", "Net10 Wireless Mobile phone service")
    assert r["doc_type"] == "utility_bill" and r["amount"] is None


def test_w2_uses_box_parser_when_layout_matches():
    text = ("c    Employer's name, address, and ZIP code\n   ACME CORP INC\n"
            "1   Wages, tips, other comp.     2 Federal income tax withheld\n"
            "          100000.00                    20000.50\n")
    r = ra.analyze_rules_only(text, "DevinB_W2_Statement for 2023")
    assert r["doc_type"] == "W-2" and r["amount"] == 100000.00 and r["tax_year"] == "2023"
    assert r["extracted_fields"]["Box 2 — Federal income tax withheld"] == 20000.50
