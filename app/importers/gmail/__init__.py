"""Gmail importer — OAuth + month-window message fetch + AI relevance review.

Phase 11H refactor: the original 843-line module is now a package split
across auth / fetch / parse / ai_review / transactions / runner. Public
symbols are re-exported here so existing callers don't change:

  - ``run_import`` — orchestrator (used by routes/import_gmail.py)
  - ``get_auth_url``, ``complete_auth``, ``is_authenticated`` — OAuth
  - ``_ai_review_email``, ``_fast_prefilter``, ``_is_known_pdf``,
    ``_text_to_pdf``, ``upsert_transaction`` — IMAP importer pulls these.
"""
from app.importers.gmail.auth import (
    get_auth_url,
    complete_auth,
    is_authenticated,
    get_credentials,
)
from app.importers.gmail.fetch import _fast_prefilter
from app.importers.gmail.parse import _is_known_pdf, _text_to_pdf
from app.importers.gmail.ai_review import _ai_review_email
from app.importers.gmail.transactions import upsert_transaction
from app.importers.gmail.runner import run_import

__all__ = [
    "run_import",
    "get_auth_url",
    "complete_auth",
    "is_authenticated",
    "get_credentials",
    # IMAP shares these
    "_ai_review_email",
    "_fast_prefilter",
    "_is_known_pdf",
    "_text_to_pdf",
    "upsert_transaction",
]
