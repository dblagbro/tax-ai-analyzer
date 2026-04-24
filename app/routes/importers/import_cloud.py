"""Cloud storage import routes: Google Drive, Dropbox, S3, and filed-return AI extraction."""
import json
import logging
import os
import threading
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX, CONSUME_PATH
from app.routes.helpers import _url, admin_required

logger = logging.getLogger(__name__)

bp = Blueprint("import_cloud", __name__)


def _cloud_unavail(service: str):
    return jsonify({"error": f"{service} adapter not configured", "configured": False}), 503


# ── Google Drive ──────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/cloud/google-drive/auth")
@login_required
def api_gdrive_auth():
    try:
        from app.cloud_adapters.google_drive import get_auth_url
        from flask import session as flask_session
        redirect_uri = url_for("import_cloud.api_gdrive_callback", _external=True)
        flask_session["gdrive_redirect_uri"] = redirect_uri
        return redirect(get_auth_url(redirect_uri))
    except ImportError:
        return _cloud_unavail("Google Drive")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/cloud/google-drive/callback")
@login_required
def api_gdrive_callback():
    try:
        from app.cloud_adapters.google_drive import handle_callback
        handle_callback(request.args)
        flash("Google Drive connected.", "success")
    except ImportError:
        flash("Google Drive adapter not available.", "warning")
    except Exception as e:
        flash(f"Google Drive auth error: {e}", "danger")
    return redirect(_url("/import"))


@bp.route(URL_PREFIX + "/api/cloud/google-drive/files")
@login_required
def api_gdrive_files():
    try:
        from app.cloud_adapters.google_drive import list_files
        return jsonify({"files": list_files(folder_id=request.args.get("folder", ""))})
    except ImportError:
        return _cloud_unavail("Google Drive")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/cloud/google-drive/import", methods=["POST"])
@login_required
def api_gdrive_import():
    try:
        from app.cloud_adapters.google_drive import import_files
    except ImportError:
        return _cloud_unavail("Google Drive")
    data = request.get_json() or {}
    file_ids = data.get("file_ids", [])
    entity_id = data.get("entity_id")
    job_id = db.create_import_job("google_drive", entity_id=entity_id,
                                  config_json=json.dumps({"file_ids": file_ids}))

    def _run(jid, fids, eid):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            count = import_files(fids, entity_id=eid)
            db.update_import_job(jid, status="completed", count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, file_ids, entity_id), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── Dropbox ───────────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/cloud/dropbox/auth")
@login_required
def api_dropbox_auth():
    try:
        import secrets
        from app.cloud_adapters.dropbox_adapter import get_auth_url
        from flask import session as flask_session
        redirect_uri = url_for("import_cloud.api_dropbox_callback", _external=True)
        flask_session["dropbox_redirect_uri"] = redirect_uri
        flask_session["dropbox_oauth_state"] = secrets.token_urlsafe(24)
        return redirect(get_auth_url(redirect_uri))
    except ImportError:
        return _cloud_unavail("Dropbox")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/cloud/dropbox/callback")
@login_required
def api_dropbox_callback():
    try:
        from flask import session as flask_session
        expected = flask_session.pop("dropbox_oauth_state", None)
        returned = request.args.get("state")
        # The Dropbox SDK uses its own csrf_token under key "dropbox_csrf" — our own
        # state param is a belt-and-suspenders check for callers that relay it.
        if expected and returned and expected != returned:
            flash("Dropbox auth state mismatch — request rejected.", "danger")
            return redirect(_url("/import"))
        from app.cloud_adapters.dropbox_adapter import handle_callback
        handle_callback(request.args)
        flash("Dropbox connected.", "success")
    except ImportError:
        flash("Dropbox adapter not available.", "warning")
    except Exception as e:
        flash(f"Dropbox auth error: {e}", "danger")
    return redirect(_url("/import"))


@bp.route(URL_PREFIX + "/api/cloud/dropbox/files")
@login_required
def api_dropbox_files():
    try:
        from app.cloud_adapters.dropbox_adapter import list_files
        return jsonify({"files": list_files(path=request.args.get("path", ""))})
    except ImportError:
        return _cloud_unavail("Dropbox")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/cloud/dropbox/import", methods=["POST"])
