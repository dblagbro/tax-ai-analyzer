"""Dashboard stats, activity log, years, and filed tax returns."""
import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import _row_list

logger = logging.getLogger(__name__)

bp = Blueprint("stats", __name__)


@bp.route(URL_PREFIX + "/api/stats")
@login_required
def api_stats():
    try:
        year = request.args.get("year") or None
        entity_id_arg = request.args.get("entity_id") or None
        entity_id_int = int(entity_id_arg) if entity_id_arg else None
        summary = db.get_financial_summary(entity_id=entity_id_int, tax_year=year)
        entities = _row_list(db.list_entities())
        by_entity = {}
        for ent in entities:
            if entity_id_int and ent["id"] != entity_id_int:
                continue
            es = db.get_financial_summary(entity_id=ent["id"], tax_year=year)
            by_entity[ent["slug"]] = {
                "name": ent["name"],
                "color": ent.get("color", "#1a3c5e"),
                "income": round(es["income"], 2),
                "expenses": round(es["expense"] + es["deduction"], 2),
                "net": round(es["net"], 2),
                "doc_count": sum(es["counts"].values()),
            }
        conn = db.get_connection()
        total_docs = conn.execute(
            "SELECT COUNT(*) FROM analyzed_documents"
        ).fetchone()[0]
        dup_count = conn.execute(
            "SELECT COUNT(*) FROM analyzed_documents WHERE is_duplicate=1"
        ).fetchone()[0]
        conn.close()
        return jsonify({
            "total_docs": total_docs,
            "analyzed": total_docs,
            "total_income": round(summary["income"], 2),
            "total_expenses": round(summary["expense"] + summary["deduction"], 2),
            "net": round(summary["net"], 2),
            "by_entity": by_entity,
            "duplicate_docs": dup_count,
        })
    except Exception as e:
        logger.error("Stats error: %s", e)
        return jsonify({
            "total_docs": 0, "analyzed": 0,
            "total_income": 0, "total_expenses": 0, "net": 0,
            "by_entity": {},
        })


@bp.route(URL_PREFIX + "/api/stats/years")
@login_required
def api_years_with_docs():
    rows = db.get_years_with_docs()
    conn = db.get_connection()
    try:
        filed_years = [r[0] for r in conn.execute(
            "SELECT DISTINCT tax_year FROM filed_tax_returns ORDER BY tax_year DESC"
        ).fetchall()]
    finally:
        conn.close()
    all_years = sorted(
        set([r["tax_year"] for r in rows] + filed_years),
        reverse=True
    )
    year_map = {r["tax_year"]: r for r in rows}
    return jsonify({
        "years": [
            {
                "year": y,
                "doc_count": year_map[y]["doc_count"] if y in year_map else 0,
                "has_filed_return": y in filed_years,
            }
            for y in all_years
        ]
    })


@bp.route(URL_PREFIX + "/api/years")
@login_required
def api_years():
    from app.config import TAX_YEARS
    return jsonify(TAX_YEARS)


@bp.route(URL_PREFIX + "/api/activity")
@login_required
def api_activity():
    """Activity log. Without filter params, returns recent rows (flat list for backwards compat).
    With any of action/search/user_id/entity_id/since/until/offset params, returns
    {'rows': [...], 'total': N} so callers can paginate.
    """
    limit = min(int(request.args.get("limit", 50)), 1000)
    has_filters = any(
        request.args.get(k) for k in
        ("action", "search", "user_id", "entity_id", "since", "until", "offset")
    )
    if not has_filters:
        return jsonify(_row_list(db.get_recent_activity(limit)))
    action = request.args.get("action") or None
    search = request.args.get("search") or None
    since = request.args.get("since") or None
    until = request.args.get("until") or None
    try:
        user_id = int(request.args["user_id"]) if request.args.get("user_id") else None
        entity_id = int(request.args["entity_id"]) if request.args.get("entity_id") else None
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "numeric filter must be integer"}), 400
    rows, total = db.search_activity(
        action=action, user_id=user_id, entity_id=entity_id,
        search=search, since=since, until=until,
        limit=limit, offset=offset,
    )
    return jsonify({"rows": rows, "total": total, "limit": limit, "offset": offset})


