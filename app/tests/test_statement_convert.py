"""Tests for the statement→OFX convert path (2026-09-06).

Everything here is mockable — ofxstatement does NOT need to be installed
for these to pass. The CLI-present branch is exercised by patching
shutil.which + subprocess.run.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ── convert_to_ofx ──────────────────────────────────────────────────────────

def test_ofx_plugin_is_passthrough():
    from app.importers.statement_convert import convert_to_ofx
    data = b"OFXHEADER:100\n<OFX><STMTTRN></STMTTRN></OFX>"
    assert convert_to_ofx(data, "x.ofx", "ofx", log=lambda m: None) == data


def test_empty_upload_raises():
    from app.importers.statement_convert import convert_to_ofx
    try:
        convert_to_ofx(b"", "x.csv", "ofx")
    except RuntimeError as e:
        assert "empty" in str(e).lower()
        return
    raise AssertionError("expected RuntimeError")


def test_oversize_upload_raises():
    from app.importers.statement_convert import convert_to_ofx, MAX_STATEMENT_BYTES
    try:
        convert_to_ofx(b"x" * (MAX_STATEMENT_BYTES + 1), "big.csv", "ofx")
    except RuntimeError as e:
        assert "cap" in str(e).lower()
        return
    raise AssertionError("expected RuntimeError")


def test_missing_plugin_raises():
    from app.importers.statement_convert import convert_to_ofx
    try:
        convert_to_ofx(b"a,b,c", "x.csv", "")
    except RuntimeError as e:
        assert "plugin required" in str(e).lower()
        return
    raise AssertionError("expected RuntimeError")


def test_cli_absent_gives_actionable_error():
    """No ofxstatement binary → clear 'rebuild image' message, not a crash."""
    from app.importers import statement_convert as sc
    with patch.object(sc.shutil, "which", return_value=None):
        try:
            sc.convert_to_ofx(b"a,b,c", "x.csv", "chase")
        except RuntimeError as e:
            assert "not installed" in str(e).lower()
            return
    raise AssertionError("expected RuntimeError")


def test_cli_present_success_path():
    """Patched subprocess.run writes an OFX file → we return its bytes."""
    from app.importers import statement_convert as sc

    def fake_run(cmd, **kw):
        # cmd = ["ofxstatement","convert","-t",plugin,src,dst]
        dst = cmd[-1]
        with open(dst, "wb") as f:
            f.write(b"<OFX>converted</OFX>")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(sc.shutil, "which", return_value="/usr/bin/ofxstatement"), \
         patch.object(sc.subprocess, "run", side_effect=fake_run):
        out = sc.convert_to_ofx(b"date,amount\n2023-03-01,-5", "acme.csv", "acme", log=lambda m: None)
    assert out == b"<OFX>converted</OFX>"


def test_cli_present_failure_surfaces_stderr_and_install_hint():
    from app.importers import statement_convert as sc

    def fake_run(cmd, **kw):
        return MagicMock(returncode=1, stdout="", stderr="No plugin named 'acme'")

    with patch.object(sc.shutil, "which", return_value="/usr/bin/ofxstatement"), \
         patch.object(sc.subprocess, "run", side_effect=fake_run):
        try:
            sc.convert_to_ofx(b"x", "acme.csv", "acme", log=lambda m: None)
        except RuntimeError as e:
            msg = str(e)
            assert "No plugin named" in msg
            assert "pip install ofxstatement-acme" in msg
            return
    raise AssertionError("expected RuntimeError")


# ── list_plugins ────────────────────────────────────────────────────────────

def test_list_plugins_cli_absent_returns_builtins_only():
    """No ofxstatement CLI → the built-in PDF parser + OFX pass-through are
    still offered (they don't need it)."""
    from app.importers import statement_convert as sc
    with patch.object(sc.shutil, "which", return_value=None):
        plugins = sc.list_plugins()
    assert [p["name"] for p in plugins] == list(sc.BUILTIN_PLUGINS)
    assert plugins[0]["name"] == "pdf-auto"  # first = default in the UI


def test_list_plugins_parses_cli_output_and_adds_builtins():
    from app.importers import statement_convert as sc

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0, stderr="",
                         stdout="The following plugins are available:\n"
                                "  chase    Chase Bank CSV\n"
                                "  bofa     Bank of America CSV\n")

    with patch.object(sc.shutil, "which", return_value="/usr/bin/ofxstatement"), \
         patch.object(sc.subprocess, "run", side_effect=fake_run):
        plugins = sc.list_plugins()
    names = [p["name"] for p in plugins]
    assert names[:len(sc.BUILTIN_PLUGINS)] == list(sc.BUILTIN_PLUGINS)  # built-ins first
    assert {"chase", "bofa"} <= set(names)
    chase = next(p for p in plugins if p["name"] == "chase")
    assert chase["description"] == "Chase Bank CSV"