@login_required
def api_dropbox_import():
    try:
        from app.cloud_adapters.dropbox_adapter import import_files
    except ImportError:
        return _cloud_unavail("Dropbox")
    data = request.get_json() or {}
    paths = data.get("paths", [])
    entity_id = data.get("entity_id")
    job_id = db.create_import_job("dropbox", entity_id=entity_id,
                                  config_json=json.dumps({"paths": paths}))

    def _run(jid, ps, eid):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            count = import_files(ps, entity_id=eid)
            db.update_import_job(jid, status="completed", count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, paths, entity_id), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── Amazon S3 ─────────────────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/cloud/s3/browse", methods=["POST"])
@login_required
def api_s3_browse():
    data = request.get_json() or {}
    settings = db.get_all_settings()
    bucket = data.get("bucket") or settings.get("s3_bucket", "")
    prefix = data.get("prefix", "")
    if not bucket:
        return jsonify({"error": "S3 bucket not configured"}), 400
    try:
        import boto3
        s3 = boto3.client(
            "s3",
            region_name=settings.get("s3_region", "us-east-1"),
            aws_access_key_id=settings.get("s3_access_key"),
            aws_secret_access_key=settings.get("s3_secret_key"),
        )
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
        return jsonify({
            "files": [{"key": o["Key"], "size": o["Size"],
                       "modified": str(o.get("LastModified", ""))}
                      for o in resp.get("Contents", [])],
            "folders": [p.get("Prefix", "") for p in resp.get("CommonPrefixes", [])],
        })
    except ImportError:
        return jsonify({"error": "boto3 not installed"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route(URL_PREFIX + "/api/cloud/s3/import", methods=["POST"])
@login_required
def api_s3_import():
    data = request.get_json() or {}
    settings = db.get_all_settings()
    bucket = data.get("bucket") or settings.get("s3_bucket", "")
    keys = data.get("keys", [])
    entity_id = data.get("entity_id")
    if not bucket or not keys:
        return jsonify({"error": "bucket and keys required"}), 400
    job_id = db.create_import_job("s3", entity_id=entity_id,
                                  config_json=json.dumps({"bucket": bucket, "keys": keys}))

    def _run(jid, bkt, ks):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            import boto3
            stt = db.get_all_settings()
            s3 = boto3.client(
                "s3",
                region_name=stt.get("s3_region", "us-east-1"),
                aws_access_key_id=stt.get("s3_access_key"),
                aws_secret_access_key=stt.get("s3_secret_key"),
            )
            count = 0
            for key in ks:
                try:
                    dest = os.path.join(CONSUME_PATH, os.path.basename(key))
                    os.makedirs(CONSUME_PATH, exist_ok=True)
                    s3.download_file(bkt, key, dest)
                    count += 1
                except Exception as ke:
                    logger.error("S3 download %s: %s", key, ke)
            db.update_import_job(jid, status="completed", count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"S3: {count} files")
        except ImportError:
            db.update_import_job(jid, status="error", error_msg="boto3 not installed",
                                 completed_at=datetime.utcnow().isoformat())
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, bucket, keys), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ── Filed returns — AI extraction from tax archive folder ─────────────────────

@bp.route(URL_PREFIX + "/api/filed-returns/import-from-folder", methods=["POST"])
@login_required
@admin_required
def api_import_filed_return_from_folder():
    import glob as _glob
    import base64
    import re as _re
    data = request.get_json(silent=True) or {}
    year = str(data.get("year", "")).strip()
    entity_id = data.get("entity_id")

    if not year:
        return jsonify({"error": "year required"}), 400

    tax_base = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
    year_dir = os.path.join(tax_base, str(year))

    if not os.path.isdir(year_dir):
        return jsonify({"error": f"No tax folder found for {year} at {year_dir}"}), 404

    pdfs = sorted(
        _glob.glob(os.path.join(year_dir, "*.pdf")) +
        _glob.glob(os.path.join(year_dir, "*.PDF"))
    )
    if not pdfs:
        return jsonify({"error": f"No PDF files found in {year_dir}"}), 404

    _RETURN_SIGNALS = ["1040", "client copy", "client_copy", "tax return",
                       "filed return", "complete return", "blag"]
    _EXCLUDE_SIGNALS = ["w2", "w-2", "1099", "1098", "property_tax", "property tax",
                        "mortgage", "statement", "invoice", "receipt", "billing",
                        "refi", "initial_disclosure", "interest"]

    def _is_return(path: str) -> bool:
        name = os.path.basename(path).lower()
        if any(s in name for s in _EXCLUDE_SIGNALS):
            return False
        return any(s in name for s in _RETURN_SIGNALS)

    preferred = [p for p in pdfs if _is_return(p)]
    pdf_path = preferred[0] if preferred else pdfs[0]

    from app import config as _cfg
    from app.llm_client import LLMClient

    llm_provider = db.get_setting("llm_provider") or _cfg.LLM_PROVIDER
    llm_api_key = db.get_setting("llm_api_key") or _cfg.LLM_API_KEY
    llm_model = db.get_setting("llm_model") or _cfg.LLM_MODEL

    if not llm_api_key:
        return jsonify({"error": "LLM API key not configured"}), 400

    prompt = (
        f"This is a US tax return PDF for tax year {year}. "
        "Extract these fields and return ONLY valid JSON (no markdown):\n"
        "filing_status, agi, wages_income, business_income, other_income, "
        "total_income, total_deductions, taxable_income, total_tax, "
        "refund_amount, amount_owed, preparer_name, preparer_firm, filed_date (YYYY-MM-DD), notes\n"
        "Use null for fields not found. Numeric fields as numbers not strings."
    )

    try:
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return jsonify({"error": f"Failed to read PDF: {e}"}), 500

    try:
        if llm_provider == "anthropic":
            import anthropic
            ac = anthropic.Anthropic(api_key=llm_api_key)
            msg = ac.messages.create(
                model=llm_model,
                max_tokens=2000,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {"type": "base64",
                                                         "media_type": "application/pdf",
                                                         "data": pdf_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw = msg.content[0].text
        else:
            import subprocess
            result = subprocess.run(["pdftotext", pdf_path, "-"],
                                    capture_output=True, text=True, timeout=30)
            pdf_text = result.stdout[:8000] if result.returncode == 0 else ""
            if not pdf_text:
                return jsonify({"error": "Could not extract text from PDF"}), 400
            client = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)
            raw = client.chat([{"role": "user",
                                "content": f"Tax return text:\n{pdf_text}\n\n{prompt}"}])
            if not isinstance(raw, str):
                raw = str(raw)

        match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not match:
            return jsonify({"error": "Could not parse JSON from AI response",
                            "raw": raw[:500]}), 500
        extracted = json.loads(match.group(0))
    except Exception as e:
        logger.error("Filed return extraction failed: %s", e)
        return jsonify({"error": f"AI extraction failed: {e}"}), 500

    if not entity_id:
        ent = db.get_entity(slug="personal")
        if ent:
            entity_id = ent["id"]
    if not entity_id:
        return jsonify({"error": "entity_id required and could not be resolved"}), 400

    allowed_fields = {
        "filing_status", "agi", "wages_income", "business_income", "other_income",
        "total_income", "total_deductions", "taxable_income", "total_tax",
        "refund_amount", "amount_owed", "preparer_name", "preparer_firm",
        "filed_date", "notes",
    }
    kwargs = {k: v for k, v in extracted.items() if v is not None and k in allowed_fields}

    try:
        result = db.upsert_filed_return(entity_id=entity_id, tax_year=str(year), **kwargs)
        return jsonify({
            "status": "ok",
            "source": pdf_path,
            "source_name": os.path.basename(pdf_path),
            "all_pdfs_found": [os.path.basename(p) for p in pdfs],
            "extracted": extracted,
            "return": result,
        })
    except Exception as e:
        return jsonify({"error": f"Database save failed: {e}"}), 500
