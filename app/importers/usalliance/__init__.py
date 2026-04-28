"""US Alliance Federal Credit Union — Playwright-based statement downloader.

Logs into the US Alliance online banking portal, navigates to eStatements,
and downloads monthly PDF statements for the requested years.

Each statement is saved to:
  <consume_path>/<entity_slug>/<year>/YYYY_MM_01_usalliance_statement.pdf

MFA handling: if an OTP prompt is detected the job enters `mfa_pending` state
and polls for a code delivered via the in-memory MFA registry (fed by the
/api/import/usalliance/mfa endpoint).

Phase 11G refactor: the original 1,132-line module is now a package split
across runner / login / mfa / estatements / download / helpers. Public
symbols are re-exported here so callers don't change.
"""
from app.importers.usalliance.runner import run_import
from app.importers.usalliance.mfa import set_mfa_code

__all__ = ["run_import", "set_mfa_code"]
