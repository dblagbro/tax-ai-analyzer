"""Document-detail route tests.

Covers MED-PASS2-1: GET /api/documents/<id> used to return HTTP 200 with a
stub `{"doc_id": <id>}` for nonexistent IDs, making client code unable to
distinguish "document exists" from "document doesn't exist".
"""
from app.web_ui import app as flask_app


def _authed_client():
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return client


class TestDocumentDetail:
    def test_get_nonexistent_returns_404(self):
        # An ID very unlikely to exist in Paperless OR local DB
        resp = _authed_client().get("/tax-ai-analyzer/api/documents/99999999")
        assert resp.status_code == 404, resp.data[:200]
        body = resp.get_json()
        assert body and "error" in body

    def test_get_zero_returns_404(self):
        # id=0 previously returned {"doc_id": 0} with 200
        resp = _authed_client().get("/tax-ai-analyzer/api/documents/0")
        assert resp.status_code == 404

    def test_requires_auth(self):
        client = flask_app.test_client()
        resp = client.get("/tax-ai-analyzer/api/documents/1")
        assert resp.status_code in (302, 401)
