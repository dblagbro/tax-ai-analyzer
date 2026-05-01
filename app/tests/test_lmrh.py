"""LMRH header builder + proxy_manager + proxy_call (Phase 12)."""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ── lmrh.build_lmrh_header ───────────────────────────────────────────────────

def test_lmrh_basic_task():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("analysis")
    # Always emits task; default cost from preset
    assert "task=analysis" in out
    assert "cost=standard" in out
    assert "safety-min=3" in out  # preset default for analysis


def test_lmrh_codegen_preset():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("codegen")
    assert "task=codegen" in out
    assert "cost=premium" in out
    assert "context-length=60000" in out


def test_lmrh_explicit_overrides_preset():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("analysis", cost="economy", safety_min=5)
    assert "cost=economy" in out
    assert "cost=standard" not in out
    assert "safety-min=5" in out


def test_lmrh_no_model_pref_hardcoded():
    """Per ops directive — never emit model-pref or fallback-chain. The proxy
    picks the model based on task+cost dims."""
    from app.llm_client.lmrh import build_lmrh_header, TASK_PRESETS
    for task in TASK_PRESETS:
        out = build_lmrh_header(task)
        assert "model-pref" not in out, f"{task!r}: model-pref leaked"
        assert "fallback-chain" not in out, f"{task!r}: fallback-chain leaked"


def test_lmrh_unknown_task_still_works():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("never-heard-of-this-task")
    assert out == "task=never-heard-of-this-task"


def test_lmrh_empty_task_returns_empty():
    from app.llm_client.lmrh import build_lmrh_header
    assert build_lmrh_header("") == ""


def test_lmrh_vision_modality():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("analysis", has_images=True)
    assert "modality=vision" in out


def test_lmrh_extras_passthrough():
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("chat", extras={"region": "us", "latency": "low"})
    assert "region=us" in out
    assert "latency=low" in out


def test_lmrh_uses_comma_separator():
    """Spec calls for comma+space separator on structured-field lists."""
    from app.llm_client.lmrh import build_lmrh_header
    out = build_lmrh_header("codegen")
    # No semicolon outside ;require — comma between dims
    assert ", " in out
    # Should NOT use semicolons as primary separator
    pieces = out.split(", ")
    for p in pieces:
        # ;require is the only legal use of ;
        if ";" in p:
            assert p.endswith(";require"), f"unexpected ; in {p!r}"


# ── lmrh.get_hint with DB override ───────────────────────────────────────────

def test_get_hint_db_override():
    """Operator can override a default hint via db.set_setting."""
    from app import db
    from app.llm_client.lmrh import get_hint
    key = "lmrh.hint.test_override"
    db.set_setting(key, "task=chat, cost=economy, region=eu")
    try:
        out = get_hint("test_override")
        assert out == "task=chat, cost=economy, region=eu"
    finally:
        db.set_setting(key, "")


# ── proxy_manager: client builder + breaker ──────────────────────────────────

def test_build_anthropic_client_v2():
    from app.llm_client.proxy_manager import build_anthropic_client
    ep = {"id": "x", "url": "http://llm-proxy2:3000/v1",
          "api_key": "test-key-v2", "version": 2}
    client = build_anthropic_client(ep, lmrh_hint="task=codegen, cost=premium")
    # Just confirm it built without raising
    assert client is not None


def test_circuit_breaker_trips_after_failures():
    from app.llm_client import proxy_manager
    proxy_manager.reset_holds() if hasattr(proxy_manager, "reset_holds") else None
    # Reset internal state
    proxy_manager._state.clear()
    eid = "test-ep-001"
    # 3 failures should trip
    for _ in range(3):
        proxy_manager.mark_failure(eid)
    status = proxy_manager.get_breaker_status(eid)
    assert status["tripped"] is True
    assert status["failures"] == 3
    # mark_success resets
    proxy_manager.mark_success(eid)
    status = proxy_manager.get_breaker_status(eid)
    assert status["tripped"] is False
    assert status["failures"] == 0


def test_get_endpoints_filters_disabled():
    """get_endpoints() should only return enabled rows."""
    from app.llm_client import proxy_manager
    eps = proxy_manager.get_endpoints()
    for e in eps:
        # llm_proxy_list_endpoints with default include_disabled=False
        assert e.get("enabled", 1) == 1


# ── proxy_call: NoProxyAvailable when pool empty ─────────────────────────────

