"""Tax archive folder manager: scan, rename, coverage, and Paperless queue."""
import os

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import admin_required

TAX_SOURCE_ROOT = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
TAX_CONSUME_ROOT = "/mnt/s/documents/tax-organizer/consume"

bp = Blueprint("folder_manager", __name__)


@bp.route(URL_PREFIX + "/api/folder-manager/scan")
@login_required
def api_fm_scan():
    from app.folder_manager import find_inconsistencies
    year = request.args.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    issues = find_inconsistencies(root)
    return jsonify({"root": root, "issues": issues, "count": len(issues)})


@bp.route(URL_PREFIX + "/api/folder-manager/tree")
@login_required
def api_fm_tree():
    from app.folder_manager import scan_tree
    year = request.args.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    tree = scan_tree(root, max_depth=3)
    return jsonify({"tree": tree})


@bp.route(URL_PREFIX + "/api/folder-manager/rename", methods=["POST"])
@login_required
@admin_required
def api_fm_rename():
    from app.folder_manager import rename_folder, merge_folders
    from pathlib import Path
    data = request.get_json(silent=True) or {}
    src = data.get("src", "").strip()
    new_name = data.get("new_name", "").strip()
    dry_run = data.get("dry_run", True)
    merge = data.get("merge", False)

    if not src or not new_name:
        return jsonify({"error": "src and new_name required"}), 400
    if not os.path.abspath(src).startswith(os.path.abspath(TAX_SOURCE_ROOT)):
        return jsonify({"error": "Path outside allowed root"}), 403

    dst = str(Path(src).parent / new_name)
    if merge and Path(dst).exists():
        result = merge_folders(src, dst, dry_run=dry_run)
    else:
        result = rename_folder(src, new_name, dry_run=dry_run)
    return jsonify(result)


@bp.route(URL_PREFIX + "/api/folder-manager/rename-all", methods=["POST"])
@login_required
@admin_required
def api_fm_rename_all():
    from app.folder_manager import apply_all_auto_renames
    data = request.get_json(silent=True) or {}
    dry_run = data.get("dry_run", True)
    year = data.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    results = apply_all_auto_renames(root, dry_run=dry_run)
    return jsonify({"dry_run": dry_run, "results": results, "count": len(results)})


@bp.route(URL_PREFIX + "/api/folder-manager/coverage")
@login_required
def api_fm_coverage():
    from app.folder_manager import check_paperless_coverage
    from app import config as _cfg
    year = request.args.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    token = db.get_setting("paperless_api_token") or _cfg.PAPERLESS_API_TOKEN
    url = db.get_setting("paperless_url") or _cfg.PAPERLESS_API_BASE_URL
    result = check_paperless_coverage(root, token, url)
    return jsonify(result)


@bp.route(URL_PREFIX + "/api/folder-manager/queue", methods=["POST"])
@login_required
@admin_required
def api_fm_queue():
    from app.folder_manager import queue_year_for_paperless
    data = request.get_json(silent=True) or {}
    year = str(data.get("year", "")).strip()
    entity_slug = data.get("entity_slug", "personal")
    dry_run = data.get("dry_run", True)
    if not year:
        return jsonify({"error": "year required"}), 400
    result = queue_year_for_paperless(TAX_SOURCE_ROOT, year, TAX_CONSUME_ROOT,
                                     entity_slug, dry_run=dry_run)
    return jsonify(result)
