"""Unit tests for the LLM-output importer validator."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


VALID_SOURCE = '''
"""Mock importer."""
from __future__ import annotations
from app.importers.base_bank_importer import launch_browser, save_auth_cookies

SOURCE = "mock_bank"


def run_import(username, password, years, consume_path, entity_slug, job_id,
               log=None, cookies=None, entity_id=None):
    return {"imported": 0, "skipped": 0, "errors": 0}


def set_mfa_code(job_id, code):
    pass
'''


def test_valid_source_passes():
    from app.ai_agents.importer_validator import validate
    status, notes = validate(VALID_SOURCE)
    assert status == "pass", f"expected pass, got {status}: {notes}"


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
