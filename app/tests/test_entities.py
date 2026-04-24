"""Entity create + update tests.

Covers:
- MED-NEW-1: /api/entities POST used to return {"id": <full entity dict>, ...}
  instead of {"id": <integer>, ...} because db.create_entity() returns a dict
  row but the route assumed it returned just the id.
- MED-NEW-2: the color field was stored server-side without validation,
  accepting payloads like 'javascript:alert(1)' or 'red" onclick="..."'.
"""
import sqlite3

import pytest

from app.web_ui import app as flask_app
from app import db as db_pkg


def _authed_client():
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return client


def _cleanup(slug_prefix: str):
    conn = db_pkg.get_connection()
    try:
        conn.execute("DELETE FROM tax_years WHERE entity_id IN "
                     "(SELECT id FROM entities WHERE slug LIKE ?)",
                     (slug_prefix + "%",))
        conn.execute("DELETE FROM entities WHERE slug LIKE ?", (slug_prefix + "%",))
        conn.commit()
    finally:
        conn.close()


class TestCreateShape:
    def setup_method(self):
        _cleanup("qa_unit_")

    def teardown_method(self):
        _cleanup("qa_unit_")

    def test_create_returns_integer_id(self):
        client = _authed_client()
        resp = client.post(
            "/tax-ai-analyzer/api/entities",
            json={"name": "qa_unit_shape", "entity_type": "personal", "color": "#ABCDEF"},
        )
        assert resp.status_code == 201, resp.data[:200]
        body = resp.get_json()
        assert isinstance(body["id"], int), f"id was {type(body['id'])!r}: {body['id']!r}"
        assert body["id"] > 0

    def test_response_contains_slug_and_name(self):
        client = _authed_client()
        resp = client.post(
            "/tax-ai-analyzer/api/entities",
            json={"name": "qa_unit_shape2", "entity_type": "personal"},
        )
        body = resp.get_json()
        assert "name" in body and "slug" in body
        assert body["name"] == "qa_unit_shape2"


class TestColorValidation:
    def setup_method(self):
        _cleanup("qa_unit_")

    def teardown_method(self):
        _cleanup("qa_unit_")

    @pytest.mark.parametrize("bad_color", [
        "javascript:alert(1)",
        'red" onclick="alert(1)"',
        "<script>",
        "rgb(255,0,0)",  # CSS-valid but not hex — reject for DB simplicity
        "blue",
        "#GGGGGG",       # not hex digits
        "#ff",           # too short (only 2 digits)
    ])
    def test_create_rejects_non_hex_color(self, bad_color):
        resp = _authed_client().post(
            "/tax-ai-analyzer/api/entities",
            json={"name": "qa_unit_bad_color", "color": bad_color},
        )
        assert resp.status_code == 400, (
            f"color {bad_color!r} was accepted: {resp.data[:200]}"
        )

    @pytest.mark.parametrize("good_color", [
        "#abc", "#ABC", "#aabbcc", "#AABBCC", "#aabbccdd",
    ])
    def test_create_accepts_valid_hex(self, good_color):
        resp = _authed_client().post(
            "/tax-ai-analyzer/api/entities",
            json={"name": f"qa_unit_good_{good_color[1:5]}", "color": good_color},
        )
        assert resp.status_code == 201, resp.data[:200]

    def test_create_defaults_color_when_missing(self):
        resp = _authed_client().post(
            "/tax-ai-analyzer/api/entities",
            json={"name": "qa_unit_no_color"},
        )
        assert resp.status_code == 201

    def test_update_rejects_bad_color(self):
        # Create first
        resp = _authed_client().post(
            "/tax-ai-analyzer/api/entities",
            json={"name": "qa_unit_upd", "color": "#123456"},
        )
        assert resp.status_code == 201
        eid = resp.get_json()["id"]
        # Attempt bad color on update
        resp2 = _authed_client().post(
            f"/tax-ai-analyzer/api/entities/{eid}",
            json={"color": "javascript:x"},
        )
        assert resp2.status_code == 400
