"""Phase 11F — codegen regenerate-with-feedback loop tests.

Mocks the LLM. Verifies:
  - parent_id chains correctly in the DB
  - feedback_text is persisted on the new row
  - the prompt sent to the LLM contains the prior draft + feedback verbatim
  - missing feedback raises a clean error
  - mismatched parent_id (different bank) is rejected
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _seed_bank_with_first_draft(label="RegenProbe"):
    """Helper: create bank + recording + a first generated draft.
    Returns (bank_id, recording_id, draft_id, har_path)."""
    from app import db
    bank_id = db.create_pending_bank(
        display_name=label,
        login_url=f"https://{label.lower()}.example.com/login",
    )
    har = {"log": {"entries": [{
        "_resourceType": "document",
        "request": {"method": "GET",
                    "url": f"https://{label.lower()}.example.com/", "headers": [],
                    "cookies": [], "queryString": [], "headersSize": 0, "bodySize": 0},
        "response": {"status": 200, "statusText": "OK", "headers": [],
                     "cookies": [], "content": {"size": 0, "mimeType": "text/html"},
                     "redirectURL": "", "headersSize": 0, "bodySize": 0},
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
    }]}}
    har_path = f"/tmp/regen_probe_{label}.har"
    with open(har_path, "w") as f:
        json.dump(har, f)
    rec_id = db.add_recording(
        pending_bank_id=bank_id, har_path=har_path,
        narration_text="Logged in via username + password.",
        byte_size=os.path.getsize(har_path),
    )
    draft_id = db.add_generated_importer(
        pending_bank_id=bank_id, recording_id=rec_id,
        source_code='SOURCE = "first_draft"\n# placeholder\n',
        test_code="", llm_model="claude-opus-4-7",
        llm_tokens_in=10, llm_tokens_out=5,
        generation_notes="first pass",
        validation_status="shape_error",
        validation_notes="missing run_import",
    )
    return bank_id, rec_id, draft_id, har_path


def _cleanup(bank_id, har_path):
    from app import db
    db.delete_pending_bank(bank_id)
    if os.path.exists(har_path):
        os.unlink(har_path)


def _fake_llm_response(source_code, gen_notes="regen pass"):
    """Build a MagicMock that mimics anthropic.messages.create's response."""
    fake = MagicMock()
    blk = MagicMock(); blk.type = "text"
    blk.text = json.dumps({
        "source_code": source_code,
        "test_code": "def test_x(): pass\n",
        "generation_notes": gen_notes,
    })
    fake.content = [blk]
    fake.usage = MagicMock(
        input_tokens=50, output_tokens=20,
        cache_creation_input_tokens=0, cache_read_input_tokens=15000,
    )
    return fake


def test_regenerate_chains_via_parent_id():
    from app import db
    from app.ai_agents.bank_codegen import generate_importer
    bank_id, rec_id, draft_id, har_path = _seed_bank_with_first_draft()
    try:
        with patch("anthropic.Anthropic") as fake_cls, \
             patch("app.llm_client.proxy_call.call_anthropic_messages",
                   side_effect=Exception("force direct fallback")):
            fake_client = MagicMock()
            fake_client.messages.create.return_value = _fake_llm_response(
                "import os\n"
                "from app.importers.base_bank_importer import launch_browser\n"
                'SOURCE = "regenprobe"\n'
                "def run_import(username,password,years,consume_path,entity_slug,"
                "job_id,log=None,cookies=None,entity_id=None):\n"
                "    return {'imported':0,'skipped':0,'errors':0}\n"
                "def set_mfa_code(j,c): pass\n"
            )
            fake_cls.return_value = fake_client
            result = generate_importer(
                bank_id, parent_generated_id=draft_id,
                feedback="Fix the missing run_import function",
            )
        # New row has parent_id pointing at the original
        new = db.get_generated_importer(result["id"])
        assert new["parent_id"] == draft_id
        assert "Fix the missing" in new["feedback_text"]
        # Original is preserved unchanged
        orig = db.get_generated_importer(draft_id)
        assert orig["source_code"] == 'SOURCE = "first_draft"\n# placeholder\n'
    finally:
        _cleanup(bank_id, har_path)


