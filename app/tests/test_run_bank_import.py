"""Tests for the Phase 14 shared bank-import orchestrator.

We don't actually launch a browser — patch launch_browser to return mock
page/context/pw triples, then verify the orchestrator calls login_fn,
optionally discover_fn, and download_fn for each (account, year) pair
with correct accumulation semantics.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _fake_browser():
    """Return a (pw, context, page) triple of MagicMocks for launch_browser."""
    pw = MagicMock()
    page = MagicMock()
    context = MagicMock()
    return pw, context, page


def test_run_bank_import_single_account_happy_path():
    from app.importers.base_bank_importer import run_bank_import

    login_calls = []
    download_calls = []

    def _login(page, context):
        login_calls.append((page, context))

    def _download(page, context, account, year):
        download_calls.append((account, year))
        return (3, 1, 0)  # 3 imported, 1 skipped, 0 errors

    with patch("app.importers.base_bank_importer.launch_browser",
               return_value=_fake_browser()):
        result = run_bank_import(
            slug="testbank",
            login_fn=_login, download_fn=_download,
            years=["2023", "2024"], cookies=None, log=lambda m: None,
        )

    assert len(login_calls) == 1, "login_fn should be called exactly once"
    assert download_calls == [(None, "2023"), (None, "2024")]
    assert result == {"imported": 6, "skipped": 2, "errors": 0}


def test_run_bank_import_with_discover_accounts():
    from app.importers.base_bank_importer import run_bank_import

    download_calls = []

    def _login(page, context):
        pass

    def _discover(page, context):
        return [{"name": "checking"}, {"name": "savings"}]

    def _download(page, context, account, year):
        download_calls.append((account["name"], year))
        return (1, 0, 0)

    with patch("app.importers.base_bank_importer.launch_browser",
               return_value=_fake_browser()):
        result = run_bank_import(
            slug="testbank",
            login_fn=_login, download_fn=_download, discover_fn=_discover,
            years=["2024"], log=lambda m: None,
        )

    # 2 accounts × 1 year = 2 downloads
    assert download_calls == [("checking", "2024"), ("savings", "2024")]
    assert result == {"imported": 2, "skipped": 0, "errors": 0}


def test_run_bank_import_per_year_error_isolation():
    """An exception in download_fn for one year must not abort other years."""
    from app.importers.base_bank_importer import run_bank_import

    def _login(page, context): pass

    def _download(page, context, _account, year):
        if year == "2023":
            raise RuntimeError("kaboom on 2023")
        return (5, 0, 0)

    with patch("app.importers.base_bank_importer.launch_browser",
               return_value=_fake_browser()):
        result = run_bank_import(
            slug="testbank",
            login_fn=_login, download_fn=_download,
            years=["2023", "2024", "2025"], log=lambda m: None,
        )

    assert result["imported"] == 10  # two successful years, 5 each
    assert result["errors"] == 1  # one failed year
    assert result["skipped"] == 0


def test_run_bank_import_login_failure_aborts():
    """If login_fn raises, no download attempts and the exception bubbles up
    AFTER cleanup (the orchestrator's try/finally still cleans the browser)."""
    from app.importers.base_bank_importer import run_bank_import

    download_calls = []

    def _login(page, context):
        raise RuntimeError("auth rejected")

    def _download(page, context, account, year):
        download_calls.append((account, year))
        return (1, 0, 0)

    pw, context, page = _fake_browser()
    with patch("app.importers.base_bank_importer.launch_browser",
               return_value=(pw, context, page)):
        try:
            run_bank_import(
                slug="testbank",
                login_fn=_login, download_fn=_download,
                years=["2024"], log=lambda m: None,
            )
        except RuntimeError as e:
            assert "auth rejected" in str(e)
        else:
            raise AssertionError("expected RuntimeError to bubble up")

    assert download_calls == [], "no downloads after login failure"
    # Cleanup still ran
    context.close.assert_called_once()
    pw.stop.assert_called_once()


def test_run_bank_import_cookie_injection_failure_does_not_abort():
    """Bad cookies shouldn't kill the import — log + proceed without."""
    from app.importers.base_bank_importer import run_bank_import

    pw, context, page = _fake_browser()
    context.add_cookies.side_effect = RuntimeError("bad cookie")

    def _login(page, context): pass
    def _download(page, context, _a, _y): return (1, 0, 0)

    with patch("app.importers.base_bank_importer.launch_browser",
               return_value=(pw, context, page)):
        result = run_bank_import(
            slug="testbank",
            login_fn=_login, download_fn=_download,
            years=["2024"], cookies=[{"name": "x", "value": "y"}],
            log=lambda m: None,
        )

    assert result["imported"] == 1
    assert result["errors"] == 0


def test_run_bank_import_discover_failure_falls_back_single_account():
    from app.importers.base_bank_importer import run_bank_import

    download_calls = []

    def _login(page, context): pass

    def _discover(page, context):
        raise RuntimeError("account API broken")

    def _download(page, context, account, year):
        download_calls.append((account, year))
        return (1, 0, 0)

    with patch("app.importers.base_bank_importer.launch_browser",
               return_value=_fake_browser()):
        result = run_bank_import(
            slug="testbank",
            login_fn=_login, download_fn=_download, discover_fn=_discover,
            years=["2024"], log=lambda m: None,
        )

    assert download_calls == [(None, "2024")]
    assert result["imported"] == 1


def test_merrick_importer_uses_run_bank_import():
    """Smoke check: importing merrick_importer doesn't raise after the refactor.
    The refactored run_import should call run_bank_import internally."""
    from app.importers import merrick_importer
    assert callable(merrick_importer.run_import)
    assert callable(merrick_importer.set_mfa_code)
    src = open(merrick_importer.__file__).read()
    assert "run_bank_import" in src


def test_verizon_importer_uses_run_bank_import():
    """Phase 14 conversion #3 — verizon delegates to run_bank_import.
    Same bool→raise translation pattern as chime."""
    from app.importers import verizon_importer
    assert callable(verizon_importer.run_import)
    assert callable(verizon_importer.set_mfa_code)
    src = open(verizon_importer.__file__).read()
    assert "run_bank_import" in src
    assert "Verizon login failed" in src


def test_chime_importer_uses_run_bank_import():
    """Phase 14 conversion #2 — chime now delegates to run_bank_import.
    Includes the bool→raise translation since chime's _login returns bool."""
    from app.importers import chime_importer
    assert callable(chime_importer.run_import)
    assert callable(chime_importer.set_mfa_code)
    src = open(chime_importer.__file__).read()
    assert "run_bank_import" in src
    # Confirm the bool-to-raise adapter is in place — without it, a False
    # login would silently proceed to download instead of failing fast
    assert "Chime login failed" in src
