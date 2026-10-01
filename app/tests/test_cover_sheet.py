"""Cover-sheet parsers against text reduced from the real 2023 forms
(amounts changed). (2026-10-01)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.export import cover_sheet as cs  # noqa: E402

W2 = """c    Employer's name, address, and ZIP code
                     ACME CORP INC
                     1 MAIN ST
b   Employer's FED ID number      a Employee's SSA number
       12-3456789                       123-45-6789
1   Wages, tips, other comp.     2 Federal income tax withheld     Reported W-2 Wages   100,000.00
          100000.00                    20000.50
3   Social security wages        4 Social security tax withheld
          101000.00                     6262.00
5   Medicare wages and tips      6 Medicare tax withheld
          101000.00                     1464.50
11 Nonqualified plans            12a See instructions for box 12
                                    D       1500.25
14 Other                         12b W      3850.00
                                 12c DD     8000.00
15 State Employer's state ID no. 16 State wages, tips, etc.
    NY         12-3456789                100000.00
17 State income tax              18 Local wages, tips, etc.
          7000.75
"""

F1098 = """  2023 MORTGAGE INTEREST RECEIVED FROM PAYER/BORROWER(S)        $6,000.10
        WOODRIDGE, NY 12789-5615        $250,000.00       12/10/2021     determines that an
        9 Number of properties          11 Mortgage acquisition
        10 Real estate taxes
        securing the mortgage           date
        $2,874.68     Total current payment      Account number
        $9,000.00
        $900.98       Escrow portion of payment
09/19     School tax payment     $0.00    $0.00   $0.00    $-6,000.00   $0.00
01/23     City tax payment       $0.00    $0.00   $0.00    $-3,000.00   $0.00
11/02     Hazard insurance pmt   $0.00    $0.00   $0.00    $-2,267.00   $0.00
"""


def test_parse_w2_boxes():
    w = cs.parse_w2(W2)
    assert w["Box 1 — Wages, tips, other compensation"] == 100000.00
    assert w["Box 2 — Federal income tax withheld"] == 20000.50
    assert w["Box 4 — Social security tax withheld"] == 6262.00
    assert w["Box 6 — Medicare tax withheld"] == 1464.50
    assert w["Box 12 D — 401(k) deferrals"] == 1500.25
    assert w["Box 12 W — HSA (employer + pre-tax)"] == 3850.00
    assert w["Box 12 DD — Employer health coverage cost"] == 8000.00
    assert w["Box 16 — NY state wages"] == 100000.00
    assert w["Box 17 — NY state income tax withheld"] == 7000.75
    assert w["_payer"] == "ACME CORP INC"


def test_parse_1098_interest_taxes_insurance():
    f = cs.parse_1098(F1098)
    assert f["Box 1 — Mortgage interest received"] == 6000.10
    assert f["Box 2 — Outstanding mortgage principal"] == 250000.00
    assert f["_origination"] == "12/10/2021"
    assert f["Box 10 — Real estate taxes paid from escrow"] == 9000.00
    assert f["   of which school tax (escrow history)"] == 6000.00
    assert f["   of which city tax (escrow history)"] == 3000.00
    assert f["Homeowner's insurance paid from escrow"] == 2267.00


def test_ssn_is_redacted_from_extracted_text():
    assert "123-45-6789" not in cs._SSN.sub("[redacted]", W2)


def test_issuer_labels():
    assert cs._issuer("EXT WD CAPITAL ONE - ONLINE PMT") == "Capital One (all cards)"
    assert cs._issuer("EXT WD AMEX EPAYMENT ER AM - ACH PMT") == "American Express"
    assert cs._issuer("EXT WD LendingClub Y - 8885963157") == "LendingClub loan"
    assert cs._issuer("EXT WD PAYMENT FOR AMZ - STORECARD") == "Amazon Store Card (Synchrony)"


def test_form_kind_is_judged_by_content_not_filename():
    # 2022 archive: the file named "W2" holds the 1095-C and vice versa
    assert cs.form_kind("Form 1095-C (2022)  Instructions for Recipient", "DevinB_W2_Statement for 2022.pdf") == "1095-C"
    assert cs.form_kind(W2 + "\nW-2 and EARNINGS SUMMARY", "DevinB_1095c_Statement for 2022.pdf") == "W-2"
    assert cs.form_kind(F1098, "anything.pdf") == "1098"
    assert cs.form_kind("", "DevinB_W2_Statement.pdf") == "W-2"          # no text layer → name is all we have
    assert cs.form_kind("Spectrum bill for internet service", "bill.pdf") == ""
    e = cs._form_entry("W-2", W2, "x.pdf", "DevinB_1095c_Statement for 2022.pdf")
    assert "NAMED like a 1095-C" in e["note"] and e["lines"]["Box 1 — Wages, tips, other compensation"] == 100000.00


def test_1099_int_amount_with_trailing_text_on_the_line():
    t = "   1 Interest income\n 1-800-222-0238\n      $99.40      Copy B\n PAYER'S TIN\n   2 Early withdrawal penalty\n   $0.00\n"
    assert cs.parse_1099_int(t)["Box 1 — Interest income"] == 99.40
