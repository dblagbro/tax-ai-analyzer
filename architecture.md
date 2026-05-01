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
│   ├── client.py       — LLMClient class: pool-walk → direct vendor SDK fallback (Phase 12)
│   ├── lmrh.py         — LMRH header builder per LMRH 1.0 spec (Phase 12)
│   ├── proxy_manager.py — Multi-endpoint pool from llm_proxy_endpoints DB
│   │                     table; 3-failure / 60-s circuit breaker; builds
│   │                     OpenAI-compat AND native Anthropic clients (Phase 12)
│   ├── proxy_call.py   — High-level call_chat() / call_anthropic_messages() /
│   │                     get_streaming_anthropic_client(); LMRH hint injection;
│   │                     captures LLM-Capability + X-LMRH-Warnings response
│   │                     headers and surfaces them in logs (Phase 12)
│   └── __init__.py     — Re-exports all public symbols
│
├── ai_agents/          — Admin-triggered codegen pipelines (Phase 11D-F)
│   ├── har_analyzer.py — HAR digester: strips noise hosts + redacts password/
│   │                     OTP/SSN values + surfaces login POSTs + download URLs
│   ├── bank_codegen.py — Anthropic call with prompt caching on a ~30k-token
│   │                     reference template; supports regenerate-with-feedback
│   │                     via parent_generated_id + feedback kwargs
│   ├── importer_validator.py — 3-layer AST validation (compile / shape / base
│   │                     imports). NEVER exec's the source.
│   └── importer_deployer.py — Writes approved+validated source to
│                         app/importers/<slug>_importer.py with a deploy-marker
│                         first line. Refuses to clobber hand-written importers.
│
├── routes/             — Flask Blueprint modules (one domain per file)
│   ├── _state.py       — Shared in-process mutable globals (job logs, stop events)
│   ├── helpers.py      — Shared decorators, helper functions, setup_chat_stream SSE factory
│   ├── __init__.py     — register_blueprints(app) wiring function
│   ├── auth.py         — /login, /logout (+ rate limiter + _safe_next open-redirect guard, Wave 3)
│   ├── pages.py        — SPA shell routes (render dashboard.html per tab)
│   ├── stats.py        — /api/stats, /api/activity, /api/health, filed returns
│   ├── entities.py     — /api/entities/*, /api/user/profile (+ hex-color validator, Wave 2)
│   ├── documents.py    — /api/documents/* (+ 404 branch for missing ids, Wave 2)
│   ├── transactions.py — /api/transactions/*
│   ├── export_.py      — /api/export/*, /export/<year>/<slug> (filename-dispatch reformat, Wave 1)
│   ├── tax_review.py   — /api/tax-review (SSE streaming)
│   ├── settings.py     — /api/settings/* (+ suffix-based credential mask, Wave A)
│   ├── analyze.py      — /api/analyze/trigger, /api/analyze/status
│   ├── users.py        — /api/users/*, user entity-access management
│   ├── chat.py         — /api/chat/sessions/* (SSE streaming, sharing, PDF export)
│   ├── ai_costs.py     — /api/ai-costs/*
│   ├── folder_manager.py— /api/folder-manager/*
│   ├── bank_onboarding.py — /api/admin/banks/*: queue + recordings + codegen +
│   │                       approve + deploy + regenerate (Phase 11A-F)
│   ├── llm_proxies.py  — /api/admin/llm-proxies/* + /api/admin/llm-hints/*:
│   │                     proxy CRUD with live test + per-task LMRH hint
│   │                     overrides (Phase 13)
│   ├── accountant.py   — /accountant/* (token-scoped read-only view)
│   ├── mileage.py      — /api/mileage/* (+ isfinite + ISO-date validation, Wave-A MED-1)
│   ├── reports.py      — /api/reports/*
│   ├── vendors.py      — /api/vendors/*
│   │
│   └── importers/      — Import-source route sub-package (Phase 8B)
│       ├── __init__.py — package marker
│       ├── import_.py            — /api/import/*: CSV, URL, OFX, LocalFS
│       ├── import_jobs.py        — /api/import/jobs/*: job CRUD + log polling + cancel
│       ├── import_cloud.py       — /api/cloud/*: GDrive, Dropbox, S3 (+ OAuth state verify, Wave B)
│       ├── import_gmail.py       — /api/import/gmail/*: OAuth + import
│       ├── import_imap.py        — /api/import/imap/*: generic IMAP
│       ├── import_paypal.py      — /api/import/paypal/*: API pull + setup chat
│       ├── import_usalliance.py  — /api/import/usalliance/*: Playwright scraper
│       ├── import_capitalone.py  — /api/import/capitalone/*
│       ├── import_simplefin.py   — /api/import/simplefin/*
│       ├── import_plaid.py       — /api/import/plaid/*
│       ├── import_usbank.py      — /api/import/usbank/*
│       ├── import_merrick.py     — /api/import/merrick/*
│       ├── import_chime.py       — /api/import/chime/*
│       ├── import_verizon.py     — /api/import/verizon/*
│       └── import_auto.py        — /api/import/auto/<slug>/*: GENERIC
│                                    dispatcher that resolves the importer
│                                    module via importlib.import_module() —
│                                    used by all auto-deployed importers (Phase 11E)
│
├── importers/          — Data source importers (one per source)
│   ├── base_bank_importer.py  — Shared Playwright launch, CAPTCHA handling,
│   │                            MFA registry, `save_auth_cookies()` +
│   │                            `load_auth_cookies()` helpers (Phase 10A)
│   ├── mfa_registry.py — OTP-code bucket keyed by job_id, populated by user POSTs
│   ├── entity_router.py — Rule-based entity tagger for imported docs
│   ├── csv_runner.py   — parse_csv() + run_csv_job() shared by all CSV import routes
│   ├── bank_csv.py     — Generic bank CSV parser
│   │
│   ├── gmail/          — (Phase 11H) Gmail OAuth + month-window fetch + AI review
│   │   ├── runner.py   — _process_month worker + run_import orchestrator
│   │   ├── fetch.py    — Gmail API connect + message-list/detail fetch
│   │   ├── auth.py     — OAuth credentials.json + token DB persistence
│   │   ├── parse.py    — HTML/text→PDF, filename, amount/date, dedup hash
│   │   ├── transactions.py — upsert_transaction (also used by IMAP importer)
│   │   ├── ai_review.py — _ai_review_email AI relevance check
│   │   └── __init__.py — Re-exports public + IMAP-shared symbols
│   ├── gmail_importer.py — Re-export shim → gmail/
│   │
│   ├── usalliance/     — (Phase 11G) US Alliance Playwright statement importer
│   │   ├── runner.py   — run_import orchestrator
│   │   ├── download.py — Per-year statement discovery + PDF download
│   │   ├── estatements.py — eStatements navigation + readiness checks
│   │   ├── login.py    — Login form fill + post-login verification
│   │   ├── mfa.py      — MFA code exchange + page detection
│   │   ├── helpers.py  — Filename / month / element-find / debug screenshot
│   │   └── __init__.py — Re-exports run_import + set_mfa_code
│   ├── usalliance_importer.py — Re-export shim → usalliance/
│   │
│   ├── paypal_api.py, paypal_importer.py
│   ├── usbank_importer.py, chime_importer.py, merrick_importer.py,
│   ├── capitalone_importer.py, verizon_importer.py (all use base launch_browser)
│   ├── plaid_importer.py, simplefin_importer.py, imap_importer.py, venmo_importer.py
│   ├── ofx_importer.py, local_fs.py
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
│       ├── setup_modals/       — (Phase 11E) Per-source Import Hub setup modals
│       │   ├── gmail.js         — Gmail Setup Modal (OAuth chat + manual upload)
│       │   ├── paypal.js        — PayPal Setup Modal
│       │   └── banks/
│       │       ├── factory.js   — Installs window.__bankFactory.{makePoller,makeBankHelpers}
│       │       ├── usalliance.js — US Alliance importer (uses factory + own clipboard/test-login)
│       │       ├── capitalone.js, usbank.js, merrick.js
│       │       ├── chime.js, verizon.js
│       │       ├── simplefin.js, imap.js, plaid.js
│       │       └── tab_dispatcher.js — Wraps impTab to fire loadXxxStatus per source
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
    ├── docs.html       — User manual shell (Phase 11F: 274-line shell + 24 partials)
    └── docs/           — (Phase 11F) Per-section docs partials
        ├── overview.html, login.html, navigation.html, entities.html
        ├── gmail.html, gmail-setup.html, gmail-run.html, paypal.html
        ├── bank.html, url-import.html, cloud.html
        ├── transactions.html, txn-add.html, txn-filter.html, txn-categorize.html
        ├── documents.html, ai-analysis.html
        ├── chat.html, reports.html, settings.html, users.html
        ├── csv-formats.html, categories.html, troubleshooting.html
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

## Container runtime (Phase 9 + Wave 4)

The container process tree:

```
tini (PID 1) — reaps zombie Chrome crashpad helpers, forwards SIGTERM
  └── docker-entrypoint.sh (briefly)
        ├── Xvfb :99 (detached via setsid -f; -ac disables access control)
        └── python -m app.main (replaces the shell via `exec`)
```

Key files:
- `Dockerfile` — python:3.11-slim base + Xvfb + tini + real Google Chrome via `patchright install chrome` + `ENV DISPLAY=:99`.
- `docker-entrypoint.sh` — cleans stale `/tmp/.X11-unix/X99` socket, starts Xvfb via `setsid -f`, verifies Xvfb is alive (5s pgrep loop), then `exec python -m app.main`.
- `tools/` — ops scripts COPY'd into the image at build time (`diag_usalliance.py`, `diag_usalliance_statement_dom.py`). Not auto-invoked; run via `docker exec`.

## Bank importer anti-detection stack (Phase 9)

All Playwright-based bank importers (US Bank, Chime, Merrick, Capital One, Verizon, US Alliance) use the following stack, shared via `base_bank_importer.launch_browser()`:

- `patchright` (hardened Playwright fork) — patches CDP Runtime.Enable leak at driver level
- `channel="chrome"` — real Google Chrome 147, not bundled Chromium
- `headless=False` + Xvfb framebuffer — headful browser is a much lower fingerprint
- `no_viewport=True` — screen size from Xvfb, not a fixed viewport arg
- `context.add_init_script(...)` — redefines `navigator.webdriver` to `undefined`, stubs `plugins` + `languages` (MED-PASS2-2)
- Auto-save of `context.cookies()` post-successful-login via `save_auth_cookies(context, bank_slug, log)` — next run can skip MFA

## Security posture (post-remediation Waves)

- `SESSION_COOKIE_SAMESITE=Lax` + `HttpOnly` + `Secure` (HIGH-1)
- `SESSION_COOKIE_SECURE=True` — cookies only over HTTPS; localhost works due to browser secure-context carve-out
- `@app.after_request` adds 5 headers: CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy (MED-4)
- In-memory rate limiter: 10 failed logins per IP per 5 min; XFF spoofing defeated by ProxyFix gating on `TRUST_PROXY_HEADERS` env var (CRIT-PASS2-1)
- `/login?next=...` sanitized via `_safe_next()` — only same-origin paths accepted (CRIT-NEW-3)
- `ADMIN_INITIAL_PASSWORD` env-var gate on fresh-DB bootstrap (HIGH-2 / HIGH-NEW-1)
- `/api/settings` credential mask uses suffix-based predicate (`_password|_pass|_secret|_token|_key`) — not a hardcoded allow-list (CRIT-1)

## LLM proxy chain (Phase 12 + 13)

All LLM traffic — analyze, extract, classify, summarize, chat, tax_review,
codegen, gmail/ai_review, cloud-import doc analysis — routes through:

1. **The proxy pool** (one or more `llm_proxy_endpoints` rows). Default pool
   has a single endpoint: `https://www.voipguru.org/llm-proxy2/v1` (public
   URL only — local-access URLs are explicitly rejected by
   `_normalize_llm_proxy_url`). Per-call LMRH hint emitted via the
   `LLM-Hint` HTTP header per the LMRH 1.0 spec
   (https://www.voipguru.org/llm-proxy2/lmrh.md). Proxy picks model+provider;
   we never hardcode model names per operation.
2. **Direct vendor SDK** (Anthropic / OpenAI) as last-resort fallback when
   the entire pool is exhausted. `proxy_call.NoProxyAvailable` signals the
   caller to drop to direct SDK.

Per-task hints (in `app/llm_client/lmrh.py:TASK_PRESETS`):

| Task          | cost     | extras                                   |
|---------------|----------|------------------------------------------|
| analysis      | standard | safety-min=3                             |
| extraction    | economy  |                                          |
| classification| economy  |                                          |
| chat          | premium  |                                          |
| reasoning     | premium  | cascade=auto                             |
| tax-review    | premium  | cascade=auto                             |
| summarize     | standard |                                          |
| codegen       | premium  | context-length=60000                     |
| vision        | standard |                                          |

Operator can override any task's hint via `db.set_setting("lmrh.hint.<task>")`
or via the LLM Routing admin tab. Response headers `LLM-Capability` and
`X-LMRH-Warnings` are captured + logged for diagnostics
(`proxy_call._log_lmrh_diagnostics`).

Circuit breaker (`proxy_manager`): 3 failures within window → 60-s cooldown
per endpoint. Process-local state. Reset via the admin tab's "Reset" button
or `mark_endpoint_success()`.

## Bank-onboarding pipeline (Phase 11A-F)

Self-service for adding new banks. Stages (each owned by a separate module):

```
User uploads HAR + narration
        │
        ▼
[bank_onboarding.py] queue + validation
        │
        ▼
[har_analyzer.py] strip noise + redact secrets → compact prompt summary
        │
        ▼
[bank_codegen.py] Anthropic call via proxy_call (prompt-cached reference template)
        │
        ▼
[importer_validator.py] AST: compile / shape / base-imports
        │
        ▼  (admin reviews + approves)
        │
        ▼
[importer_deployer.py] write app/importers/<slug>_importer.py with deploy marker
        │
        ▼
[routes/importers/import_auto.py] /api/import/auto/<slug>/* dispatcher
        │
        ▼  (admin starts import)
        │
        ▼
[<slug>_importer.py via base_bank_importer.launch_browser()] Playwright run
```

Re-iteration loop: admin types corrective notes → POST
`/api/admin/banks/<id>/generated/<gen_id>/regenerate` → new draft chained
via `parent_id` column with `feedback_text` preserved. Previous draft is
not deleted.

## Browser engine dispatch (Step 6 + 7)

`base_bank_importer.launch_browser(bank_slug)` resolves the engine via:

1. `db.get_setting(f"{slug}_browser_engine")` ∈ {`chrome`, `firefox`}
2. `db.get_setting("default_browser_engine")`
3. env `BROWSER_ENGINE`
4. Default `chrome`

`chrome` → `_launch_patchright()` (real Chrome via patchright + Xvfb).
`firefox` → `_launch_camoufox()` (hardened Firefox fork; binary cached in
the image). Camoufox has a different fingerprint surface than Chromium —
last-resort when Chrome paths are detected by a specific bank.

Optional residential proxy (Step 6): `<slug>_proxy_url` setting plumbs
through to both engines via Playwright's `proxy={server,username,password}`
config. Provider-agnostic (any HTTP/SOCKS5 URL).
