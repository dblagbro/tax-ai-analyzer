"""CSV, URL, OFX, and local filesystem import routes."""
import json
import logging
import os
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX, CONSUME_PATH
from app.routes._state import append_job_log
from app.importers.csv_runner import run_csv_job

logger = logging.getLogger(__name__)

bp = Blueprint("import_", __name__)


# ── CSV importers ─────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/paypal/csv", methods=["POST"])
@login_required
def api_import_paypal_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "")
    csv_bytes = f.read()
    col_map = {"date": "Date", "description": "Name", "amount": "Amount"}
    job_id = db.create_import_job("paypal", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename}))
    threading.Thread(target=run_csv_job,
                     args=(job_id, csv_bytes, "paypal", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@bp.route(URL_PREFIX + "/api/import/venmo/csv", methods=["POST"])
@login_required
def api_import_venmo_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "")
    csv_bytes = f.read()
    col_map = {"date": "Datetime", "description": "Note", "amount": "Amount (total)"}
    job_id = db.create_import_job("venmo", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename}))
    threading.Thread(target=run_csv_job,
                     args=(job_id, csv_bytes, "venmo", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@bp.route(URL_PREFIX + "/api/import/bank-csv", methods=["POST"])
@login_required
def api_import_bank_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "")
    col_map = {
        "date": request.form.get("date_col", "Date"),
        "description": request.form.get("desc_col", "Description"),
        "amount": request.form.get("amount_col", "Amount"),
    }
    csv_bytes = f.read()
    job_id = db.create_import_job("bank_csv", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename,
                                                          "col_map": col_map}))
    threading.Thread(target=run_csv_job,
                     args=(job_id, csv_bytes, "bank_csv", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── URL import ────────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/url", methods=["POST"])
@login_required
def api_import_url():
    data = request.get_json() or {}
    import_url = data.get("url", "").strip()
    entity_id = data.get("entity_id")
    if not import_url:
        return jsonify({"error": "url required"}), 400
    job_id = db.create_import_job("url", entity_id=entity_id,
                                  config_json=json.dumps({"url": import_url}))

    def _run(jid, u):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            import httpx
            r = httpx.get(u, follow_redirects=True, timeout=30)
            r.raise_for_status()
            db.update_import_job(jid, status="completed", count_imported=1,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"URL: {u}")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, import_url), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── OFX import ────────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/bank-ofx", methods=["POST"])
@login_required
def api_import_bank_ofx():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    entity_id = request.form.get("entity_id", type=int)
    year = request.form.get("year", "") or None
    content = f.read()
    job_id = db.create_import_job("ofx_import", entity_id=entity_id,
                                  config_json=json.dumps({"filename": f.filename}))

    def _run(jid, data, eid, yr):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.ofx_importer import parse_ofx
            txns = parse_ofx(data, entity_id=eid, default_year=yr)
            total = dup = 0
            for t in txns:
                # 2026-09-06: transactions has no UNIQUE index — check first,
                # otherwise re-uploading the same OFX doubles every row.
                if db.transaction_exists(t.get("source", "ofx_import"), t.get("source_id", "")):
                    dup += 1
                    continue
                try:
                    db.add_transaction(t)
                    total += 1
                except Exception:
                    pass
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"OFX: {total} transactions imported ({dup} already present)")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, content, entity_id, year),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── Statement convert (2026-09-06) ────────────────────────────────────────────
# Manually-downloaded bank/card statement (CSV/PDF) → ofxstatement → OFX →
# the existing OFX importer. LLM-free, scraper-free. Closes the Mar–Dec 2023
# gap while the proxy key is being reissued and the Playwright scrapers are
# down. See app/importers/statement_convert.py.

@bp.route(URL_PREFIX + "/api/import/statement/plugins", methods=["GET"])
@login_required
def api_import_statement_plugins():
    """List installed ofxstatement plugins so the UI can offer a dropdown."""
    from app.importers.statement_convert import list_plugins, ofxstatement_available
    return jsonify({
        "available": ofxstatement_available(),
        "plugins": list_plugins(),
        "install_hint": "pip install ofxstatement-<bank>  (e.g. ofxstatement-chase, "
                        "ofxstatement-bofa, ofxstatement-wellsfargo) then rebuild image",
    })


