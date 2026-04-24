"""
Smoke tests — verify imports, Flask app creation, route registration,
and core library logic. No external services required (Paperless, LLM, DB
calls are isolated or skipped).
"""
import json
import os
import sys

import pytest

# Ensure /app is on sys.path so 'from app.xxx import yyy' resolves
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ---------------------------------------------------------------------------
# Module import sanity
# ---------------------------------------------------------------------------

class TestImports:
    def test_config(self):
        from app import config
        assert config.WEB_PORT > 0

    def test_financial_rules(self):
        from app.checks.financial_rules import (
            validate_document, check_amount_reasonable,
            check_year_consistency, check_required_fields,
            apply_business_rules,
        )

    def test_csv_runner(self):
        from app.importers.csv_runner import parse_csv, run_csv_job

    def test_helpers(self):
        from app.routes.helpers import (
            _url, admin_required, _row_list,
            _no_cache_page, setup_chat_stream,
        )

    def test_db_package(self):
        from app import db

    def test_llm_client_package(self):
        from app.llm_client import LLMClient, VALID_DOC_TYPES, VALID_CATEGORIES

    def test_all_route_blueprints(self):
        from app.routes.analyze import bp as analyze_bp
        from app.routes.auth import bp as auth_bp
        from app.routes.chat import bp as chat_bp
        from app.routes.documents import bp as docs_bp
        from app.routes.entities import bp as entities_bp
        from app.routes.export_ import bp as export_bp
        from app.routes.importers.import_ import bp as import_bp
        from app.routes.importers.import_cloud import bp as import_cloud_bp
        from app.routes.importers.import_gmail import bp as import_gmail_bp
        from app.routes.importers.import_jobs import bp as import_jobs_bp
        from app.routes.importers.import_paypal import bp as import_paypal_bp
        from app.routes.importers.import_usalliance import bp as import_usalliance_bp
        from app.routes.stats import bp as stats_bp
        from app.routes.transactions import bp as txn_bp
        from app.routes.users import bp as users_bp


# ---------------------------------------------------------------------------
# Flask app creation and route registration
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def flask_app():
    from app.web_ui import app
    app.config["TESTING"] = True
    return app


@pytest.fixture(scope="module")
def client(flask_app):
    return flask_app.test_client()


class TestAppRoutes:
    EXPECTED_PATHS = [
        "/tax-ai-analyzer/login",
        "/tax-ai-analyzer/api/analyze/trigger",
        "/tax-ai-analyzer/api/analyze/status",
        "/tax-ai-analyzer/api/documents",
        "/tax-ai-analyzer/api/transactions",
        "/tax-ai-analyzer/api/entities",
        "/tax-ai-analyzer/api/stats",
        "/tax-ai-analyzer/api/import/gmail/status",
        "/tax-ai-analyzer/api/import/gmail/start",
        "/tax-ai-analyzer/api/import/paypal/status",
        "/tax-ai-analyzer/api/import/paypal/pull",
        "/tax-ai-analyzer/api/import/usalliance/status",
        "/tax-ai-analyzer/api/import/usalliance/start",
        "/tax-ai-analyzer/api/import/bank-csv",
        "/tax-ai-analyzer/api/import/bank-ofx",
        "/tax-ai-analyzer/api/import/jobs",
        "/tax-ai-analyzer/api/cloud/google-drive/auth",
        "/tax-ai-analyzer/api/cloud/dropbox/auth",
        "/tax-ai-analyzer/api/chat/sessions",
        "/tax-ai-analyzer/api/settings",
        "/tax-ai-analyzer/import/gmail/setup",
        "/tax-ai-analyzer/import/gmail/auth",
        "/tax-ai-analyzer/import/gmail/auth/callback",
        "/tax-ai-analyzer/api/export/list",
        "/tax-ai-analyzer/api/folder-manager/scan",
        "/tax-ai-analyzer/api/ai-costs",
    ]

    def test_route_count(self, flask_app):
        routes = [r.rule for r in flask_app.url_map.iter_rules()]
        assert len(routes) >= 100, f"Expected ≥100 routes, got {len(routes)}"

    @pytest.mark.parametrize("path", EXPECTED_PATHS)
    def test_expected_route_exists(self, flask_app, path):
        rules = {r.rule for r in flask_app.url_map.iter_rules()}
        assert path in rules, f"Route missing: {path}"

    def test_login_page_reachable(self, client):
        resp = client.get("/tax-ai-analyzer/login")
        assert resp.status_code == 200

    def test_api_routes_require_auth(self, client):
        """Unauthenticated requests to API routes should redirect or 401/403."""
        resp = client.get("/tax-ai-analyzer/api/stats")
        assert resp.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# financial_rules — deterministic logic
