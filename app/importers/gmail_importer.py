"""Re-export shim — see ``app/importers/gmail/`` for the implementation.

Kept so existing callers (``from app.importers.gmail_importer import ...``)
keep working without code changes.
"""
from app.importers.gmail import (  # noqa: F401
    run_import,
    get_auth_url,
    complete_auth,
    is_authenticated,
    get_credentials,
    _ai_review_email,
    _fast_prefilter,
    _is_known_pdf,
    _text_to_pdf,
    upsert_transaction,
)
