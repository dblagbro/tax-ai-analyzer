"""Blueprint registry — call register_blueprints(app) from the app factory."""


def register_blueprints(app):
    from app.routes.auth import bp as auth_bp
    from app.routes.pages import bp as pages_bp
    from app.routes.stats import bp as stats_bp
    from app.routes.entities import bp as entities_bp
    from app.routes.documents import bp as documents_bp
    from app.routes.transactions import bp as transactions_bp
    from app.routes.importers.import_ import bp as import_bp
    from app.routes.importers.import_jobs import bp as import_jobs_bp
    from app.routes.importers.import_cloud import bp as import_cloud_bp
    from app.routes.importers.import_gmail import bp as import_gmail_bp
    from app.routes.importers.import_imap import bp as import_imap_bp
    from app.routes.importers.import_paypal import bp as import_paypal_bp
    from app.routes.importers.import_usalliance import bp as import_usalliance_bp
    from app.routes.importers.import_capitalone import bp as import_capitalone_bp
    from app.routes.importers.import_simplefin import bp as import_simplefin_bp
    from app.routes.importers.import_plaid import bp as import_plaid_bp
    from app.routes.importers.import_usbank import bp as import_usbank_bp
    from app.routes.importers.import_merrick import bp as import_merrick_bp
    from app.routes.importers.import_chime import bp as import_chime_bp
    from app.routes.importers.import_verizon import bp as import_verizon_bp
    from app.routes.accountant import bp as accountant_bp
    from app.routes.mileage import bp as mileage_bp
    from app.routes.reports import bp as reports_bp
    from app.routes.vendors import bp as vendors_bp
    from app.routes.export_ import bp as export_bp
    from app.routes.tax_review import bp as tax_review_bp
    from app.routes.settings import bp as settings_bp
    from app.routes.analyze import bp as analyze_bp
    from app.routes.users import bp as users_bp
    from app.routes.chat import bp as chat_bp
    from app.routes.ai_costs import bp as ai_costs_bp
    from app.routes.folder_manager import bp as folder_manager_bp
    from app.routes.bank_onboarding import bp as bank_onboarding_bp

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
        import_gmail_bp,
        import_imap_bp,
        import_paypal_bp,
        import_usalliance_bp,
        import_capitalone_bp,
        import_simplefin_bp,
        import_plaid_bp,
        import_usbank_bp,
        import_merrick_bp,
        import_chime_bp,
        import_verizon_bp,
        accountant_bp,
        mileage_bp,
        reports_bp,
        vendors_bp,
        export_bp,
        tax_review_bp,
        settings_bp,
        analyze_bp,
        users_bp,
        chat_bp,
        ai_costs_bp,
        folder_manager_bp,
        bank_onboarding_bp,
    ):
        app.register_blueprint(bp)
