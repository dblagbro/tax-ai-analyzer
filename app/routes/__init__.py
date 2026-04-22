"""Blueprint registry — call register_blueprints(app) from the app factory."""


def register_blueprints(app):
    from app.routes.auth import bp as auth_bp
    from app.routes.pages import bp as pages_bp
    from app.routes.stats import bp as stats_bp
    from app.routes.entities import bp as entities_bp
    from app.routes.documents import bp as documents_bp
    from app.routes.transactions import bp as transactions_bp
    from app.routes.import_ import bp as import_bp
    from app.routes.import_jobs import bp as import_jobs_bp
    from app.routes.import_cloud import bp as import_cloud_bp
    from app.routes.export_ import bp as export_bp
    from app.routes.tax_review import bp as tax_review_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.analyze import bp as analyze_bp
    from app.routes.users import bp as users_bp
    from app.routes.chat import bp as chat_bp
    from app.routes.ai_costs import bp as ai_costs_bp
    from app.routes.folder_manager import bp as folder_manager_bp

    for bp in (
        auth_bp,
        pages_bp,
        stats_bp,
        entities_bp,
        documents_bp,
        transactions_bp,
        import_bp,
        import_jobs_bp,
        import_cloud_bp,
        export_bp,
        tax_review_bp,
        settings_bp,
        analyze_bp,
        users_bp,
        chat_bp,
        ai_costs_bp,
        folder_manager_bp,
    ):
        app.register_blueprint(bp)
