"""
Tax AI Analyzer — Flask Web UI
Complete multi-tab financial bookkeeping platform with AI support.
Uses the SQLite-backed db module and Flask-Login auth.
"""
import csv
import io
import json
import logging
import os
import re
import threading
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    session as flask_session, Response, send_file, flash,
    stream_with_context,
)
from flask_login import (
    LoginManager, login_required, login_user, logout_user, current_user,
)

from app import auth, db
from app.config import (
    URL_PREFIX, WEB_PORT, get_flask_secret_key,
    EXPORT_PATH, PAPERLESS_API_BASE_URL, LLM_MODEL,
    GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES, GMAIL_YEARS,
    CONSUME_PATH,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = get_flask_secret_key()
app.config["APPLICATION_ROOT"] = URL_PREFIX
app.config["PREFERRED_URL_SCHEME"] = "https"
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

# Trust X-Forwarded-Proto/Host headers from nginx reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def _user_loader(user_id: str):
    return auth.load_user(user_id)


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

with app.app_context():
    try:
        db.init_db()
        db.ensure_default_data()
        # Clean up any jobs that were "running" when the container last stopped.
        # These will never complete — mark them as interrupted so the UI is accurate.
        _stuck = db.get_connection()
        try:
            _stuck.execute(
                "UPDATE import_jobs SET status='interrupted', completed_at=? "
                "WHERE status='running'",
                (datetime.utcnow().isoformat(),)
            )
            _stuck.commit()
        finally:
            _stuck.close()
    except Exception as _boot_err:
        logger.warning(f"DB bootstrap deferred: {_boot_err}")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url(path: str) -> str:
    return URL_PREFIX + path


def admin_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            if request.path.startswith(URL_PREFIX + "/api/"):
                return jsonify({"error": "Admin access required"}), 403
            flash("Admin access required.", "danger")
            return redirect(_url("/"))
        return f(*args, **kwargs)
    return _wrap


def superuser_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_superuser:
            if request.path.startswith(URL_PREFIX + "/api/"):
                return jsonify({"error": "Superuser access required"}), 403
            flash("Superuser access required.", "danger")
            return redirect(_url("/"))
        return f(*args, **kwargs)
    return _wrap


def _user_can_access_session(sess) -> bool:
    """Return True if current user may read this chat session."""
    if not sess:
        return False
    if current_user.is_admin:
        return True
    if sess["user_id"] == current_user.id:
        return True
    # Check share table
    shares = db.get_chat_shares(sess["id"])
    return any(s["shared_with_user_id"] == current_user.id for s in shares)


def _user_can_write_session(sess) -> bool:
    """Return True if current user may post to this chat session."""
    if not sess:
        return False
    if current_user.is_admin or sess["user_id"] == current_user.id:
        return True
    shares = db.get_chat_shares(sess["id"])
    return any(s["shared_with_user_id"] == current_user.id and s["can_write"]
               for s in shares)


def _row_list(rows) -> list:
    """Convert sqlite3.Row list to dict list."""
    return [dict(r) for r in rows] if rows else []


# ---------------------------------------------------------------------------
# Context processor
# ---------------------------------------------------------------------------

@app.context_processor
def _inject():
    entities = []
    try:
        entities = _row_list(db.list_entities())
    except Exception:
        pass
    from app.config import TAX_YEARS, PAPERLESS_WEB_URL
    return {
        "url_prefix": URL_PREFIX,
        "entities": entities,
        "tax_years": TAX_YEARS,
        "paperless_web_url": PAPERLESS_WEB_URL,
        "app_name": "Financial AI Analyzer",
        "current_user": current_user,
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_url("/"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.authenticate(username, password)
        if user:
            login_user(user, remember=request.form.get("remember") == "on")
            db.log_activity("login", f"User '{username}' logged in",
                            user_id=user.id)
            return redirect(request.args.get("next") or _url("/"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, url_prefix=URL_PREFIX)


@app.route(URL_PREFIX + "/logout")
@login_required
def logout():
    db.log_activity("logout", f"User '{current_user.username}' logged out",
                    user_id=current_user.id)
    logout_user()
    return redirect(_url("/login"))


# ---------------------------------------------------------------------------
# SPA shell — all tabs render dashboard.html
# ---------------------------------------------------------------------------

def _no_cache_page(html_response):
    """Add no-cache headers so browsers always fetch fresh HTML."""
    from flask import make_response
    resp = make_response(html_response)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route(URL_PREFIX + "/")
@app.route(URL_PREFIX + "")
@login_required
def index():
    return _no_cache_page(render_template("dashboard.html", active_tab="dashboard"))


@app.route(URL_PREFIX + "/transactions")
@login_required
def transactions_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="transactions"))


@app.route(URL_PREFIX + "/documents")
@login_required
def documents_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="documents"))


@app.route(URL_PREFIX + "/import")
@login_required
def import_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="import"))


@app.route(URL_PREFIX + "/chat")
@login_required
def chat_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="chat"))


@app.route(URL_PREFIX + "/reports")
@login_required
def reports_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="reports"))


@app.route(URL_PREFIX + "/settings")
@login_required
@admin_required
def settings_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="settings"))


@app.route(URL_PREFIX + "/users")
@login_required
@admin_required
def users_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="users"))


@app.route(URL_PREFIX + "/entities")
@login_required
def entities_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="entities"))


@app.route(URL_PREFIX + "/ai-costs")
@app.route(URL_PREFIX + "/ai_costs")
@login_required
def ai_costs_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="ai_costs"))


@app.route(URL_PREFIX + "/docs")
@login_required
def docs_page():
    return render_template("docs.html")


# ---------------------------------------------------------------------------
# API — Stats / Activity
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/stats/years")
@login_required
def api_years_with_docs():
    """Return years that have analyzed documents, with counts."""
    rows = db.get_years_with_docs()
    # Also include any years from filed returns
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


@app.route(URL_PREFIX + "/api/filed-returns", methods=["GET"])
@login_required
def api_list_filed_returns():
    entity_id = request.args.get("entity_id", type=int)
    returns = db.list_filed_returns(entity_id=entity_id)
    return jsonify({"returns": returns})


@app.route(URL_PREFIX + "/api/filed-returns", methods=["POST"])
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


@app.route(URL_PREFIX + "/api/tax-review")
@login_required
def api_tax_review():
    """Stream AI-generated tax review questions for a year."""
    import json as _json
    year = request.args.get("year")
    entity_id = request.args.get("entity_id", type=int)

    # Gather all documents for the year
    docs = _row_list(db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=500))
    filed = db.list_filed_returns(entity_id=entity_id)
    filed_this_year = next((r for r in filed if r["tax_year"] == year), None)

    # Build summary for AI
    summary_lines = [f"Tax Year: {year}"]
    if entity_id:
        conn = db.get_connection()
        try:
            ent = conn.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
            if ent:
                summary_lines.append(f"Entity: {ent['name']}")
        finally:
            conn.close()

    summary_lines.append(f"Total documents analyzed: {len(docs)}")

    income_docs = [d for d in docs if d.get("category") == "income"]
    expense_docs = [d for d in docs if d.get("category") == "expense"]
    deduction_docs = [d for d in docs if d.get("category") == "deduction"]
    asset_docs = [d for d in docs if d.get("category") == "asset"]
    low_conf_docs = [d for d in docs if (d.get("confidence") or 1.0) < 0.6]
    capital_docs = [d for d in docs if d.get("doc_type") == "capital_improvement"]

    total_income = sum(d.get("amount") or 0 for d in income_docs)
    total_expense = sum(d.get("amount") or 0 for d in expense_docs)
    total_deductions = sum(d.get("amount") or 0 for d in deduction_docs)

    summary_lines += [
        f"\nINCOME ({len(income_docs)} docs, ${total_income:,.2f} total):",
    ]
    for d in sorted(income_docs, key=lambda x: -(x.get("amount") or 0))[:15]:
        summary_lines.append(f"  - [{d.get('doc_type')}] {d.get('vendor','')} ${d.get('amount') or 0:,.2f} ({d.get('date','?')})")

    summary_lines += [f"\nEXPENSES ({len(expense_docs)} docs, ${total_expense:,.2f} total):"]
    for d in sorted(expense_docs, key=lambda x: -(x.get("amount") or 0))[:20]:
        summary_lines.append(f"  - [{d.get('doc_type')}] {d.get('vendor','')} ${d.get('amount') or 0:,.2f} ({d.get('date','?')}) doc#{d.get('paperless_doc_id')}")

    if deduction_docs:
        summary_lines.append(f"\nDEDUCTIONS ({len(deduction_docs)} docs, ${total_deductions:,.2f}):")
        for d in deduction_docs[:10]:
            summary_lines.append(f"  - [{d.get('doc_type')}] {d.get('vendor','')} ${d.get('amount') or 0:,.2f}")

    if capital_docs:
        summary_lines.append(f"\nCAPITAL IMPROVEMENTS ({len(capital_docs)} items):")
        for d in capital_docs:
            summary_lines.append(f"  - {d.get('vendor','')} ${d.get('amount') or 0:,.2f} ({d.get('date','?')}) — needs depreciation schedule")

    if low_conf_docs:
        summary_lines.append(f"\nLOW CONFIDENCE ITEMS ({len(low_conf_docs)} docs with <60% confidence):")
        for d in low_conf_docs[:10]:
            summary_lines.append(f"  - doc#{d.get('paperless_doc_id')} [{d.get('doc_type')}] {d.get('vendor','')} ${d.get('amount') or 0:,.2f} (conf:{int((d.get('confidence') or 0)*100)}%)")

    if filed_this_year:
        summary_lines.append(f"\nFILED RETURN DATA for {year}:")
        summary_lines.append(f"  AGI: ${filed_this_year.get('agi') or '?':,}")
        summary_lines.append(f"  Total income: ${filed_this_year.get('total_income') or '?':,}")
        summary_lines.append(f"  Total deductions: ${filed_this_year.get('total_deductions') or '?':,}")
        summary_lines.append(f"  Tax owed: ${filed_this_year.get('total_tax') or '?':,}")
        refund = filed_this_year.get("refund_amount")
        owed = filed_this_year.get("amount_owed")
        if refund:
            summary_lines.append(f"  Refund: ${refund:,}")
        if owed:
            summary_lines.append(f"  Amount owed: ${owed:,}")
    else:
        summary_lines.append(f"\nNO FILED RETURN DATA for {year} — not yet entered or not filed.")

    doc_summary = "\n".join(summary_lines)

    prompt = f"""You are an expert US tax accountant reviewing financial documents for a client's tax year {year}.

Here is a summary of all documents analyzed for this year:

{doc_summary}

Please act as the client's tax accountant and:
1. Identify items that need clarification or additional documentation
2. Flag potential issues, missing documents, or inconsistencies
3. Note any capital improvements that need depreciation schedules
4. Ask specific questions about unclear items
5. Compare analyzed amounts to filed return amounts if available and flag discrepancies
6. Note any income sources that might be missing (e.g., if W-2 shows wages but no 1099s found)
7. Flag any deductions that seem high or unusual
8. Note which expenses may be deductible vs. non-deductible

Format your response as a structured report with numbered questions and flagged items.
Use markdown formatting. Be specific — reference vendor names, amounts, and document IDs where relevant.
Focus on what the client needs to gather or clarify before filing (or for amended returns)."""

    from app.llm_client import LLMClient
    from app import config as _cfg

    llm_provider = db.get_setting("llm_provider") or _cfg.LLM_PROVIDER
    llm_api_key = db.get_setting("llm_api_key") or _cfg.LLM_API_KEY
    llm_model = db.get_setting("llm_model") or _cfg.LLM_MODEL

    def generate():
        try:
            client = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)
            # Use streaming if available, otherwise regular
            for chunk in client.stream_text(prompt):
                yield f"data: {_json.dumps({'chunk': chunk})}\n\n"
        except AttributeError:
            # Fallback: non-streaming
            try:
                result = client.chat([{"role": "user", "content": prompt}])
                text = result if isinstance(result, str) else str(result)
                yield f"data: {_json.dumps({'chunk': text})}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps({'error': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route(URL_PREFIX + "/api/tax-review/followup", methods=["POST"])