@bp.route(URL_PREFIX + "/api/activity/actions")
@login_required
def api_activity_actions():
    """Distinct action values + counts for filter dropdown."""
    return jsonify(db.distinct_activity_actions())


@bp.route(URL_PREFIX + "/api/filed-returns", methods=["GET"])
@login_required
def api_list_filed_returns():
    entity_id = request.args.get("entity_id", type=int)
    returns = db.list_filed_returns(entity_id=entity_id)
    return jsonify({"returns": returns})


@bp.route(URL_PREFIX + "/api/filed-returns", methods=["POST"])
@login_required
def api_upsert_filed_return():
    data = request.get_json(force=True) or {}
    entity_id = data.get("entity_id")
    tax_year = data.get("tax_year")
    if not entity_id or not tax_year:
        return jsonify({"error": "entity_id and tax_year required"}), 400
    result = db.upsert_filed_return(int(entity_id), str(tax_year), **{
        k: data.get(k) for k in [
            "filing_status", "agi", "wages_income", "business_income", "other_income",
            "total_income", "total_deductions", "taxable_income", "total_tax",
            "refund_amount", "amount_owed", "preparer_name", "preparer_firm",
            "filed_date", "notes"
        ] if k in data
    })
    return jsonify({"status": "ok", "return": result})


@bp.route(URL_PREFIX + "/api/health")
@login_required
def api_health():
    import httpx, socket, os
    results = {}

    def _check(name, fn):
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"status": "error", "message": str(e)[:120]}

    results["tax-ai-analyzer"] = {"status": "ok", "message": "Running"}

    def _paperless():
        from app.config import PAPERLESS_API_BASE_URL
        settings = db.get_all_settings()
        base = (settings.get("paperless_url") or PAPERLESS_API_BASE_URL or "").rstrip("/")
        token = settings.get("paperless_token") or os.environ.get("PAPERLESS_API_TOKEN", "")
        headers = {"Authorization": f"Token {token}"} if token else {}
        r = httpx.get(f"{base}/api/documents/?page_size=1", headers=headers,
                      timeout=5, follow_redirects=True)
        if r.status_code == 200:
            count = r.json().get("count", "?")
            return {"status": "ok", "message": f"Paperless OK — {count} docs"}
        if r.status_code == 403:
            return {"status": "warn", "message": "Paperless reachable but token invalid"}
        return {"status": "warn", "message": f"HTTP {r.status_code}"}
    _check("tax-paperless-web", _paperless)

    def _elastic():
        es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
        es_pass = os.environ.get("ELASTICSEARCH_PASSWORD", "")
        auth_pair = ("elastic", es_pass) if es_pass else None
        r = httpx.get(f"{es_url}/_cluster/health", auth=auth_pair, timeout=5)
        data = r.json()
        status = "ok" if data.get("status") in ("green", "yellow") else "warn"
        return {"status": status, "message": f"cluster: {data.get('status','?')}"}
    _check("elasticsearch", _elastic)

    def _redis():
        s = socket.create_connection(("tax-paperless-redis", 6379), timeout=3)
        s.close()
        return {"status": "ok", "message": "TCP reachable"}
    _check("tax-paperless-redis", _redis)

    def _postgres():
        s = socket.create_connection(("tax-paperless-postgres", 5432), timeout=3)
        s.close()
        return {"status": "ok", "message": "TCP reachable"}
    _check("tax-paperless-postgres", _postgres)

    return jsonify(results)


