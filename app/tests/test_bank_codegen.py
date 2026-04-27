"""Unit tests for the Phase 11D bank-codegen agent.

Don't hit the network. We test:
  - HAR parser strips noise hosts and sensitive form values
  - HAR parser flags login POSTs and download URLs correctly
  - render_summary_for_prompt produces compact text under the cap
  - bank_codegen.generate_importer raises clean errors when DB state is missing
  - The JSON-response parser tolerates fenced markdown
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ── HAR parser ───────────────────────────────────────────────────────────────

def _make_har(entries):
    return {"log": {"version": "1.2", "creator": {"name": "test"}, "entries": entries}}


def _entry(method, url, status=200, mime="text/html", post_params=None,
           resource_type="document"):
    e = {
        "_resourceType": resource_type,
        "request": {"method": method, "url": url, "headers": [], "cookies": [],
                    "queryString": [], "headersSize": 0, "bodySize": 0},
        "response": {
            "status": status, "statusText": "OK", "headers": [], "cookies": [],
            "content": {"size": 0, "mimeType": mime},
            "redirectURL": "", "headersSize": 0, "bodySize": 0,
        },
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
    }
    if post_params is not None:
        e["request"]["postData"] = {
            "mimeType": "application/x-www-form-urlencoded",
            "params": [{"name": k, "value": v} for k, v in post_params.items()],
        }
    return e


def test_har_parser_strips_noise_hosts():
    from app.ai_agents.har_analyzer import parse_har
    har = _make_har([
        _entry("GET", "https://www.google-analytics.com/collect?event=pageview"),
        _entry("GET", "https://bank.example.com/dashboard"),
        _entry("GET", "https://newrelic.com/agent.js"),
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".har", delete=False) as f:
        json.dump(har, f)
        path = f.name
    try:
        summary = parse_har(path)
        flow_urls = [e["url"] for e in summary["flow"]]
        assert any("bank.example.com" in u for u in flow_urls)
        assert not any("google-analytics" in u for u in flow_urls)
        assert not any("newrelic" in u for u in flow_urls)
    finally:
        os.unlink(path)


def test_har_parser_redacts_sensitive_fields():
    from app.ai_agents.har_analyzer import parse_har
    har = _make_har([
        _entry("POST", "https://bank.example.com/login", status=302,
               post_params={"username": "alice", "password": "hunter2",
                            "otp_code": "123456", "_csrf": "tok"}),
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".har", delete=False) as f:
        json.dump(har, f)
        path = f.name
    try:
        summary = parse_har(path)
        assert summary["form_posts"], "login POST should be captured"
        form = summary["form_posts"][0]["request_form"]
        assert form["username"] == "alice"
        assert form["password"] == "[REDACTED]"
        assert form["otp_code"] == "[REDACTED]"
        assert form["_csrf"] == "tok"  # not sensitive
    finally:
        os.unlink(path)


def test_har_parser_detects_login_url():
    from app.ai_agents.har_analyzer import parse_har
    har = _make_har([
        _entry("GET", "https://bank.example.com/"),
        _entry("POST", "https://bank.example.com/auth/login", status=200,
               post_params={"username": "alice", "password": "x"}),
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".har", delete=False) as f:
        json.dump(har, f)
        path = f.name
    try:
        summary = parse_har(path)
        assert "login" in summary["login_url"].lower()
        assert summary["host"] == "bank.example.com"
    finally:
        os.unlink(path)


def test_har_parser_flags_download_urls():
    from app.ai_agents.har_analyzer import parse_har
    har = _make_har([
        _entry("GET", "https://bank.example.com/statement.pdf",
               mime="application/pdf", resource_type="other"),
        _entry("GET", "https://bank.example.com/api/transactions/export.csv",
               mime="text/csv", resource_type="xhr"),
    ])
    with tempfile.NamedTemporaryFile("w", suffix=".har", delete=False) as f:
        json.dump(har, f)
        path = f.name
    try:
        summary = parse_har(path)
        notable = summary["notable_urls"]
        assert any("statement.pdf" in u for u in notable)
        assert any("export.csv" in u for u in notable)
    finally:
        os.unlink(path)


def test_render_summary_obeys_char_cap():
    from app.ai_agents.har_analyzer import render_summary_for_prompt
    summary = {
        "host": "bank.example.com",
        "login_url": "https://bank.example.com/login",
        "flow": [{"method": "GET", "url": f"https://bank.example.com/page-{i}",
                  "status": 200, "response_mime": "text/html"} for i in range(500)],
        "form_posts": [], "notable_urls": [], "error": "",
    }
    out = render_summary_for_prompt(summary, max_chars=2000)
    assert len(out) <= 2050  # gives a tiny slack for the truncation marker
    assert "truncated" in out


def test_har_parser_handles_missing_file():
    from app.ai_agents.har_analyzer import parse_har
    summary = parse_har("/tmp/definitely-does-not-exist.har")
    assert summary["error"]
    assert summary["flow"] == []


# ── codegen agent ────────────────────────────────────────────────────────────

def test_generate_importer_rejects_unknown_bank():
    from app.ai_agents.bank_codegen import generate_importer
    try:
        generate_importer(99999999)
    except RuntimeError as e:
        assert "not found" in str(e)
        return
    raise AssertionError("expected RuntimeError for unknown bank")


def test_parse_json_response_handles_fences():
    from app.ai_agents.bank_codegen import _parse_json_response
    payload = '{"source_code": "x", "test_code": "y", "generation_notes": "z"}'
    # Direct
    assert _parse_json_response(payload)["source_code"] == "x"
    # Markdown-fenced
    fenced = f"```json\n{payload}\n```"
    assert _parse_json_response(fenced)["source_code"] == "x"
    # Prose preamble that the model sometimes adds
    prosed = f"Sure! Here's the importer:\n\n{payload}\n\nLet me know if..."
    assert _parse_json_response(prosed)["source_code"] == "x"


def test_parse_json_response_raises_on_garbage():
    from app.ai_agents.bank_codegen import _parse_json_response
    try:
        _parse_json_response("this is not json at all")
    except RuntimeError as e:
        assert "could not parse" in str(e).lower()
        return
    raise AssertionError("expected RuntimeError on garbage input")


def test_build_user_message_includes_all_fields():
    from app.ai_agents.bank_codegen import _build_user_message
    bank = {
        "display_name": "Example Bank", "slug": "example_bank",
        "login_url": "https://example.com/login",
        "statements_url": "https://example.com/statements",
        "platform_hint": "fiserv", "notes": "uses SMS MFA",
    }
    recording = {"narration_text": "I logged in and clicked statements"}
    msg = _build_user_message(bank, recording, "(no flow)")
    assert "Example Bank" in msg
    assert "example_bank" in msg
    assert "fiserv" in msg
    assert "uses SMS MFA" in msg
    assert "I logged in and clicked statements" in msg


def test_load_reference_template_is_nonempty():
    from app.ai_agents.bank_codegen import _load_reference_template
    ref = _load_reference_template()
    assert len(ref) > 5000, f"reference template suspiciously short: {len(ref)}"
    assert "save_auth_cookies" in ref
    assert "run_import" in ref
