"""Admin routes for the LLM proxy endpoint pool + per-task LMRH hint overrides.

Phase 13. Surfaces what's already in the DB layer (db.llm_proxy_*) plus the
per-task hint registry (lmrh.TASK_PRESETS) as REST APIs the dashboard tab
consumes.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import admin_required

logger = logging.getLogger(__name__)
bp = Blueprint("llm_proxies", __name__)


# ── proxy endpoint CRUD ──────────────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/admin/llm-proxies", methods=["GET"])
@login_required
@admin_required
def api_proxies_list():
    """List all proxy endpoints (enabled + disabled) with breaker state."""
    from app.llm_client import proxy_manager
    rows = db.llm_proxy_list_endpoints(include_disabled=True)
    out = []
    for r in rows:
        bs = proxy_manager.get_breaker_status(r["id"])
        out.append({
            "id": r["id"],
            "label": r["label"],
            "url": r["url"],
            "version": r["version"],
            "priority": r["priority"],
            "enabled": bool(r["enabled"]),
            # api_key intentionally NOT returned — only show last 4 to confirm
            "api_key_tail": (r.get("api_key") or "")[-4:] if r.get("api_key") else "",
            "breaker": bs,
        })
    return jsonify({"endpoints": out})


@bp.route(URL_PREFIX + "/api/admin/llm-proxies", methods=["POST"])
@login_required
@admin_required
def api_proxies_create():
    """Add a new proxy endpoint to the chain."""
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    url = (data.get("url") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    version = int(data.get("version") or 2)
    priority = int(data.get("priority") or 10)
    enabled = bool(data.get("enabled", True))
    if not label:
        return jsonify({"error": "label required"}), 400
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "url must be http(s)://..."}), 400
    if not api_key:
        return jsonify({"error": "api_key required"}), 400
    if version not in (1, 2):
        return jsonify({"error": "version must be 1 or 2"}), 400
    eid = db.llm_proxy_add_endpoint(
        label=label, url=url, api_key=api_key,
        version=version, priority=priority, enabled=enabled,
    )
    db.log_activity("llm_proxy_added", f"label={label} v{version} pri={priority}",
                    user_id=current_user.id)
    return jsonify({"id": eid, "status": "created"}), 201


@bp.route(URL_PREFIX + "/api/admin/llm-proxies/<eid>", methods=["PATCH"])
@login_required
@admin_required
def api_proxies_update(eid):
    """Mutate one or more fields on an endpoint."""
    data = request.get_json(silent=True) or {}
    allowed = {"label", "url", "api_key", "version", "priority", "enabled"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if "version" in fields:
        if int(fields["version"]) not in (1, 2):
            return jsonify({"error": "version must be 1 or 2"}), 400
        fields["version"] = int(fields["version"])
    if "priority" in fields:
        fields["priority"] = int(fields["priority"])
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if not fields:
        return jsonify({"error": "no valid fields to update"}), 400
    ok = db.llm_proxy_update_endpoint(eid, **fields)
    if not ok:
        return jsonify({"error": "endpoint not found"}), 404
    db.log_activity("llm_proxy_updated",
                    f"id={eid} fields={list(fields.keys())}",
                    user_id=current_user.id)
    return jsonify({"status": "updated"})


@bp.route(URL_PREFIX + "/api/admin/llm-proxies/<eid>", methods=["DELETE"])
@login_required
@admin_required
def api_proxies_delete(eid):
    ok = db.llm_proxy_delete_endpoint(eid)
    if not ok:
        return jsonify({"error": "endpoint not found"}), 404
    db.log_activity("llm_proxy_deleted", f"id={eid}", user_id=current_user.id)
    return jsonify({"status": "deleted"})


@bp.route(URL_PREFIX + "/api/admin/llm-proxies/<eid>/test", methods=["POST"])
@login_required
@admin_required
def api_proxies_test(eid):
    """Smoke-test an endpoint with a tiny call. Returns success/failure + latency.

    Sends a minimal /v1/messages call to the endpoint with model=auto (so the
    proxy picks something cheap based on the LMRH hint we send). Useful for
    confirming that an `llmp-*` API key actually works after rotation.
    """
    rows = [r for r in db.llm_proxy_list_endpoints(include_disabled=True)
            if r["id"] == eid]
    if not rows:
        return jsonify({"error": "endpoint not found"}), 404
    ep = rows[0]
    if not ep["enabled"]:
        return jsonify({"error": "endpoint is disabled — enable it first"}), 400

    import time as _t
    from app.llm_client.proxy_manager import build_anthropic_client
    from app.llm_client.lmrh import build_lmrh_header

    hint = build_lmrh_header("classification", cost="economy")
    client = build_anthropic_client(ep, lmrh_hint=hint)
    t0 = _t.time()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
        )
        latency_ms = round((_t.time() - t0) * 1000)
        text = ""
        if resp.content:
            text = "".join(getattr(b, "text", "") for b in resp.content)[:80]
        return jsonify({
            "status": "ok",
            "latency_ms": latency_ms,
            "model": getattr(resp, "model", ""),
            "reply": text,
        })
    except Exception as e:
        latency_ms = round((_t.time() - t0) * 1000)
        return jsonify({
            "status": "error",
            "latency_ms": latency_ms,
            "error": str(e)[:300],
        }), 502


@bp.route(URL_PREFIX + "/api/admin/llm-proxies/<eid>/reset-breaker",
          methods=["POST"])
@login_required
@admin_required
def api_proxies_reset_breaker(eid):
    """Clear circuit-breaker state on a tripped endpoint."""
    from app.llm_client import proxy_manager
    proxy_manager.mark_success(eid)  # resets failures + cooldown
    return jsonify({"status": "reset"})


# ── per-task LMRH hint overrides ─────────────────────────────────────────────

@bp.route(URL_PREFIX + "/api/admin/llm-hints", methods=["GET"])
@login_required
@admin_required
def api_hints_list():
    """Return all task presets + their default + override + effective hint.

    Effective = override if set (non-empty), else default.
    """
    from app.llm_client.lmrh import TASK_PRESETS, build_lmrh_header
    out = []
    for task in sorted(TASK_PRESETS.keys()):
        default = build_lmrh_header(task)
        override = (db.get_setting(f"lmrh.hint.{task}") or "").strip()
        out.append({
            "task": task,
            "default": default,
            "override": override,
            "effective": override or default,
        })
    return jsonify({"hints": out})


@bp.route(URL_PREFIX + "/api/admin/llm-hints/<task>", methods=["POST"])
@login_required
@admin_required
def api_hints_set(task):
    """Set an override for one task. Empty value clears the override."""
    from app.llm_client.lmrh import TASK_PRESETS
    if task not in TASK_PRESETS:
        return jsonify({"error": f"unknown task {task!r}"}), 400
    data = request.get_json(silent=True) or {}
    override = (data.get("override") or "").strip()
    db.set_setting(f"lmrh.hint.{task}", override)
    db.log_activity(
        "llm_hint_updated",
        f"task={task} override={'set' if override else 'cleared'}",
        user_id=current_user.id,
    )
    return jsonify({
        "task": task,
        "override": override,
        "effective": override or _build_default(task),
    })


def _build_default(task: str) -> str:
    from app.llm_client.lmrh import build_lmrh_header
    return build_lmrh_header(task)
