"""pytest fixtures shared across the test suite.

Primary purpose: keep tests from polluting the production `llm_usage.db`.

Several tests (notably the bank-codegen regen suite) mock the LLM SDK but
still let `bank_codegen.generate_importer()` reach `tracker.log_usage()` —
which would write fake-token rows to whichever DB `tracker._USAGE_DB_PATH`
points at. With no isolation, that DB is the one mounted from the production
data volume, so test runs accumulate phantom records that look like real
usage and confuse downstream analysis (e.g. "why are we showing 56 calls
in 7 days when the proxy team sees 0?").

This fixture redirects `tracker._USAGE_DB_PATH` to a per-session temp file
for the duration of the test run, then restores it after. `_initialized` is
also reset so the fresh temp DB gets a clean schema on first write.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Make sure the app package is importable when pytest is invoked from the
# repo root vs. the app/ subdir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


@pytest.fixture(autouse=True, scope="session")
def _isolate_llm_usage_db():
    """Redirect llm_usage_tracker to a temp DB for the entire test session.

    `autouse=True` means this fires for every test without callers needing
    to opt in — exactly what we want, because the pollution is silent
    (tests pass; rows just leak).
    """
    from app import llm_usage_tracker as tracker

    orig_path = tracker._USAGE_DB_PATH
    orig_initialized = tracker._initialized

    tmpdir = tempfile.mkdtemp(prefix="tax_ai_tests_")
    tracker._USAGE_DB_PATH = os.path.join(tmpdir, "llm_usage_test.db")
    tracker._initialized = False  # force fresh schema init on first write

    yield

    tracker._USAGE_DB_PATH = orig_path
    tracker._initialized = orig_initialized
    # Best-effort cleanup; no big deal if it lingers
    for f in os.listdir(tmpdir):
        try:
            os.unlink(os.path.join(tmpdir, f))
        except Exception:
            pass
    try:
        os.rmdir(tmpdir)
    except Exception:
        pass
