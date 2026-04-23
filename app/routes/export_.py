"""Export generation, download, and listing."""
import logging
import os
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX, EXPORT_PATH
from app.routes.helpers import _url

logger = logging.getLogger(__name__)

bp = Blueprint("export_", __name__)


@bp.route(URL_PREFIX + "/api/export/<year>/<entity_slug>", methods=["POST"])
@login_required
def api_export_generate(year, entity_slug):
    try:
        from app.export import export_all
        entity = db.get_entity(slug=entity_slug)
        entity_id = entity["id"] if entity else None
        doc_count = len(db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000))
        db.log_activity("export_started", f"{entity_slug}/{year}: {doc_count} docs",
                        user_id=current_user.id)
        result = export_all(year, entity_slug)
        files = result.get("files", {})
        errors = list(result.get("errors", {}).values())
        db.log_activity("export_complete", f"{entity_slug}/{year}", user_id=current_user.id)
        return jsonify({
            "status": "ok", "year": year, "entity": entity_slug,
            "doc_count": doc_count,
            "files": [os.path.basename(f) for f in files.values()],
            "zip": os.path.basename(files["zip"]) if "zip" in files else None,
            "errors": errors,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route(URL_PREFIX + "/api/export/<year>/<entity_slug>/download/<format_name>")
@login_required
def api_export_download(year, entity_slug, format_name):
    import re
    if not re.fullmatch(r"\d{4}", year):
        return jsonify({"error": "invalid year"}), 400
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", entity_slug):
        return jsonify({"error": "invalid entity"}), 400
    ext_map = {"csv": ".csv", "json": ".json", "iif": ".iif",
               "ofx": ".ofx", "txf": ".txf", "pdf": ".pdf", "zip": ".zip"}
    ext = ext_map.get(format_name.lower())
    if not ext:
        return jsonify({"error": "unsupported format"}), 400
    filename = f"{entity_slug}_{year}{ext}"
    for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
        path = os.path.realpath(os.path.join(base, filename))
        if not path.startswith(os.path.realpath(EXPORT_PATH)):
            return jsonify({"error": "invalid path"}), 400
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=filename)
    return jsonify({"error": "file not found"}), 404


@bp.route(URL_PREFIX + "/api/export/list")
@login_required
def api_export_list():
    files = []
    if os.path.exists(EXPORT_PATH):
        for root, dirs, fnames in os.walk(EXPORT_PATH):
            for fname in fnames:
                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                    files.append({
                        "filename": fname,
                        "path": os.path.relpath(fpath, EXPORT_PATH),
                        "size": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    })
                except Exception:
                    pass
    files.sort(key=lambda x: x.get("modified", ""), reverse=True)
    return jsonify(files)


@bp.route(URL_PREFIX + "/export/<year>/<entity_slug>")
@login_required
def export_download_direct(year, entity_slug):
    format_name = request.args.get("format", "zip")
    ext_map = {"csv": ".csv", "json": ".json", "iif": ".iif",
               "ofx": ".ofx", "txf": ".txf", "pdf": ".pdf", "zip": ".zip"}
    ext = ext_map.get(format_name.lower(), f".{format_name}")
    filename = f"{entity_slug}_{year}{ext}"
    for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=filename)
    try:
        if format_name == "csv":
            from app.export.csv_exporter import export_transactions_csv
            path = export_transactions_csv(year, entity_slug)
        elif format_name == "pdf":
            from app.export.pdf_report import export_pdf
            path = export_pdf(year, entity_slug)
        elif format_name == "iif":
            from app.export.quickbooks import export_iif
            path = export_iif(year, entity_slug)
        elif format_name == "ofx":
            from app.export.ofx_exporter import export_ofx
            path = export_ofx(year, entity_slug)
        elif format_name == "txf":
            from app.export.txf_exporter import export_txf
            path = export_txf(year, entity_slug)
        elif format_name == "zip":
            from app.export import export_all
            result = export_all(year, entity_slug)
            path = result.get("files", {}).get("zip")
        else:
            return jsonify({"error": f"unknown format: {format_name}"}), 400
        if path and os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=os.path.basename(path))
        return jsonify({"error": "export failed or no data"}), 500
    except Exception as e:
        logger.error("Export error %s/%s/%s: %s", year, entity_slug, format_name, e)
        return jsonify({"error": str(e)}), 500
