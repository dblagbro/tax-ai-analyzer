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
    # net pay is a transfer — the W-2 is the tax document for wages
    assert d["DIRECT DEPOSIT ACME PAYROLL"]["category"] == "transfer"
    assert d["DIRECT DEPOSIT ACME PAYROLL"]["metadata"]["transfer_kind"] == "payroll_net_pay"
    assert d["ONLINE PAYMENT TO CAPITAL ONE"]["metadata"]["transfer_kind"] == "card_payment"
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


# ── real 2023 layouts (2026-10-01), reduced and anonymised ───────────────────

CREDIT_UNION = """                              Rye, NY 10580-1410          STATEMENT DATE 03-31-2023        PAGE 1
Deposit Summary
Account Description                         Starting Balance      Ending Balance
MyLifeChecking                                    $2,957.27           $2,700.37
Deposit Detail
 MyLifeChecking - 1916245540                                      Interest Rate 0.000
Trans Eff
Date    Date Description                                  Deposit      Withdrawal       Balance
03-01 03-01 Starting Balance                                                           $2,957.27
03-03 03-03 Deposit Mobile Check                           $51.10                      $3,008.37
03-06 03-06 EXT WD AMEX EPAYMENT ER AM - ACH PMT                       ($150.00)       $2,858.37
03-09 03-09 EXT DEP ACME CORP, INC 4100075043     VM -   $3,059.64                     $5,918.01
                 DIRECT DEP
03-10 03-10 EXT WD TWC - SPECTRUM - ONLINE PMT                         ($120.00)       $5,798.01
03-10 03-10 EXT WD WELLS FARGO HOM - ONLINE PMT                      ($2,900.00)       $2,898.01
03-17 03-17 Check 9404                                                  ($68.68)       $2,829.33
03-20 03-20 Withdrawal Transfer To *5530; REF                          ($128.96)       $2,700.37
03-31 03-31 Ending Balance                                                             $2,700.37
Date   Check #       Amount      Date  Check #    Amount
               STATEMENT DATE 03-31-2023        PAGE 2
03-17 9404           $68.68      03-18 9405*      $10.00
* Denotes a break in sequence
"""

DISCOVER = """ Account Summary                        09/25/2023 - 10/24/2023        Payment Information
Previous Balance                                            $387.77   New Balance     Minimum Payment
Credit Line                                                 $12,700
                             DISCOVER IT CARD ENDING IN 6338
TRANS.
DATE         PAYMENTS AND CREDITS                                                    AMOUNT
10/03        INTERNET PAYMENT - THANK YOU                                           -$190.30       1% Cashback Bonus          +$35.16
TRANS.
DATE         PURCHASES                              MERCHANT CATEGORY               AMOUNT
09/27        WALMART.COM 8009666546 BENTONVILLE     Merchandise                    $821.97 maximum when you activate. Plus, earn 1% cash
09/27        WALMART.COM 800-966-6546 AR            Merchandise                    $141.31 For details, see Information For You section.
10/14        ELLENVILLE DISCOUNT BEVE ELLENVILLE NY Merchandise                     $47.62
"""

CAPONE_MONTHLY = """                                                          Walmart Rewards Card
                                               Nov 08, 2023 - Dec 08, 2023
  New Balance      Minimum Payment Due       Credit Limit
JANE DOE #4587: Payments, Credits and Adjustments
Trans Date        Post Date      Description                                             Amount
Nov 9             Nov 9          CAPITAL ONE ONLINE PYMTAuthDate 09-Nov               - $150.00
JANE DOE #4587: Transactions
Trans Date        Post Date      Description                                             Amount
Dec 2             Dec 4          WALMART.COM 8009666546BENTONVILLEAR                     $60.32
JANE DOE #4587: Total Transactions                                                       $60.32
Total Transactions for This Period                                                       $60.32
"""

CAPONE_SUMMARY = """Year-End Summary 2023
Section 4_Transaction Details                                             Page 9
          Dining
Date    Merchant Name            Merchant Location        Amount Deduct
Card Ending in 5933
12/02   THEWOODSTOCKPUB          WOODSTOCK      NY        $135.27
12/30   MCDONALD'S F4021         LIBERTY     NY            $40.02
                                 TOTAL CHARGES            $175.29
          Gas/Automotive
Date    Merchant Name            Merchant Location        Amount Deduct
Card Ending in 5933
11/30   EXXON BETHEL DISCOUNT    WOODBOURNE       NY       $90.00
"""


def _reconcile(txns, start):
    return round(start + sum(t["amount"] for t in txns), 2)


