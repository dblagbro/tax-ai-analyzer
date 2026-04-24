# Architecture

## Overview

Financial AI Analyzer is a single-process Flask application that runs inside Docker. It connects to a Paperless-ngx instance for document storage and uses Anthropic/OpenAI APIs for AI analysis.

## Process layout

```
main.py
├── Starts Flask (web_ui.app) in a thread
├── Runs the analysis daemon loop (polls Paperless, analyzes docs)
└── Runs a daily dedup scan thread
```

## Module map

```
app/
├── web_ui.py           — Flask app factory; registers Blueprints and error handlers
├── main.py             — Entry point; daemon orchestration
├── config.py           — All env/config constants
├── auth.py             — Flask-Login User model + authenticate/load_user helpers
├── state.py            — JSON file-backed document processing state
├── vector_store.py     — Simple in-process vector search (RAG)
├── paperless_client.py — HTTP client for Paperless-ngx REST API
├── categorizer.py      — Rule-based doc classification (wraps LLMClient)
├── extractor.py        — Financial data extraction (wraps LLMClient)
├── folder_manager.py   — Tax archive folder consistency tooling
├── llm_usage_tracker.py— SQLite-backed LLM call/cost log
│
├── db/                 — SQLite database package
│   ├── core.py         — Connection, schema init, migrations
│   ├── users.py        — User CRUD + password hashing
│   ├── entities.py     — Entity/tax-year CRUD + access control
│   ├── documents.py    — Analyzed doc records + dedup + PDF hash store
│   ├── transactions.py — Financial transaction CRUD
│   ├── import_jobs.py  — Import job records, credentials, URL pollers, Gmail dedup
│   ├── chat.py         — Chat sessions, messages, sharing
│   ├── settings.py     — Key-value runtime settings store
│   ├── activity.py     — Activity log + DB bootstrap (ensure_default_data)
│   └── __init__.py     — Re-exports all public symbols (backwards-compatible)
│
├── llm_client/         — AI provider abstraction package
│   ├── vocab.py        — Valid doc types/categories + fallback model chains
│   ├── prompts.py      — System prompt strings (ANALYSIS_SYSTEM, CHAT_SYSTEM_TEMPLATE…)
│   ├── client.py       — LLMClient class; Anthropic + OpenAI with fallback chains
│   └── __init__.py     — Re-exports all public symbols
│
├── routes/             — Flask Blueprint modules (one domain per file)
│   ├── importers/      — Import-source route package (moved from flat routes/ in Phase 8)
│   │   ├── import_.py, import_jobs.py, import_cloud.py, import_gmail.py,
│   │   ├── import_imap.py, import_paypal.py, import_usalliance.py,
│   │   ├── import_capitalone.py, import_simplefin.py, import_plaid.py,
│   │   ├── import_usbank.py, import_merrick.py, import_chime.py,
│   │   └── import_verizon.py
│   ├── _state.py       — Shared in-process mutable globals (job logs, stop events)
│   ├── helpers.py      — Shared decorators, helper functions, setup_chat_stream SSE factory
│   ├── __init__.py     — register_blueprints(app) wiring function
│   ├── auth.py         — /login, /logout
│   ├── pages.py        — SPA shell routes (render dashboard.html per tab)
│   ├── stats.py        — /api/stats, /api/activity, /api/health, filed returns
│   ├── entities.py     — /api/entities/*, /api/user/profile
│   ├── documents.py    — /api/documents/*
│   ├── transactions.py — /api/transactions/*
│   ├── import_.py      — /api/import/*: CSV (PayPal/Venmo/Bank), URL, OFX, LocalFS
│   ├── import_jobs.py  — /api/import/jobs/*: job CRUD, log polling, cancel
│   ├── import_cloud.py — /api/cloud/*: GDrive, Dropbox, S3; /api/filed-returns/import-from-folder
│   ├── import_gmail.py — /api/import/gmail/*, /import/gmail/*: OAuth + import
│   ├── import_paypal.py— /api/import/paypal/*: API pull + setup chat
│   ├── import_usalliance.py— /api/import/usalliance/*: Playwright scraper
│   ├── export_.py      — /api/export/*, /export/<year>/<slug>
│   ├── tax_review.py   — /api/tax-review (SSE streaming)
│   ├── settings.py     — /api/settings/*, LLM/Paperless test endpoints
│   ├── analyze.py      — /api/analyze/trigger, /api/analyze/status
│   ├── users.py        — /api/users/*, user entity-access management
│   ├── chat.py         — /api/chat/sessions/* (SSE streaming, sharing, PDF export)
│   ├── ai_costs.py     — /api/ai-costs/*
│   └── folder_manager.py— /api/folder-manager/*
│
├── importers/          — Data source importers (one per source)
│   ├── csv_runner.py   — parse_csv() + run_csv_job() shared by all CSV import routes
│   ├── gmail_importer.py
│   ├── paypal_api.py
│   ├── usalliance_importer.py
│   ├── ofx_importer.py
│   └── local_fs.py
│
├── export/             — Export formatters
│   ├── csv_exporter.py, pdf_report.py, quickbooks.py, ofx_exporter.py, txf_exporter.py
│   └── __init__.py     — export_all() orchestrator
│
├── cloud_adapters/     — Optional cloud storage backends
│   ├── google_drive.py, dropbox_adapter.py
│
├── checks/             — Deterministic classification rules
│   └── financial_rules.py  — validate_document(), check_*, apply_business_rules()
│
├── static/             — Served at {URL_PREFIX}/static/ via Flask static_url_path config
│   └── js/dashboard/   — Dashboard JS modules (Phase 6 extraction)
│       ├── core.js             — Utilities, health polling, job-log modal, tab switcher
│       ├── table_manager.js    — Sortable/filterable/resizable column class
│       ├── dashboard.js        — Overview tab: stat cards, activity, recent jobs, jump helpers
│       ├── transactions.js     — Transactions tab: list, reconcile, bulk-edit, vendor merge
│       ├── documents.js        — Documents tab: table, file browser, override modal
│       ├── import_hub.js       — Import Hub tab: source selectors, jobs list, Gmail import polling
│       ├── setup_modals.js     — Gmail/PayPal/all bank setup modal IIFEs
│       ├── chat.js             — AI Chat tab: sessions, messages, sharing, PDF export
│       ├── tax_review.js       — Tax Review tab: filed returns, SSE stream, Q&A followups
│       ├── reports.js          — Reports tab: export generate/download, Year-over-Year
│       ├── admin.js            — Settings, Users, Analysis trigger, Activity-log filter view, Profile, Help/About (Phase 8)
│       ├── mileage.js          — Mileage tab
│       ├── entities.js         — Entity Management tab (Phase 8, from _modal_paypal.html)
│       ├── ai_costs.js         — AI Costs tab (Phase 8)
│       └── folder_manager.js   — File Organizer tab (Phase 8)
│
└── templates/          — Jinja2 templates
    ├── dashboard.html  — SPA shell (31-line wrapper; all content via {% include %})
    ├── dashboard/      — Tab and modal partials for dashboard.html
    │   ├── _head.html              — CSS / <head> block
    │   ├── _topbar_sidebar.html    — Topbar nav + sidebar
    │   ├── _tab_dashboard.html     — Overview tab
    │   ├── _tab_transactions.html  — Transactions tab
    │   ├── _tab_documents.html     — Documents tab
    │   ├── _tab_import.html        — Import Hub shell (51 lines; per-source panels via {% include %}, Phase 7)
    │   ├── _tab_chat.html          — AI Chat tab + share modal
    │   ├── _tab_tax_review.html    — Tax Review tab
    │   ├── _tab_reports.html       — Reports & Exports tab
    │   ├── _tab_entities.html      — Entity Management tab + modals
    │   ├── _tab_settings.html      — Settings tab
    │   ├── _tab_users.html         — User Admin tab
    │   ├── _tab_folder_manager.html— File Organizer tab
    │   ├── _tab_ai_costs.html      — AI Costs tab
    │   ├── _modals.html            — Shared modals (job log, txn, users, Gmail, profile, help, about…)
    │   ├── _scripts.html           — Thin bootstrap (~42 lines): Jinja globals + ordered <script src="..."> (Phase 6)
    │   ├── _modal_paypal.html      — PayPal Setup modal HTML only (trimmed to 28 lines in Phase 8; JS moved to entities.js/ai_costs.js/folder_manager.js/admin.js)
    │   └── import/                 — Per-source Import Hub panels (Phase 7)
    │       ├── _source_gmail.html, _source_imap.html, _source_paypal.html
    │       ├── _source_usalliance.html, _source_capitalone.html, _source_usbank.html
    │       ├── _source_merrick.html, _source_chime.html, _source_verizon.html
    │       ├── _source_simplefin.html, _source_plaid.html, _source_venmo.html
    │       ├── _source_bank.html, _source_localfs.html, _source_url.html
    │       └── _source_cloud.html
    ├── login.html
    ├── gmail_setup.html
    └── docs.html
```

