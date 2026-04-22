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

## Pending

- **Phase 5**: Split `templates/dashboard.html` (~5,000 lines) into `templates/base.html` + `templates/dashboard/` tab partials using Jinja `{% include %}`.