def test_proxy_call_chat_raises_when_pool_empty():
    from app.llm_client import proxy_call
    with patch("app.llm_client.proxy_manager.get_all_clients", return_value=[]):
        try:
            proxy_call.call_chat("analysis", system="s", messages=[
                {"role": "user", "content": "hi"}])
        except proxy_call.NoProxyAvailable:
            return
        raise AssertionError("expected NoProxyAvailable")


def test_proxy_call_anthropic_raises_when_pool_empty():
    from app.llm_client import proxy_call
    with patch(
        "app.llm_client.proxy_manager.get_all_anthropic_clients",
        return_value=[],
    ):
        try:
            proxy_call.call_anthropic_messages(
                "codegen", model="m", system="s",
                messages=[{"role": "user", "content": "hi"}],
            )
        except proxy_call.NoProxyAvailable:
            return
        raise AssertionError("expected NoProxyAvailable")


def test_proxy_call_chat_walks_chain_on_failure():
    """Two endpoints; first one raises, second returns success."""
    from app.llm_client import proxy_call

    # Build mock OpenAI clients
    fail_client = MagicMock()
    fail_client.chat.completions.create.side_effect = RuntimeError("boom")

    ok_client = MagicMock()
    fake_resp = MagicMock()
    fake_choice = MagicMock()
    fake_choice.message.content = "ok-response"
    fake_resp.choices = [fake_choice]
    fake_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    fake_resp.model = "claude-haiku-4-5-20251001"
    ok_client.chat.completions.create.return_value = fake_resp

    with patch(
        "app.llm_client.proxy_manager.get_all_clients",
        return_value=[(fail_client, "ep1"), (ok_client, "ep2")],
    ):
        result = proxy_call.call_chat(
            "classification", system="s", messages=[
                {"role": "user", "content": "hi"}],
        )
    assert result["content"] == "ok-response"
    assert result["endpoint_id"] == "ep2"
    assert result["in_tokens"] == 10
    assert result["out_tokens"] == 5


def test_streaming_client_picks_top_priority():
    """get_streaming_anthropic_client returns the highest-priority healthy
    endpoint pre-configured with the LMRH hint."""
    from unittest.mock import MagicMock, patch
    from app.llm_client import proxy_call

    fake_a = MagicMock(); fake_b = MagicMock()
    with patch(
        "app.llm_client.proxy_manager.get_all_anthropic_clients",
        return_value=[(fake_a, "ep_top"), (fake_b, "ep_lower")],
    ):
        client, eid = proxy_call.get_streaming_anthropic_client("chat")
    assert client is fake_a
    assert eid == "ep_top"


def test_streaming_client_raises_when_pool_empty():
    from unittest.mock import patch
    from app.llm_client import proxy_call
    with patch("app.llm_client.proxy_manager.get_all_anthropic_clients",
               return_value=[]):
        try:
            proxy_call.get_streaming_anthropic_client("chat")
        except proxy_call.NoProxyAvailable:
            return
        raise AssertionError("expected NoProxyAvailable")


