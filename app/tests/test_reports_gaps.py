"""Coverage-gap report: month strip counts transactions AND analyzed documents,
and a month of analyzed documents counts as covered. (2026-09-06)"""
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


def test_gaps_shape_and_document_coverage():
    from app import db
    _cleanup()
    try:
        for i in range(12):  # 12 docs in March 2099, none elsewhere
            db.mark_document_analyzed(DOC_BASE + i, None, YEAR, "receipt", "expense",
                                      "Vendor", 10.0, f"{YEAR}-03-{i + 1:02d}", 0.9, "{}")
        c = _client()
        r = c.get(f"/tax-ai-analyzer/api/reports/gaps?year={YEAR}&min_txns=10")
        assert r.status_code == 200
        body = r.get_json()
        months = body["months"]
        assert len(months) == 12
        assert {"month", "transactions", "with_amount", "documents", "total", "sources"} <= set(months[0])
        march = next(m for m in months if m["month"] == f"{YEAR}-03")
        assert march["documents"] == 12 and march["transactions"] == 0
        assert f"{YEAR}-03" not in body["sparse_months"]
        assert f"{YEAR}-04" in body["sparse_months"]
        assert body["summary"]["months_covered"] == 1
    finally:
        _cleanup()


def test_gaps_requires_year():
    c = _client()
    assert c.get("/tax-ai-analyzer/api/reports/gaps").status_code == 400
    assert c.get("/tax-ai-analyzer/api/reports/gaps?year=23").status_code == 400
