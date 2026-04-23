"""SPA shell routes — every tab renders dashboard.html with an active_tab param."""
from flask import Blueprint, render_template
from flask_login import login_required

from app.config import URL_PREFIX
from app.routes.helpers import _no_cache_page, admin_required

bp = Blueprint("pages", __name__)


@bp.route(URL_PREFIX + "/")
@bp.route(URL_PREFIX + "")
@login_required
def index():
    return _no_cache_page(render_template("dashboard.html", active_tab="dashboard"))


@bp.route(URL_PREFIX + "/transactions")
@login_required
def transactions_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="transactions"))


@bp.route(URL_PREFIX + "/documents")
@login_required
def documents_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="documents"))


@bp.route(URL_PREFIX + "/import")
@login_required
def import_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="import"))


@bp.route(URL_PREFIX + "/chat")
@login_required
def chat_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="chat"))


@bp.route(URL_PREFIX + "/reports")
@login_required
def reports_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="reports"))


@bp.route(URL_PREFIX + "/settings")
@login_required
@admin_required
def settings_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="settings"))


@bp.route(URL_PREFIX + "/users")
@login_required
@admin_required
def users_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="users"))


@bp.route(URL_PREFIX + "/entities")
@login_required
def entities_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="entities"))


@bp.route(URL_PREFIX + "/ai-costs")
@bp.route(URL_PREFIX + "/ai_costs")
@login_required
def ai_costs_page():
    return _no_cache_page(render_template("dashboard.html", active_tab="ai_costs"))


@bp.route(URL_PREFIX + "/docs")
@login_required
def docs_page():
    return render_template("docs.html")


@bp.route(URL_PREFIX + "/tax-review")
@login_required
def tax_review_page():
    return render_template("dashboard.html", active_tab="tax_review")


@bp.route(URL_PREFIX + "/folder-manager")
@login_required
def folder_manager_page():
    return render_template("dashboard.html", active_tab="folder_manager")


@bp.route(URL_PREFIX + "/mileage")
@login_required
def mileage_page():
    return render_template("dashboard.html", active_tab="mileage")


@bp.route(URL_PREFIX + "/activity")
@login_required
def activity_page():
    return render_template("dashboard.html", active_tab="activity")