def test_credit_union_dash_dates_signed_columns_and_check_recap():
    txns = ps.parse_statement_text(CREDIT_UNION, filename="March 2023 Regular Statement.pdf")
    assert ps.detect_kind(CREDIT_UNION) == "bank"
    d = {t["description"]: t for t in txns}
    assert "Starting Balance" not in d and "Ending Balance" not in d
    # the check recap table (split by a page header) must NOT add rows
    assert not any(k.startswith("9404") or k.startswith("9405") for k in d)
    assert len(txns) == 7
    assert _reconcile(txns, 2957.27) == 2700.37            # start + activity == end
    assert d["Deposit Mobile Check"]["amount"] == 51.10 and d["Deposit Mobile Check"]["category"] == "income"
    assert d["Check 9404"]["amount"] == -68.68 and d["Check 9404"]["category"] == "expense"
    assert d["Check 9404"]["date"] == "2023-03-17"          # year from STATEMENT DATE
    pay = d["EXT DEP ACME CORP, INC 4100075043 VM - DIRECT DEP"]  # wrapped description re-joined
    assert pay["amount"] == 3059.64 and pay["metadata"]["transfer_kind"] == "payroll_net_pay"
    assert d["EXT WD AMEX EPAYMENT ER AM - ACH PMT"]["metadata"]["transfer_kind"] == "card_payment"
    assert d["EXT WD WELLS FARGO HOM - ONLINE PMT"]["metadata"]["transfer_kind"] == "mortgage_payment"
    assert d["Withdrawal Transfer To *5530; REF"]["metadata"]["transfer_kind"] == "internal_transfer"
    spectrum = d["EXT WD TWC - SPECTRUM - ONLINE PMT"]
    assert spectrum["category"] == "expense" and spectrum["vendor"] == "TWC - SPECTRUM - ONLINE PMT"
    assert all(t["metadata"]["account_last4"] == "5540" for t in txns)


def test_discover_rows_with_trailing_marketing_text():
    txns = ps.parse_statement_text(DISCOVER, filename="Discover-Statement-20231024-6338.pdf")
    d = {t["description"]: t for t in txns}
    assert d["WALMART.COM 8009666546 BENTONVILLE Merchandise"]["amount"] == -821.97
    assert d["WALMART.COM 800-966-6546 AR Merchandise"]["amount"] == -141.31
    pay = d["INTERNET PAYMENT - THANK YOU"]
    assert pay["amount"] == 190.30 and pay["category"] == "payment"
    assert pay["metadata"]["statement_section"] == "PAYMENTS AND CREDITS"
    assert round(-sum(t["amount"] for t in txns if t["category"] == "expense"), 2) == 1010.90
    assert all(t["metadata"]["account_last4"] == "6338" for t in txns)


def test_capital_one_monthly_month_name_dates():
    txns = ps.parse_statement_text(CAPONE_MONTHLY)
    d = {t["description"]: t for t in txns}
    pay = d["CAPITAL ONE ONLINE PYMTAuthDate 09-Nov"]
    assert pay["date"] == "2023-11-09" and pay["amount"] == 150.00 and pay["category"] == "payment"
    buy = d["WALMART.COM 8009666546BENTONVILLEAR"]
    assert buy["date"] == "2023-12-02" and buy["amount"] == -60.32
    assert buy["metadata"]["second_date"] == "Dec 4" and buy["metadata"]["account_last4"] == "4587"
    assert len(txns) == 2                                   # total rows are not transactions


def test_capital_one_year_end_summary_sections():
    txns = ps.parse_statement_text(CAPONE_SUMMARY, filename="CapOne_Smry_2023_5933.pdf")
    assert [t["metadata"]["statement_section"] for t in txns] == ["Dining", "Dining", "Gas/Automotive"]
    assert [t["date"] for t in txns] == ["2023-12-02", "2023-12-30", "2023-11-30"]
    assert round(-sum(t["amount"] for t in txns), 2) == 265.29
    assert all(t["metadata"]["account_last4"] == "5933" for t in txns)


def test_same_row_on_two_cards_gets_distinct_ids():
    a = ps.parse_statement_text(CAPONE_SUMMARY, filename="x_5933.pdf")
    b = ps.parse_statement_text(CAPONE_SUMMARY.replace("5933", "8811"), filename="x_8811.pdf")
    assert not {t["source_id"] for t in a} & {t["source_id"] for t in b}


def test_classify_bank_row():
    c = ps.classify_bank_row
    assert c("EXT WD CAPITAL ONE - ONLINE PMT") == "card_payment"
    assert c("EXT WD PAYMENT FOR AMZ - STORECARD") == "card_payment"
    assert c("EXT WD LendingClub Y - 8885963157") == "loan_payment"
    assert c("EXT DEP VENMO - CASHOUT") == "p2p_transfer"
    assert c("ATM WD USALLIANCE FCU 7500 ROUTE 209 WALMART") == "cash"
    assert c("EXT DEP CITIBANK - BT DEPOSIT") == "balance_transfer"
    assert c("Overdraft Protection Deposit") == "internal_transfer"
    assert c("EXT WD TRAVELERS - PER INSUR") is None
    assert c("EXT WD CENTRALHUDSON") is None