@login_required
def api_tax_review_followup():
    """Stream a follow-up response in the tax review Q&A thread."""
    import json as _json
    data = request.get_json(silent=True) or {}
    year = data.get("year", "")
    entity_id = data.get("entity_id")
    messages = data.get("messages", [])   # [{role, content}, ...] full history
    if not messages:
        return jsonify({"error": "messages required"}), 400

    from app.llm_client import LLMClient
    from app import config as _cfg

    llm_provider = db.get_setting("llm_provider") or _cfg.LLM_PROVIDER
    llm_api_key = db.get_setting("llm_api_key") or _cfg.LLM_API_KEY
    llm_model = db.get_setting("llm_model") or _cfg.LLM_MODEL

    # System context so the AI stays in tax-advisor mode
    system = (
        f"You are an expert US tax accountant helping a client with their {year} tax return. "
        "You have already reviewed all their financial documents and generated an initial review. "
        "Continue the conversation, answering their questions and clarifying items from your review. "
        "Be specific: reference amounts, document IDs, vendor names, and tax rules where relevant. "
        "Use markdown formatting."
    )

    def generate():
        try:
            client = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)
            if llm_provider == "anthropic":
                import anthropic
                ac = anthropic.Anthropic(api_key=llm_api_key)
                with ac.messages.stream(
                    model=llm_model,
                    max_tokens=4096,
                    system=system,
                    messages=messages,
                ) as stream:
                    for chunk in stream.text_stream:
                        yield f"data: {_json.dumps({'chunk': chunk})}\n\n"
            else:
                # OpenAI
                import openai as _oai
                oai = _oai.OpenAI(api_key=llm_api_key)
                stream = oai.chat.completions.create(
                    model=llm_model,
                    messages=[{"role": "system", "content": system}] + messages,
                    stream=True,
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield f"data: {_json.dumps({'chunk': delta})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route(URL_PREFIX + "/tax-review")
@login_required
def tax_review_page():
    return render_template("dashboard.html", active_tab="tax_review")


@app.route(URL_PREFIX + "/api/stats")
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
        total_docs = 0
        try:
            from app.state import get_stats as ss
            total_docs = ss().get("total", 0)
        except Exception:
            pass
        conn = db.get_connection()
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
        logger.error(f"Stats error: {e}")
        return jsonify({
            "total_docs": 0, "analyzed": 0,
            "total_income": 0, "total_expenses": 0, "net": 0,
            "by_entity": {},
        })


@app.route(URL_PREFIX + "/api/activity")
@login_required
def api_activity():
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify(_row_list(db.get_recent_activity(limit)))


# ---------------------------------------------------------------------------
# API — Entities
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/entities", methods=["GET"])
@login_required
def api_entities_list():
    return jsonify(_row_list(db.list_entities()))


@app.route(URL_PREFIX + "/api/entities", methods=["POST"])
@login_required
@admin_required
def api_entities_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    slug = re.sub(r"[^\w]", "_", name.lower())
    import json as _json
    try:
        eid = db.create_entity(
            name=name, slug=slug,
            entity_type=data.get("type", "personal"),
            description=data.get("description", ""),
            tax_id=data.get("tax_id", ""),
            color=data.get("color", "#1a3c5e"),
            parent_entity_id=data.get("parent_entity_id") or None,
            display_name=data.get("display_name") or name,
            metadata_json=_json.dumps(data.get("metadata", {})),
            sort_order=data.get("sort_order", 0),
        )
        db.log_activity("entity_created", f"Entity: {name}", user_id=current_user.id)
        return jsonify({"id": eid, "name": name, "slug": slug}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route(URL_PREFIX + "/api/entities/<int:entity_id>", methods=["POST"])
@login_required
@admin_required
def api_entities_update(entity_id):
    import json as _json
    data = request.get_json() or {}
    # Convert nested metadata dict → metadata_json string
    if "metadata" in data:
        data["metadata_json"] = _json.dumps(data.pop("metadata"))
    db.update_entity(entity_id, **data)
    row = db.get_entity(entity_id=entity_id)
    db.log_activity("entity_updated", f"ID: {entity_id}", user_id=current_user.id)
    return jsonify(dict(row) if row else {})


@app.route(URL_PREFIX + "/api/entities/<int:entity_id>/archive", methods=["POST"])
@login_required
@admin_required
def api_entities_archive(entity_id):
    db.update_entity(entity_id, archived=1)
    db.log_activity("entity_archived", f"ID: {entity_id}", user_id=current_user.id)
    return jsonify({"status": "archived"})


# ---------------------------------------------------------------------------
# User Profile
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/user/profile", methods=["GET"])
@login_required
def api_user_profile_get():
    """Get current user's profile."""
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (current_user.id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        d = dict(row)
        d.pop("password_hash", None)
        # Parse profile metadata
        try:
            import json as _json
            d["profile"] = _json.loads(d.get("profile_json") or "{}")
        except Exception:
            d["profile"] = {}
        return jsonify(d)
    finally:
        conn.close()


@app.route(URL_PREFIX + "/api/user/profile", methods=["POST"])
@login_required
def api_user_profile_save():
    """Save current user's profile metadata."""
    import json as _json
    data = request.get_json() or {}
    conn = db.get_connection()
    try:
        # Ensure profile_json column exists
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "profile_json" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN profile_json TEXT DEFAULT '{}'")
            conn.commit()
        profile = {
            "full_name": data.get("full_name", ""),
            "email": data.get("email", ""),
            "phone": data.get("phone", ""),
            "address": data.get("address", ""),
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "zip": data.get("zip", ""),
            "notify_email": data.get("notify_email", False),
            "notify_import_complete": data.get("notify_import_complete", False),
        }
        conn.execute("UPDATE users SET profile_json=? WHERE id=?",
                     (_json.dumps(profile), current_user.id))
        conn.commit()
        return jsonify({"status": "saved"})
    finally:
        conn.close()


@app.route(URL_PREFIX + "/api/entities/tree")
@login_required
def api_entities_tree():
    """Return entities as a hierarchy tree."""
    tree = db.get_entity_tree()
    return jsonify(tree)


@app.route(URL_PREFIX + "/api/entities/<int:entity_id>/merge", methods=["POST"])
@login_required
@admin_required
def api_entity_merge(entity_id):
    """Merge entity into another — moves all docs/txns then archives source."""
    data = request.get_json() or {}
    target_id = data.get("target_entity_id")
    if not target_id:
        return jsonify({"error": "target_entity_id required"}), 400
    if int(target_id) == entity_id:
        return jsonify({"error": "source and target must differ"}), 400
    source = db.get_entity(entity_id=entity_id)
    target = db.get_entity(entity_id=int(target_id))
    if not source or not target:
        return jsonify({"error": "entity not found"}), 404
    counts = db.merge_entities(entity_id, int(target_id))
    db.log_activity(
        "entity_merged",
        f"Merged '{source['name']}' → '{target['name']}': {counts}",
        user_id=current_user.id,
    )
    return jsonify({"status": "merged", "moved": counts})


@app.route(URL_PREFIX + "/api/entities/<int:entity_id>/transfer-docs", methods=["POST"])
@login_required
@admin_required
def api_entity_transfer_docs(entity_id):
    """Transfer documents/transactions from one entity to another (non-destructive — keeps source active)."""
    import json as _json
    data = request.get_json() or {}
    target_id = data.get("target_entity_id")
    doc_ids = data.get("doc_ids") or []
    txn_ids = data.get("txn_ids") or []
    if not target_id:
        return jsonify({"error": "target_entity_id required"}), 400
    conn = db.get_connection()
    moved = {"documents": 0, "transactions": 0}
    try:
        for did in doc_ids:
            conn.execute("UPDATE analyzed_documents SET entity_id=? WHERE id=? AND entity_id=?",
                         (target_id, did, entity_id))
            moved["documents"] += conn.execute("SELECT changes()").fetchone()[0]
        for tid in txn_ids:
            conn.execute("UPDATE transactions SET entity_id=? WHERE id=? AND entity_id=?",
                         (target_id, tid, entity_id))
            moved["transactions"] += conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    db.log_activity("entity_transfer", f"Transferred {moved} from {entity_id} → {target_id}",
                    user_id=current_user.id)
    return jsonify({"status": "ok", "moved": moved})


@app.route(URL_PREFIX + "/api/entities/<int:entity_id>/stats")
@login_required
def api_entity_stats(entity_id):
    row = db.get_entity(entity_id=entity_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    summary = db.get_financial_summary(entity_id=entity_id)
    txn_summary = db.get_transaction_summary(entity_id=entity_id)
    years = _row_list(db.list_tax_years(entity_id=entity_id))
    return jsonify({
        "entity": dict(row),
        "income": round(summary["income"], 2),
        "expenses": round(summary["expense"] + summary["deduction"], 2),
        "net": round(summary["net"], 2),
        "doc_count": sum(summary["counts"].values()),
        "txn_count": sum(v["count"] for v in txn_summary.values()),
        "years": years,
    })


@app.route(URL_PREFIX + "/api/years")
@login_required
def api_years():
    from app.config import TAX_YEARS
    return jsonify(TAX_YEARS)


@app.route(URL_PREFIX + "/api/entities/<int:entity_id>/years", methods=["POST"])
@login_required
@admin_required
def api_entity_add_year(entity_id):
    data = request.get_json() or {}
    year = data.get("year", "").strip()
    if not year or not re.match(r"^\d{4}$", year):
        return jsonify({"error": "valid 4-digit year required"}), 400
    if not db.get_entity(entity_id=entity_id):
        return jsonify({"error": "entity not found"}), 404
    ty_id = db.ensure_tax_year(entity_id, year)
    return jsonify({"status": "ok", "tax_year_id": ty_id, "year": year})


# ---------------------------------------------------------------------------
# API — Documents
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/documents")
@login_required
def api_documents_list():
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    category = request.args.get("category")
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = db.get_analyzed_documents(entity_id=entity_id, tax_year=year,
                                     category=category, limit=limit)
    docs = _row_list(rows)
    # Fill missing titles: DB title → state file → "Document {id}"
    try:
        from app.state import get_result
        for d in docs:
            if not d.get("title"):
                sr = get_result(d.get("paperless_doc_id") or 0)
                paperless_title = sr.get("title", "")
                if paperless_title:
                    d["title"] = paperless_title
                else:
                    # Construct from analyzed fields
                    parts = [d.get("doc_type", "")]
                    if d.get("vendor"):
                        parts.append(f"— {d['vendor']}")
                    if d.get("tax_year"):
                        parts.append(f"({d['tax_year']})")
                    d["title"] = " ".join(p for p in parts if p) or f"Document {d.get('paperless_doc_id','?')}"
    except Exception:
        for d in docs:
            if not d.get("title"):
                d["title"] = f"Document {d.get('paperless_doc_id','?')}"
    return jsonify({"total": len(docs), "documents": docs})


@app.route(URL_PREFIX + "/api/documents/dedup", methods=["POST"])
@login_required
@admin_required
def api_documents_dedup():
    """Scan analyzed_documents for duplicates and flag them with is_duplicate=1."""
    result = db.flag_duplicate_analyzed_docs()
    db.log_activity("dedup_scan",
        f"Flagged {result['flagged']} duplicates in {result['groups']} groups "
        f"({result['already_flagged']} already flagged)",
        user_id=current_user.id)
    return jsonify({"status": "ok", **result})


@app.route(URL_PREFIX + "/api/documents/backfill-titles", methods=["POST"])
@login_required
def api_backfill_titles():
    """Populate titles for analyzed docs that have none.

    For each untitled doc:
    1. Try the Paperless API title (already human-readable from Paperless OCR/filename)
    2. Fall back to constructing from doc_type + vendor + tax_year
    Re-running full AI analysis is NOT needed — this just uses existing data.
    """
    from app.paperless_client import get_document
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT paperless_doc_id, doc_type, vendor, tax_year "
            "FROM analyzed_documents WHERE title IS NULL OR title = ''"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return jsonify({"status": "ok", "updated": 0, "message": "All titles already populated"})

    updated = 0
    errors = []
    for row in rows:
        doc_id = row["paperless_doc_id"]
        try:
            # Try Paperless for a real title first
            paperless_doc = get_document(doc_id)
            pl_title = (paperless_doc.get("title") or "").strip()
            if pl_title and pl_title != str(doc_id):
                title = pl_title
            else:
                # Build from analyzed fields
                parts = [row["doc_type"] or ""]
                if row["vendor"]:
                    parts.append(f"— {row['vendor']}")
                if row["tax_year"]:
                    parts.append(f"({row['tax_year']})")
                title = " ".join(p for p in parts if p) or f"Document {doc_id}"

            conn2 = db.get_connection()
            try:
                conn2.execute(
                    "UPDATE analyzed_documents SET title=? WHERE paperless_doc_id=?",
                    (title, doc_id)
                )
                conn2.commit()
            finally:
                conn2.close()
            updated += 1
        except Exception as e:
            errors.append(f"doc {doc_id}: {e}")

    return jsonify({"status": "ok", "updated": updated, "errors": errors[:10]})


@app.route(URL_PREFIX + "/api/documents/<int:doc_id>")
@login_required
def api_document_detail(doc_id):
    try:
        from app.state import get_result
        state_doc = get_result(doc_id)
        paperless_doc = {}
        try:
            from app.paperless_client import get_document
            paperless_doc = get_document(doc_id)
        except Exception:
            pass
        conn = db.get_connection()
        row = conn.execute(
            "SELECT d.*, e.name as entity_name FROM analyzed_documents d "
            "LEFT JOIN entities e ON e.id=d.entity_id WHERE d.paperless_doc_id=?",
            (doc_id,)).fetchone()
        conn.close()
        db_rec = dict(row) if row else {}
        return jsonify({**paperless_doc, **state_doc, **db_rec, "doc_id": doc_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/documents/<int:doc_id>/recategorize", methods=["POST"])
@login_required
def api_document_recategorize(doc_id):
    def _run():
        try:
            from app.paperless_client import get_document, apply_tags
            from app.categorizer import categorize
            from app.extractor import extract
            from app.state import mark_analyzed
            doc = get_document(doc_id)
            content = doc.get("content", "")
            title = doc.get("title", f"Document {doc_id}")
            cat = categorize(content, title)
            ext = extract(content)
            result = {"doc_id": doc_id, "title": title,
                      "analyzed_at": datetime.utcnow().isoformat(), "recategorized": True,
                      **cat,
                      **{k: v for k, v in ext.items() if v is not None and k not in cat}}
            mark_analyzed(doc_id, result)
            entity_row = db.get_entity(slug=cat.get("entity", "personal"))
            db.mark_document_analyzed(
                paperless_doc_id=doc_id,
                entity_id=entity_row["id"] if entity_row else None,
                tax_year=str(cat.get("tax_year") or ""),
                doc_type=cat.get("doc_type", "other"),
                category=cat.get("category", "other"),
                vendor=cat.get("vendor") or "",
                amount=float(cat.get("amount") or 0),
                date=ext.get("date") or "",
                confidence=float(cat.get("confidence") or 0),
                extracted_json=json.dumps(ext),
            )
            try:
                tags = [t for t in cat.get("tags", []) if t] + [
                    f"tax-{cat.get('entity','personal')}", f"year-{cat.get('tax_year','unknown')}"]
                apply_tags(doc_id, tags)
            except Exception:
                pass
            db.log_activity("doc_recategorized", f"Doc {doc_id}: {cat.get('doc_type')}")
        except Exception as e:
            logger.error(f"Recategorize doc {doc_id}: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "recategorizing", "doc_id": doc_id})


@app.route(URL_PREFIX + "/api/documents/<int:doc_id>/override", methods=["POST"])
@login_required
def api_document_override(doc_id):
    """Manual classification override — directly corrects stored fields without re-running AI.
    Accepts: doc_type, category, vendor, amount, date, tax_year, title.
    Sets confidence=1.0 to mark as human-verified so the AI daemon won't overwrite it.
    """
    from app.llm_client import VALID_DOC_TYPES, VALID_CATEGORIES
    data = request.get_json() or {}
    allowed = {"doc_type", "category", "vendor", "amount", "date", "tax_year", "title"}
    if "doc_type" in data and data["doc_type"] not in VALID_DOC_TYPES:
        return jsonify({"error": f"Invalid doc_type: {data['doc_type']}"}), 400
    if "category" in data and data["category"] not in VALID_CATEGORIES:
        return jsonify({"error": f"Invalid category: {data['category']}"}), 400
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "No valid fields provided"}), 400
    conn = db.get_connection()
    try:
        sets = [f"{k}=?" for k in fields] + ["confidence=1.0"]
        params = list(fields.values()) + [doc_id]
        conn.execute(
            f"UPDATE analyzed_documents SET {', '.join(sets)} WHERE paperless_doc_id=?",
            params,
        )
        conn.commit()
        db.log_activity("doc_override",
                        f"Doc {doc_id} manually overridden: {fields}",
                        user_id=current_user.id)
        return jsonify({"status": "ok", "doc_id": doc_id, "updated": fields})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API — Transactions
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/transactions", methods=["GET"])
@login_required
def api_transactions_list():
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    source = request.args.get("source")
    limit = min(int(request.args.get("limit", 100)), 1000)
    rows = db.list_transactions(entity_id=entity_id, tax_year=year,
                                source=source, limit=limit)
    txns = _row_list(rows)
    return jsonify({"total": len(txns), "transactions": txns})


@app.route(URL_PREFIX + "/api/transactions", methods=["POST"])
@login_required
def api_transactions_create():
    data = request.get_json() or {}
    for field in ("date", "amount", "description"):
        if not data.get(field):
            return jsonify({"error": f"{field} required"}), 400
    try:
        tid = db.upsert_transaction(
            source="manual",
            source_id=f"manual_{datetime.utcnow().timestamp()}",
            entity_id=data.get("entity_id"),
            tax_year=data.get("year") or data.get("tax_year", ""),
            date=data["date"],
            amount=float(data["amount"]),
            vendor=data.get("vendor", ""),
            description=data["description"],
            category=data.get("category", ""),
            doc_type=data.get("doc_type", ""),
        )
        db.log_activity("txn_created", data["description"][:80], user_id=current_user.id)
        return jsonify({"id": tid, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route(URL_PREFIX + "/api/transactions/<int:txn_id>/edit", methods=["POST"])
@login_required
def api_transactions_edit(txn_id):
    data = request.get_json() or {}
    if not db.get_transaction(txn_id):
        return jsonify({"error": "not found"}), 404
    db.update_transaction(txn_id, **data)
    db.log_activity("txn_updated", f"ID: {txn_id}", user_id=current_user.id)
    return jsonify({"status": "updated", "id": txn_id})


# ---------------------------------------------------------------------------
# CSV import helper
# ---------------------------------------------------------------------------

def _parse_csv(csv_bytes: bytes, source: str, entity_id, year: str, col_map: dict):
    txns, errors = [], []
    try:
        text = csv_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for i, row in enumerate(reader):
            try:
                date_val = row.get(col_map.get("date", "Date"), "").strip()
                desc_val = row.get(col_map.get("description", "Description"), "").strip()
                raw_amt = row.get(col_map.get("amount", "Amount"), "0").strip()
                amount_val = float(re.sub(r"[,$\s]", "", raw_amt or "0") or "0")
                if not date_val and not desc_val:
                    continue
                row_year = year
                if not row_year:
                    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y"):
                        try:
                            row_year = str(datetime.strptime(date_val, fmt).year)
                            break
                        except ValueError:
                            pass
                txns.append({
                    "source": source,
                    "source_id": f"{source}_{i}_{date_val}_{amount_val}",
                    "entity_id": entity_id,
                    "tax_year": row_year or "",
                    "date": date_val,
                    "amount": abs(amount_val),
                    "vendor": "",
                    "description": desc_val,
                    "category": "expense" if amount_val < 0 else "income",
                })
            except Exception as e:
                errors.append(f"Row {i+2}: {e}")
    except Exception as e:
        return [], str(e)
    return txns, ("; ".join(errors[:5]) if errors else None)


def _run_csv_job(job_id, csv_bytes, source, entity_id, year, col_map):
    db.update_import_job(job_id, status="running",
                         started_at=datetime.utcnow().isoformat())
    txns, err = _parse_csv(csv_bytes, source, entity_id, year, col_map)
    if err and not txns:
        db.update_import_job(job_id, status="error", error_msg=err,
                             completed_at=datetime.utcnow().isoformat())
        return
    saved = 0
    for t in txns:
        try:
            db.upsert_transaction(**t)
            saved += 1
        except Exception:
            pass
    db.update_import_job(job_id, status="completed",
                         count_imported=saved,
                         completed_at=datetime.utcnow().isoformat())
    db.log_activity("import_complete", f"{source}: {saved} transactions")


# ---------------------------------------------------------------------------
# API — Import
# ---------------------------------------------------------------------------

# In-memory log store per job_id — capped at 2000 lines
_job_logs: dict = {}
_job_logs_lock = threading.Lock()

# In-memory stop signals for active chat streams: session_id → threading.Event
_chat_stop_events: dict[int, threading.Event] = {}
_chat_stop_lock = threading.Lock()

# In-memory stop signals for import jobs: job_id → threading.Event
_job_stop_events: dict[int, threading.Event] = {}
_job_stop_lock = threading.Lock()

def _append_job_log(job_id: int, msg: str):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    logger.info(f"job#{job_id}: {msg}")
    with _job_logs_lock:
        if job_id not in _job_logs:
            _job_logs[job_id] = []
        _job_logs[job_id].append(entry)
        if len(_job_logs[job_id]) > 2000:
            _job_logs[job_id] = _job_logs[job_id][-2000:]
    # Persist to DB so logs survive container restarts
    try:
        db.append_import_job_log(job_id, entry)
    except Exception:
        pass


@app.route(URL_PREFIX + "/api/import/jobs/<int:job_id>/logs")
@login_required
def api_job_logs(job_id: int):
    offset = int(request.args.get("offset", 0))
    # Try in-memory first (current run), fall back to DB (previous runs / after restart)
    with _job_logs_lock:
        mem_lines = list((_job_logs.get(job_id) or []))
    if mem_lines:
        return jsonify({"lines": mem_lines[offset:], "total": len(mem_lines), "source": "memory"})
    # Fall back to DB
    db_lines, total = db.get_import_job_logs(job_id, offset=offset)
    return jsonify({"lines": db_lines, "total": total, "source": "db"})


@app.route(URL_PREFIX + "/api/import/jobs/<int:job_id>/cancel", methods=["POST"])
@login_required
def api_import_job_cancel(job_id: int):
    """Signal a running import job to stop, or mark orphaned jobs as cancelled."""
    with _job_stop_lock:
        ev = _job_stop_events.get(job_id)
        if ev:
            ev.set()
            db.update_import_job(job_id, status="cancelling")
            return jsonify({"status": "cancelling"})
    # No live stop event — job may be orphaned from a previous container.
    # Check DB and mark cancelled if it's stuck in running/pending state.
    job = db.get_import_job(job_id)
    if job and job.get("status") in ("running", "pending", "cancelling"):
        db.update_import_job(job_id, status="cancelled",
                             completed_at=datetime.utcnow().isoformat())
        return jsonify({"status": "cancelled", "note": "Orphaned job marked cancelled"})
    return jsonify({"status": "not_running"})


@app.route(URL_PREFIX + "/api/import/gmail/start", methods=["POST"])
@login_required
def api_import_gmail_start():
    data = request.get_json() or {}
    entity_id = data.get("entity_id")
    years = data.get("years", GMAIL_YEARS)
    if not os.path.exists(GMAIL_CREDENTIALS_FILE) and not db.get_setting("gmail_oauth_token"):
        return jsonify({"error": "Gmail not configured. Use Setup / Credentials first."}), 400
    job_id = db.create_import_job("gmail", entity_id=entity_id,
                                  config_json=json.dumps({"years": years}))
    _job_logs[job_id] = []
    stop_ev = threading.Event()
    with _job_stop_lock:
        _job_stop_events[job_id] = stop_ev

    def _run(jid, eid, yrs, stop):
        log = lambda msg: _append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.gmail_importer import run_import
            entity_slug = "personal"
            if eid:
                e = db.get_entity(entity_id=eid)
                if e:
                    entity_slug = e.get("slug", "personal")
            def _flush(imported, skipped):
                db.update_import_job(jid, count_imported=imported,
                                     count_skipped=skipped)
            result = run_import(entity_id=eid, years=yrs,
                                consume_path=CONSUME_PATH, entity_slug=entity_slug,
                                log_fn=log, stop_event=stop, progress_fn=_flush)
            count = result.get("imported", 0) if isinstance(result, dict) else result
            skipped = result.get("skipped", 0) if isinstance(result, dict) else 0
            filtered = result.get("ai_filtered", 0) if isinstance(result, dict) else 0
            final_status = "cancelled" if stop.is_set() else "completed"
            db.update_import_job(jid, status=final_status, count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"Gmail: {count} imported, {filtered} AI-filtered, {skipped} skipped")
        except Exception as e:
            import traceback
            _append_job_log(jid, f"FATAL ERROR: {e}")
            _append_job_log(jid, traceback.format_exc()[:500])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())
        finally:
            with _job_stop_lock:
                _job_stop_events.pop(jid, None)

    threading.Thread(target=_run, args=(job_id, entity_id, years, stop_ev),
                     daemon=True, name=f"gmail-{job_id}").start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route(URL_PREFIX + "/api/import/gmail/credentials", methods=["POST"])
@login_required
@admin_required
def api_import_gmail_credentials():
    logger.info(f"gmail credentials upload: files={list(request.files.keys())} form={list(request.form.keys())} json={request.is_json}")
    if "credentials" in request.files:
        f = request.files["credentials"]
        try:
            content = f.read()
            logger.info(f"gmail credentials file size={len(content)}")
            json.loads(content)
            os.makedirs(os.path.dirname(GMAIL_CREDENTIALS_FILE), exist_ok=True)
            with open(GMAIL_CREDENTIALS_FILE, "wb") as out:
                out.write(content)
            db.log_activity("gmail_creds_saved", "credentials.json uploaded",
                            user_id=current_user.id)
            return jsonify({"status": "saved"})
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON file"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    data = request.get_json() or {}
    try:
        parsed = json.loads(data.get("credentials_json", "{}"))
        os.makedirs(os.path.dirname(GMAIL_CREDENTIALS_FILE), exist_ok=True)
        with open(GMAIL_CREDENTIALS_FILE, "w") as out:
            json.dump(parsed, out, indent=2)
        db.log_activity("gmail_creds_saved", "pasted", user_id=current_user.id)
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route(URL_PREFIX + "/api/import/paypal/csv", methods=["POST"])
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
    threading.Thread(target=_run_csv_job,
                     args=(job_id, csv_bytes, "paypal", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route(URL_PREFIX + "/api/import/venmo/csv", methods=["POST"])
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
    threading.Thread(target=_run_csv_job,
                     args=(job_id, csv_bytes, "venmo", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route(URL_PREFIX + "/api/import/bank-csv", methods=["POST"])
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
    threading.Thread(target=_run_csv_job,
                     args=(job_id, csv_bytes, "bank_csv", entity_id, year, col_map),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route(URL_PREFIX + "/api/import/url", methods=["POST"])
@login_required
def api_import_url():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    entity_id = data.get("entity_id")
    if not url:
        return jsonify({"error": "url required"}), 400
    job_id = db.create_import_job("url", entity_id=entity_id,
                                  config_json=json.dumps({"url": url}))

    def _run(jid, import_url):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            import httpx
            r = httpx.get(import_url, follow_redirects=True, timeout=30)
            r.raise_for_status()
            db.update_import_job(jid, status="completed", count_imported=1,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"URL: {import_url}")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, url), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route(URL_PREFIX + "/api/import/jobs")
@login_required
def api_import_jobs():
    return jsonify(_row_list(db.list_import_jobs(limit=50)))


@app.route(URL_PREFIX + "/api/import/jobs/<int:job_id>")
@login_required
def api_import_job_status(job_id):
    row = db.get_import_job(job_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route(URL_PREFIX + "/api/import/jobs/<int:job_id>", methods=["DELETE"])
@login_required
@admin_required
def api_import_job_delete(job_id):
    """Delete an import job (admin only). Running jobs cannot be deleted."""
    job = db.get_import_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job.get("status") in ("running", "pending", "cancelling"):
        return jsonify({"error": "Cannot delete a running job. Cancel it first."}), 400
    db.delete_import_job(job_id)
    # Clear in-memory logs if present
    with _job_logs_lock:
        _job_logs.pop(job_id, None)
    return jsonify({"status": "deleted"})


@app.route(URL_PREFIX + "/api/health")
@login_required
def api_health():
    """Check status of all backend components."""
    import httpx
    results = {}

    def _check(name, fn):
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"status": "error", "message": str(e)[:120]}

    # Self (always up if we're responding)
    results["tax-ai-analyzer"] = {"status": "ok", "message": "Running"}

    # Paperless-ngx
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

    # Elasticsearch
    def _elastic():
        es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
        es_pass = os.environ.get("ELASTICSEARCH_PASSWORD", "")
        auth = ("elastic", es_pass) if es_pass else None
        r = httpx.get(f"{es_url}/_cluster/health", auth=auth, timeout=5)
        data = r.json()
        status = "ok" if data.get("status") in ("green", "yellow") else "warn"
        return {"status": status, "message": f"cluster: {data.get('status','?')}"}
    _check("elasticsearch", _elastic)

    # Redis (check via paperless API since we can't reach it directly)
    # We'll just try a tcp connect via socket
    def _redis():
        import socket
        s = socket.create_connection(("tax-paperless-redis", 6379), timeout=3)
        s.close()
        return {"status": "ok", "message": "TCP reachable"}
    _check("tax-paperless-redis", _redis)

    # Postgres
    def _postgres():
        import socket
        s = socket.create_connection(("tax-paperless-postgres", 5432), timeout=3)
        s.close()
        return {"status": "ok", "message": "TCP reachable"}
    _check("tax-paperless-postgres", _postgres)

    return jsonify(results)


@app.route(URL_PREFIX + "/api/settings/llm-models")
@login_required
def api_llm_models():
    """Return available model lists by provider."""
    models = {
        "anthropic": [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-haiku-20240307",
        ],
        "openai": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-4",
            "gpt-3.5-turbo",
        ],
    }
    return jsonify(models)


# ---------------------------------------------------------------------------
# API — PayPal OAuth API
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/import/paypal/credentials", methods=["POST"])
@login_required
def api_paypal_save_credentials():
    """Save PayPal client_id + client_secret and optionally test them."""
    data = request.get_json() or {}
    client_id = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "").strip()
    sandbox = bool(data.get("sandbox", False))
    if not client_id or not client_secret:
        return jsonify({"error": "client_id and client_secret required"}), 400
    db.set_setting("paypal_client_id", client_id)
    db.set_setting("paypal_client_secret", client_secret)
    db.set_setting("paypal_sandbox", "1" if sandbox else "0")
    # Test credentials
    try:
        from app.importers.paypal_api import get_access_token
        get_access_token(client_id, client_secret, sandbox=sandbox)
        return jsonify({"status": "ok", "message": "Credentials saved and verified."})
    except Exception as e:
        return jsonify({"status": "saved", "message": f"Saved but test failed: {e}"})


@app.route(URL_PREFIX + "/api/import/paypal/pull", methods=["POST"])
@login_required
def api_import_paypal_pull():
    """Pull transactions from PayPal API for one or more years."""
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or [str(datetime.utcnow().year)]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]

    client_id = data.get("client_id") or db.get_setting("paypal_client_id")
    client_secret = data.get("client_secret") or db.get_setting("paypal_client_secret")
    sandbox = (db.get_setting("paypal_sandbox") or "0") == "1"

    if not client_id or not client_secret:
        return jsonify({"error": "PayPal credentials not configured. Save them first."}), 400

    job_id = db.create_import_job("paypal_api", entity_id=entity_id,
                                  config_json=json.dumps({"years": years, "sandbox": sandbox}))

    def _run(jid, cid, csec, ys, eid, sbx):
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.paypal_api import pull_transactions_for_year
            total = 0
            for yr in ys:
                txns = pull_transactions_for_year(cid, csec, yr,
                                                   entity_id=eid, sandbox=sbx)
                for t in txns:
                    try:
                        db.add_transaction(t)
                        total += 1
                    except Exception:
                        pass
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                            f"PayPal API: {total} transactions imported for years {ys}")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())
            logger.error(f"PayPal API import error: {e}")

    threading.Thread(target=_run, args=(job_id, client_id, client_secret,
                                        years, entity_id, sandbox),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route(URL_PREFIX + "/api/import/paypal/status", methods=["GET"])
@login_required
def api_paypal_status():
    """Return whether PayPal credentials are configured."""
    cid = db.get_setting("paypal_client_id") or ""
    return jsonify({
        "configured": bool(cid),
        "sandbox": (db.get_setting("paypal_sandbox") or "0") == "1",
        "client_id_preview": (cid[:8] + "…") if len(cid) > 8 else cid,
    })


# ---------------------------------------------------------------------------
# API — US Alliance FCU Playwright importer
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/import/usalliance/credentials", methods=["POST"])
@login_required
def api_usalliance_save_credentials():
    """Save US Alliance online banking username and password."""
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    db.set_setting("usalliance_username", username)
    db.set_setting("usalliance_password", password)
    return jsonify({"status": "saved", "message": "Credentials saved."})


@app.route(URL_PREFIX + "/api/import/usalliance/cookies", methods=["POST"])
@login_required
def api_usalliance_save_cookies():
    """Save browser session cookies for US Alliance (bypasses bot detection).

    Accepts either a JSON array of cookie objects (exported from browser DevTools)
    or a raw Netscape/curl cookie string.  The cookies are stored as a JSON string
    and injected into Playwright before navigation — the login form is skipped.
    """
    data = request.get_json() or {}
    cookies_raw = data.get("cookies")
    if not cookies_raw:
        return jsonify({"error": "cookies field is required"}), 400

    # Accept raw JSON array or string
    if isinstance(cookies_raw, str):
        try:
            cookies_list = json.loads(cookies_raw)
        except Exception:
            return jsonify({"error": "cookies must be a valid JSON array"}), 400
    elif isinstance(cookies_raw, list):
        cookies_list = cookies_raw
    else:
        return jsonify({"error": "cookies must be a JSON array"}), 400

    if not isinstance(cookies_list, list) or len(cookies_list) == 0:
        return jsonify({"error": "cookies must be a non-empty JSON array"}), 400

    db.set_setting("usalliance_cookies", json.dumps(cookies_list))
    return jsonify({"status": "saved", "message": f"{len(cookies_list)} cookies saved.",
                    "count": len(cookies_list)})


@app.route(URL_PREFIX + "/api/import/usalliance/cookies", methods=["DELETE"])
@login_required
def api_usalliance_clear_cookies():
    """Clear stored browser cookies."""
    db.set_setting("usalliance_cookies", "")
    return jsonify({"status": "cleared"})


@app.route(URL_PREFIX + "/api/import/usalliance/status", methods=["GET"])
@login_required
def api_usalliance_status():
    """Return whether US Alliance credentials are configured."""
    user = db.get_setting("usalliance_username") or ""
    cookies_raw = db.get_setting("usalliance_cookies") or ""
    cookies_count = 0
    if cookies_raw:
        try:
            cookies_count = len(json.loads(cookies_raw))
        except Exception:
            pass
    return jsonify({
        "configured": bool(user),
        "username_preview": (user[:3] + "…") if len(user) > 3 else user,
        "cookies_saved": cookies_count > 0,
        "cookies_count": cookies_count,
    })


@app.route(URL_PREFIX + "/api/import/usalliance/test", methods=["POST"])
@login_required
def api_usalliance_test():
    """Test US Alliance credentials by attempting a login (no download)."""
    username = db.get_setting("usalliance_username")
    password = db.get_setting("usalliance_password")
    if not username or not password:
        return jsonify({"error": "Credentials not saved — enter username and password first"}), 400
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return jsonify({"error": "Playwright not installed in this container"}), 500
    try:
        from playwright_stealth import Stealth
        _stealth = Stealth(navigator_webdriver=True, navigator_plugins=True,
                           chrome_app=True, chrome_csi=True, webgl_vendor=True,
                           navigator_platform_override="Win32")
    except ImportError:
        _stealth = None
    try:
        with sync_playwright() as p:
            if _stealth:
                _stealth.hook_playwright_context(p)
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--headless=new", "--disable-blink-features=AutomationControlled",
                      "--window-size=1280,900"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = context.new_page()
            if _stealth:
                _stealth.apply_stealth_sync(page)
            login_url = "https://account.usalliance.org/login"
            page.goto(login_url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            content = page.content().lower()
            current_url = page.url
            browser.close()
            # If we were blocked/redirected to a 404, bot detection fired
            if "404 page not found" in content or "404 - page not found" in content:
                return jsonify({"error": "Portal is blocking this server's browser (bot detection). Stealth is active — try running a full import which may succeed."})
            # Check if login page loaded
            if "login" in current_url or "username" in content or "password" in content or "sign in" in content:
                return jsonify({"status": "ok", "message": "Login page reached successfully. Run a full import to authenticate."})
            elif "dashboard" in current_url or "account" in current_url:
                return jsonify({"status": "ok", "message": "Already authenticated (session active)."})
            else:
                return jsonify({"status": "ok", "message": f"Portal reached at {current_url}. Run a full import to authenticate."})
    except Exception as e:
        return jsonify({"error": f"Test failed: {str(e)[:200]}"})


@app.route(URL_PREFIX + "/api/import/usalliance/mfa", methods=["POST"])
@login_required
def api_usalliance_mfa():
    """Submit a MFA/OTP code for a running import job."""
    data = request.get_json() or {}
    job_id = data.get("job_id")
    code = data.get("code", "").strip()
    if not job_id or not code:
        return jsonify({"error": "job_id and code required"}), 400
    from app.importers.usalliance_importer import set_mfa_code
    set_mfa_code(int(job_id), code)
    return jsonify({"status": "ok"})


@app.route(URL_PREFIX + "/api/import/usalliance/start", methods=["POST"])
@login_required
def api_import_usalliance_start():
    """Start a US Alliance statement download job."""
    data = request.get_json() or {}
    entity_id = data.get("entity_id") or None
    years = data.get("years") or ["2022", "2023", "2024", "2025"]
    if isinstance(years, str):
        years = [y.strip() for y in years.split(",") if y.strip()]

    username = db.get_setting("usalliance_username")
    password = db.get_setting("usalliance_password")
    if not username or not password:
        return jsonify({"error": "US Alliance credentials not configured. Save them first."}), 400

    # Load browser cookies if saved (bypass bot detection)
    cookies = None
    cookies_raw = db.get_setting("usalliance_cookies") or ""
    if cookies_raw:
        try:
            cookies = json.loads(cookies_raw)
        except Exception:
            cookies = None

    # Resolve entity slug
    entity_slug = "personal"
    if entity_id:
        ent = db.get_entity(entity_id=entity_id)
        if ent:
            entity_slug = ent.get("slug", "personal")

    job_id = db.create_import_job("usalliance", entity_id=entity_id,
                                  config_json=json.dumps({"years": years,
                                                          "cookie_auth": cookies is not None}))
    _job_logs[job_id] = []

    def _run(jid, uname, pw, yrs, eid, eslug, ckies):
        log = lambda msg: _append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        try:
            from app.importers.usalliance_importer import run_import
            result = run_import(
                username=uname,
                password=pw,
                years=yrs,
                consume_path=CONSUME_PATH,
                entity_slug=eslug,
                job_id=jid,
                log=log,
                cookies=ckies,
            )
            total = result.get("imported", 0)
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete",
                f"US Alliance: {total} statements imported for years {yrs}")
        except Exception as e:
            import traceback
            log(f"Fatal error: {e}")
            log(traceback.format_exc()[:600])
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run,
                     args=(job_id, username, password, years, entity_id, entity_slug, cookies),
                     daemon=True, name=f"usalliance-{job_id}").start()
    return jsonify({"status": "started", "job_id": job_id})


# ---------------------------------------------------------------------------
# API — OFX/QFX bank import
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/import/bank-ofx", methods=["POST"])
@login_required
def api_import_bank_ofx():
    """Import transactions from an OFX/QFX file (US Alliance FCU, etc.)."""
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
            total = 0
            for t in txns:
                try:
                    db.add_transaction(t)
                    total += 1
                except Exception:
                    pass
            db.update_import_job(jid, status="completed", count_imported=total,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"OFX: {total} transactions imported")
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e)[:500],
                                 completed_at=datetime.utcnow().isoformat())
            logger.error(f"OFX import error: {e}")

    threading.Thread(target=_run, args=(job_id, content, entity_id, year),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ---------------------------------------------------------------------------
# API — Local filesystem import
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/import/local/scan", methods=["POST"])
@login_required
def api_import_local_scan():
    """Scan a local directory and return file count preview."""
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
        # Auto-detect entity from path
        entities = db.get_entities()
        suggested = detect_entity_from_path(path, entities)
        return jsonify({
            "path": path,
            "total": len(files),
            "counts": counts,
            "suggested_entity": {"id": suggested["id"], "name": suggested["name"]}
                                 if suggested else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/import/local/run", methods=["POST"])
@login_required
def api_import_local_run():
    """Import all financial files from a local directory."""
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    entity_id = data.get("entity_id") or None
    year = data.get("year", "") or None
    if not path:
        return jsonify({"error": "path required"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Directory not found: {path}"}), 400

    consume_path = CONSUME_PATH
    job_id = db.create_import_job("local_fs", entity_id=entity_id,
                                  config_json=json.dumps({"path": path, "year": year}))

    entities_list = [dict(e) for e in db.list_entities()]

    def _run(jid, fpath, eid, yr, cpath, ents):
        def log(msg):
            _append_job_log(jid, msg)
        db.update_import_job(jid, status="running",
                             started_at=datetime.utcnow().isoformat())
        log(f"Scanning: {fpath}")
        log(f"Consume path: {cpath}")
        try:
            from app.importers.local_fs import import_directory, scan_directory
            all_files = scan_directory(fpath, recursive=True)
            log(f"Found {len(all_files)} files ({sum(1 for f in all_files if f['ext']=='.pdf')} PDFs, "
                f"{sum(1 for f in all_files if f['ext']=='.csv')} CSVs, "
                f"{sum(1 for f in all_files if f['ext'] in {'.ofx','.qfx','.qbo'})} OFX)")
            log(f"Per-file entity detection enabled ({len(ents)} entities)")
            import os
            if not cpath or not os.path.isdir(cpath):
                log(f"ERROR: consume path not accessible: {cpath}")
            result = import_directory(fpath, entity_id=eid, default_year=yr,
                                       consume_path=cpath, recursive=True,
                                       entities=ents)
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
                log(f"Entity breakdown: " + ", ".join(
                    f"{slug}: {cnt}" for slug, cnt in sorted(entity_counts.items())))
            for err in errors[:20]:  # log up to 20 errors
                log(f"  ERROR: {err}")
            if len(errors) > 20:
                log(f"  ... and {len(errors)-20} more errors")
            log(f"Done: {pdfs} PDFs queued to Paperless, {total_txns} transactions imported"
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
            logger.error(f"Local FS import error: {e}")

    threading.Thread(target=_run, args=(job_id, path, entity_id, year, consume_path, entities_list),
                     daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ---------------------------------------------------------------------------
# API — Cloud adapters
# ---------------------------------------------------------------------------

def _cloud_unavail(service: str):
    return jsonify({"error": f"{service} adapter not configured", "configured": False}), 503


@app.route(URL_PREFIX + "/api/cloud/google-drive/auth")
@login_required
def api_gdrive_auth():
    try:
        from app.cloud_adapters.google_drive import get_auth_url
        return redirect(get_auth_url(url_for("api_gdrive_callback", _external=True)))
    except ImportError:
        return _cloud_unavail("Google Drive")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/cloud/google-drive/callback")
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


@app.route(URL_PREFIX + "/api/cloud/google-drive/files")
@login_required
def api_gdrive_files():
    try:
        from app.cloud_adapters.google_drive import list_files
        return jsonify({"files": list_files(folder_id=request.args.get("folder", ""))})
    except ImportError:
        return _cloud_unavail("Google Drive")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/cloud/google-drive/import", methods=["POST"])
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


@app.route(URL_PREFIX + "/api/cloud/dropbox/auth")
@login_required
def api_dropbox_auth():
    try:
        from app.cloud_adapters.dropbox_adapter import get_auth_url
        return redirect(get_auth_url(url_for("api_dropbox_callback", _external=True)))
    except ImportError:
        return _cloud_unavail("Dropbox")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/cloud/dropbox/callback")
@login_required
def api_dropbox_callback():
    try:
        from app.cloud_adapters.dropbox_adapter import handle_callback
        handle_callback(request.args)
        flash("Dropbox connected.", "success")
    except ImportError:
        flash("Dropbox adapter not available.", "warning")
    except Exception as e:
        flash(f"Dropbox auth error: {e}", "danger")
    return redirect(_url("/import"))


@app.route(URL_PREFIX + "/api/cloud/dropbox/files")
@login_required
def api_dropbox_files():
    try:
        from app.cloud_adapters.dropbox_adapter import list_files
        return jsonify({"files": list_files(path=request.args.get("path", ""))})
    except ImportError:
        return _cloud_unavail("Dropbox")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/cloud/dropbox/import", methods=["POST"])
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


@app.route(URL_PREFIX + "/api/cloud/s3/browse", methods=["POST"])
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
            "folders": [p.get("Prefix", "")
                        for p in resp.get("CommonPrefixes", [])],
        })
    except ImportError:
        return jsonify({"error": "boto3 not installed"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/cloud/s3/import", methods=["POST"])
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
            from app.config import CONSUME_PATH
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
                    logger.error(f"S3 download {key}: {ke}")
            db.update_import_job(jid, status="completed", count_imported=count,
                                 completed_at=datetime.utcnow().isoformat())
            db.log_activity("import_complete", f"S3: {count} files")
        except ImportError:
            db.update_import_job(jid, status="error",
                                 error_msg="boto3 not installed",
                                 completed_at=datetime.utcnow().isoformat())
        except Exception as e:
            db.update_import_job(jid, status="error", error_msg=str(e),
                                 completed_at=datetime.utcnow().isoformat())

    threading.Thread(target=_run, args=(job_id, bucket, keys), daemon=True).start()
    return jsonify({"status": "started", "job_id": job_id})


# ---------------------------------------------------------------------------
# API — Chat
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/chat/sessions", methods=["GET"])
@login_required
def api_chat_sessions_list():
    q = request.args.get("q", "").strip()
    if q:
        rows = db.search_chat_sessions(
            current_user.id, q, is_admin=current_user.is_admin
        )
        return jsonify(_row_list(rows))
    return jsonify(_row_list(db.list_chat_sessions(user_id=current_user.id)))


@app.route(URL_PREFIX + "/api/chat/sessions", methods=["POST"])
@login_required
def api_chat_sessions_create():
    data = request.get_json() or {}
    sess = db.create_chat_session(
        user_id=current_user.id,
        entity_id=data.get("entity_id"),
        tax_year=data.get("year"),
        title=data.get("title", "New Chat"),
    )
    # create_chat_session returns a dict; return it directly so callers get sess.id
    if isinstance(sess, dict):
        return jsonify(sess), 201
    return jsonify({"id": sess, "title": data.get("title", "New Chat")}), 201


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/messages", methods=["GET"])
@login_required
def api_chat_messages(session_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if not _user_can_access_session(sess):
        return jsonify({"error": "forbidden"}), 403
    msgs = db.get_chat_messages(session_id)
    shares = _row_list(db.get_chat_shares(session_id))
    return jsonify({
        "session": dict(sess),
        "messages": _row_list(msgs),
        "shares": shares,
        "can_write": _user_can_write_session(sess),
    })


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/send", methods=["POST"])
@login_required
def api_chat_send(session_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "session not found"}), 404
    if not _user_can_write_session(sess):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json() or {}
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "message required"}), 400

    db.append_chat_message(session_id, "user", user_msg)

    # Build context
    entity_ctx = ""
    entity_slug_filter = None
    if sess.get("entity_id"):
        ent = db.get_entity(entity_id=sess["entity_id"])
        if ent:
            entity_ctx = f"Entity: {ent['name']} ({ent['type']}). "
            entity_slug_filter = ent.get("slug")
    year_filter = sess.get("tax_year") or None
    if year_filter:
        entity_ctx += f"Tax year: {year_filter}. "

    # ── RAG: vector search over embedded documents ───────────────────────────
    rag_ctx = ""
    try:
        from app.vector_store import search
        hits = search(
            user_msg,
            entity_slug=entity_slug_filter,
            tax_year=year_filter,
            limit=5,
        )
        if hits:
            lines = []
            for h in hits:
                amt = f" ${h['amount']}" if h.get("amount") else ""
                lines.append(
                    f"• [{h.get('doc_type','?')}] {h.get('title','?')}"
                    f" ({h.get('tax_year','?')}, {h.get('entity_slug','?')})"
                    f"{amt} — {h.get('snippet','')[:120]}"
                )
            rag_ctx = "\n\nRelevant documents in the system:\n" + "\n".join(lines)
    except Exception as _rag_err:
        logger.debug(f"RAG failed: {_rag_err}")

    # ── DB document stats — give AI real answers about what years/entities exist ─
    db_stats_ctx = ""
    try:
        _conn = db.get_connection()
        _year_rows = _conn.execute(
            "SELECT tax_year, COUNT(*) as n, SUM(CASE WHEN amount IS NOT NULL THEN 1 ELSE 0 END) as with_amt"
            " FROM analyzed_documents"
            " WHERE tax_year IS NOT NULL AND tax_year != ''"
            " GROUP BY tax_year ORDER BY tax_year DESC LIMIT 20"
        ).fetchall()
        _ent_rows = _conn.execute(
            "SELECT COALESCE(e.name,'Unknown') as ename, COUNT(a.id) as n"
            " FROM analyzed_documents a LEFT JOIN entities e ON e.id=a.entity_id"
            " GROUP BY a.entity_id ORDER BY n DESC LIMIT 10"
        ).fetchall()
        _type_rows = _conn.execute(
            "SELECT doc_type, COUNT(*) as n FROM analyzed_documents"
            " WHERE doc_type IS NOT NULL AND doc_type != ''"
            " GROUP BY doc_type ORDER BY n DESC LIMIT 15"
        ).fetchall()
        _total = _conn.execute("SELECT COUNT(*) FROM analyzed_documents").fetchone()[0]
        _conn.close()
        if _year_rows:
            db_stats_ctx += "\n\nDocument index summary:"
            db_stats_ctx += f"\n  Total analyzed: {_total}"
            db_stats_ctx += "\n  By year: " + ", ".join(
                f"{r['tax_year']} ({r['n']} docs, {r['with_amt']} with amounts)" for r in _year_rows
            )
        if _ent_rows:
            db_stats_ctx += "\n  By entity: " + ", ".join(
                f"{r['ename']}: {r['n']}" for r in _ent_rows
            )
        if _type_rows:
            db_stats_ctx += "\n  By type: " + ", ".join(
                f"{r['doc_type']}: {r['n']}" for r in _type_rows
            )
    except Exception as _stats_err:
        logger.debug(f"DB stats for AI failed: {_stats_err}")

    settings = db.get_all_settings()
    api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    model = settings.get("llm_model") or LLM_MODEL
    system_prompt = (
        "You are a professional financial bookkeeping and tax AI assistant with direct "
        "access to the user's indexed financial documents. "
        f"{entity_ctx}"
        "Help users understand their financial records, categorize transactions, identify "
        "deductions, and prepare for taxes. Always cite dollar amounts with 2 decimal places. "
        "When referencing a document, mention its title and year. "
        "You have access to the actual document index — use the stats below to give precise, "
        "direct answers. Do NOT say you cannot see the documents or need to be asked differently."
        f"{db_stats_ctx}"
        f"{rag_ctx}"
    )

    prev_msgs_rows = db.get_chat_messages(session_id)
    prev_msgs = [{"role": m["role"], "content": m["content"]}
                 for m in list(prev_msgs_rows)[-13:-1]
                 if m["role"] in ("user", "assistant")]

    # Register stop event for this session
    stop_event = threading.Event()
    with _chat_stop_lock:
        _chat_stop_events[session_id] = stop_event

    def _generate():
        full = []
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model=model, max_tokens=2048,
                system=system_prompt,
                messages=prev_msgs + [{"role": "user", "content": user_msg}],
            ) as stream:
                for chunk in stream.text_stream:
                    if stop_event.is_set():
                        yield f"data: {json.dumps({'text': ' [stopped]', 'done': True})}\n\n"
                        break
                    full.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
            db.append_chat_message(session_id, "assistant",
                                   "".join(full), model_used=model)
            if not stop_event.is_set():
                yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            err = f"AI error: {e}"
            db.append_chat_message(session_id, "assistant", err)
            yield f"data: {json.dumps({'text': err, 'done': True})}\n\n"
        finally:
            with _chat_stop_lock:
                _chat_stop_events.pop(session_id, None)

    return Response(stream_with_context(_generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/stop", methods=["POST"])
@login_required
def api_chat_stop(session_id):
    """Signal the running stream for this session to stop."""
    with _chat_stop_lock:
        ev = _chat_stop_events.get(session_id)
        if ev:
            ev.set()
            return jsonify({"status": "stopped"})
    return jsonify({"status": "not_running"})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/edit", methods=["POST"])
@login_required
def api_chat_edit_message(session_id):
    """Edit a user message: truncate everything from that message onward, re-send."""
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if not _user_can_write_session(sess):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json() or {}
    from_msg_id = data.get("from_message_id")
    new_text = data.get("message", "").strip()
    if not from_msg_id or not new_text:
        return jsonify({"error": "from_message_id and message required"}), 400
    db.truncate_messages_from(session_id, int(from_msg_id))
    return jsonify({"status": "truncated"})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>", methods=["DELETE"])
@login_required
def api_chat_session_delete(session_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if sess["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    db.delete_chat_session(session_id)
    return jsonify({"status": "deleted"})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/share", methods=["POST"])
@login_required
def api_chat_share(session_id):
    """Share a session with another user by username."""
    sess = db.get_chat_session(session_id)
    if not sess or not _user_can_access_session(sess):
        return jsonify({"error": "not found"}), 404
    if sess["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "Only the owner can share"}), 403
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    can_write = bool(data.get("can_write", False))
    if not username:
        return jsonify({"error": "username required"}), 400
    target = db.get_user_by_username(username)
    if not target:
        return jsonify({"error": f"User '{username}' not found"}), 404
    db.share_chat_session(session_id, target["id"], current_user.id, can_write)
    return jsonify({"status": "shared", "shared_with": username})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/share/<int:user_id>",
           methods=["DELETE"])
@login_required
def api_chat_unshare(session_id, user_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if sess["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    db.unshare_chat_session(session_id, user_id)
    return jsonify({"status": "unshared"})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/rename", methods=["POST"])
@login_required
def api_chat_rename(session_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if not _user_can_write_session(sess):
        return jsonify({"error": "forbidden"}), 403
    title = (request.get_json() or {}).get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    conn = db.get_connection()
    conn.execute("UPDATE chat_sessions SET title=? WHERE id=?", (title, session_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "renamed", "title": title})


@app.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/export")
@login_required
def api_chat_export(session_id):
    """Export a chat session as a PDF."""
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if not _user_can_access_session(sess):
        return jsonify({"error": "forbidden"}), 403
    msgs = [dict(m) for m in db.get_chat_messages(session_id)]
    title = sess.get("title") or f"Chat #{session_id}"
    import html as _html_lib
    rows_html = ""
    for m in msgs:
        role_label = "You" if m["role"] == "user" else "AI Assistant"
        role_color = "#1a3c5e" if m["role"] == "user" else "#2d6a4f"
        bg = "#e8f4fd" if m["role"] == "user" else "#f0f7f4"
        ts = str(m.get("created_at", ""))[:16].replace("T", " ")
        content = _html_lib.escape(m.get("content", "")).replace("\n", "<br>")
        rows_html += (
            f'<div style="margin-bottom:16px;padding:12px 16px;background:{bg};'
            f'border-radius:8px;border-left:4px solid {role_color}">'
            f'<div style="font-weight:700;color:{role_color};font-size:.85rem;margin-bottom:6px">'
            f'{role_label} <span style="font-weight:400;color:#888;font-size:.78rem">{ts}</span></div>'
            f'<div style="font-size:.9rem;line-height:1.6;color:#222">{content}</div></div>'
        )
    html_doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
margin:32px;color:#222}}h1{{font-size:1.2rem;color:#1a3c5e;margin-bottom:4px}}
.meta{{font-size:.8rem;color:#888;margin-bottom:24px;border-bottom:1px solid #ddd;padding-bottom:12px}}</style>
</head><body>
<h1>{_html_lib.escape(title)}</h1>
<div class="meta">Exported {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC &mdash; {len(msgs)} messages</div>
{rows_html}
</body></html>"""
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html_doc).write_pdf()
        safe = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:50]
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"chat_{safe}.pdf",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Entity access management (admin only) ────────────────────────────────────

@app.route(URL_PREFIX + "/api/users/<int:user_id>/entity-access", methods=["GET"])
@login_required
@admin_required
def api_user_entity_access_list(user_id):
    return jsonify(db.get_user_entity_access(user_id))


@app.route(URL_PREFIX + "/api/users/<int:user_id>/entity-access", methods=["POST"])
@login_required
@admin_required
def api_user_entity_access_grant(user_id):
    data = request.get_json() or {}
    entity_id = data.get("entity_id")
    level = data.get("access_level", "read")
    if not entity_id:
        return jsonify({"error": "entity_id required"}), 400
    db.set_user_entity_access(user_id, entity_id, level, current_user.id)
    return jsonify({"status": "granted"})


@app.route(URL_PREFIX + "/api/users/<int:user_id>/entity-access/<int:entity_id>",
           methods=["DELETE"])
@login_required
@admin_required
def api_user_entity_access_revoke(user_id, entity_id):
    db.revoke_user_entity_access(user_id, entity_id)
    return jsonify({"status": "revoked"})


# ---------------------------------------------------------------------------
# API — Reports / Export
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/export/<year>/<entity_slug>", methods=["POST"])
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

        db.log_activity("export_complete", f"{entity_slug}/{year}",
                        user_id=current_user.id)
        return jsonify({
            "status": "ok", "year": year, "entity": entity_slug,
            "doc_count": doc_count,
            "files": [os.path.basename(f) for f in files.values()],
            "zip": os.path.basename(files["zip"]) if "zip" in files else None,
            "errors": errors,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route(URL_PREFIX + "/api/export/<year>/<entity_slug>/download/<format_name>")
@login_required
def api_export_download(year, entity_slug, format_name):
    ext_map = {"csv": ".csv", "json": ".json", "iif": ".iif",
               "ofx": ".ofx", "txf": ".txf", "pdf": ".pdf", "zip": ".zip"}
    ext = ext_map.get(format_name.lower(), f".{format_name}")
    filename = f"{entity_slug}_{year}{ext}"
    for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=filename)
    return jsonify({"error": "file not found"}), 404


@app.route(URL_PREFIX + "/api/export/list")
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


# ---------------------------------------------------------------------------
# API — Settings
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/settings", methods=["GET"])
@login_required
@admin_required
def api_settings_get():
    raw = db.get_all_settings()
    masked = dict(raw)
    for key in ("llm_api_key", "paperless_token", "smtp_pass",
                "dropbox_token", "s3_secret_key"):
        if masked.get(key):
            masked[key] = "***" + str(masked[key])[-4:]
    masked.setdefault("llm_model", LLM_MODEL)
    masked.setdefault("paperless_url", PAPERLESS_API_BASE_URL)
    return jsonify(masked)


@app.route(URL_PREFIX + "/api/settings", methods=["POST"])
@login_required
@admin_required
def api_settings_save():
    data = request.get_json() or {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith("***"):
            continue
        db.set_setting(key, str(value))
    db.log_activity("settings_updated", f"{len(data)} keys", user_id=current_user.id)
    return jsonify({"status": "saved"})


@app.route(URL_PREFIX + "/api/settings/test-llm", methods=["POST"])
@login_required
@admin_required
def api_settings_test_llm():
    try:
        import anthropic
        settings = db.get_all_settings()
        api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
        model = settings.get("llm_model") or LLM_MODEL
        if not api_key:
            return jsonify({"status": "error", "message": "No API key configured"}), 400
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model, max_tokens=20,
            messages=[{"role": "user", "content": "Say OK"}])
        return jsonify({"status": "ok", "model": model,
                        "response": msg.content[0].text if msg.content else ""})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route(URL_PREFIX + "/api/settings/test-paperless", methods=["POST"])
@login_required
@admin_required
def api_settings_test_paperless():
    try:
        import httpx
        settings = db.get_all_settings()
        base = settings.get("paperless_url") or PAPERLESS_API_BASE_URL
        token = settings.get("paperless_token") or os.environ.get("PAPERLESS_API_TOKEN", "")
        headers = {"Authorization": f"Token {token}"} if token else {}
        r = httpx.get(f"{base}/api/", headers=headers, timeout=10)
        return jsonify({"status": "ok" if r.status_code == 200 else "auth_error",
                        "code": r.status_code, "url": base})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# API — Analysis
# ---------------------------------------------------------------------------

_is_analyzing = False


@app.route(URL_PREFIX + "/api/analyze/trigger", methods=["POST"])
@login_required
def api_analyze_trigger():
    global _is_analyzing
    if _is_analyzing:
        return jsonify({"status": "already_running"})

    def _run():
        global _is_analyzing
        _is_analyzing = True
        try:
            from app.paperless_client import get_all_document_ids, get_document, apply_tags
            from app.categorizer import categorize
            from app.extractor import extract
            from app.state import is_analyzed, mark_analyzed
            from app.vector_store import index_document

            db.log_activity("analysis_started", "Manual trigger")
            doc_ids = get_all_document_ids()
            new_ids = [d for d in doc_ids if not is_analyzed(d)]
            analyzed = 0

            for doc_id in new_ids[:20]:
                try:
                    doc = get_document(doc_id)
                    content = doc.get("content", "")
                    title = doc.get("title", f"Document {doc_id}")
                    if not content or len(content.strip()) < 10:
                        mark_analyzed(doc_id, {"doc_id": doc_id, "title": title,
                                               "skipped": True, "reason": "no_content"})
                        continue
                    cat = categorize(content, title)
                    ext = extract(content)
                    result = {
                        "doc_id": doc_id, "title": title,
                        "analyzed_at": datetime.utcnow().isoformat(),
                        **cat,
                        **{k: v for k, v in ext.items()
                           if v is not None and k not in cat},
                    }
                    from app.main import _apply_business_rules
                    result = _apply_business_rules(result, content, title)
                    entity_tag = cat.get("entity") or "personal"
                    year_tag = str(cat.get("tax_year") or "unknown")
                    tags = [t for t in cat.get("tags", []) if t] + [
                        f"tax-{entity_tag}", f"year-{year_tag}"]
                    try:
                        apply_tags(doc_id, tags)
                    except Exception:
                        pass
                    try:
                        index_document(doc_id, title, content, {
                            "doc_type": cat.get("doc_type"),
                            "category": cat.get("category"),
                            "entity": cat.get("entity"),
                            "tax_year": cat.get("tax_year"),
                        })
                    except Exception:
                        pass
                    mark_analyzed(doc_id, result)
                    # Mirror to SQLite
                    entity_row = db.get_entity(slug=entity_tag)
                    db.mark_document_analyzed(
                        paperless_doc_id=doc_id,
                        entity_id=entity_row["id"] if entity_row else None,
                        tax_year=year_tag,
                        doc_type=cat.get("doc_type", "other"),
                        category=cat.get("category", "other"),
                        vendor=cat.get("vendor") or "",
                        amount=float(cat.get("amount") or 0),
                        date=ext.get("date") or "",
                        confidence=float(cat.get("confidence") or 0),
                        extracted_json=json.dumps(ext),
                    )
                    analyzed += 1
                    db.log_activity("doc_analyzed",
                                    f"Doc {doc_id}: {cat.get('doc_type')} / "
                                    f"{entity_tag} / ${cat.get('amount') or 0}")
                except Exception as e:
                    logger.error(f"Error analyzing doc {doc_id}: {e}")
                    mark_analyzed(doc_id, {"doc_id": doc_id, "error": str(e),
                                           "analyzed_at": datetime.utcnow().isoformat()})
            db.log_activity("analysis_complete", f"Analyzed {analyzed} docs")
        except Exception as e:
            db.log_activity("analysis_error", str(e))
        finally:
            _is_analyzing = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route(URL_PREFIX + "/api/analyze/status")
@login_required
def api_analyze_status():
    recent = _row_list(db.get_recent_activity(10))
    return jsonify({"is_analyzing": _is_analyzing, "recent_log": recent})


# ---------------------------------------------------------------------------
# API — Users (admin)
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/users", methods=["GET"])
@login_required
@admin_required
def api_users_list():
    return jsonify([u.to_dict() for u in auth.list_users()])


@app.route(URL_PREFIX + "/api/users", methods=["POST"])
@login_required
@admin_required
def api_users_create():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    try:
        role = "admin" if data.get("is_admin") else "standard"
        uid = auth.create_user(username=username, password=password,
                               email=data.get("email", ""), role=role)
        db.log_activity("user_created", f"Username: {username}",
                        user_id=current_user.id)
        return jsonify({"id": uid, "username": username, "role": role}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route(URL_PREFIX + "/api/users/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def api_users_update(user_id):
    data = request.get_json() or {}
    if user_id == current_user.id:
        data.pop("role", None)
    data.pop("password", None)
    auth.update_user(user_id, **data)
    db.log_activity("user_updated", f"User ID: {user_id}", user_id=current_user.id)
    return jsonify({"status": "updated", "id": user_id})


@app.route(URL_PREFIX + "/api/users/<int:user_id>", methods=["DELETE"])
@login_required
@admin_required
def api_users_delete(user_id):
    if user_id == current_user.id:
        return jsonify({"error": "Cannot delete your own account"}), 400
    auth.delete_user(user_id)
    db.log_activity("user_deleted", f"User ID: {user_id}", user_id=current_user.id)
    return jsonify({"status": "deleted"})


@app.route(URL_PREFIX + "/api/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def api_users_reset_password(user_id):
    data = request.get_json() or {}
    new_pw = data.get("password", "").strip()
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    auth.update_user(user_id, password=new_pw)
    db.log_activity("password_reset", f"User ID: {user_id}", user_id=current_user.id)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Gmail setup pages
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/import/gmail/setup")
@login_required
def gmail_setup_page():
    return render_template("gmail_setup.html",
                           has_credentials=os.path.exists(GMAIL_CREDENTIALS_FILE),
                           has_token=os.path.exists(GMAIL_TOKEN_FILE),
                           url_prefix=URL_PREFIX)


@app.route(URL_PREFIX + "/import/gmail/credentials", methods=["POST"])
@login_required
@admin_required
def gmail_upload_credentials():
    f = request.files.get("credentials")
    if f:
        try:
            content = f.read()
            json.loads(content)
            os.makedirs(os.path.dirname(GMAIL_CREDENTIALS_FILE), exist_ok=True)
            with open(GMAIL_CREDENTIALS_FILE, "wb") as out:
                out.write(content)
            flash("credentials.json saved.", "success")
        except json.JSONDecodeError:
            flash("Invalid JSON file.", "danger")
        except Exception as e:
            flash(f"Error: {e}", "danger")
    return redirect(_url("/import/gmail/setup"))


GMAIL_CALLBACK_URL = f"https://www.voipguru.org{URL_PREFIX}/import/gmail/auth/callback"


def _make_flow(redirect_uri=None):
    """Build the correct OAuth Flow for the stored credentials (web or installed)."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(
        GMAIL_CREDENTIALS_FILE,
        scopes=GMAIL_SCOPES,
        redirect_uri=redirect_uri or GMAIL_CALLBACK_URL,
    )
    return flow


@app.route(URL_PREFIX + "/import/gmail/auth")
@login_required
def gmail_oauth_start():
    try:
        flow = _make_flow(redirect_uri=GMAIL_CALLBACK_URL)
        auth_url, state = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true")
        flask_session["gmail_oauth_state"] = state
        logger.info(f"Gmail OAuth start: redirect_uri={GMAIL_CALLBACK_URL}")
        return redirect(auth_url)
    except FileNotFoundError:
        flash("credentials.json not found.", "danger")
        return redirect(_url("/import"))
    except ImportError:
        flash("google-auth-oauthlib not installed.", "danger")
        return redirect(_url("/import"))
    except Exception as e:
        logger.error(f"Gmail OAuth start error: {e}")
        flash(f"OAuth error: {e}", "danger")
        return redirect(_url("/import"))


@app.route(URL_PREFIX + "/import/gmail/auth/callback")
@login_required
def gmail_oauth_callback():
    try:
        flow = _make_flow(redirect_uri=GMAIL_CALLBACK_URL)
        # Rebuild the full callback URL using our canonical domain
        auth_response = GMAIL_CALLBACK_URL + "?" + request.query_string.decode()
        logger.info(f"Gmail OAuth callback: {auth_response[:100]}")
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else GMAIL_SCOPES,
        }
        os.makedirs(os.path.dirname(GMAIL_TOKEN_FILE), exist_ok=True)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)
        # Also persist to DB so gmail_importer.get_credentials() can find it
        db.set_setting("gmail_oauth_token", json.dumps(token_data))
        db.log_activity("gmail_oauth_complete", "Token saved",
                        user_id=current_user.id)
        return """<!doctype html><html><head><title>Gmail Connected</title>
<style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f0fdf4}
.box{text-align:center;padding:40px;background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.1)}
h2{color:#16a34a;margin:0 0 8px}p{color:#555;margin:0 0 20px}button{padding:8px 20px;background:#16a34a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:1rem}</style>
</head><body><div class="box"><h2>&#10003; Gmail Connected!</h2>
<p>Authorization complete. You can close this tab and return to the Tax Organizer.</p>
<button onclick="window.close()">Close Tab</button></div>
<script>setTimeout(function(){window.close();},3000);</script>
</body></html>"""
    except ImportError:
        flash("google-auth-oauthlib not installed.", "danger")
    except Exception as e:
        logger.error(f"Gmail OAuth callback error: {e}")
        flash(f"OAuth callback error: {e}", "danger")
    return redirect(_url("/import"))


# ---------------------------------------------------------------------------
# Gmail AI setup chat (guided OAuth setup assistant)
# ---------------------------------------------------------------------------

GMAIL_SETUP_SYSTEM_PROMPT = """You are a friendly setup assistant helping the user connect their personal Gmail account to a self-hosted tax document organizer app. This app automatically imports tax-related emails (receipts, invoices, 1099s, W-2s, etc.) into a local document management system.

KEY FACTS:
- Works with personal @gmail.com accounts (NOT just Google Workspace)
- Select "External" user type on the OAuth consent screen (for personal Gmail)
- Create a "Desktop app" credential (NOT Web application)
- App stays in "Testing" mode — that is fine and expected
- Must add themselves as a test user on the consent screen

CRITICAL — REDIRECT URI:
The app callback URL is: https://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback
This MUST be added as an Authorized Redirect URI in the Google Cloud credential.
Without this, Google redirects to localhost and the token is never received.

CREDENTIAL TYPE: Must be "Web application" (NOT Desktop app).
Desktop app credentials use localhost redirect which won't work for a server-hosted app.

SETUP STEPS:
1. Go to console.cloud.google.com — sign in with the Google account to scan
2. Create a new project (name it e.g. "tax-gmail-collector")
3. Enable Gmail API: APIs & Services → Library → search "Gmail API" → Enable
4. Configure OAuth consent screen: APIs & Services → OAuth consent screen → External → fill app name, emails → Save
5. Add scope: Scopes step → Add or Remove Scopes → search "gmail.readonly" → select → Update → Save
6. Add test user: Test Users step → Add Users → add their @gmail.com → Save
7. Create credential: APIs & Services → Credentials → Create Credentials → OAuth client ID
   → Select type: "Web application" (NOT Desktop app)
   → Under "Authorized redirect URIs" click Add URI
   → Enter: https://voipguru.org/tax-ai-analyzer/import/gmail/auth/callback
   → Click Create
8. Download JSON: click download icon on the new credential
9. Upload here: use the upload section below the chat

If the user already has a Desktop app credential: tell them to either edit it and add the redirect URI, or delete it and create a new "Web application" credential.

Google's UI changes frequently. Adapt guidance if the user describes a different layout. Be concise and step-by-step."""


@app.route(URL_PREFIX + "/api/import/gmail/status")
@login_required
def gmail_status_api():
    from app.config import GMAIL_SEARCH_TERMS
    token_in_db = bool(db.get_setting("gmail_oauth_token"))
    callback_url = GMAIL_CALLBACK_URL
    return jsonify({
        "has_credentials": os.path.exists(GMAIL_CREDENTIALS_FILE),
        "has_token": os.path.exists(GMAIL_TOKEN_FILE) or token_in_db,
        "authenticated": token_in_db,
        "search_terms": GMAIL_SEARCH_TERMS,
        "callback_url": callback_url,
    })


@app.route(URL_PREFIX + "/api/import/gmail/search-terms", methods=["POST"])
@login_required
def api_save_gmail_search_terms():
    data = request.get_json(force=True) or {}
    terms = data.get("terms", "")
    db.set_setting("gmail_search_terms", terms.strip())
    return jsonify({"status": "ok", "terms": terms.strip()})


@app.route(URL_PREFIX + "/api/import/gmail/setup-chat", methods=["POST"])
@login_required
def gmail_setup_chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    history = data.get("history", [])
    if not user_message:
        return jsonify({"error": "message required"}), 400

    settings = db.get_all_settings()
    api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    model = settings.get("llm_model") or LLM_MODEL

    messages = []
    for h in history[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    def _generate():
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model=model, max_tokens=1024,
                system=GMAIL_SETUP_SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(stream_with_context(_generate()),
                    mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# PayPal AI setup chat (guided API credentials assistant)
# ---------------------------------------------------------------------------

PAYPAL_SETUP_SYSTEM_PROMPT = """You are a friendly setup assistant helping the user connect their PayPal account to a self-hosted tax document organizer app. This app pulls transaction history directly from PayPal's Transactions API to categorize income and expenses for tax purposes.

KEY FACTS:
- Requires a PayPal Developer account (free, at developer.paypal.com)
- Uses REST API credentials: Client ID + Client Secret
- Must create a "Live" app (not Sandbox) to pull real transactions
- Sandbox mode only works for test transactions, not real PayPal history
- The app only READS transactions — it never initiates payments or transfers

SETUP STEPS:
1. Go to developer.paypal.com and log in with your PayPal business or personal account
2. Click "My Apps & Credentials" in the top navigation
3. Make sure the toggle at the top-right is set to "Live" (not Sandbox)
4. Click "Create App" button
5. App name: use something like "Tax Organizer" — the name is just for your reference
6. App Type: select "Merchant" (gives access to Transactions API)
7. Click "Create App"
8. On the app detail page, copy the "Client ID" (starts with A)
9. Click "Show" under "Secret" and copy the Client Secret (starts with E)
10. Paste both into the fields on the Import Hub → PayPal tab and click "Save & Test Credentials"

TROUBLESHOOTING:
- "401 Unauthorized": double-check you copied Live credentials, not Sandbox
- "403 Forbidden": the app may not have Transactions API permission — check App Features
- If you see "App Features" section, enable "Transaction Search" if available
- Sandbox mode: only use this if you want to test with PayPal's test environment

Be concise and step-by-step. Ask clarifying questions if the user seems stuck."""


@app.route(URL_PREFIX + "/api/import/paypal/setup-chat", methods=["POST"])
@login_required
def paypal_setup_chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    history = data.get("history", [])
    if not user_message:
        return jsonify({"error": "message required"}), 400

    settings = db.get_all_settings()
    api_key = settings.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    model = settings.get("llm_model") or LLM_MODEL

    messages = []
    for h in history[-20:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    def _generate():
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            with client.messages.stream(
                model=model, max_tokens=1024,
                system=PAYPAL_SETUP_SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(stream_with_context(_generate()),
                    mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/health")
def health():
    return jsonify({"status": "ok", "service": "tax-ai-analyzer"})


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def err_404(e):
    if request.path.startswith(URL_PREFIX + "/api/"):
        return jsonify({"error": "not found"}), 404
    return redirect(_url("/"))


@app.errorhandler(500)
def err_500(e):
    logger.error(f"500: {e}")
    if request.path.startswith(URL_PREFIX + "/api/"):
        return jsonify({"error": "internal server error"}), 500
    return redirect(_url("/"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(EXPORT_PATH, exist_ok=True)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)


# ---------------------------------------------------------------------------
# Route aliases for dashboard compatibility
# ---------------------------------------------------------------------------

@app.route(URL_PREFIX + "/api/analyze", methods=["POST"])
@login_required
def api_analyze_alias():
    """Alias for /api/analyze/trigger"""
    return api_analyze_trigger()


@app.route(URL_PREFIX + "/export/<year>/<entity_slug>")
@login_required
def export_download_direct(year, entity_slug):
    """Direct export URL: generate if needed, then download."""
    format_name = request.args.get("format", "zip")
    # Try to find an already-generated file first
    ext_map = {"csv": ".csv", "json": ".json", "iif": ".iif",
               "ofx": ".ofx", "txf": ".txf", "pdf": ".pdf", "zip": ".zip"}
    ext = ext_map.get(format_name.lower(), f".{format_name}")
    filename = f"{entity_slug}_{year}{ext}"
    for base in (os.path.join(EXPORT_PATH, year), EXPORT_PATH):
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=filename)
    # Generate on the fly
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
        logger.error(f"Export error {year}/{entity_slug}/{format_name}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route(URL_PREFIX + "/api/ai-costs")
@login_required
def api_ai_costs():
    """LLM API usage statistics."""
    days = request.args.get("days", 30, type=int)
    from app import llm_usage_tracker as tracker
    stats = tracker.get_stats(days=days)
    return jsonify({"stats": stats})


@app.route(URL_PREFIX + "/api/ai-costs/recent")
@login_required
def api_ai_costs_recent():
    """Most recent LLM API calls."""
    limit = request.args.get("limit", 50, type=int)
    from app import llm_usage_tracker as tracker
    calls = tracker.get_recent_calls(limit=limit)
    return jsonify({"calls": calls})


@app.route(URL_PREFIX + "/api/filed-returns/import-from-folder", methods=["POST"])
@login_required
@admin_required
def api_import_filed_return_from_folder():
    """Read a filed tax return PDF from /tax/<year>/ folder and extract 1040 data via AI."""
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

    # Prefer files that look like a complete filed tax return
    _RETURN_SIGNALS = ["1040", "client copy", "client_copy", "tax return",
                       "filed return", "complete return", "blag"]
    # Exclude files that are clearly NOT the return
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
                        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw = msg.content[0].text
        else:
            # OpenAI: extract text with pdftotext then send as text
            import subprocess
            result = subprocess.run(["pdftotext", pdf_path, "-"], capture_output=True, text=True, timeout=30)
            pdf_text = result.stdout[:8000] if result.returncode == 0 else ""
            if not pdf_text:
                return jsonify({"error": "Could not extract text from PDF"}), 400
            client = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)
            raw = client.chat([{"role": "user", "content": f"Tax return text:\n{pdf_text}\n\n{prompt}"}])
            if not isinstance(raw, str):
                raw = str(raw)

        match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not match:
            return jsonify({"error": "Could not parse JSON from AI response", "raw": raw[:500]}), 500
        extracted = json.loads(match.group(0))
    except Exception as e:
        logger.error(f"Filed return extraction failed: {e}")
        return jsonify({"error": f"AI extraction failed: {e}"}), 500

    # Resolve entity
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
        return jsonify({"error": f"DB save failed: {e}"}), 500


# ── Folder Manager ────────────────────────────────────────────────────────────
TAX_SOURCE_ROOT = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
TAX_CONSUME_ROOT = "/mnt/s/documents/tax-organizer/consume"


@app.route(URL_PREFIX + "/api/folder-manager/scan")
@login_required
def api_fm_scan():
    """Return inconsistent folders in the tax archive."""
    from app.folder_manager import find_inconsistencies, scan_tree
    year = request.args.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    issues = find_inconsistencies(root)
    return jsonify({"root": root, "issues": issues, "count": len(issues)})


@app.route(URL_PREFIX + "/api/folder-manager/tree")
@login_required
def api_fm_tree():
    """Return folder tree for a given year (or root)."""
    from app.folder_manager import scan_tree
    year = request.args.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    tree = scan_tree(root, max_depth=3)
    return jsonify({"tree": tree})


@app.route(URL_PREFIX + "/api/folder-manager/rename", methods=["POST"])
@login_required
@admin_required
def api_fm_rename():
    """Rename or merge a single folder. Pass dry_run=false to execute."""
    from app.folder_manager import rename_folder, merge_folders
    data = request.get_json(silent=True) or {}
    src = data.get("src", "").strip()
    new_name = data.get("new_name", "").strip()
    dry_run = data.get("dry_run", True)
    merge = data.get("merge", False)

    if not src or not new_name:
        return jsonify({"error": "src and new_name required"}), 400
    # Safety: must be within TAX_SOURCE_ROOT
    if not os.path.abspath(src).startswith(os.path.abspath(TAX_SOURCE_ROOT)):
        return jsonify({"error": "Path outside allowed root"}), 403

    from pathlib import Path
    dst = str(Path(src).parent / new_name)
    if merge and Path(dst).exists():
        result = merge_folders(src, dst, dry_run=dry_run)
    else:
        result = rename_folder(src, new_name, dry_run=dry_run)
    return jsonify(result)


@app.route(URL_PREFIX + "/api/folder-manager/rename-all", methods=["POST"])
@login_required
@admin_required
def api_fm_rename_all():
    """Apply all auto-detected renames. Pass dry_run=false to execute."""
    from app.folder_manager import apply_all_auto_renames
    data = request.get_json(silent=True) or {}
    dry_run = data.get("dry_run", True)
    year = data.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    results = apply_all_auto_renames(root, dry_run=dry_run)
    return jsonify({"dry_run": dry_run, "results": results, "count": len(results)})


@app.route(URL_PREFIX + "/api/folder-manager/coverage")
@login_required
def api_fm_coverage():
    """Check which source PDFs are already in Paperless."""
    from app.folder_manager import check_paperless_coverage
    from app import config as _cfg
    year = request.args.get("year")
    root = os.path.join(TAX_SOURCE_ROOT, year) if year else TAX_SOURCE_ROOT
    token = db.get_setting("paperless_api_token") or _cfg.PAPERLESS_API_TOKEN
    url = db.get_setting("paperless_url") or _cfg.PAPERLESS_API_BASE_URL
    result = check_paperless_coverage(root, token, url)
    return jsonify(result)


@app.route(URL_PREFIX + "/api/folder-manager/queue", methods=["POST"])
@login_required
@admin_required
def api_fm_queue():
    """Copy source PDFs for a year into the Paperless consume queue."""
    from app.folder_manager import queue_year_for_paperless
    data = request.get_json(silent=True) or {}
    year = str(data.get("year", "")).strip()
    entity_slug = data.get("entity_slug", "personal")
    dry_run = data.get("dry_run", True)
    if not year:
        return jsonify({"error": "year required"}), 400
    result = queue_year_for_paperless(TAX_SOURCE_ROOT, year, TAX_CONSUME_ROOT, entity_slug, dry_run=dry_run)
    return jsonify(result)


@app.route(URL_PREFIX + "/folder-manager")
@login_required
def folder_manager_page():
    return render_template("dashboard.html", active_tab="folder_manager")


@app.route(URL_PREFIX + "/import/gmail/clear-credentials", methods=["POST"])
@login_required
@admin_required
def gmail_clear_credentials():
    """Remove saved Gmail credentials and token."""
    import shutil
    try:
        if os.path.exists(GMAIL_CREDENTIALS_FILE):
            os.remove(GMAIL_CREDENTIALS_FILE)
        if os.path.exists(GMAIL_TOKEN_FILE):
            os.remove(GMAIL_TOKEN_FILE)
        db.log_activity("Gmail credentials cleared")
        flash("Gmail credentials and token cleared.", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(_url("/import/gmail/setup"))
