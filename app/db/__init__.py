"""
Financial AI Analyzer — database package.

All public symbols are re-exported here so existing callers using
``from app import db; db.something()`` or ``from app.db import something``
continue to work without modification.
"""

from app.db.core import DB_PATH, get_connection, init_db

from app.db.users import (
    authenticate_user,
    create_user,
    delete_user,
    get_user_by_id,
    get_user_by_username,
    list_users,
    update_user,
    user_count,
)

from app.db.entities import (
    archive_entity,
    create_entity,
    ensure_tax_year,
    get_entities,
    get_entity,
    get_entity_dict,
    get_entity_tree,
    get_user_entity_access,
    list_entities,
    list_entity_access,
    list_tax_years,
    merge_entities,
    revoke_user_entity_access,
    set_user_entity_access,
    update_entity,
    update_tax_year_status,
)

from app.db.documents import (
    delete_many_analyzed_documents,
    find_duplicate_analyzed_docs,
    flag_duplicate_analyzed_docs,
    get_analyzed_doc_ids,
    get_analyzed_documents,
    get_financial_summary,
    get_years_with_docs,
    is_near_duplicate_analyzed_doc,
    list_filed_returns,
    mark_document_analyzed,
    pdf_hash_exists,
    pdf_hash_stats,
    record_pdf_hash,
    set_paperless_tags_applied,
    update_many_analyzed_documents,
    upsert_filed_return,
)

from app.db.transactions import (
    add_transaction,
    count_transactions,
    delete_many_transactions,
    get_transaction,
    get_transaction_summary,
    get_transactions,
    list_transactions,
    update_many_transactions,
    update_transaction,
    upsert_transaction,
)

from app.db.mileage import (
    IRS_MILEAGE_RATES,
    add_mileage,
    delete_mileage,
    get_mileage,
    irs_rate_for_year,
    list_mileage,
    mileage_summary,
    update_mileage,
)

from app.db.import_jobs import (
    append_import_job_log,
    create_import_job,
    create_url_poller,
    delete_credential,
    delete_import_job,
    delete_url_poller,
    get_credential,
    get_import_job,
    get_import_job_logs,
    get_import_jobs,
    gmail_processed_stats,
    is_gmail_message_processed,
    list_credentials,
    list_import_jobs,
    list_url_pollers,
    record_gmail_message,
    save_credential,
    prune_old_import_jobs,
    update_import_job,
    update_url_poller_poll,
)

from app.db.chat import (
    add_chat_message,
    append_chat_message,
    create_chat_session,
    delete_chat_session,
    get_chat_messages,
    get_chat_session,
    get_chat_sessions,
    get_chat_shares,
    list_chat_sessions,
    search_chat_sessions,
    share_chat_session,
    truncate_messages_from,
    unshare_chat_session,
    update_chat_session_title,
)

from app.db.settings import (
    delete_setting,
    get_all_settings,
    get_setting,
    get_settings,
    save_settings,
    set_setting,
)

from app.db.activity import (
    distinct_activity_actions,
    ensure_default_data,
    get_activity_log,
    get_recent_activity,
    log_activity,
    search_activity,
)

from app.db.bank_onboarding import (
    add_generated_importer,
    add_recording,
    approve_generated_importer,
    create_pending_bank,
    delete_pending_bank,
    get_generated_importer,
    get_pending_bank,
    get_pending_bank_by_slug,
    get_recording,
    list_generated_importers,
    list_pending_banks,
    list_recordings,
    update_pending_bank,
)
