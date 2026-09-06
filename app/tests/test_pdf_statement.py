"""Tests for the built-in PDF statement parser (2026-09-06).

The text-level parser is exercised directly (no poppler needed). The PDF
round-trip test renders a statement with WeasyPrint and runs pdftotext, and
skips itself when either tool is absent.
"""
import os
import shutil
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.importers import pdf_statement as ps  # noqa: E402

CARD = """CAPITAL ONE                                   Statement Period: 03/01/2023 - 03/31/2023
Payment Due Date 04/25/2023    Minimum Payment Due $25.00        New Balance $1,234.56
Credit Limit $10,000.00

Trans Date  Post Date   Description                                     Amount
03/02       03/03       AMAZON.COM*2K3JF4 AMZN.COM/BILL WA               45.67
03/05       03/05       VERIZON WIRELESS PAYMENT                         120.00
03/10       03/11       PAYMENT - THANK YOU                             -500.00
03/12       03/12       TRACTOR SUPPLY #123 SOMEWHERE TX                  89.10
03/12       03/12       TRACTOR SUPPLY #123 SOMEWHERE TX                  89.10
03/20       03/21       AMAZON.COM REFUND                                 12.00 CR
Previous Balance                                                        1,000.00
Total Fees Charged                                                          0.00
"""

BANK = """US BANK                                    Statement Period 04/01/2023 through 04/30/2023
Beginning Balance   2,500.00        Ending Balance   2,300.00

Date    Description                                        Amount        Balance
04/03   DIRECT DEPOSIT ACME PAYROLL                      1,500.00       4,000.00
04/05   CHECK 1042                                         700.00       3,300.00
04/12   ONLINE PAYMENT TO CAPITAL ONE                    1,000.00       2,300.00
"""

DEC_JAN = """Statement Period: 12/15/2023 - 01/14/2024
Minimum Payment Due 25.00
12/20   HOLIDAY STORE                     10.00
01/05   NEW YEAR STORE                    20.00
"""


def test_card_statement_kind_sign_and_category():
    txns = ps.parse_statement_text(CARD, filename="capone_2023-03.pdf")
    assert ps.detect_kind(CARD) == "card"
    by_desc = {}
    for t in txns:
        by_desc.setdefault(t["description"], []).append(t)
    amazon = by_desc["AMAZON.COM*2K3JF4 AMZN.COM/BILL WA"][0]
    assert amazon["amount"] == -45.67 and amazon["category"] == "expense"
    assert amazon["date"] == "2023-03-02" and amazon["tax_year"] == "2023"
    # "VERIZON WIRELESS PAYMENT" is a charge, not a credit
    assert by_desc["VERIZON WIRELESS PAYMENT"][0]["amount"] == -120.00
    pay = by_desc["PAYMENT - THANK YOU"][0]
    assert pay["amount"] == 500.00 and pay["category"] == "payment"
    refund = by_desc["AMAZON.COM REFUND"][0]
    assert refund["amount"] == 12.00  # "CR" suffix = credit
    # summary rows without a leading date are ignored
    assert not any("Previous Balance" in d or "Total Fees" in d for d in by_desc)
    assert all(t["source"] == "pdf_statement" and t["metadata"]["statement_kind"] == "card" for t in txns)


def test_identical_lines_keep_distinct_ids_but_reparse_is_stable():
    a = ps.parse_statement_text(CARD)
    b = ps.parse_statement_text(CARD)
    ts = [t for t in a if t["description"].startswith("TRACTOR SUPPLY")]
    assert len(ts) == 2 and ts[0]["source_id"] != ts[1]["source_id"]
    assert [t["source_id"] for t in a] == [t["source_id"] for t in b]
    assert all(t["source_id"] == t["dedup_hash"] for t in a)


def test_bank_statement_deposit_vs_withdrawal_and_balance_column():
    txns = ps.parse_statement_text(BANK, kind="auto")
    assert ps.detect_kind(BANK) == "bank"
    d = {t["description"]: t for t in txns}
    assert d["DIRECT DEPOSIT ACME PAYROLL"]["amount"] == 1500.00
    assert d["DIRECT DEPOSIT ACME PAYROLL"]["category"] == "income"
    assert d["CHECK 1042"]["amount"] == -700.00
    # "ONLINE PAYMENT TO ..." on a BANK statement is money out
    assert d["ONLINE PAYMENT TO CAPITAL ONE"]["amount"] == -1000.00
    # first number is the amount, last is the running balance
    assert d["CHECK 1042"]["metadata"]["running_balance"] == 3300.00
    assert d["CHECK 1042"]["date"] == "2023-04-05"


def test_year_rollover_inside_statement_period():
    txns = ps.parse_statement_text(DEC_JAN)
    d = {t["description"]: t["date"] for t in txns}
    assert d["HOLIDAY STORE"] == "2023-12-20"
    assert d["NEW YEAR STORE"] == "2024-01-05"


def test_explicit_year_param_used_when_no_period():
    txns = ps.parse_statement_text("03/04  COFFEE SHOP   4.50\n", year="2022")
    assert txns[0]["date"] == "2022-03-04" and txns[0]["tax_year"] == "2022"


def test_amount_token_parsing():
    assert ps._parse_amount("(12.34)") == -12.34
    assert ps._parse_amount("12.34-") == -12.34
    assert ps._parse_amount("$1,234.56") == 1234.56
    assert ps._parse_amount("12.34 CR") == -12.34
    assert ps._parse_amount("abc") is None


def test_bad_kind_rejected():
    with pytest.raises(ValueError):
        ps.parse_statement_text(CARD, kind="loan")


def test_pdf_to_text_guards():
    with pytest.raises(RuntimeError):
        ps.pdf_to_text(b"")
    with patch.object(ps.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError) as e:
            ps.pdf_to_text(b"%PDF-1.4 fake")
        assert "pdftotext" in str(e.value)


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler not installed")
def test_pdf_roundtrip_with_weasyprint():
    try:
        from weasyprint import HTML
    except Exception:
        pytest.skip("weasyprint not installed")
    html = "<pre style='font-family:monospace;font-size:9pt'>" + CARD.replace("<", "&lt;") + "</pre>"
    pdf = HTML(string=html).write_pdf()
    txns = ps.parse_pdf_statement(pdf, filename="card.pdf")
    descs = {t["description"] for t in txns}
    assert "PAYMENT - THANK YOU" in descs
    assert any(d.startswith("AMAZON.COM*2K3JF4") for d in descs)
    assert len(txns) >= 5