def test_regenerate_includes_prior_source_in_prompt():
    """The prompt sent to the LLM must contain BOTH the prior source AND the
    admin's feedback verbatim — that's the entire point of the loop."""
    from app.ai_agents.bank_codegen import generate_importer
    bank_id, rec_id, draft_id, har_path = _seed_bank_with_first_draft()
    captured = {}
    try:
        with patch("anthropic.Anthropic") as fake_cls, \
             patch("app.llm_client.proxy_call.call_anthropic_messages",
                   side_effect=Exception("force direct")):
            fake_client = MagicMock()

            def grab(**kwargs):
                captured["messages"] = kwargs.get("messages")
                return _fake_llm_response(
                    'SOURCE = "x"\n'
                    'def run_import(username,password,years,consume_path,'
                    'entity_slug,job_id): pass\n'
                    'def set_mfa_code(j,c): pass\n'
                )
            fake_client.messages.create.side_effect = grab
            fake_cls.return_value = fake_client
            generate_importer(
                bank_id, parent_generated_id=draft_id,
                feedback="MAGIC_FEEDBACK_MARKER xyz",
            )
        user_text = captured["messages"][0]["content"]
        assert "MAGIC_FEEDBACK_MARKER xyz" in user_text
        assert 'SOURCE = "first_draft"' in user_text
        assert "REGENERATION REQUEST" in user_text
    finally:
        _cleanup(bank_id, har_path)


def test_regenerate_requires_feedback_when_parent_set():
    from app.ai_agents.bank_codegen import generate_importer
    bank_id, rec_id, draft_id, har_path = _seed_bank_with_first_draft()
    try:
        try:
            generate_importer(bank_id, parent_generated_id=draft_id, feedback="")
        except RuntimeError as e:
            assert "feedback" in str(e).lower()
            return
        raise AssertionError("expected RuntimeError")
    finally:
        _cleanup(bank_id, har_path)


def test_regenerate_rejects_parent_from_other_bank():
    from app.ai_agents.bank_codegen import generate_importer
    bank_a_id, _, draft_a, har_a = _seed_bank_with_first_draft(label="BankA")
    bank_b_id, _, _, har_b = _seed_bank_with_first_draft(label="BankB")
    try:
        try:
            generate_importer(
                bank_b_id, parent_generated_id=draft_a,
                feedback="x",
            )
        except RuntimeError as e:
            assert "not found" in str(e).lower()
            return
        raise AssertionError("expected RuntimeError")
    finally:
        _cleanup(bank_a_id, har_a)
        _cleanup(bank_b_id, har_b)


def test_first_pass_has_no_parent():
    """A fresh generation (no parent_generated_id) leaves parent_id NULL."""
    from app import db
    from app.ai_agents.bank_codegen import generate_importer
    bank_id, rec_id, draft_id, har_path = _seed_bank_with_first_draft(
        label="FreshGen")
    try:
        with patch("anthropic.Anthropic") as fake_cls, \
             patch("app.llm_client.proxy_call.call_anthropic_messages",
                   side_effect=Exception("force direct")):
            fake_client = MagicMock()
            fake_client.messages.create.return_value = _fake_llm_response(
                'SOURCE = "fresh"\n'
                'def run_import(username,password,years,consume_path,'
                'entity_slug,job_id): pass\n'
                'def set_mfa_code(j,c): pass\n'
            )
            fake_cls.return_value = fake_client
            result = generate_importer(bank_id)  # no parent
        new = db.get_generated_importer(result["id"])
        assert new["parent_id"] is None
        assert (new["feedback_text"] or "") == ""
    finally:
        _cleanup(bank_id, har_path)


# ── Route ────────────────────────────────────────────────────────────────────

def test_regenerate_route_rejects_empty_feedback():
    from app.web_ui import app
    bank_id, rec_id, draft_id, har_path = _seed_bank_with_first_draft(
        label="RouteFeed")
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = "1"; sess["_fresh"] = True
        r = client.post(
            f"/tax-ai-analyzer/api/admin/banks/{bank_id}/generated/{draft_id}/regenerate",
            json={"feedback": "  "},
        )
        assert r.status_code == 400
        assert "feedback" in r.get_json()["error"].lower()
    finally:
        _cleanup(bank_id, har_path)


def test_regenerate_route_404_for_unknown_draft():
    from app.web_ui import app
    bank_id, rec_id, draft_id, har_path = _seed_bank_with_first_draft(
        label="Route404")
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = "1"; sess["_fresh"] = True
        r = client.post(
            f"/tax-ai-analyzer/api/admin/banks/{bank_id}/generated/9999999/regenerate",
            json={"feedback": "x"},
        )
        assert r.status_code == 404
    finally:
        _cleanup(bank_id, har_path)
