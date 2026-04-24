"""Unit tests for the shared auth-cookie save/load helpers in
base_bank_importer. Without these, a subtle regression (e.g. changing the
setting-key format, or dropping JSON round-trip) would silently break
every bank importer's cookie persistence on the next edit.
"""
import json

import pytest

from app import db as _db
from app.importers.base_bank_importer import (
    load_auth_cookies,
    save_auth_cookies,
)


class _FakeContext:
    """Stand-in for Playwright's BrowserContext with just `.cookies()`."""
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


@pytest.fixture(autouse=True)
def _cleanup():
    """Remove any test-slug settings before and after each test so runs
    never share state."""
    slugs = ["qa_test_bank", "qa_bank_a", "qa_bank_b"]
    for s in slugs:
        _db.set_setting(f"{s}_cookies", "")
    yield
    for s in slugs:
        _db.set_setting(f"{s}_cookies", "")


class TestSaveAuthCookies:
    def test_roundtrip_non_empty(self):
        cookies = [
            {"name": "sessionId", "value": "abc123", "domain": ".example.com"},
            {"name": "csrf", "value": "xyz", "domain": ".example.com"},
        ]
        n = save_auth_cookies(_FakeContext(cookies), "qa_test_bank")
        assert n == 2
        loaded = load_auth_cookies("qa_test_bank")
        assert loaded == cookies

    def test_empty_cookies_saves_nothing(self):
        n = save_auth_cookies(_FakeContext([]), "qa_test_bank")
        assert n == 0
        assert load_auth_cookies("qa_test_bank") is None

    def test_key_is_slug_plus_cookies_suffix(self):
        cookies = [{"name": "a", "value": "1"}]
        save_auth_cookies(_FakeContext(cookies), "qa_test_bank")
        # Verify the exact setting key used
        raw = _db.get_setting("qa_test_bank_cookies")
        assert raw
        assert json.loads(raw) == cookies

    def test_different_slugs_isolated(self):
        save_auth_cookies(_FakeContext([{"name": "a", "value": "A"}]), "qa_bank_a")
        save_auth_cookies(_FakeContext([{"name": "b", "value": "B"}]), "qa_bank_b")
        a = load_auth_cookies("qa_bank_a")
        b = load_auth_cookies("qa_bank_b")
        assert a and a[0]["name"] == "a"
        assert b and b[0]["name"] == "b"

    def test_save_overwrites_prior_value(self):
        save_auth_cookies(_FakeContext([{"name": "old", "value": "1"}]), "qa_test_bank")
        save_auth_cookies(_FakeContext([{"name": "new", "value": "2"}]), "qa_test_bank")
        loaded = load_auth_cookies("qa_test_bank")
        assert len(loaded) == 1
        assert loaded[0]["name"] == "new"


class TestLoadAuthCookies:
    def test_returns_none_for_unsaved_slug(self):
        assert load_auth_cookies("qa_test_bank") is None

    def test_returns_none_for_empty_string_value(self):
        _db.set_setting("qa_test_bank_cookies", "")
        assert load_auth_cookies("qa_test_bank") is None

    def test_returns_none_for_malformed_json(self):
        _db.set_setting("qa_test_bank_cookies", "not{valid[json")
        assert load_auth_cookies("qa_test_bank") is None

    def test_returns_none_for_non_list_json(self):
        # If someone manually wrote a dict instead of a list
        _db.set_setting("qa_test_bank_cookies", '{"not":"a list"}')
        assert load_auth_cookies("qa_test_bank") is None

    def test_returns_none_for_empty_list(self):
        _db.set_setting("qa_test_bank_cookies", "[]")
        assert load_auth_cookies("qa_test_bank") is None