def test_proxy_call_anthropic_walks_chain_and_logs_cache():
    from app.llm_client import proxy_call

    fail_client = MagicMock()
    fail_client.messages.create.side_effect = RuntimeError("boom")

    ok_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.usage = MagicMock(
        input_tokens=100, output_tokens=20,
        cache_creation_input_tokens=20000, cache_read_input_tokens=0,
    )
    fake_resp.model = "claude-opus-4-7"
    ok_client.messages.create.return_value = fake_resp

    with patch(
        "app.llm_client.proxy_manager.get_all_anthropic_clients",
        return_value=[(fail_client, "ep1"), (ok_client, "ep2")],
    ):
        result = proxy_call.call_anthropic_messages(
            "codegen", model="claude-opus-4-7", system="s",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert result["endpoint_id"] == "ep2"
    assert result["in_tokens"] == 100
    assert result["out_tokens"] == 20
    assert result["cache_creation"] == 20000
    assert result["cache_read"] == 0
    assert result["model"] == "claude-opus-4-7"


# ── DB seed: v1 not in seed (per ops 2026-04-30) ─────────────────────────────

# ── URL normalization (Devin's "no local-access URLs" directive) ─────────────

def test_normalize_local_urls_to_public():
    from app.db.core import _normalize_llm_proxy_url, PUBLIC_LLM_PROXY2_URL
    # Every form of "local" must rewrite to public
    cases = [
        "http://localhost:8055/v1",
        "http://127.0.0.1:8055/v1",
        "http://host.docker.internal:8055/v1",
        "http://llm-proxy2:3000/v1",
        "http://llm-proxy-manager:3000/v1",
        "http://[::1]:8055/v1",
        "",
    ]
    for u in cases:
        assert _normalize_llm_proxy_url(u) == PUBLIC_LLM_PROXY2_URL, \
            f"failed to normalize {u!r}"


def test_public_url_passes_through():
    from app.db.core import _normalize_llm_proxy_url, PUBLIC_LLM_PROXY2_URL
    assert _normalize_llm_proxy_url(PUBLIC_LLM_PROXY2_URL) == PUBLIC_LLM_PROXY2_URL
    # A different but public URL should also pass through
    other = "https://other-proxy.example.com/v1"
    assert _normalize_llm_proxy_url(other) == other


def test_boot_migration_rewrites_local_url_and_swaps_key():
    """A pre-existing endpoint with a local URL + old key gets rewritten on
    every boot to public URL + LLM_PROXY2_KEY (if set)."""
    import os, sqlite3, tempfile
    from app.db import core as dbcore

    orig_path = dbcore.DB_PATH
    orig_env = {k: os.environ.get(k) for k in
                ("LLM_PROXY_KEY", "LLM_PROXY2_KEY")}
    tmp = tempfile.mkdtemp()
    fresh = os.path.join(tmp, "norm_probe.db")
    try:
        dbcore.DB_PATH = fresh
        # First boot: seed with old vars (local URL, old key)
        os.environ["LLM_PROXY_KEY"] = "old-key-aaa"
        os.environ.pop("LLM_PROXY2_KEY", None)
        os.environ["LLM_PROXY2_URL"] = "http://llm-proxy2:3000/v1"
        dbcore.init_db()
        # Confirm initial state — even the seed should normalize the local URL
        conn = sqlite3.connect(fresh)
        rows = conn.execute(
            "SELECT url, api_key FROM llm_proxy_endpoints"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == dbcore.PUBLIC_LLM_PROXY2_URL
        assert rows[0][1] == "old-key-aaa"

        # Now ops drops a new key; second boot picks it up and rewrites.
        os.environ["LLM_PROXY2_KEY"] = "llmp-new-bbb"
        dbcore.init_db()
        conn = sqlite3.connect(fresh)
        rows = conn.execute(
            "SELECT url, api_key FROM llm_proxy_endpoints"
        ).fetchall()
        conn.close()
        assert rows[0][0] == dbcore.PUBLIC_LLM_PROXY2_URL
        assert rows[0][1] == "llmp-new-bbb"
    finally:
        dbcore.DB_PATH = orig_path
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if os.path.exists(fresh):
            os.remove(fresh)
        if os.path.exists(tmp):
            os.rmdir(tmp)


def test_seed_does_not_add_v1():
    """Ensure the seed function never inserts an llm-proxy-manager (v1) row.

    Behavioural test: run the seed against an in-memory DB and inspect the
    rows it actually wrote (rather than grepping source).
    """
    import os, sqlite3, tempfile
    from app.db import core as dbcore

    # Save + restore globals so other tests aren't disturbed
    orig_path = dbcore.DB_PATH
    orig_proxy_key = os.environ.get("LLM_PROXY_KEY", "")
    tmp = tempfile.mkdtemp()
    fresh = os.path.join(tmp, "seed_probe.db")
    try:
        dbcore.DB_PATH = fresh
        os.environ["LLM_PROXY_KEY"] = "probe-key"
        dbcore.init_db()
        conn = sqlite3.connect(fresh)
        rows = conn.execute(
            "SELECT label, url, version, enabled FROM llm_proxy_endpoints"
        ).fetchall()
        conn.close()
        # Exactly one seed row: v2 only
        assert len(rows) == 1, f"expected 1 seeded row, got {len(rows)}: {rows}"
        label, url, version, enabled = rows[0]
        assert version == 2, f"version must be 2, got {version}"
        assert enabled == 1, "primary v2 row must be enabled"
        assert "v2" in label.lower() or "proxy2" in label.lower()
        # No legacy v1 marker anywhere
        assert "manager" not in label.lower(), \
            f"v1 (llm-proxy-manager) leaked into seed: {label!r}"
    finally:
        dbcore.DB_PATH = orig_path
        if orig_proxy_key:
            os.environ["LLM_PROXY_KEY"] = orig_proxy_key
        else:
            os.environ.pop("LLM_PROXY_KEY", None)
        if os.path.exists(fresh):
            os.remove(fresh)
        if os.path.exists(tmp):
            os.rmdir(tmp)