@bp.route(URL_PREFIX + "/api/health/extended")
@login_required
def api_health_extended():
    """One-shot 'is anything broken?' — row counts, threads, disk, feature activity.

    Non-fatal: each section is wrapped so a failure in one doesn't taint others.
    """
    import os
    import shutil
    import threading

    out: dict = {}

    # ── row counts (all critical tables) ────────────────────────────────
    try:
        conn = db.get_connection()
        tables = ["users", "entities", "analyzed_documents", "transactions",
                  "transaction_links", "import_jobs", "activity_log",
                  "pdf_content_hashes", "mileage_log", "plaid_items"]
        counts = {}
        for t in tables:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception as e:
                counts[t] = {"error": str(e)[:80]}
        # Sub-metrics
        counts["transactions_with_vendor"] = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE vendor IS NOT NULL AND vendor != ''"
        ).fetchone()[0]
        counts["transactions_missing_normalized"] = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE (vendor_normalized IS NULL OR vendor_normalized='') AND vendor IS NOT NULL"
        ).fetchone()[0]
        counts["documents_duplicate"] = conn.execute(
            "SELECT COUNT(*) FROM analyzed_documents WHERE is_duplicate=1"
        ).fetchone()[0]
        conn.close()
        out["row_counts"] = counts
    except Exception as e:
        out["row_counts"] = {"error": str(e)[:120]}

    # ── background threads ──────────────────────────────────────────────
    try:
        threads = {t.name: {"alive": t.is_alive(), "daemon": t.daemon}
                   for t in threading.enumerate()}
        expected = {"analysis-daemon", "dedup-scheduler"}
        out["threads"] = {
            "alive": threads,
            "count": len(threads),
            "expected_present": {n: (n in threads and threads[n]["alive"])
                                 for n in expected},
        }
    except Exception as e:
        out["threads"] = {"error": str(e)[:120]}

    # ── disk usage (consume + data) ─────────────────────────────────────
    try:
        disks = {}
        for path in ("/consume", "/app/data", "/app/export"):
            if os.path.exists(path):
                total, used, free = shutil.disk_usage(path)
                disks[path] = {
                    "total_gb":  round(total / 1_073_741_824, 2),
                    "used_gb":   round(used  / 1_073_741_824, 2),
                    "free_gb":   round(free  / 1_073_741_824, 2),
                    "pct_used":  round(used * 100.0 / total, 1) if total else None,
                }
            else:
                disks[path] = {"exists": False}
        out["disk"] = disks
    except Exception as e:
        out["disk"] = {"error": str(e)[:120]}

    # ── recent activity per major action type ───────────────────────────
    try:
        conn = db.get_connection()
        rows = conn.execute(
            """SELECT action, MAX(created_at) as last_seen, COUNT(*) as total
               FROM activity_log
               WHERE action IN ('import_complete','dedup_scan','login',
                                'vendor_merge','txn_bulk_update','doc_bulk_update',
                                'mileage_add','plaid_connected','txn_receipt_attached',
                                'link_manual')
               GROUP BY action
               ORDER BY last_seen DESC"""
        ).fetchall()
        out["recent_activity"] = {r["action"]: {"last_seen": r["last_seen"],
                                                 "total": r["total"]} for r in rows}
        conn.close()
    except Exception as e:
        out["recent_activity"] = {"error": str(e)[:120]}

    # ── feature configuration state ─────────────────────────────────────
    try:
        cfg = {
            "gmail_configured":    bool(db.get_setting("gmail_oauth_token")),
            "plaid_configured":    bool(db.get_setting("plaid_client_id")),
            "imap_configured":     bool(db.get_setting("imap_host")) and bool(db.get_setting("imap_password")),
            "simplefin_connected": bool(db.get_setting("simplefin_access_url")),
            "paperless_configured":bool(db.get_setting("paperless_token")),
            "accountant_portal_active": bool(db.get_setting("accountant_token")),
        }
        out["features"] = cfg
    except Exception as e:
        out["features"] = {"error": str(e)[:120]}

    # Overall health signal — any section with error or missing thread = degraded
    problems = []
    if "error" in out.get("row_counts", {}):
        problems.append("row_counts")
    tp = out.get("threads", {}).get("expected_present", {})
    for thread_name, alive in tp.items():
        if not alive:
            problems.append(f"thread:{thread_name}")
    for path, info in (out.get("disk", {}) or {}).items():
        if isinstance(info, dict) and info.get("pct_used", 0) > 90:
            problems.append(f"disk:{path}")
    out["overall"] = {"status": "ok" if not problems else "degraded",
                      "problems": problems}

    return jsonify(out)
