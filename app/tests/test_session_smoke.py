"""Session-smoke tests — complement to test_smoke.py.

These validate the shape of the running app against the 2026-04 additions:
- Every rendered page crawls without Jinja errors, traceback leaks, or broken
  onclick handler references.
- The full route matrix returns 200/302/401/403 — never 500.
- Fresh-DB init_db() succeeds and creates all expected columns + indexes.
- /api/health/extended reports the shape the UI expects.

Run from inside the container:  python3 -m pytest app/tests/test_session_smoke.py -v
Or via module:                   python3 -m app.tests.test_session_smoke
"""
import os
import re
import sys
import tempfile

try:
    import pytest  # optional — runnable as a plain script too
except ImportError:
    pytest = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PAGES = [
    "/tax-ai-analyzer/",
    "/tax-ai-analyzer/documents",
    "/tax-ai-analyzer/import",
    "/tax-ai-analyzer/chat",
    "/tax-ai-analyzer/reports",
    "/tax-ai-analyzer/settings",
    "/tax-ai-analyzer/entities",
    "/tax-ai-analyzer/ai-costs",
    "/tax-ai-analyzer/tax-review",
    "/tax-ai-analyzer/folder-manager",
    "/tax-ai-analyzer/mileage",
    "/tax-ai-analyzer/activity",
]

RED_FLAGS = [
    ("jinja eval error",    re.compile(r"jinja2\.exceptions\.", re.I)),
    ("UndefinedError",      re.compile(r"UndefinedError", re.I)),
    ("TemplateSyntaxError", re.compile(r"TemplateSyntaxError", re.I)),
    ("Python traceback",    re.compile(r"Traceback \(most recent call last\)")),
]

JS_BUILTINS = {
    "event", "if", "return", "this", "Math", "parseInt", "parseFloat",
    "alert", "confirm", "prompt", "setTimeout", "setInterval",
    "Number", "String", "Array", "Object", "JSON", "encodeURIComponent",
    "decodeURIComponent", "window", "document", "fetch", "Date", "Promise",
    "true", "false", "null", "undefined", "new", "typeof", "void",
    "delete", "in", "of", "navigator", "location", "history",
    "localStorage",
    # App-global helpers
    "esc", "fmt", "post", "toast", "sw", "closeM", "openM", "P",
}


def _client_with_admin():
    from app.web_ui import app
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return app, client


def _extract_fn_calls(html):
    calls = set()
    for attr in ["onclick", "onchange", "onsubmit", "oninput", "onload", "onblur"]:
        for m in re.findall(attr + r'=["\']([^"\']+)["\']', html):
            for fn in re.findall(r"(?<![\w.])([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", m):
                if fn not in JS_BUILTINS:
                    calls.add(fn)
    return calls


def _extract_defined_fns(html):
    names = set()
    patterns = [
        r"(?:^|\n)\s*(?:async\s+)?function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(",
        r"window\.([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:async\s+)?function",
        r"window\.([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:async\s+)?\(",
        r"(?:const|let|var)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:async\s+)?(?:function|\()",
    ]
    for p in patterns:
        names.update(re.findall(p, html))
    return names


# ---------------------------------------------------------------------------
# HTML crawl: every page must render without leaks or broken handlers
# ---------------------------------------------------------------------------

class TestHtmlCrawl:
    def test_all_pages_render_200(self):
        _, client = _client_with_admin()
        for path in PAGES:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path}: {resp.status_code}"

    def test_no_jinja_leaks(self):
        _, client = _client_with_admin()
        for path in PAGES:
            body = client.get(path).data.decode("utf-8", errors="replace")
            for name, pat in RED_FLAGS:
                assert not pat.search(body), f"{path}: {name}"
            # Strip scripts/pre/code and check for dangling Jinja
            s = re.sub(r"<script\b[^>]*>.*?</script>", "", body, flags=re.DOTALL)
            s = re.sub(r"<pre\b[^>]*>.*?</pre>", "", s, flags=re.DOTALL)
            s = re.sub(r"<code\b[^>]*>.*?</code>", "", s, flags=re.DOTALL)
            dangling = re.findall(r"\{\{[^}]+\}\}", s)
            assert not dangling, f"{path}: unrendered Jinja: {dangling[:3]}"

    def test_handler_functions_resolve(self):
        _, client = _client_with_admin()
        all_html = ""
        per_page = {}
        for path in PAGES:
            body = client.get(path).data.decode("utf-8", errors="replace")
            all_html += body
            per_page[path] = body
        defined = _extract_defined_fns(all_html)
        for path, body in per_page.items():
            missing = _extract_fn_calls(body) - defined
            assert not missing, f"{path}: unresolved handler fns: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Route matrix: no 5xx on any GET
