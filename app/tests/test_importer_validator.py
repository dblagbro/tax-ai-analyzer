"""Unit tests for the LLM-output importer validator."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


VALID_SOURCE = '''
"""Mock importer following the Phase 14 run_bank_import pattern."""
from __future__ import annotations
from app.importers.base_bank_importer import (
    launch_browser, run_bank_import, save_auth_cookies,
)

SOURCE = "mock_bank"


def run_import(username, password, years, consume_path, entity_slug, job_id,
               log=None, cookies=None, entity_id=None):
    def _login_fn(page, context):
        pass
    def _download_fn(page, context, _account, year):
        return (0, 0, 0)
    return run_bank_import(
        slug="mock_bank", login_fn=_login_fn, download_fn=_download_fn,
        years=years, cookies=cookies, headless=True, log=log,
    )


def set_mfa_code(job_id, code):
    pass
'''

# Old-style: passes shape + import checks but trips Phase 14 pattern_warning
LEGACY_PATTERN_SOURCE = '''
"""Old-style importer (pre-Phase-14) — runs but doesn\'t use orchestrator."""
from __future__ import annotations
from app.importers.base_bank_importer import launch_browser, save_auth_cookies

SOURCE = "old_style"


def run_import(username, password, years, consume_path, entity_slug, job_id,
               log=None, cookies=None, entity_id=None):
    pw, context, page = launch_browser("old_style", headless=True, log=log)
    try:
        pass
    finally:
        context.close()
        pw.stop()
    return {"imported": 0, "skipped": 0, "errors": 0}


def set_mfa_code(job_id, code):
    pass
'''


def test_phase14_compliant_source_passes():
    from app.ai_agents.importer_validator import validate
    status, notes = validate(VALID_SOURCE)
    assert status == "pass", f"expected pass, got {status}: {notes}"


def test_legacy_pattern_returns_warning_not_error():
    """Old-style importers (no run_bank_import) should yield pattern_warning,
    NOT a blocking error — old shape still runs."""
    from app.ai_agents.importer_validator import validate
    status, notes = validate(LEGACY_PATTERN_SOURCE)
    assert status == "pattern_warning"
    assert "run_bank_import" in notes


def test_pattern_warning_unblocked_in_deploy_gate():
    """Deploy + approve gates must not treat pattern_warning as blocking."""
    # Just confirm the constant set is defined the way we expect — both gates
    # use the same {syntax_error, shape_error, import_error} set; if someone
    # adds pattern_warning to that set, this assertion will fail and remind
    # them the warning is advisory.
    import inspect
    from app.ai_agents.importer_deployer import deploy
    src = inspect.getsource(deploy)
    assert "BLOCKING" in src
    assert "pattern_warning" not in src.split("BLOCKING = ")[1].split("}")[0]


def test_syntax_error_caught():
    from app.ai_agents.importer_validator import validate
    bad = "def run_import(\n    username,\n    password\n# missing colon and body"
    status, notes = validate(bad)
    assert status == "syntax_error"
    assert "SyntaxError" in notes


def test_missing_run_import_caught():
    from app.ai_agents.importer_validator import validate
    src = 'SOURCE = "x"\n\ndef set_mfa_code(j, c): pass\n'
    status, notes = validate(src)
    assert status == "shape_error"
    assert "run_import" in notes


def test_missing_source_constant_caught():
    from app.ai_agents.importer_validator import validate
    src = (
        "def run_import(username, password, years, consume_path, "
        "entity_slug, job_id): pass\n"
        "def set_mfa_code(j, c): pass\n"
    )
    status, notes = validate(src)
    assert status == "shape_error"
    assert "SOURCE" in notes


def test_missing_run_import_param_caught():
    from app.ai_agents.importer_validator import validate
    src = (
        'SOURCE = "x"\n'
        "def run_import(username, password, years): pass\n"
        "def set_mfa_code(j, c): pass\n"
    )
    status, notes = validate(src)
    assert status == "shape_error"
    assert "missing parameters" in notes


def test_email_cred_importer_passes():
    """Chime-shaped (`email` not `username`) source should validate.

    Phase 14 / HIGH-POST14-2: the validator previously rejected any
    importer that didn't have `username` as a parameter. Chime uses
    `email` because that's Chime's actual auth model. The fix accepts
    either as the credential parameter.
    """
    from app.ai_agents.importer_validator import validate
    src = '''
"""Email-cred importer."""
from __future__ import annotations
from app.importers.base_bank_importer import run_bank_import
SOURCE = "email_bank"
def run_import(email, password, years, consume_path, entity_slug, job_id,
               log=None, cookies=None, entity_id=None):
    def _login_fn(page, context): pass
    def _download_fn(page, context, _account, year): return (0,0,0)
    return run_bank_import(slug="email_bank", login_fn=_login_fn,
        download_fn=_download_fn, years=years, cookies=cookies,
        headless=True, log=log)
def set_mfa_code(job_id, code): pass
'''
    status, notes = validate(src)
    assert status == "pass", f"expected pass, got {status}: {notes}"


def test_real_chime_importer_passes_validation():
    """Direct regression guard: feed the real chime_importer.py source
    through validate(). Before HIGH-POST14-2 fix this returned shape_error
    because chime uses `email`. After the fix it must return pass."""
    from app.ai_agents.importer_validator import validate
    from app.importers import chime_importer
    src = open(chime_importer.__file__).read()
    status, notes = validate(src)
    # chime_importer is a production-shipped Phase-14 importer; it must pass.
    # The only legal non-"pass" outcome is "pattern_warning" if for some
    # reason the helper-usage check fails — but it shouldn't.
    assert status in ("pass", "pattern_warning"), \
        f"chime_importer should validate; got {status}: {notes}"


def test_missing_both_credentials_caught():
    """Sanity: a source with NEITHER username NOR email should still fail."""
    from app.ai_agents.importer_validator import validate
    src = (
        'SOURCE = "x"\n'
        "def run_import(password, years, consume_path, entity_slug, "
        "job_id): pass\n"
        "def set_mfa_code(j, c): pass\n"
    )
    status, notes = validate(src)
    assert status == "shape_error"
    assert "username" in notes or "email" in notes


def test_hallucinated_base_import_caught():
    from app.ai_agents.importer_validator import validate
    src = (
        "from app.importers.base_bank_importer import launch_browser, "
        "totally_fake_helper\n"
        'SOURCE = "x"\n'
        "def run_import(username, password, years, consume_path, "
        "entity_slug, job_id): pass\n"
        "def set_mfa_code(j, c): pass\n"
    )
    status, notes = validate(src)
    assert status == "import_error"
    assert "totally_fake_helper" in notes


def test_wildcard_import_rejected():
    from app.ai_agents.importer_validator import validate
    src = (
        "from app.importers.base_bank_importer import *\n"
        'SOURCE = "x"\n'
        "def run_import(username, password, years, consume_path, "
        "entity_slug, job_id): pass\n"
        "def set_mfa_code(j, c): pass\n"
    )
    status, notes = validate(src)
    assert status == "import_error"
    assert "wildcard" in notes