def test_list_plugins_no_plugins_available_message_is_not_a_plugin():
    """Real ofxstatement 0.9.3 output with nothing installed — the 'See https://…'
    line used to be parsed as a plugin named 'See'."""
    from app.importers import statement_convert as sc

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0, stderr="",
                         stdout="No plugins available. Install plugin eggs or create your own.\n"
                                "See https://github.com/kedder/ofxstatement for more info.\n")

    with patch.object(sc.shutil, "which", return_value="/usr/bin/ofxstatement"), \
         patch.object(sc.subprocess, "run", side_effect=fake_run):
        plugins = sc.list_plugins()
    assert [p["name"] for p in plugins] == list(sc.BUILTIN_PLUGINS)


def test_pdf_plugins_rejected_by_convert_to_ofx():
    from app.importers.statement_convert import convert_to_ofx
    try:
        convert_to_ofx(b"%PDF", "s.pdf", "pdf-auto")
    except RuntimeError as e:
        assert "parse_pdf_statement" in str(e)
        return
    raise AssertionError("expected RuntimeError")


# ── routes ──────────────────────────────────────────────────────────────────

def _client():
    from app.web_ui import app
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def test_plugins_route_shape():
    c = _client()
    r = c.get("/tax-ai-analyzer/api/import/statement/plugins")
    assert r.status_code == 200
    body = r.get_json()
    assert "available" in body and isinstance(body["available"], bool)
    assert "plugins" in body and isinstance(body["plugins"], list)
    assert "install_hint" in body


def test_convert_route_rejects_missing_file():
    c = _client()
    r = c.post("/tax-ai-analyzer/api/import/statement/convert", data={"plugin": "ofx"})
    assert r.status_code == 400
    assert "file" in r.get_json()["error"].lower()


def test_convert_route_rejects_missing_plugin():
    import io
    c = _client()
    r = c.post("/tax-ai-analyzer/api/import/statement/convert",
               data={"file": (io.BytesIO(b"x"), "s.csv")},
               content_type="multipart/form-data")
    assert r.status_code == 400
    assert "plugin" in r.get_json()["error"].lower()


def test_convert_route_rejects_bad_year():
    import io
    c = _client()
    r = c.post("/tax-ai-analyzer/api/import/statement/convert",
               data={"file": (io.BytesIO(b"x"), "s.csv"), "plugin": "ofx", "year": "23"},
               content_type="multipart/form-data")
    assert r.status_code == 400
    assert "yyyy" in r.get_json()["error"].lower()


def test_convert_route_starts_job_for_ofx_passthrough():
    """End-to-end through the route with plugin=ofx: creates an import_job,
    the background thread parses the OFX and completes. We poll the job
    row briefly rather than sleeping a fixed time."""
    import io, time
    from app import db
    ofx = (b"OFXHEADER:100\nDATA:OFXSGML\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
           b"<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20230315<TRNAMT>-12.34"
           b"<FITID>testfit001<NAME>Test Vendor</STMTTRN></BANKTRANLIST>"
           b"</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>")
    c = _client()
    r = c.post("/tax-ai-analyzer/api/import/statement/convert",
               data={"file": (io.BytesIO(ofx), "s.ofx"), "plugin": "ofx", "year": "2023"},
               content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    job_id = r.get_json()["job_id"]
    status = None
    for _ in range(40):
        row = db.get_import_job(job_id)
        status = row["status"] if row else None
        if status in ("completed", "error"):
            break
        time.sleep(0.1)
    assert status == "completed", f"job ended as {status!r}: {row}"
    # cleanup the test transaction (source_id is deterministic via FITID)
    try:
        conn = db.get_connection()
        conn.execute("DELETE FROM transactions WHERE source_id LIKE '%testfit001%'")
        conn.execute("DELETE FROM import_jobs WHERE id = ?", (job_id,))
        conn.commit(); conn.close()
    except Exception:
        pass
