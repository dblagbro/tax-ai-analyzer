# Refactor Log

## Phase 1 — Deduplicate db.py (completed)

**Problem**: `app/db.py` (2,090 lines) contained 3 pairs of duplicate function definitions. Python silently uses the last definition, making the first copies permanently dead code.

**Dead code removed**:
- `update_import_job` (lines ~998-1012): first version lacked `message→error_msg` / `progress→count_imported` kwarg mapping
- `get_import_job` (lines ~1030-1039): first version returned sqlite3.Row instead of dict
- `list_chat_sessions` (lines ~1165-1182): first version lacked `include_shared` parameter and share JOIN

**Approach**: kept the second (richer) definition of each, deleted the first.

---

## Phase 2 — db.py → db/ package (completed)

**Before**: single 2,090-line file mixing schema management, auth logic, entity CRUD, document tracking, transaction storage, import job management, chat history, settings, and activity logging.

**After**: 9 focused modules under `app/db/`:

| Module | Responsibility |
|--------|---------------|
| `core.py` | `get_connection()`, `init_db()`, `_migrate()`, all CREATE TABLE / ALTER TABLE |
| `users.py` | User CRUD, password hashing |
| `entities.py` | Entity/tax-year CRUD, merge, access control |
| `documents.py` | Analyzed doc records, dedup, PDF hash store |
| `transactions.py` | Financial transaction CRUD |
| `import_jobs.py` | Import job records, credentials, URL pollers, Gmail dedup tracking |
| `chat.py` | Chat sessions, messages, sharing |
| `settings.py` | Key-value runtime settings |
| `activity.py` | Activity log, `ensure_default_data()` bootstrap |

`__init__.py` re-exports all 60+ public functions — zero callers changed.

**Circular import**: `ensure_default_data()` in `activity.py` calls functions from `users.py` and `entities.py`. Resolved by using local (inside-function) imports rather than top-level imports.

---

## Phase 3 — llm_client.py → llm_client/ package (completed)

**Before**: single 805-line file mixing vocabulary constants, system prompt strings, model fallback chains, API client logic, normalization helpers, and singleton management.

**After**: 3 focused modules under `app/llm_client/`:

| Module | Responsibility |
|--------|---------------|
| `vocab.py` | `VALID_DOC_TYPES`, `VALID_CATEGORIES`, `ANTHROPIC_FALLBACK_CHAIN`, `OPENAI_FALLBACK_CHAIN` |
| `prompts.py` | `ANALYSIS_SYSTEM`, `EXTRACTION_SYSTEM`, `CHAT_SYSTEM_TEMPLATE`, `SUMMARY_SYSTEM` |
| `client.py` | `LLMClient` class, `get_client()` singleton, convenience functions |

`__init__.py` re-exports all public symbols — zero callers changed.

---

## Phase 4 — web_ui.py → routes/ Blueprints (completed)

**Before**: single 3,568-line Flask file with 130 routes, 4 mutable globals, helper decorators, context processors, error handlers, all mixed together.

**After**: thin `web_ui.py` (app factory, ~100 lines) + `app/routes/` package with 15 Blueprint modules.

**Verification**: route coverage confirmed identical — old app: 120 routes, new app: 120 routes, diff empty.

| Blueprint | Routes |
|-----------|--------|
| `auth` | login, logout |
| `pages` | 13 SPA shell page routes |
| `stats` | api_stats, activity, health, filed returns, years |
| `entities` | entity CRUD, tax years, user profile |
| `documents` | document list/detail/override/recategorize/dedup |
| `transactions` | transaction CRUD |
| `import_` | all import sources: Gmail, PayPal, US Alliance, OFX, local FS, cloud adapters, import job management |
| `export_` | export generate, download, list |
| `tax_review` | SSE tax review stream + followup |
| `settings` | settings CRUD, LLM/Paperless test endpoints, LLM model list |
| `analyze` | manual analysis trigger and status |
| `users` | user admin CRUD, entity-access management |
| `chat` | chat session management, SSE streaming, PDF export, sharing |
| `ai_costs` | LLM usage statistics |
| `folder_manager` | tax archive folder tooling |

**Shared infrastructure**:
- `_state.py`: `_job_logs`, `_chat_stop_events`, `_job_stop_events`, `_is_analyzing`, `append_job_log()`
- `helpers.py`: `admin_required`, `superuser_required`, `_url()`, `_row_list()`, `_no_cache_page()`, `_user_can_access_session()`, `_user_can_write_session()`

**Breaking change**: `login_manager.login_view` changed from `"login"` to `"auth.login"` (required by Blueprint namespacing). Verified Flask-Login respects the Blueprint-qualified endpoint name correctly.