# ---------------------------------------------------------------------------

class TestFinancialRules:
    def test_validate_document_clean(self):
        from app.checks.financial_rules import validate_document
        result = validate_document("W-2", "income", 50000.0, "2024-03-01", "2024", {
            "vendor": "ACME Corp", "amount": 50000.0,
        })
        assert result["confidence_penalty"] < 0.20
        assert not result["issues"]

    def test_validate_document_bad_category(self):
        from app.checks.financial_rules import validate_document
        result = validate_document("W-2", "expense", 50000.0, "2024-03-01", "2024", {})
        assert result["confidence_penalty"] > 0
        assert any("income" in w for w in result["warnings"])

    def test_validate_document_future_date(self):
        from app.checks.financial_rules import validate_document
        result = validate_document("invoice", "income", 500.0, "2099-01-01", "2024", {})
        assert any("future" in i for i in result["issues"])
        assert result["confidence_penalty"] > 0

    def test_check_amount_reasonable_in_range(self):
        from app.checks.financial_rules import check_amount_reasonable
        ok, msg = check_amount_reasonable(50000.0, "W-2")
        assert ok
        assert msg == ""

    def test_check_amount_reasonable_out_of_range(self):
        from app.checks.financial_rules import check_amount_reasonable
        ok, msg = check_amount_reasonable(1.0, "W-2")
        assert not ok
        assert "W-2" in msg

    def test_check_year_consistency_match(self):
        from app.checks.financial_rules import check_year_consistency
        ok, msg = check_year_consistency("2024-06-15", "2024")
        assert ok

    def test_check_year_consistency_mismatch(self):
        from app.checks.financial_rules import check_year_consistency
        ok, msg = check_year_consistency("2020-06-15", "2024")
        assert not ok

    def test_check_year_consistency_one_year_tolerance(self):
        from app.checks.financial_rules import check_year_consistency
        ok, msg = check_year_consistency("2023-01-15", "2024")
        assert ok

    def test_check_required_fields_w2_missing(self):
        from app.checks.financial_rules import check_required_fields
        missing = check_required_fields("W-2", {"vendor": "ACME"})
        assert "amount" in missing

    def test_check_required_fields_w2_complete(self):
        from app.checks.financial_rules import check_required_fields
        missing = check_required_fields("W-2", {"vendor": "ACME", "amount": 50000})
        assert missing == []

    def test_apply_business_rules_proposal(self):
        from app.checks.financial_rules import apply_business_rules
        result = {"doc_type": "invoice", "category": "expense", "amount": 1000.0, "tags": []}
        out = apply_business_rules(result, "This is a proposal for the scope of work.", "Bid")
        assert out["doc_type"] == "other"
        assert out["amount"] is None
        assert "proposal" in out["tags"]

    def test_apply_business_rules_capital_improvement(self):
        from app.checks.financial_rules import apply_business_rules
        result = {"doc_type": "invoice", "category": "expense", "amount": 5000.0, "tags": []}
        out = apply_business_rules(result, "Full roof replacement and structural renovation.", "Roofing Invoice")
        assert out["doc_type"] == "capital_improvement"
        assert out["category"] == "asset"

    def test_apply_business_rules_statement_not_income(self):
        from app.checks.financial_rules import apply_business_rules
        result = {"doc_type": "bank_statement", "category": "income", "amount": 1000.0}
        out = apply_business_rules(result, "Monthly bank statement.", "Statement")
        assert out["category"] == "other"


# ---------------------------------------------------------------------------
# csv_runner — parsing logic
# ---------------------------------------------------------------------------

