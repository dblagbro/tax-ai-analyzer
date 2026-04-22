"""Chat session management, message streaming, sharing, and PDF export."""
import html as _html_lib
import io
import json
import logging
import os
import re
import threading
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX, LLM_MODEL
from app.routes._state import _chat_stop_events, _chat_stop_lock
from app.routes.helpers import _row_list, _user_can_access_session, _user_can_write_session

logger = logging.getLogger(__name__)

bp = Blueprint("chat", __name__)


@bp.route(URL_PREFIX + "/api/chat/sessions", methods=["GET"])
@login_required
def api_chat_sessions_list():
    q = request.args.get("q", "").strip()
    if q:
        rows = db.search_chat_sessions(
            current_user.id, q, is_admin=current_user.is_admin
        )
        return jsonify(_row_list(rows))
    return jsonify(_row_list(db.list_chat_sessions(user_id=current_user.id)))


@bp.route(URL_PREFIX + "/api/chat/sessions", methods=["POST"])
@login_required
def api_chat_sessions_create():
    data = request.get_json() or {}
    sess = db.create_chat_session(
        user_id=current_user.id,
        entity_id=data.get("entity_id"),
        tax_year=data.get("year"),
        title=data.get("title", "New Chat"),
    )
    return jsonify(sess), 201


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/messages", methods=["GET"])
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


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/send", methods=["POST"])
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

    # Build entity/year context
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

    # RAG: vector search over embedded documents
    rag_ctx = ""
    try:
        from app.vector_store import search
        hits = search(user_msg, entity_slug=entity_slug_filter, tax_year=year_filter, limit=5)
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
        logger.debug("RAG failed: %s", _rag_err)

    # DB document stats — give AI real answers about what years/entities exist
    db_stats_ctx = ""
    try:
        _conn = db.get_connection()
        _year_rows = _conn.execute(
            "SELECT tax_year, COUNT(*) as n, "
            "SUM(CASE WHEN amount IS NOT NULL THEN 1 ELSE 0 END) as with_amt"
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
                f"{r['tax_year']} ({r['n']} docs, {r['with_amt']} with amounts)"
                for r in _year_rows
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
        logger.debug("DB stats for AI failed: %s", _stats_err)

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
            db.append_chat_message(session_id, "assistant", "".join(full), model_used=model)
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
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/stop", methods=["POST"])
@login_required
def api_chat_stop(session_id):
    with _chat_stop_lock:
        ev = _chat_stop_events.get(session_id)
        if ev:
            ev.set()
            return jsonify({"status": "stopped"})
    return jsonify({"status": "not_running"})


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/edit", methods=["POST"])
@login_required
def api_chat_edit_message(session_id):
    sess = db.get_chat_session(session_id)
    if not sess or not _user_can_write_session(sess):
        return jsonify({"error": "not found or access denied"}), 403
    data = request.get_json() or {}
    from_msg_id = data.get("from_message_id")
    if not from_msg_id:
        return jsonify({"error": "from_message_id required"}), 400
    db.truncate_messages_from(session_id, int(from_msg_id))
    return jsonify({"status": "truncated"})


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>", methods=["DELETE"])
@login_required
def api_chat_session_delete(session_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if sess["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "access denied"}), 403
    db.delete_chat_session(session_id)
    return jsonify({"status": "deleted"})


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/share", methods=["POST"])
@login_required
def api_chat_share(session_id):
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


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/share/<int:user_id>",
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


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/rename", methods=["POST"])
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


@bp.route(URL_PREFIX + "/api/chat/sessions/<int:session_id>/export")
@login_required
def api_chat_export(session_id):
    sess = db.get_chat_session(session_id)
    if not sess:
        return jsonify({"error": "not found"}), 404
    if not _user_can_access_session(sess):
        return jsonify({"error": "forbidden"}), 403
    msgs = [dict(m) for m in db.get_chat_messages(session_id)]
    title = sess.get("title") or f"Chat #{session_id}"
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
