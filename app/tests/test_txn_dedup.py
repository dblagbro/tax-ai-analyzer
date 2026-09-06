"""Deterministic source_id + transaction_exists so re-imports don't duplicate.
(2026-09-06 — transactions has no UNIQUE index; importers must check first.)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

OFX = (b"OFXHEADER:100\nDATA:OFXSGML\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
       b"<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20230315<TRNAMT>-12.34"
       b"<FITID>dedupfit001<NAME>Test Vendor</STMTTRN>"
       b"<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20230316<TRNAMT>100.00"
       b"<FITID>dedupfit002<NAME>Payroll</STMTTRN></BANKTRANLIST>"
       b"</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>")


def test_parse_ofx_sets_source_id_from_fitid():
    from app.importers.ofx_importer import parse_ofx
    txns = parse_ofx(OFX, default_year="2023")
    assert len(txns) == 2
    for t in txns:
        assert t["source_id"] and t["source_id"] == t["dedup_hash"]
    assert txns[0]["source_id"] != txns[1]["source_id"]
    # stable across parses
    assert [t["source_id"] for t in parse_ofx(OFX)] == [t["source_id"] for t in txns]


def test_transaction_exists_roundtrip():
    from app import db
    from app.db.core import get_connection
    sid = "dedup-test-" + os.urandom(4).hex()
    try:
        assert db.transaction_exists("ofx_import", sid) is False
        assert db.transaction_exists("ofx_import", "") is False
        db.add_transaction({"source": "ofx_import", "source_id": sid, "date": "2023-03-15",
                            "amount": -1.0, "description": "dedup probe", "tax_year": "2023"})
        assert db.transaction_exists("ofx_import", sid) is True
        assert db.transaction_exists("other_source", sid) is False
    finally:
        conn = get_connection()
        conn.execute("DELETE FROM transactions WHERE source_id = ?", (sid,))
        conn.commit()
        conn.close()