@bp.route(URL_PREFIX + "/api/import/statement/convert", methods=["POST"])
@login_required
def api_import_statement_convert():
    """Multipart: file (one or many), plugin, entity_id (int), year (YYYY, optional).

    plugin:
      pdf-auto | pdf-card | pdf-bank  built-in PDF statement parser — no
                                      ofxstatement plugin, no LLM
                                      (app.importers.pdf_statement)
      ofx                             upload is already OFX — pass-through
      <anything else>                 `ofxstatement convert -t <plugin>` → OFX

    Every transaction carries a deterministic source_id and is skipped when
    already stored, so re-uploading a statement is a no-op. The whole batch
    runs as ONE import_job so the UI can poll a single log.
    """
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        return jsonify({"error": "No file uploaded"}), 400
    plugin = (request.form.get("plugin") or "").strip()
    if not plugin:
        return jsonify({"error": "plugin required — GET /api/import/statement/plugins"}), 400
    entity_id = request.form.get("entity_id", type=int)
    year = (request.form.get("year") or "").strip() or None
    if year and not (len(year) == 4 and year.isdigit()):
        return jsonify({"error": "year must be YYYY"}), 400
    uploads = [(f.filename or "statement", f.read()) for f in files]

    job_id = db.create_import_job(
        "statement_convert", entity_id=entity_id,
        config_json=json.dumps({"files": [u[0] for u in uploads], "plugin": plugin, "year": year}),
    )

    def _run(jid, batch, plug, eid, yr):
        log = lambda m: append_job_log(jid, m)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        imported = skipped = failed = 0
        try:
            from app.importers.statement_convert import convert_to_ofx
            from app.importers.ofx_importer import parse_ofx
            from app.importers.pdf_statement import parse_pdf_statement
            for fname, data in batch:
                try:
                    if plug.startswith("pdf-"):
                        txns = parse_pdf_statement(data, filename=fname, year=yr,
                                                   entity_id=eid, kind=plug[4:])
                    else:
                        ofx_bytes = convert_to_ofx(data, fname, plug, log=log)
                        txns = parse_ofx(ofx_bytes, entity_id=eid, default_year=yr)
                except Exception as fe:
                    failed += 1
                    log(f"✗ {fname}: {fe}")
                    continue
                n_new = n_dup = 0
                for t in txns:
                    if db.transaction_exists(t.get("source", ""), t.get("source_id", "")):
                        n_dup += 1
                        continue
                    try:
                        db.add_transaction(t)
                        n_new += 1
                    except Exception as te:
                        log(f"  row error: {te}")
                imported += n_new
                skipped += n_dup
                log(f"✓ {fname}: {len(txns)} parsed, {n_new} imported, {n_dup} already present")
            status = "completed" if failed < len(batch) else "error"
            db.update_import_job(
                jid, status=status, count_imported=imported, count_skipped=skipped,
                error_msg=(f"{failed} of {len(batch)} file(s) failed — see log" if failed else None),
                completed_at=datetime.utcnow().isoformat(),
            )
            db.log_activity("import_complete",
                            f"Statement ({plug}): {imported} transactions from {len(batch)} file(s)")
            log(f"done — {imported} imported, {skipped} skipped as duplicates, {failed} file(s) failed")
        except Exception as e:
            log(f"FAILED: {e}")
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, uploads, plugin, entity_id, year),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id, "files": len(uploads)})


# ── Local filesystem ──────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/import/local/scan", methods=["POST"])
@login_required
def api_import_local_scan():
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Directory not found: {path}"}), 400
    try:
        from app.importers.local_fs import scan_directory, detect_entity_from_path
        files = scan_directory(path, recursive=True)
        counts = {"pdf": 0, "csv": 0, "ofx": 0}
        for fi in files:
            ext = fi["ext"]
            if ext == ".pdf":
                counts["pdf"] += 1
            elif ext == ".csv":
                counts["csv"] += 1
            elif ext in (".ofx", ".qfx", ".qbo"):
                counts["ofx"] += 1
        entities = db.get_entities()
        suggested = detect_entity_from_path(path, entities)
        return jsonify({
            "path": path, "total": len(files), "counts": counts,
            "suggested_entity": {"id": suggested["id"], "name": suggested["name"]}
                                 if suggested else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/import/local/run", methods=["POST"])
@login_required
def api_import_local_run():
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    entity_id = data.get("entity_id") or None
    year = data.get("year", "") or None
    if not path:
        return jsonify({"error": "path required"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Directory not found: {path}"}), 400
    job_id = db.create_import_job("local_fs", entity_id=entity_id,
                                  config_json=json.dumps({"path": path, "year": year}))
    entities_list = [dict(e) for e in db.list_entities()]

    def _run(jid, fpath, eid, yr, cpath, ents):
        def log(msg):
            append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        log(f"Scanning: {fpath}")
        log(f"Consume path: {cpath}")
        try:
            from app.importers.local_fs import import_directory, scan_directory
            all_files = scan_directory(fpath, recursive=True)
            log(f"Found {len(all_files)} files")
            if not cpath or not os.path.isdir(cpath):
                log(f"ERROR: consume path not accessible: {cpath}")
            result = import_directory(fpath, entity_id=eid, default_year=yr,
                                       consume_path=cpath, recursive=True, entities=ents)
            total_txns = 0
            for t in result.get("transactions", []):
                try:
                    db.add_transaction(t)
                    total_txns += 1
                except Exception:
                    pass
            pdfs = result.get("pdfs_queued", 0)
            errors = result.get("errors", [])
            entity_counts = result.get("entity_counts", {})
            if entity_counts:
                log("Entity breakdown: " + ", ".join(
                    f"{slug}: {cnt}" for slug, cnt in sorted(entity_counts.items())))
            for err in errors[:20]:
                log(f"  ERROR: {err}")
            if len(errors) > 20:
                log(f"  ... and {len(errors)-20} more errors")
            log(f"Done: {pdfs} PDFs queued, {total_txns} transactions imported"
                + (f" | {len(errors)} errors" if errors else ""))
            db.update_import_job(jid, status="completed",
                                 count_imported=total_txns + pdfs,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"Local FS: {pdfs} PDFs, {total_txns} txns from {fpath}")
        except Exception as e:
            log(f"FATAL ERROR: {e}")
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run,
                     args=(job_id, path, entity_id, year, CONSUME_PATH, entities_list),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})