# ---------------------------------------------------------------------------

class TestRouteMatrix:
    def test_no_get_route_returns_5xx(self):
        app, client = _client_with_admin()
        testable = [
            str(r) for r in app.url_map.iter_rules()
            if "GET" in r.methods and "<" not in str(r) and "static" not in r.endpoint
        ]
        failures = []
        for path in testable:
            resp = client.get(path)
            if resp.status_code >= 500:
                failures.append((path, resp.status_code))
        assert not failures, f"5xx on: {failures}"


# ---------------------------------------------------------------------------
# Fresh-DB migrations
# ---------------------------------------------------------------------------

class TestFreshDbInit:
    def test_fresh_init_creates_new_schema(self):
        from app.db import core as dbcore
        # Save + restore globals so subsequent tests still see the production DB
        orig_db_path = dbcore.DB_PATH
        orig_data_dir = os.environ.get("DATA_DIR")
        tmp = tempfile.mkdtemp()
        fresh = os.path.join(tmp, "fresh.db")
        try:
            os.environ["DATA_DIR"] = tmp
            dbcore.DB_PATH = fresh
            dbcore.init_db()
            conn = dbcore.get_connection()
            try:
                checks = [
                    ("transactions", "vendor_normalized"),
                    ("analyzed_documents", "cross_source_duplicate"),
                    ("analyzed_documents", "is_duplicate"),
                    ("import_jobs", "created_at"),
                ]
                for table, col in checks:
                    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                    assert col in cols, f"{table}.{col} missing from fresh init"

                tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for t in ["transaction_links", "mileage_log", "plaid_items"]:
                    assert t in tables, f"table {t} missing"

                idx = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                for i in ["idx_txnlinks_txn", "idx_txnlinks_doc",
                          "idx_plaid_item_id", "idx_mileage_date"]:
                    assert i in idx, f"index {i} missing"
            finally:
                conn.close()
        finally:
            # Always restore, even on test failure
            dbcore.DB_PATH = orig_db_path
            if orig_data_dir is not None:
                os.environ["DATA_DIR"] = orig_data_dir
            else:
                os.environ.pop("DATA_DIR", None)
            if os.path.exists(fresh):
                os.remove(fresh)
            if os.path.exists(tmp):
                os.rmdir(tmp)


# ---------------------------------------------------------------------------
# Extended health endpoint shape
# ---------------------------------------------------------------------------

class TestHealthExtended:
    def test_shape(self):
        _, client = _client_with_admin()
        r = client.get("/tax-ai-analyzer/api/health/extended")
        assert r.status_code == 200
        data = r.get_json()
        for key in ["row_counts", "threads", "disk", "recent_activity",
                    "features", "overall"]:
            assert key in data, f"missing key: {key}"
        assert "status" in data["overall"]
        assert isinstance(data["row_counts"].get("transactions"), int)


# ---------------------------------------------------------------------------
# Allow running as a script
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Minimal self-run without pytest
    classes = [TestHtmlCrawl, TestRouteMatrix, TestFreshDbInit, TestHealthExtended]
    failed = 0
    passed = 0
    for cls in classes:
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                try:
                    getattr(inst, name)()
                    print(f"  OK   {cls.__name__}.{name}")
                    passed += 1
                except AssertionError as e:
                    print(f"  FAIL {cls.__name__}.{name}: {e}")
                    failed += 1
                except Exception as e:
                    print(f"  ERR  {cls.__name__}.{name}: {type(e).__name__}: {e}")
                    failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if not failed else 1)