**Preserved**: `web_ui_monolith.py` — the original file kept as reference. Safe to delete after confirming no regressions in production.

---

## Phase 5 — dashboard.html → partials (completed)

**Before**: single `app/templates/dashboard.html` — 5,023 lines mixing CSS, topbar/sidebar HTML, 12 tab panels, 8 modal dialogs, 2,334 lines of JavaScript, and a PayPal setup modal.

**After**: thin `dashboard.html` shell (31 lines) + `app/templates/dashboard/` package with 16 focused partials.

| Partial | Lines | Content |
|---------|-------|---------|
| `_head.html` | 393 | `<head>` block: all CSS / style declarations |
| `_topbar_sidebar.html` | 126 | Topbar nav + sidebar navigation |
| `_tab_dashboard.html` | 45 | Dashboard overview: stat cards, entities, activity, recent jobs, filed returns |
| `_tab_transactions.html` | 52 | Transaction list + sub-tabs + filters |
| `_tab_documents.html` | 52 | Document table + file browser view |
| `_tab_import.html` | 324 | Import Hub: Gmail, PayPal, US Alliance, Venmo, OFX, Bank CSV, Local FS, URL, Cloud |
| `_tab_chat.html` | 70 | AI chat UI + Chat Share modal |
| `_tab_tax_review.html` | 83 | Tax review form, filed return entry, SSE output + Q&A thread |
| `_tab_reports.html` | 34 | Export cards per entity/year + existing files table |
| `_tab_entities.html` | 151 | Entity tree + Add/Edit modal + Merge modal |
| `_tab_settings.html` | 70 | LLM / Paperless / SMTP / S3 settings form |
| `_tab_users.html` | 17 | User admin table |
| `_tab_folder_manager.html` | 52 | Archive migration status + folder naming issues |
| `_tab_ai_costs.html` | 49 | AI cost stats: by model, by operation, daily, recent calls |
| `_modals.html` | 516 | Shared modals: Job Log, Add Txn, Add User, Reset Pw, Gmail Setup, User Profile, Help, About, Classification, Filed Return |
| `_scripts.html` | 2,334 | Main JS + US Alliance FCU importer JS |
| `_modal_paypal.html` | 643 | PayPal Setup modal HTML + its JS |

**Verification**: `render_template("dashboard.html", ...)` inside `tax-ai-analyzer` container renders 289,115 bytes. All 18 structural markers (tab IDs, modal IDs, `</body>`, `</html>`) confirmed present.

---

## Incremental Refactor #1 (completed)

Three targeted improvements to maintainability and module boundaries.

### 1 — Deleted `web_ui_monolith.py` (3,568 lines)

The original monolith had been kept as a fallback reference after Phase 4. With all 120 routes verified in production and Phase 5 template rendering confirmed, it was dead code. Removed.

### 2 — Split `routes/import_.py` (1,290 → 876 lines) into 3 files

| File | Lines | Responsibility |
|------|-------|----------------|
| `routes/import_.py` | 876 | Gmail, PayPal, US Alliance, CSV/OFX/URL/LocalFS — transactional import sources |
| `routes/import_jobs.py` | 74 | Job CRUD, log polling, cancel — orthogonal to import source choice |
| `routes/import_cloud.py` | 375 | GDrive, Dropbox, S3, filed-return AI extraction — remote storage adapters |

**Why this split**: Job management (debugging a stuck job, polling logs, cancelling) is a distinct operator concern from import source orchestration. Cloud adapters are already fully abstracted in `app/cloud_adapters/`; the route file is a thin HTTP wrapper and deserved its own home. Adding a new cloud adapter now only touches `import_cloud.py`.

`url_for("import_.api_gdrive_callback")` references updated to `url_for("import_cloud.api_gdrive_callback")` in the auth routes — URL path unchanged, only the Flask endpoint name changed.

### 3 — Moved `_apply_business_rules` from `main.py` to `checks/financial_rules.py`

`main.py` is the process entry point (daemon orchestration). It was exporting domain classification logic that `routes/analyze.py` imported via `from app.main import _apply_business_rules` — a route importing from the entry point is a layering violation.

- **Moved to**: `app/checks/financial_rules.py` as `apply_business_rules()` (now public)
- **Updated callers**: `main.py` and `routes/analyze.py` both now import from `checks.financial_rules`
- **`main.py`**: 353 → 303 lines
- **`checks/financial_rules.py`**: 256 → 311 lines

**Verification**: All 6 changed modules import cleanly in container. All 5 tested endpoints present in `app.url_map`. `apply_business_rules` logic confirmed correct (proposal override test passes).
