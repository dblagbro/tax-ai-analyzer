"""Coverage-gap report: month strip counts transactions, amount-bearing
transactions AND analyzed documents; a month is covered by amount-bearing
transactions or documents — never by amount-less Gmail pointer rows.
(2026-09-06)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

YEAR = "2099"          # never collides with real data
DOC_BASE = 9_900_000   # paperless ids far outside the real range


def _client():
    from app.web_ui import app
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _cleanup():
    from app.db.core import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM analyzed_documents WHERE paperless_doc_id BETWEEN ? AND ?",
                 (DOC_BASE, DOC_BASE + 100))
    conn.execute("DELETE FROM transactions WHERE tax_year = ?", (YEAR,))
    conn.commit()
    conn.close()


def test_gaps_shape_and_coverage_rules():
    from app import db
    _cleanup()
    try:
        for i in range(12):  # 12 docs in March
            db.mark_document_analyzed(DOC_BASE + i, None, YEAR, "receipt", "expense",
                                      "Vendor", 10.0, f"{YEAR}-03-{i + 1:02d}", 0.9, "{}")
        for i in range(12):  # 12 amount-less pointer rows in May → NOT coverage
            db.add_transaction({"source": "gmail", "source_id": f"gaps-t-may-{i}", "tax_year": YEAR,
                                "date": f"{YEAR}-05-{i + 1:02d}", "amount": 0, "description": "ptr"})
        for i in range(12):  # 12 real rows in June → coverage
            db.add_transaction({"source": "pdf_statement", "source_id": f"gaps-t-jun-{i}", "tax_year": YEAR,
                                "date": f"{YEAR}-06-{i + 1:02d}", "amount": -5.0, "description": "real"})
        c = _client()
        r = c.get(f"/tax-ai-analyzer/api/reports/gaps?year={YEAR}&min_txns=10")
        assert r.status_code == 200
        body = r.get_json()
        months = {m["month"]: m for m in body["months"]}
        assert len(months) == 12
        assert {"month", "transactions", "with_amount", "documents", "total", "sources"} <= set(months[f"{YEAR}-03"])
        assert months[f"{YEAR}-03"]["documents"] == 12
        assert months[f"{YEAR}-05"]["transactions"] == 12 and months[f"{YEAR}-05"]["with_amount"] == 0
        assert months[f"{YEAR}-06"]["with_amount"] == 12
        sparse = set(body["sparse_months"])
        assert f"{YEAR}-03" not in sparse   # documents
        assert f"{YEAR}-06" not in sparse   # amount-bearing transactions
        assert f"{YEAR}-05" in sparse       # pointer rows only
        assert f"{YEAR}-04" in sparse
        assert body["summary"]["months_covered"] == 2
    finally:
        _cleanup()


def test_gaps_requires_year():
    c = _client()
    assert c.get("/tax-ai-analyzer/api/reports/gaps").status_code == 400
    assert c.get("/tax-ai-analyzer/api/reports/gaps?year=23").status_code == 400
