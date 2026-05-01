"""Phase 13 — admin REST routes for proxy endpoints + LMRH hint overrides.

Doesn't actually fire requests through the proxy (the /test route does, but
we don't run it in CI — it's covered by a unit test that mocks the call).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _admin_client():
    from app.web_ui import app
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return client


# ── /api/admin/llm-proxies CRUD ──────────────────────────────────────────────

def test_proxies_list_returns_seeded():
    client = _admin_client()
    r = client.get("/tax-ai-analyzer/api/admin/llm-proxies")
    assert r.status_code == 200
    data = r.get_json()
    assert "endpoints" in data
    # Should have at least the seeded llm-proxy2 row
    labels = [e["label"] for e in data["endpoints"]]
    assert any("proxy2" in l.lower() or "v2" in l.lower() for l in labels)
    # api_key field must NOT be returned in full — only the tail
    for e in data["endpoints"]:
        assert "api_key" not in e
        assert "api_key_tail" in e
        assert len(e["api_key_tail"]) <= 4


def test_proxies_create_then_delete():
    client = _admin_client()
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies", json={
        "label": "test-probe-endpoint",
        "url": "http://probe.example.com/v1",
        "api_key": "test-key-zzz",
        "version": 2,
        "priority": 99,
        "enabled": False,
    })
    assert r.status_code == 201, r.get_json()
    eid = r.get_json()["id"]
    try:
        # GET shows it
        r = client.get("/tax-ai-analyzer/api/admin/llm-proxies")
        labels = [e["label"] for e in r.get_json()["endpoints"]]
        assert "test-probe-endpoint" in labels
    finally:
        # DELETE removes it
        r = client.delete(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}")
        assert r.status_code == 200


def test_proxies_create_validation():
    client = _admin_client()
    # Missing label
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies",
                    json={"url": "http://x", "api_key": "k"})
    assert r.status_code == 400
    # Bad URL scheme
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies",
                    json={"label": "x", "url": "ftp://x", "api_key": "k"})
    assert r.status_code == 400
    # Missing api_key
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies",
                    json={"label": "x", "url": "http://x"})
    assert r.status_code == 400


def test_proxies_update_changes_priority():
    client = _admin_client()
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies", json={
        "label": "upd-test", "url": "http://upd.example.com/v1",
        "api_key": "k", "version": 2, "priority": 50, "enabled": False,
    })
    eid = r.get_json()["id"]
    try:
        r = client.patch(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}",
                         json={"priority": 5, "enabled": True})
        assert r.status_code == 200
        # Confirm via GET
        r = client.get("/tax-ai-analyzer/api/admin/llm-proxies")
        ep = next(e for e in r.get_json()["endpoints"] if e["id"] == eid)
        assert ep["priority"] == 5
        assert ep["enabled"] is True
    finally:
        client.delete(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}")


def test_proxies_update_rejects_bad_version():
    client = _admin_client()
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies", json={
        "label": "ver-test", "url": "http://v.example.com/v1",
        "api_key": "k", "version": 2, "enabled": False,
    })
    eid = r.get_json()["id"]
    try:
        r = client.patch(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}",
                         json={"version": 99})
        assert r.status_code == 400
    finally:
        client.delete(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}")


def test_proxies_reset_breaker():
    client = _admin_client()
    r = client.post("/tax-ai-analyzer/api/admin/llm-proxies", json={
        "label": "brk-test", "url": "http://b.example.com/v1",
        "api_key": "k", "version": 2, "enabled": False,
    })
    eid = r.get_json()["id"]
    try:
        # Trip the breaker by hand
        from app.llm_client import proxy_manager
        for _ in range(3):
            proxy_manager.mark_failure(eid)
        assert proxy_manager.get_breaker_status(eid)["tripped"] is True
        # Reset via API
        r = client.post(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}/reset-breaker")
        assert r.status_code == 200
        assert proxy_manager.get_breaker_status(eid)["tripped"] is False
    finally:
        client.delete(f"/tax-ai-analyzer/api/admin/llm-proxies/{eid}")


# ── /api/admin/llm-hints ─────────────────────────────────────────────────────

def test_hints_list_returns_all_tasks():
    client = _admin_client()
    r = client.get("/tax-ai-analyzer/api/admin/llm-hints")
    assert r.status_code == 200
    data = r.get_json()
    tasks = [h["task"] for h in data["hints"]]
    # All preset tasks should appear
    for expected in ("analysis", "codegen", "chat", "extraction", "classification"):
        assert expected in tasks, f"missing task {expected}"
    # codegen default must include cost=premium (per ops directive)
    codegen = next(h for h in data["hints"] if h["task"] == "codegen")
    assert "cost=premium" in codegen["default"]


def test_hint_set_and_clear():
    client = _admin_client()
    # Set
    r = client.post("/tax-ai-analyzer/api/admin/llm-hints/classification",
                    json={"override": "task=classification, cost=premium, region=us"})
    assert r.status_code == 200
    assert "cost=premium" in r.get_json()["effective"]
    # Clear
    r = client.post("/tax-ai-analyzer/api/admin/llm-hints/classification",
                    json={"override": ""})
    assert r.status_code == 200
    # After clearing, effective falls back to default (which uses economy)
    eff = r.get_json()["effective"]
    assert "cost=economy" in eff or "task=classification" in eff


def test_hint_unknown_task_rejected():
    client = _admin_client()
    r = client.post("/tax-ai-analyzer/api/admin/llm-hints/never-heard-of-this",
                    json={"override": "task=x"})
    assert r.status_code == 400


# ── cascade in build_lmrh_header ─────────────────────────────────────────────

def test_lmrh_cascade_in_reasoning_default():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("reasoning")
    assert "cascade=auto" in out


def test_lmrh_cascade_explicit_arg():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("analysis", cascade="auto")
    assert "cascade=auto" in out
    out2 = build_lmrh_header("analysis")
    assert "cascade" not in out2  # no preset for analysis


def test_proxy_call_mirrors_cascade_to_x_cot_header():
    """When the LMRH hint contains cascade=, proxy_call should also send
    X-Cot-Cascade for proxy versions that read it from the dedicated header."""
    from unittest.mock import MagicMock, patch
    from app.llm_client import proxy_call

    captured = {}

    def fake_create(**kwargs):
        captured["headers"] = kwargs.get("extra_headers") or {}
        resp = MagicMock()
        choice = MagicMock(); choice.message.content = "OK"
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
        resp.model = "test"
        return resp

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = fake_create

    with patch("app.llm_client.proxy_manager.get_all_clients",
               return_value=[(fake_client, "ep1")]):
        proxy_call.call_chat("reasoning", system="s",
                              messages=[{"role": "user", "content": "hi"}])

    headers = captured["headers"]
    assert "LLM-Hint" in headers
    assert "cascade=auto" in headers["LLM-Hint"]
    assert headers.get("X-Cot-Cascade") == "auto"