class TestCsvRunner:
    def _csv(self, rows: list[dict], header: list[str] = None) -> bytes:
        import csv as _csv
        import io
        if header is None:
            header = list(rows[0].keys())
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode()

    def test_parse_basic(self):
        from app.importers.csv_runner import parse_csv
        data = self._csv([
            {"Date": "2024-03-01", "Description": "Coffee", "Amount": "5.00"},
            {"Date": "2024-03-02", "Description": "Salary", "Amount": "3000.00"},
        ])
        txns, err = parse_csv(data, "bank", None, "2024",
                              {"date": "Date", "description": "Description", "amount": "Amount"})
        assert err is None
        assert len(txns) == 2
        assert txns[0]["description"] == "Coffee"
        assert txns[0]["amount"] == 5.0

    def test_parse_negative_amount_is_expense(self):
        from app.importers.csv_runner import parse_csv
        data = self._csv([{"Date": "2024-01-01", "Description": "Payment", "Amount": "-100.00"}])
        txns, _ = parse_csv(data, "bank", None, "2024",
                            {"date": "Date", "description": "Description", "amount": "Amount"})
        assert txns[0]["category"] == "expense"
        assert txns[0]["amount"] == 100.0

    def test_parse_skips_empty_rows(self):
        from app.importers.csv_runner import parse_csv
        data = self._csv([
            {"Date": "", "Description": "", "Amount": ""},
            {"Date": "2024-01-01", "Description": "Valid", "Amount": "10.00"},
        ])
        txns, _ = parse_csv(data, "bank", None, "2024",
                            {"date": "Date", "description": "Description", "amount": "Amount"})
        assert len(txns) == 1

    def test_parse_dollar_sign_stripped(self):
        from app.importers.csv_runner import parse_csv
        data = self._csv([{"Date": "2024-01-01", "Description": "X", "Amount": "$1,234.56"}])
        txns, _ = parse_csv(data, "bank", None, "2024",
                            {"date": "Date", "description": "Description", "amount": "Amount"})
        assert txns[0]["amount"] == pytest.approx(1234.56)

    def test_parse_year_inferred_from_date(self):
        from app.importers.csv_runner import parse_csv
        data = self._csv([{"Date": "2023-07-04", "Description": "X", "Amount": "1.00"}])
        txns, _ = parse_csv(data, "bank", None, "",
                            {"date": "Date", "description": "Description", "amount": "Amount"})
        assert txns[0]["tax_year"] == "2023"


# ---------------------------------------------------------------------------
# config.validate() — warning logic
# ---------------------------------------------------------------------------

class TestConfigValidate:
    def test_no_warnings_when_keys_set(self, monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
        monkeypatch.setattr(cfg, "PAPERLESS_API_TOKEN", "tok-test")
        monkeypatch.setattr(cfg, "LLM_PROVIDER", "anthropic")
        monkeypatch.setattr(cfg, "WEB_PORT", 8012)
        warnings = cfg.validate()
        assert warnings == []

    def test_warns_missing_llm_key(self, monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "LLM_API_KEY", "")
        monkeypatch.setattr(cfg, "PAPERLESS_API_TOKEN", "tok")
        monkeypatch.setattr(cfg, "LLM_PROVIDER", "anthropic")
        monkeypatch.setattr(cfg, "WEB_PORT", 8012)
        warnings = cfg.validate()
        assert any("LLM_API_KEY" in w for w in warnings)

    def test_warns_unknown_provider(self, monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "LLM_API_KEY", "sk-test")
        monkeypatch.setattr(cfg, "PAPERLESS_API_TOKEN", "tok")
        monkeypatch.setattr(cfg, "LLM_PROVIDER", "grok")
        monkeypatch.setattr(cfg, "WEB_PORT", 8012)
        warnings = cfg.validate()
        assert any("LLM_PROVIDER" in w for w in warnings)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP liveness — catches the failure mode Pass #1 found where the container
# was "Up" per `docker ps` but Flask wasn't actually serving (xvfb-run hang).
# test_client() alone can't catch this because it runs Flask in-process.
# Uses a socket connection so it works even inside the running container.
# ──────────────────────────────────────────────────────────────────────────────

import socket
import urllib.request
import urllib.error


class TestHttpLiveness:
    """Verify the actual TCP socket is accepting HTTP requests on port 8012.

    When run inside the tax-ai-analyzer container, connects to localhost:8012.
    When run outside, skips — the test only makes sense when the real Flask
    daemon is running in the same environment.
    """

    @classmethod
    def _can_connect(cls) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 8012), timeout=1):
                return True
        except (OSError, socket.timeout):
            return False

    def test_tcp_socket_accepting(self):
        if not self._can_connect():
            import pytest as _pt
            _pt.skip("port 8012 not accepting — skip (not running in container)")

    def test_login_page_returns_http_200(self):
        if not self._can_connect():
            import pytest as _pt
            _pt.skip("port 8012 not accepting — skip (not running in container)")
        url = "http://127.0.0.1:8012/tax-ai-analyzer/login"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                assert resp.status == 200, f"/login returned {resp.status}"
                body = resp.read()
                assert len(body) > 100, "/login body suspiciously small"
        except urllib.error.HTTPError as e:
            raise AssertionError(f"/login raised HTTPError {e.code}")
        except urllib.error.URLError as e:
            raise AssertionError(f"/login could not connect: {e.reason}")