## Data flow: document analysis

```
Paperless-ngx → paperless_client.get_all_document_ids()
              → get_document(id) → content text
              → categorizer.categorize() → LLMClient → doc_type, category, entity, amount
              → extractor.extract()    → LLMClient → date, vendor, amounts
              → db.mark_document_analyzed() → SQLite analyzed_documents
              → vector_store.index_document() → in-memory embeddings
              → paperless_client.apply_tags() → Paperless tags
```

## Persistence

| Store | Purpose |
|-------|---------|
| SQLite (`/app/data/analyzer.db`) | All structured data: users, entities, documents, transactions, jobs, chat, settings |
| JSON state file (`/app/data/state_default.json`) | Set of already-processed Paperless doc IDs |
| Filesystem (`/app/data/`) | Gmail OAuth tokens, credentials.json |
| Paperless-ngx PostgreSQL | Original document storage (not owned by this app) |

## Authentication

Flask-Login with bcrypt-hashed passwords. Three roles: `admin`, `standard`, and optionally `superuser`. All API routes require `@login_required`; admin-only routes add `@admin_required`.

## Streaming responses

Chat (`/api/chat/sessions/<id>/send`) and tax review (`/api/tax-review`) use Server-Sent Events (SSE) via Flask `Response(stream_with_context(...), mimetype="text/event-stream")`. Import job logs are polled (not streamed) via `/api/import/jobs/<id>/logs`.

Stop signals for active chat streams are stored in `_state._chat_stop_events` (dict of `session_id → threading.Event`). Stop signals for import jobs use `_state._job_stop_events`.
