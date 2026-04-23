"""Accountant portal — read-only view by year and entity.

Accessible at /tax-ai-analyzer/accountant/<year>/<entity_slug>
No editing, no importing, no admin functions.

Protected by a separate accountant token (stored in settings as
'accountant_token'). If no token is set, falls back to requiring
normal login. Token is passed via ?token= query param.
"""
import logging
from datetime import datetime

from flask import Blueprint, abort, jsonify, redirect, render_template, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX

logger = logging.getLogger(__name__)
bp = Blueprint("accountant", __name__)


def _check_access() -> bool:
    """Return True if the request is authorized for accountant portal access."""
    token = request.args.get("token") or request.cookies.get("accountant_token")
    stored = db.get_setting("accountant_token") or ""
    if stored and token and token == stored:
        return True
    return current_user.is_authenticated


@bp.route(URL_PREFIX + "/accountant")
def accountant_index():
    if not _check_access():
        return redirect(URL_PREFIX + "/login?next=" + request.url)
    token = request.args.get("token", "")
    entities = db.get_entities() or []
    years = list(range(datetime.now().year, 2019, -1))
    return render_template("accountant/index.html",
                           entities=entities, years=years,
                           token=token, url_prefix=URL_PREFIX)


@bp.route(URL_PREFIX + "/accountant/<year>/<entity_slug>")
def accountant_report(year, entity_slug):
    if not _check_access():
        return redirect(URL_PREFIX + "/login?next=" + request.url)

    import re
    if not re.match(r"^\d{4}$", year) or not re.match(r"^[a-zA-Z0-9_\-]+$", entity_slug):
        abort(400)

    token = request.args.get("token", "")
    entity = db.get_entity(slug=entity_slug)
    if not entity:
        abort(404)

    entity_id = entity["id"]
    docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=2000)
    txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=5000)

    # Group by category
    income_docs = [d for d in docs if (d.get("category") or "").lower() in ("income", "revenue")]
    expense_docs = [d for d in docs if (d.get("category") or "").lower() in ("expense", "expenses")]
    deduction_docs = [d for d in docs if (d.get("category") or "").lower() in ("deduction", "deductions")]
    other_docs = [d for d in docs if d not in income_docs and d not in expense_docs and d not in deduction_docs]

    income_txns = [t for t in txns if (t.get("category") or "").lower() in ("income", "revenue") or (t.get("amount") or 0) > 0]
    expense_txns = [t for t in txns if (t.get("category") or "").lower() in ("expense", "expenses") or (t.get("amount") or 0) < 0]

    def _sum(items, key="amount"):
        return round(sum(abs(i.get(key) or 0) for i in items), 2)

    summary = {
        "income_total": _sum(income_docs) or _sum(income_txns),
        "expense_total": _sum(expense_docs) or _sum(expense_txns),
        "deduction_total": _sum(deduction_docs),
        "doc_count": len(docs),
        "txn_count": len(txns),
    }

    return render_template("accountant/report.html",
                           entity=dict(entity),
                           year=year,
                           summary=summary,
                           income_docs=income_docs,
                           expense_docs=expense_docs,
                           deduction_docs=deduction_docs,
                           other_docs=other_docs,
                           income_txns=income_txns,
                           expense_txns=expense_txns,
                           token=token,
                           url_prefix=URL_PREFIX,
                           now=datetime.now().strftime("%Y-%m-%d %H:%M"),
                           paperless_url=db.get_setting("paperless_url") or "")


@bp.route(URL_PREFIX + "/api/accountant/token", methods=["POST"])
@login_required
def api_set_accountant_token():
    """Generate or update the accountant access token."""
    import secrets
    data = request.get_json() or {}
    action = data.get("action", "generate")
    if action == "clear":
        db.set_setting("accountant_token", "")
        return jsonify({"status": "cleared"})
    token = secrets.token_urlsafe(24)
    db.set_setting("accountant_token", token)
    base = request.host_url.rstrip("/")
    url = f"{base}{URL_PREFIX}/accountant?token={token}"
    return jsonify({"status": "ok", "token": token, "url": url})


@bp.route(URL_PREFIX + "/api/accountant/token")
@login_required
def api_get_accountant_token():
    token = db.get_setting("accountant_token") or ""
    if not token:
        return jsonify({"token": None, "url": None})
    base = request.host_url.rstrip("/")
    url = f"{base}{URL_PREFIX}/accountant?token={token}"
    return jsonify({"token": token, "url": url})
