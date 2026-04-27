"""JS unit tests for app/static/js/dashboard/core.js helpers.

Runs the JS through Node.js if available on the host. Skips entirely if Node
isn't on PATH (the tax-ai-analyzer container doesn't ship Node — the tests
are intended to run on the dev host or in CI). The util helpers tested here
are pure (no DOM, no fetch) so a Node sandbox is sufficient.
"""
import json
import os
import shutil
import subprocess

import pytest

CORE_JS = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dashboard", "core.js")
CORE_JS = os.path.abspath(CORE_JS)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not on PATH")


def _run_in_node(setup_js: str, expr_js: str) -> str:
    """Load core.js into Node, run setup, evaluate `expr`, print as JSON."""
    # core.js references P, esc, fmt, etc. as globals. We define stubs first
    # so the file can load (definitions may reference each other).
    # Node 22 supports top-level await + global object.
    script = f"""
const fs = require('fs');
// Stubs for symbols core.js expects to exist but doesn't define
globalThis.P = '/tax-ai-analyzer';
globalThis.document = {{
  addEventListener: () => {{}},
  getElementById: () => null,
  querySelectorAll: () => [],
  createElement: () => ({{ classList: {{ add() {{}} }} }}),
  body: {{ appendChild: () => {{}} }},
}};
globalThis.window = globalThis;
globalThis.fetch = () => Promise.resolve({{ json: () => ({{}}) }});
globalThis.setInterval = () => 0;

// Load core.js
const code = fs.readFileSync({json.dumps(CORE_JS)}, 'utf-8');
eval(code);

// Test setup + assertion
{setup_js}
const result = ({expr_js});
process.stdout.write(JSON.stringify(result));
"""
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"Node failed (rc={p.returncode}): {p.stderr[:400]}")
    return p.stdout.strip()


# ── esc() — HTML escaping ─────────────────────────────────────────────────────

class TestEsc:
    def test_amp_lt_gt_quote(self):
        out = _run_in_node("", 'esc(\'<script>alert("x")</script>\')')
        assert json.loads(out) == '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;'

    def test_null_returns_empty_string(self):
        out = _run_in_node("", "esc(null)")
        assert json.loads(out) == ""

    def test_undefined_returns_empty_string(self):
        out = _run_in_node("", "esc(undefined)")
        assert json.loads(out) == ""

    def test_empty_string(self):
        out = _run_in_node("", "esc('')")
        assert json.loads(out) == ""

    def test_already_safe_text_unchanged(self):
        out = _run_in_node("", "esc('hello world 123')")
        assert json.loads(out) == "hello world 123"

    def test_number_coerced_to_string(self):
        out = _run_in_node("", "esc(42)")
        assert json.loads(out) == "42"


# ── escColor() — CSS hex-color whitelist (XSS guard) ──────────────────────────

class TestEscColor:
    def test_short_hex_accepted(self):
        out = _run_in_node("", "escColor('#abc')")
        assert json.loads(out) == "#abc"

    def test_long_hex_accepted(self):
        out = _run_in_node("", "escColor('#aabbcc')")
        assert json.loads(out) == "#aabbcc"

    def test_uppercase_hex_accepted(self):
        out = _run_in_node("", "escColor('#AABBCC')")
        assert json.loads(out) == "#AABBCC"

    def test_javascript_scheme_replaced_with_default(self):
        out = _run_in_node("", "escColor('javascript:alert(1)')")
        assert json.loads(out) == "#1a3c5e"

    def test_onclick_injection_blocked(self):
        out = _run_in_node("", "escColor('red\" onclick=\"alert(1)\"')")
        assert json.loads(out) == "#1a3c5e"

    def test_named_color_replaced(self):
        out = _run_in_node("", "escColor('red')")
        assert json.loads(out) == "#1a3c5e"

    def test_null_returns_default(self):
        out = _run_in_node("", "escColor(null)")
        assert json.loads(out) == "#1a3c5e"

    def test_invalid_hex_returns_default(self):
        out = _run_in_node("", "escColor('#GGGGGG')")
        assert json.loads(out) == "#1a3c5e"

    def test_too_short_hex_returns_default(self):
        out = _run_in_node("", "escColor('#ff')")
        assert json.loads(out) == "#1a3c5e"


# ── fmt() — number formatting ─────────────────────────────────────────────────

class TestFmt:
    def test_integer(self):
        out = _run_in_node("", "fmt(1234)")
        assert json.loads(out) == "1,234.00"

    def test_decimal(self):
        out = _run_in_node("", "fmt(1234.5)")
        assert json.loads(out) == "1,234.50"

    def test_zero(self):
        out = _run_in_node("", "fmt(0)")
        assert json.loads(out) == "0.00"

    def test_null_treated_as_zero(self):
        out = _run_in_node("", "fmt(null)")
        assert json.loads(out) == "0.00"

    def test_negative(self):
        out = _run_in_node("", "fmt(-50.5)")
        assert json.loads(out) == "-50.50"

    def test_large_number(self):
        out = _run_in_node("", "fmt(1234567.89)")
        assert json.loads(out) == "1,234,567.89"

    def test_caps_at_two_decimal_places(self):
        out = _run_in_node("", "fmt(1.236)")
        assert json.loads(out) == "1.24"  # rounded to 2dp


# ── fmtB() — byte size formatting ─────────────────────────────────────────────

class TestFmtB:
    def test_zero(self):
        out = _run_in_node("", "fmtB(0)")
        assert json.loads(out) == "0 B"

    def test_bytes(self):
        out = _run_in_node("", "fmtB(500)")
        assert json.loads(out) == "500.0 B"

    def test_kilobytes(self):
        out = _run_in_node("", "fmtB(2048)")
        assert json.loads(out) == "2.0 KB"

    def test_megabytes(self):
        out = _run_in_node("", "fmtB(5_000_000)")
        # 5_000_000 / 1024 / 1024 ≈ 4.77
        assert "MB" in json.loads(out)

    def test_caps_at_GB(self):
        out = _run_in_node("", "fmtB(5e12)")
        # Beyond GB the formatter still tops out at GB per the implementation
        assert "GB" in json.loads(out)


# ── registerTabLoader / loadTab — registry behavior ───────────────────────────

class TestTabRegistry:
    def test_register_and_lookup(self):
        # Mark a fake loader, call loadTab, confirm it was invoked
        out = _run_in_node(
            "let called = false;"
            "registerTabLoader('xyz', () => { called = true; });"
            "loadTab('xyz');",
            "called"
        )
        assert json.loads(out) is True

    def test_unknown_tab_no_error(self):
        # `loadTab('nope')` uses optional chaining; should not throw
        out = _run_in_node(
            "let ok = true;"
            "try { loadTab('does-not-exist'); } catch(e) { ok = false; }",
            "ok"
        )
        assert json.loads(out) is True

    def test_overwrite_replaces_loader(self):
        out = _run_in_node(
            "let v = 'unset';"
            "registerTabLoader('z', () => v = 'first');"
            "registerTabLoader('z', () => v = 'second');"
            "loadTab('z');",
            "v"
        )
        assert json.loads(out) == "second"
