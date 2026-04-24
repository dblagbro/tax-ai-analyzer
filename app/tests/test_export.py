"""Export generate + download end-to-end tests.

Covers HIGH-PASS2-1 — prior to this test, every /api/export/<year>/<slug>/download/<fmt>
route returned 404 because the downloader looked for {slug}_{year}{ext} while the
generator writes export_{year}_{slug}{ext} (plus transactions_{year}_{slug}.csv
and summary_{year}_{slug}.pdf).

These tests use the pre-populated docker_tax_ai_data volume's export directory
and do NOT regenerate exports — they only verify the download route finds the
files that already exist.
"""
import os

import pytest

from app.config import EXPORT_PATH
from app.routes.export_ import _candidate_filenames, _VALID_FORMATS
from app.web_ui import app as flask_app


def _authed_client():
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        sess["_fresh"] = True
    return client


def _existing_export_target():
    """Find a (year, slug) pair that actually has files on disk, else skip."""
    if not os.path.isdir(EXPORT_PATH):
        return None
    for entry in sorted(os.listdir(EXPORT_PATH)):
        year_dir = os.path.join(EXPORT_PATH, entry)
        if not (os.path.isdir(year_dir) and entry.isdigit() and len(entry) == 4):
            continue
        for fname in os.listdir(year_dir):
            # match the generator's naming
            if fname.startswith("export_") and fname.endswith((".json", ".qbo", ".ofx", ".txf", ".iif")):
                parts = fname[len("export_"):].rsplit(".", 1)[0]
                yr, slug = parts.split("_", 1)
                if yr == entry:
                    return entry, slug
    return None


TARGET = _existing_export_target()


class TestCandidateFilenames:
    """Unit tests for the filename dispatch — no filesystem required."""

    def test_csv_uses_transactions_prefix(self):
        names = _candidate_filenames("csv", "2024", "personal")
        assert "transactions_2024_personal.csv" in names

    def test_pdf_uses_summary_prefix(self):
        names = _candidate_filenames("pdf", "2024", "personal")
        assert "summary_2024_personal.pdf" in names

    def test_zip_uses_tax_complete_suffix(self):
        names = _candidate_filenames("zip", "2024", "personal")
        assert "tax_2024_personal_complete.zip" in names

    def test_ofx_uses_export_prefix(self):
        names = _candidate_filenames("ofx", "2024", "personal")
        assert "export_2024_personal.ofx" in names

    def test_legacy_filename_always_included_as_fallback(self):
        for fmt in ("csv", "json", "iif", "qbo", "ofx", "txf", "pdf", "zip"):
            names = _candidate_filenames(fmt, "2024", "personal")
            assert any(n.startswith("personal_2024.") for n in names), \
                f"legacy name missing for {fmt}: {names}"

    def test_every_valid_format_produces_non_empty_candidates(self):
        for fmt in _VALID_FORMATS:
            names = _candidate_filenames(fmt, "2024", "x")
            assert names and all(names)


@pytest.mark.skipif(TARGET is None, reason="no generated exports on disk — skip roundtrip")
class TestDownloadRoundtrip:
    """Integration: file exists on disk → download route returns 200 + bytes."""

    def test_download_existing_ofx(self):
        year, slug = TARGET
        resp = _authed_client().get(f"/tax-ai-analyzer/api/export/{year}/{slug}/download/ofx")
        assert resp.status_code == 200, resp.data[:200]
        assert len(resp.data) > 0

    def test_download_unsupported_format_rejected(self):
        year, slug = TARGET
        resp = _authed_client().get(f"/tax-ai-analyzer/api/export/{year}/{slug}/download/docx")
        assert resp.status_code == 400

    def test_download_invalid_year_rejected(self):
        _, slug = TARGET
        resp = _authed_client().get(f"/tax-ai-analyzer/api/export/999/{slug}/download/csv")
        assert resp.status_code == 400

    def test_download_nonexistent_file_returns_404(self):
        year = TARGET[0]
        resp = _authed_client().get(f"/tax-ai-analyzer/api/export/{year}/nonexistent_slug_xyz/download/csv")
        assert resp.status_code == 404


class TestDownloadAuthGate:
    def test_download_requires_login(self):
        client = flask_app.test_client()  # no session
        resp = client.get("/tax-ai-analyzer/api/export/2024/personal/download/csv")
        assert resp.status_code in (302, 401)
