"""LLM API usage statistics."""
from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.config import URL_PREFIX

bp = Blueprint("ai_costs", __name__)


@bp.route(URL_PREFIX + "/api/ai-costs")
@login_required
def api_ai_costs():
    days = request.args.get("days", 30, type=int)
    from app import llm_usage_tracker as tracker
    stats = tracker.get_stats(days=days)
    return jsonify({"stats": stats})


@bp.route(URL_PREFIX + "/api/ai-costs/recent")
@login_required
def api_ai_costs_recent():
    limit = request.args.get("limit", 50, type=int)
    from app import llm_usage_tracker as tracker
    calls = tracker.get_recent_calls(limit=limit)
    return jsonify({"calls": calls})
