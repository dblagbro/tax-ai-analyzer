"""Export generation, download, and listing."""
import logging
import os
import re
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX, EXPORT_PATH
from app.routes.helpers import _url

logger = logging.getLogger(__name__)

bp = Blueprint("export_", __name__)

_YEAR_RE = re.compile(r"^\d{4}$")
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
_VALID_FORMATS = {"csv", "json", "iif", "qbo", "ofx", "txf", "pdf", "zip"}
_EXT_MAP = {"csv": ".csv", "json": ".json", "iif": ".iif", "qbo": ".qbo",
            "ofx": ".ofx", "txf": ".txf", "pdf": ".pdf", "zip": ".zip"}


def _candidate_filenames(fmt: str, year: str, entity_slug: str) -> list[str]:
    """Return possible filenames for a given export format on disk.

    The generator (app/export/__init__.py:export_all) writes:
      csv  → transactions_{year}_{slug}.csv
      pdf  → summary_{year}_{slug}.pdf
      zip  → tax_{year}_{slug}_complete.zip
      json/iif/qbo/ofx/txf → export_{year}_{slug}.{ext}

    The original download route looked for {slug}_{year}{ext} which never
    matched any generated file — this helper enumerates every known
    convention so both current and legacy files stay reachable.
    """
    ext = _EXT_MAP[fmt]
    primary = {
        "csv": f"transactions_{year}_{entity_slug}.csv",
        "pdf": f"summary_{year}_{entity_slug}.pdf",
        "zip": f"tax_{year}_{entity_slug}_complete.zip",
    }.get(fmt, f"export_{year}_{entity_slug}{ext}")
    legacy = f"{entity_slug}_{year}{ext}"
    return [primary, legacy]


def _validate_year_slug(year: str, entity_slug: str):
    """Return (year, entity_slug) if valid, raise ValueError otherwise."""
    if not _YEAR_RE.match(year):
        raise ValueError("invalid year")
    if not _SLUG_RE.match(entity_slug):
        raise ValueError("invalid entity slug")
    return year, entity_slug


def _safe_export_path(year: str, entity_slug: str, ext: str) -> str:
    """Build an export file path and verify it stays inside EXPORT_PATH."""
    filename = f"{entity_slug}_{year}{ext}"
    export_root = os.path.realpath(EXPORT_PATH)
    for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
        candidate = os.path.realpath(os.path.join(base, filename))
        if candidate.startswith(export_root + os.sep) or candidate == export_root:
            return candidate
    raise ValueError("computed path escapes export directory")


@bp.route(URL_PREFIX + "/api/export/<year>/<entity_slug>", methods=["POST"])
@login_required
def api_export_generate(year, entity_slug):
    try:
        _validate_year_slug(year, entity_slug)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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
    try:
        _validate_year_slug(year, entity_slug)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    fmt = format_name.lower()
    if fmt not in _VALID_FORMATS:
        return jsonify({"error": "unsupported format"}), 400
    export_root = os.path.realpath(EXPORT_PATH)
    for filename in _candidate_filenames(fmt, year, entity_slug):
        for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
            path = os.path.realpath(os.path.join(base, filename))
            if not path.startswith(export_root):
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
    try:
        _validate_year_slug(year, entity_slug)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    format_name = request.args.get("format", "zip")
    fmt = format_name.lower()
    if fmt not in _VALID_FORMATS:
        return jsonify({"error": f"unknown format: {fmt}"}), 400
    ext = _EXT_MAP[fmt]
    filename = f"{entity_slug}_{year}{ext}"
    export_root = os.path.realpath(EXPORT_PATH)
    for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
        path = os.path.realpath(os.path.join(base, filename))
        if not path.startswith(export_root):
            return jsonify({"error": "invalid path"}), 400
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
        elif format_name == "qbo":
            from app.export.quickbooks import export_qbo
            path = export_qbo(year, entity_slug)
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
