"""process_document — the one implementation shared by the daemon and the
manual trigger. Fake Paperless client + fake LLM; real DB rows (ids far
outside the live range, cleaned up). (2026-09-06)"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app import analysis_core as ac  # noqa: E402

BASE = 9_910_000

W2 = ("2023 Form W-2 Wage and Tax Statement\nEmployer: ACME CORP\n"
      "1 Wages, tips, other compensation 55,000.00\n")


class FakeClient:
    def __init__(self):
        self.tags = {}

    def apply_tags(self, doc_id, tags):
        self.tags[doc_id] = list(tags)


class FakeLLM:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def analyze_document(self, content, title, entity_hint, year_hint, doc_id=None):
        self.calls += 1
        return dict(self.result)


def _row(doc_id):
    from app.db.core import get_connection
    conn = get_connection()
    try:
        r = conn.execute("SELECT * FROM analyzed_documents WHERE paperless_doc_id=?", (doc_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _cleanup():
    from app.db.core import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM analyzed_documents WHERE paperless_doc_id BETWEEN ? AND ?", (BASE, BASE + 50))
    conn.commit()
    conn.close()


def _doc(content, title="acme_w2_2023.pdf", tags=()):
    return {"content": content, "title": title, "tags": list(tags)}


def test_helpers():
    assert ac.hints_from_tags(["tax-voipguru", "year-2023", "receipt"]) == ("voipguru", "2023")
    assert ac.hints_from_tags([]) == ("personal", None)
    assert ac.derive_tax_year({"tax_year": "2022"}, "2023") == "2022"
    assert ac.derive_tax_year({}, "2023") == "2023"
    assert ac.derive_tax_year({"date": "2023-03-04"}, None) == "2023"
    assert ac.derive_tax_year({"date": "1999-01-01"}, None) is None
    assert ac.llm_analysis_failed({"confidence": 0.0, "description": "Analysis failed: 401"})
    assert not ac.llm_analysis_failed({"confidence": 0.9, "description": "Receipt"})


def test_empty_content_marked_without_llm_call():
    _cleanup()
    try:
        llm = FakeLLM({"confidence": 0.9})
        out = ac.process_document(BASE + 1, _doc("   "), llm=llm, llm_ok=True, client=FakeClient(), embed=False)
        assert out["status"] == "empty" and llm.calls == 0
        assert _row(BASE + 1)["doc_type"] == "other"
    finally:
        _cleanup()


def test_llm_success_persists_and_tags():
    _cleanup()
    try:
        llm = FakeLLM({"doc_type": "W-2", "category": "income", "entity": "personal", "tax_year": None,
                       "vendor": "ACME CORP", "amount": 55000.0, "date": "2023-01-31",
                       "confidence": 0.95, "description": "W-2", "tags": [], "extracted_fields": {}})
        client = FakeClient()
        out = ac.process_document(BASE + 2, _doc(W2), llm=llm, llm_ok=True, client=client, embed=False)
        assert out["status"] == "analyzed" and not out["provisional"] and not out["llm_failed"]
        row = _row(BASE + 2)
        assert row["doc_type"] == "W-2" and row["amount"] == 55000.0
        assert row["tax_year"] == "2023"           # derived from the date when the LLM omits it
        assert '"provisional": true' not in row["extracted_json"]
        assert "year-2023" in client.tags[BASE + 2]  # tags applied for real results
    finally:
        _cleanup()


def test_llm_failure_stores_provisional_not_poison():
    _cleanup()
    try:
        llm = FakeLLM({"doc_type": "other", "category": "other", "entity": "personal", "tax_year": None,
                       "vendor": None, "amount": None, "date": None, "confidence": 0.0,
                       "description": "Analysis failed: 401 Unauthorized", "tags": [], "extracted_fields": {}})
        client = FakeClient()
        out = ac.process_document(BASE + 3, _doc(W2), llm=llm, llm_ok=True, client=client, embed=False)
        assert out["status"] == "provisional" and out["llm_failed"] and out["provisional"]
        row = _row(BASE + 3)
        assert row["doc_type"] == "W-2"                      # rules found it
        assert "Analysis failed" not in row["extracted_json"]
        assert '"provisional": true' in row["extracted_json"]
        assert row["confidence"] <= 0.35
        assert BASE + 3 not in client.tags                    # no Paperless tags for guesses
        from app import db
        assert BASE + 3 in db.get_provisional_doc_ids()
    finally:
        _cleanup()


def test_no_llm_is_rules_only():
    _cleanup()
    try:
        client = FakeClient()
        out = ac.process_document(BASE + 4, _doc(W2, tags=["year-2022"]), llm=None, llm_ok=False,
                                  client=client, embed=False)
        assert out["status"] == "provisional" and not out["llm_failed"]
        row = _row(BASE + 4)
        assert row["tax_year"] == "2022"   # Paperless year tag wins over body text
        assert row["doc_type"] == "W-2"
        assert json.loads(row["extracted_json"])["method"].startswith("rules")
    finally:
        _cleanup()
