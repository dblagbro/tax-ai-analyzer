"""Unit tests for the Phase 11E auto-deploy pipeline.

Don't hit the network. We verify:
  - Pre-conditions reject unapproved / unvalidated / missing-source rows
  - Successful deploy writes a file with our deploy marker
  - Refuses to overwrite a hand-written importer (no marker on line 1)
  - undeploy() refuses to remove a non-marker file
  - The auto-import dispatcher resolves an importer module by slug
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


VALID_SOURCE = '''"""Mock importer."""
from __future__ import annotations
SOURCE = "deploy_probe_bank"


def run_import(username, password, years, consume_path, entity_slug, job_id,
               log=None, cookies=None, entity_id=None):
    return {"imported": 0, "skipped": 0, "errors": 0}


def set_mfa_code(job_id, code):
    pass
'''


def _make_bank_with_generated(*, approved=True, validation_status="pass",
                              source=VALID_SOURCE, slug_prefix="deploy_probe"):
    """Helper: create a pending bank + a generated_importers row.
    Returns (bank_id, gen_id, slug). Caller cleans up via db.delete_pending_bank()."""
    from app import db
    # use a unique slug per call so parallel test runs don't collide
    import uuid
    slug = f"{slug_prefix}_{uuid.uuid4().hex[:6]}"
    bank_id = db.create_pending_bank(
        display_name=f"Deploy Probe {slug}",
        login_url="https://example.com/login",
        slug=slug,
    )
    gen_id = db.add_generated_importer(
        pending_bank_id=bank_id,
        source_code=source,
        test_code="",
        llm_model="claude-opus-4-7",
        llm_tokens_in=10, llm_tokens_out=5,
        generation_notes="test",
        validation_status=validation_status,
        validation_notes="",
    )
    if approved:
        db.approve_generated_importer(gen_id, approved_by=1)
    return bank_id, gen_id, slug


def _cleanup(bank_id, slug):
    from app import db
    from app.ai_agents.importer_deployer import IMPORTERS_DIR
    db.delete_pending_bank(bank_id)
    target = IMPORTERS_DIR / f"{slug}_importer.py"
    if target.exists():
        target.unlink()


def test_deploy_writes_marker_and_marks_live():
    from app import db
    from app.ai_agents.importer_deployer import deploy, IMPORTERS_DIR, DEPLOY_MARKER
    bank_id, gen_id, slug = _make_bank_with_generated()
    try:
        result = deploy(gen_id)
        path = result["path"]
        assert os.path.exists(path)
        with open(path) as f:
            first = f.readline().rstrip()
        assert first == DEPLOY_MARKER

        # DB tracking updated
        row = db.get_generated_importer(gen_id)
        assert row["deployed_path"] == path
        assert row["deployed_at"]

        bank = db.get_pending_bank(bank_id)
        assert bank["status"] == "live"
    finally:
        _cleanup(bank_id, slug)


def test_deploy_rejects_unapproved():
    from app.ai_agents.importer_deployer import deploy, DeployError
    bank_id, gen_id, slug = _make_bank_with_generated(approved=False)
    try:
        try:
            deploy(gen_id)
        except DeployError as e:
            assert "approved" in str(e).lower()
        else:
            raise AssertionError("expected DeployError for unapproved")
    finally:
        _cleanup(bank_id, slug)


def test_deploy_rejects_failed_validation():
    from app.ai_agents.importer_deployer import deploy, DeployError
    bank_id, gen_id, slug = _make_bank_with_generated(validation_status="syntax_error")
    try:
        try:
            deploy(gen_id)
        except DeployError as e:
            assert "validation" in str(e).lower()
        else:
            raise AssertionError("expected DeployError for failed validation")
        # force=True bypasses
        result = deploy(gen_id, force=True)
        assert os.path.exists(result["path"])
    finally:
        _cleanup(bank_id, slug)


def test_deploy_refuses_to_overwrite_handwritten_file():
    """A pre-existing importer file with no deploy marker must be untouchable."""
    from app.ai_agents.importer_deployer import deploy, DeployError, IMPORTERS_DIR
    bank_id, gen_id, slug = _make_bank_with_generated()
    handwritten = IMPORTERS_DIR / f"{slug}_importer.py"
    handwritten.write_text('"""Hand-written, not auto-deployed."""\nSOURCE = "x"\n')
    try:
        try:
            deploy(gen_id)
        except DeployError as e:
            assert "refusing to overwrite" in str(e).lower()
        else:
            raise AssertionError("expected DeployError refusing to clobber")
    finally:
        _cleanup(bank_id, slug)


def test_deploy_can_overwrite_previous_auto_deploy():
    """If we previously auto-deployed, re-deploy should succeed (re-gen flow)."""
    from app.ai_agents.importer_deployer import deploy
    bank_id, gen_id, slug = _make_bank_with_generated()
    try:
        deploy(gen_id)  # first deploy
        # re-deploy should not raise
        result = deploy(gen_id)
        assert os.path.exists(result["path"])
    finally:
        _cleanup(bank_id, slug)


def test_undeploy_removes_marked_file():
    from app.ai_agents.importer_deployer import deploy, undeploy
    bank_id, gen_id, slug = _make_bank_with_generated()
    try:
        deploy(gen_id)
        assert undeploy(slug) is True
        from app.ai_agents.importer_deployer import IMPORTERS_DIR
        assert not (IMPORTERS_DIR / f"{slug}_importer.py").exists()
    finally:
        _cleanup(bank_id, slug)


def test_undeploy_refuses_unmarked_file():
    from app.ai_agents.importer_deployer import undeploy, IMPORTERS_DIR
    slug = "test_unmarked_xyz"
    target = IMPORTERS_DIR / f"{slug}_importer.py"
    target.write_text('"""hand-written"""\n')
    try:
        assert undeploy(slug) is False
        assert target.exists()  # unchanged
    finally:
        target.unlink()


def test_unsafe_slug_rejected():
    """Pure unit check on the slug guard — slugs from the DB go through
    create_pending_bank's slugify, but defense in depth."""
    from app.ai_agents.importer_deployer import deploy, DeployError, _SAFE_SLUG_RE
    # Quick sanity: the regex itself rejects path-traversal attempts
    assert not _SAFE_SLUG_RE.match("../etc/passwd")
    assert not _SAFE_SLUG_RE.match("/abs/path")
    assert not _SAFE_SLUG_RE.match("with-dash")  # only underscores allowed
    assert not _SAFE_SLUG_RE.match("UpperCase")
    assert _SAFE_SLUG_RE.match("normal_slug_123")


def test_auto_dispatcher_404s_unknown_slug():
    from app.web_ui import app
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = "1"; s["_fresh"] = True
    r = client.get("/tax-ai-analyzer/api/import/auto/no_such_slug/status")
    assert r.status_code == 404


def test_auto_dispatcher_finds_deployed_module():
    """Full round-trip: deploy → /status returns the configured shape."""
    from app.web_ui import app
    from app.ai_agents.importer_deployer import deploy
    bank_id, gen_id, slug = _make_bank_with_generated()
    try:
        deploy(gen_id)
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = "1"; s["_fresh"] = True
        r = client.get(f"/tax-ai-analyzer/api/import/auto/{slug}/status")
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert "configured" in data
        assert data["configured"] is False  # no creds saved
    finally:
        _cleanup(bank_id, slug)
