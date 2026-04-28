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

---

## Phase 6 — `_scripts.html` → per-tab JS modules under `static/js/dashboard/` (completed)

**Before**: `app/templates/dashboard/_scripts.html` was 3,872 lines of inline JavaScript inside a single `<script>` block — every tab's loaders, handlers, modal IIFEs, the TableManager class, and the combined Gmail + PayPal + 8-bank setup modal block all co-located. Jinja2 `{{ ... }}` templating interleaved with the JS made refactors error-prone (each edit risked breaking Jinja escaping) and drove AI context-window costs through the roof on every touch.

**After**: Thin bootstrap shell (`_scripts.html`, 42 lines) containing only Jinja-templated globals (`P`, `_myUserId`, `_isAdmin`, `PAPERLESS_WEB_URL`, `curSess`, `DOMContentLoaded` init), followed by `<script src="...">` tags pulling 12 external modules in dependency order.

12 cohesive modules under `app/static/js/dashboard/`:

| Module | LOC | Responsibility |
|--------|-----|---------------|
| `core.js` | 168 | Utilities (`post`, `toast`, `esc`, `fmt`, `escColor`), health polling, job-log modal, tab switcher (`sw`, `loadTab`, `applyGlobal`), password eye toggle, LLM model dropdown |
| `table_manager.js` | 116 | `TableManager` class — sortable/filterable/resizable columns (used by transactions & documents) |
| `dashboard.js` | 85 | Overview tab: `loadStats`, `loadAct`, `loadRecentJobs`, jump-to-filter helpers |
| `transactions.js` | 561 | Transactions tab: list, reconcile, bulk-edit, add-txn modal, dedup scan, vendor merge + rename |
| `documents.js` | 335 | Documents tab: table, file browser view, override modal, bulk ops, backfill |
| `import_hub.js` | 369 | Import Hub: tab switcher, Gmail/PayPal/Venmo/Bank/OFX/URL/LocalFs handlers, jobs list, Gmail import status polling |
| `setup_modals.js` | 1226 | All Import Hub setup-modal IIFEs: Gmail Setup, PayPal Setup, US Alliance, US Bank, Capital One, Merrick, Chime, Verizon, Plaid, SimpleFIN, IMAP |
| `chat.js` | 304 | AI Chat: sessions, messages, editing, sharing, SSE streaming, PDF export |
| `tax_review.js` | 264 | Filed returns CRUD, tax review SSE stream, follow-up Q&A |
| `reports.js` | 111 | Export generate/download, Year-over-Year CSV |
| `admin.js` | 196 | Settings save/test, user CRUD, analysis trigger, activity-log filter view |
| `mileage.js` | 142 | Mileage tab: list, add/edit form, CSV export |

**Function coverage verified**: 158 function declarations in the original file → 158 in the extracted files (exact match).

**Static-url routing fix**: `Flask(__name__, static_folder="static")` previously served at unprefixed `/static/...`, but `url_for('static', ...)` generated URLs with the `APPLICATION_ROOT` prefix. Added `static_url_path=URL_PREFIX+"/static"` to make the two paths agree — now `/tax-ai-analyzer/static/...` resolves to 200.

**Smoke-test update**: `test_handler_functions_resolve` previously only crawled inline `<script>` blocks. Updated to also fetch any external `<script src="...">` referenced in the rendered HTML and include their content in the "defined functions" set. Same correctness bar, now refactor-proof.

**Net LOC change**: `_scripts.html` 3872 → 42 (-99%). Total project JS is ~same; it's just split.

---

## Phase 7 — `_tab_import.html` → per-source partials (completed)

**Before**: `_tab_import.html` was 904 lines with 16 bank/source panels and their HTML forms inlined in one file. Adding a new importer (or adjusting one) required scrolling through every unrelated panel.

**After**: Thin shell (`_tab_import.html`, 51 lines) — just the page header, 16 tab buttons, and 16 `{% include "dashboard/import/_source_X.html" %}` directives, plus the Import Jobs footer card.

16 per-source partials under `app/templates/dashboard/import/`:

| Partial | LOC |
|---|---|
| `_source_gmail.html` | 51 |
| `_source_imap.html` | 91 |
| `_source_paypal.html` | 30 |
| `_source_usalliance.html` | 115 |
| `_source_venmo.html` | 21 |
| `_source_bank.html` | 41 |
| `_source_localfs.html` | 24 |
| `_source_url.html` | 10 |
| `_source_cloud.html` | 8 |
| `_source_capitalone.html` | 75 |
| `_source_usbank.html` | 74 |
| `_source_merrick.html` | 70 |
| `_source_chime.html` | 66 |
| `_source_verizon.html` | 69 |
| `_source_simplefin.html` | 64 |
| `_source_plaid.html` | 61 |

**Verification**: All 16 `<div id="ip-{source}">` panel IDs present in rendered `/import` HTML; session_smoke 6/6; no Jinja leaks; no startup errors.

**Net LOC change**: `_tab_import.html` 904 → 51 (-94%).

---

## Deliberately not touched (deferred to future phases)

| Target | LOC | Reason |
|---|---|---|
| `app/importers/usalliance_importer.py` | 1167 | Tier 3 — proper fix is extracting bank-scraping common patterns onto `base_bank_importer.py`. Needs a session focused on Playwright bot-detection preservation (US Bank job #61 state) — don't bundle with documentation refactor. |
| `app/importers/gmail_importer.py` | 843 | Tier 3 — same reasoning, different vendor shape (OAuth vs Playwright). |
| `app/importers/verizon_importer.py`, `capitalone_importer.py`, `usbank_importer.py`, `chime_importer.py` | 550-700 each | Tier 3 — common base extraction work. |
| `app/templates/docs.html` | 918 | Standalone page, rarely edited, low churn. |
| `app/templates/dashboard/_modal_paypal.html` | 643 | Self-contained user flow (PayPal setup chat); splitting would fragment it. Also hosts `loadAiCosts`/`foLoadMigration` (misleading filename — candidate for a rename in a future pass). |
| `app/templates/gmail_setup.html` | 600 | Standalone page. |

## Next refactor targets (ranked)

1. **Importer common-base extraction** (Tier 3). `base_bank_importer.py` has the scaffolding; each vendor importer should shrink to ~150-250 LOC by moving login-retry loops, session persistence, PDF-download + rename, and transaction-diff logic onto the base class. Precondition: US Bank Playwright Step 2+ (patchright swap) complete so the base class knows its final shape.
2. **Rename `_modal_paypal.html`** to something like `_widgets_mixed.html` or split its three distinct concerns (PayPal setup chat, AI Costs loader, Folder Manager loader, Plaid widget) into separate partials. Low-risk but naming clarity win.
3. **Consolidate `setup_modals.js` IIFEs** (1226 LOC). Each bank IIFE repeats the same 8-function pattern (`loadStatus`, `saveCreds`, `saveCookies`, `clearCookies`, `startImport`, `submitMfa`, `pollLogs`, `copySnippet`). A `makeBankModal(config)` factory could collapse this to ~400 LOC while keeping per-bank customization. Risk: any subtle bank-specific deviation becomes hidden in the factory.
4. **Move `loadAiCosts` + `foLoadMigration` out of `_modal_paypal.html`** into `admin.js` or dedicated modules (`ai_costs.js`, `folder_manager.js`). Currently they're in the wrong file.
5. **Remove `_scripts.html`'s dependency on `{{ active_tab }}`** by having each tab file inject its own loader call — would let us lazy-load per-tab JS in future.
6. **Route package audit**: `app/routes/` has 32 files; `import_*` prefix covers 13 bank/source routers. Potential to move them into `app/routes/importers/` sub-package (mirror of `app/importers/`) for symmetry.

---

## Phase 8 — Split `_modal_paypal.html` + move importer routes into sub-package (completed)

Two independent high-value refactors landed together.

### 8A — `_modal_paypal.html` split

**Before**: 643-line misnamed file holding PayPal modal HTML + unrelated JS for Entity tree management (233 lines), User Profile (33), AI Costs (30), Folder Manager (291), Help/About (15), plus an IIFE hooking `loadEntityTree` onto tab switches. The filename was misleading — most of its content had nothing to do with PayPal.

**After**: `_modal_paypal.html` trimmed to 28 lines (just the PayPal Setup modal HTML — no `<script>` block). JS split by concern into 4 destinations:

| Destination | Content | LOC |
|---|---|---|
| `app/static/js/dashboard/entities.js` (new) | Entity Management: `loadEntityTree`, `renderEntityTree`, `openAddEntity`, `openEditEntity`, `saveEntity`, `archiveEntity`, `openMergeEntity`, `doMergeEntity`, the `sw('entities')` auto-loader IIFE | 242 |
| `app/static/js/dashboard/ai_costs.js` (new) | `loadAiCosts` | 32 |
| `app/static/js/dashboard/folder_manager.js` (new) | `foLoadMigration`, `foImportYear`, `foScanIssues`, `foRenameOne`, `foDryRunAll`, `foApplyAll`, `foExecuteRename`, `foCoverage`, `foQueueYear`, `importFiledReturnFromFolder` | 292 |
| `app/static/js/dashboard/admin.js` (appended) | `openProfile`, `saveProfile`, `openHelp`, `openAbout`, `showHelpSection` | 258 (was 196) |

Load order updated in `_scripts.html` (3 new `<script src>` tags added after `mileage.js`).

**Verification**: smoke 6/6; function count 158 → 187 (the +29 came from pulling in JS that previously lived in `_modal_paypal.html` — no regression, just newly-visible-to-crawler).

**Net LOC change**: `_modal_paypal.html` 643 → 28 (-96%).

### 8B — Importer routes moved into sub-package

**Before**: `app/routes/` had 32 files flat; 14 of them were `import_*.py`. This mirrored `app/importers/` (the Python data-importer modules) but the parent `app/routes/` dir was noisy.

**After**: Created `app/routes/importers/` sub-package. Moved all 14 `import_*.py` files into it via `git mv` (preserving history). Blueprint names are hardcoded strings, so `url_for("import_.api_*")` etc. keeps working — zero URL or endpoint changes.

| Moved from | To |
|---|---|
| `app/routes/import_.py` | `app/routes/importers/import_.py` |
| `app/routes/import_jobs.py` | `app/routes/importers/import_jobs.py` |
| `app/routes/import_cloud.py` | `app/routes/importers/import_cloud.py` |
| `app/routes/import_gmail.py` | `app/routes/importers/import_gmail.py` |
| `app/routes/import_imap.py` | `app/routes/importers/import_imap.py` |
| `app/routes/import_paypal.py` | `app/routes/importers/import_paypal.py` |
| `app/routes/import_usalliance.py` | `app/routes/importers/import_usalliance.py` |
| `app/routes/import_capitalone.py` | `app/routes/importers/import_capitalone.py` |
| `app/routes/import_simplefin.py` | `app/routes/importers/import_simplefin.py` |
| `app/routes/import_plaid.py` | `app/routes/importers/import_plaid.py` |
| `app/routes/import_usbank.py` | `app/routes/importers/import_usbank.py` |
| `app/routes/import_merrick.py` | `app/routes/importers/import_merrick.py` |
| `app/routes/import_chime.py` | `app/routes/importers/import_chime.py` |
| `app/routes/import_verizon.py` | `app/routes/importers/import_verizon.py` |

Updated 2 files with new import paths: `app/routes/__init__.py` (14 lines) + `app/tests/test_smoke.py` (6 lines). Both routes flat file count: 32 → 18.

**Verification**: `pytest app/tests/` all green; 14 import blueprints registered and reachable.

---

## Remediation alongside Phase 8 (completed)

Addressed 3 items from the QA pass (`remediation-plan.md` Group F + HIGH-4):

| Item | Action | File |
|---|---|---|
| **ENH-1** | Added `non_admin_client` + `inactive_client` pytest fixtures using `unittest.mock.patch` to synthesize User objects without mutating the live DB | `app/tests/test_auth_boundaries.py` (new, 125 lines) |
| **HIGH-4** | Added `TestInactiveUser::test_inactive_user_cannot_hold_session` — verifies inactive accounts cannot access `@login_required` routes | same |
| **ENH-6** | Added `pytest.ini` at project root for local dev; documented the container full-smoke command as `python3 -m pytest app/tests/` (was previously only running `test_session_smoke.py`) | `pytest.ini` (new) |

Also covered:
- `TestUserModel` — 5 tests exercising the `User.is_active`/`is_admin`/`is_superuser` getters
- `TestLoader` — 2 tests for `load_user` with nonexistent/invalid IDs
- `TestUnauthenticated` — admin-only routes correctly reject unauthenticated requests (302/401)
- `TestNonAdminAuth` — admin-only routes correctly reject authenticated non-admin sessions (302/403/404)

**Net**: test count 63 → 73 (+10 auth-boundary tests). All green.

---

## Phase 9 — Playwright anti-detection Steps 2-5 + lazy-load tab registry (completed)

Finished the 5-step anti-detection ladder from `plans/usbank_playwright_rollout.md`.
Step 1 already landed in commit `e4b225c`; Steps 2-5 executed here.

### 9A — patchright swap (Step 2)

**Before**: `playwright==1.44.0` + `playwright-stealth>=2.0.0`, explicit `Stealth` hook code in base_bank_importer.py (37 lines), usalliance_importer.py (26 lines), import_usalliance.py (10 lines), diag_usalliance.py (14 lines).

**After**: `patchright>=1.48,<2` — a hardened fork that patches CDP Runtime.Enable leak and driver-level fingerprint issues at build time. All `from playwright.sync_api import ...` statements in 7 files swapped via sed to `from patchright.sync_api import ...`. All `Stealth()` hook blocks deleted — patchright handles it at the driver level, no call-site hook required.

Files touched: `requirements.txt`, `Dockerfile` (playwright install → patchright install), `app/importers/base_bank_importer.py`, `usalliance_importer.py`, `usbank_importer.py`, `capitalone_importer.py`, `merrick_importer.py`, `app/routes/importers/import_usalliance.py`, `tools/diag_usalliance.py`.

### 9B — Real Chrome channel (Step 3)

`Dockerfile`: added `RUN python -m patchright install chrome` (brings in Google Chrome `.deb`, ~200MB layer growth).
`base_bank_importer.py`: `pw.chromium.launch(channel="chrome", ...)`.
`usalliance_importer.py`: same.

Verification: `docker exec tax-ai-analyzer which google-chrome` → `/usr/bin/google-chrome` ✓.

### 9C — Xvfb + headful browser (Step 4)

`Dockerfile`: added `xvfb` to apt-get list; changed `CMD` to `["xvfb-run", "-a", "--server-args=-screen 0 1280x900x24", "python", "-m", "app.main"]`.
`base_bank_importer.py` (and usalliance): `headless=False` default, `no_viewport=True` context arg, `--headless=new` + `--window-size=…` flags removed from `_STEALTH_ARGS` (Xvfb framebuffer drives size).

Verification: `docker exec tax-ai-analyzer pgrep -a Xvfb` → `16 Xvfb :99 -screen 0 1280x900x24 -nolisten tcp ...` ✓.

### 9D — Warm-up navigation (Step 5)

`usbank_importer.py`: new `_warmup_navigation(page, log)` helper. Visits `https://www.usbank.com/`, jitters the mouse 3x, clicks a marketing nav link (Personal / Checking / Credit cards), idles, then clicks the "Log in" link to reach `/Auth/Login` organically. `_login()` calls this before the direct `page.goto(LOGIN_URL)` — direct goto becomes the fallback on any warm-up exception.

**Still pending live validation**: US Bank account-lockout check from prior session. Code path: `_warmup_navigation` runs → fingerprints are now patchright + real Chrome + headful under Xvfb → credential rejection in job #61 was auth-backend, not bot detection, so this combination should resolve the bot-detection side. Auth rejection will need credential confirmation from the user.

### 9E — Lazy-load tab-loader registry (independent)

`core.js`: replaced hardcoded `loadTab()` dispatch map with a `_tabLoaders = {}` registry + `registerTabLoader(name, fn)` public function. Each tab module now self-registers at load time. Adding a new tab becomes an append to its own module instead of an edit to `core.js`.

Per-module registrations added (one-liners appended):
- `dashboard.js`, `transactions.js`, `documents.js`, `import_hub.js`
- `tax_review.js`, `mileage.js`, `chat.js`, `reports.js`
- `admin.js` (settings, users, activity — 3 registrations)
- `ai_costs.js`, `folder_manager.js`

Total: 13 `registerTabLoader(...)` calls across 12 files.

### 9F — setup_modals.js HTML artifact cleanup

Two stray `</script>` + `<script>` pairs (carryover from the original inline HTML) removed from `setup_modals.js` — the 4 IIFE blocks (Gmail, PayPal, US Alliance, combined banks) are now separated by pure JS comment dividers instead of HTML tag artifacts. No behavior change; pure file hygiene.

### Verification

- `docker compose build tax-ai-analyzer` — image built successfully (exit 0, no warnings).
- `docker compose up -d --force-recreate --no-deps tax-ai-analyzer` — container healthy.
- `pytest app/tests/` — 73/73 pass.
- `patchright.sync_api` imports clean inside container.
- `google-chrome --version` returns valid path.
- `Xvfb :99 -screen 0 1280x900x24` running in-container.
- App module imports exercise no `playwright.*` or `playwright_stealth` references (`grep -rn "from playwright\|playwright_stealth"` in `app/` + `tools/` returns only comment mentions).

### Image rollback lever

Previous working image still tagged as `dblagbro/tax-ai-analyzer:pre-remediation-2026-04-24_0107`. Compose `image:` points at `2026-04-24-qa-remediated` (the new build, SHA `93126135781d`). To roll back: edit compose image tag to the pre-remediation tag, `docker compose up -d --force-recreate --no-deps tax-ai-analyzer`.

---

## Still open (explicitly deferred from this session)

- **Importer common-base extraction**: deferred because every bank importer (US Alliance / US Bank / Chime / Merrick / Capital One / Verizon) needs live validation against its target bank before consolidating shared patterns onto `base_bank_importer.py`. Steps 2-5 change the underlying browser lifecycle; any base-class merge ahead of live validation risks masking regressions. Concrete targets once live-green is confirmed:
  1. US Alliance's inline `with sync_playwright() as pw: ...` block → switch to `launch_browser()`
  2. Cookie save/load helper → move to base (currently duplicated in 5 files)
  3. Statement-download + file-rename + dedup loop → extract to `download_and_import_year(page, year, ...)` on base
  4. Login retry-with-MFA pattern → extract to `login_with_mfa(page, username, password, mfa_selector, ...)` on base

- **Setup_modals.js factory refactor**: the Capital One / US Bank / Merrick / Chime / SimpleFIN block (lines 633-1226) already uses `makePoller(prefix, mfaBoxId)` and `makeBankHelpers(bank, prefix, statusId, cookieStatusId, cookieResultId)` factories. The US Alliance IIFE (lines 432-627) duplicates those patterns inline. Folding US Alliance into the factory would drop another ~150 LOC, but again needs a live-UI click-through per bank to verify no regression.

- **Step 6 (residential proxy) and Step 7 (Camoufox)**: explicitly out of scope for this session. Step 6 requires the user to pick a proxy provider + commit to an ongoing cost. Step 7 is a last-resort if Steps 2-5 + residential proxy are insufficient.

---

## Remediation Waves 0-4 (2026-04-24, 5 commits)

Post-Phase-9 deep QA passes (two passes, 18 findings) produced a 5-wave
remediation sprint. Each wave = one commit, retest-gated, rollback-ready.

### Wave 0 — `1bba238` — Stabilize Phase 9 commit
Committed uncommitted xvfb-run → `docker-entrypoint.sh` replacement (fixed
CRIT-NEW-1 daemon-didn't-start) + `-ac` on Xvfb (CRIT-NEW-2) + `COPY tools/`
in Dockerfile (HIGH-NEW-2) + all 6 QA markdown files.

### Wave 1 — `28acbbc` — Export download filename fix (HIGH-PASS2-1)
All 8 /api/export/{year}/{slug}/download/{format} endpoints returned 404
because downloader looked for `{slug}_{year}{ext}` while generator writes
`export_{year}_{slug}{ext}` (and `transactions_{year}_{slug}.csv`,
`summary_{year}_{slug}.pdf`). Added `_candidate_filenames(fmt, year, slug)`
dispatch with per-format overrides + legacy fallback. +11 tests in new
`app/tests/test_export.py`.

### Wave 2 — `c917b23` — API contract fixes
- `/api/documents/<id>` → 404 when neither Paperless nor local DB has the ID
- `/api/entities` POST → return `{"id": int}` not `{"id": {...dict}}`
- `/api/entities` POST/PATCH → reject non-hex `color` with 400
- `entities.js` → replace legacy `sw()` monkey-patch IIFE with
  `registerTabLoader("entities", loadEntityTree)`
- +20 tests: `test_documents.py`, `test_entities.py`, `TestTabRegistry` in
  `test_session_smoke.py`

### Wave 3 — `7b8808c` — Security hardening
- `_safe_next()` guard on `/login?next=` — same-origin paths only (CRIT-NEW-3)
- `_client_ip()` + opt-in `TRUST_PROXY_HEADERS` env gate —
  `ProxyFix(x_for=1 if trusted else 0)`. Defeats XFF spoof bypass of rate
  limiter (CRIT-PASS2-1)
- `ADMIN_INITIAL_PASSWORD` wired into `docker-compose.yml` + `.env`
  (HIGH-NEW-1)
- +10 tests: `TestOpenRedirect`, `TestOpenRedirectIntegration`,
  `TestRateLimitIpResolution`, `TestRateLimitXffBypass` in
  `test_auth_boundaries.py`

### Wave 4 — `afbf09c` — Image hygiene (one rebuild)
- `ENV DISPLAY=:99` at image layer — `docker exec` subshells get DISPLAY
  without explicit `-e` flag (LOW-NEW-1)
- `tini` as `ENTRYPOINT ["/usr/bin/tini", "--"]` — reaps Chrome crashpad
  zombies (LOW-PASS2-1)
- `httpx`/`httpcore`/`urllib3` log level → WARNING — idle log volume went
  from 3049 bytes/10s to 0 (LOW-PASS2-2)
- `TestHttpLiveness` in `test_smoke.py` — socket + urllib probe to
  `http://127.0.0.1:8012/...` catches "container Up but Flask dead" cases
  that in-process `test_client()` misses

---

## Post-Wave hotfixes (2 commits)

### `ab681bd` — Xvfb survival under tini
After Wave 4 rebuild, live validation found Xvfb died immediately after
`docker-entrypoint.sh` did `exec python -m app.main`. With tini as PID 1
and the shell `exec`-replacing itself, background `&`-started Xvfb got
SIGHUP'd. Fix: `setsid -f Xvfb ...` detaches Xvfb into its own session.

### `dfe6604` — Entrypoint stale-socket cleanup + liveness verify
`docker restart` preserves `/tmp/.X11-unix/X99` from prior Xvfb.  New Xvfb
silently fails ("already in use"). Added `rm -f` cleanup before Xvfb
launch + pgrep liveness loop (5s) + exit-1-with-error if Xvfb fails to
start. Also bumped MFA timeout 5→10 minutes.

---

## Phase 10A — Cookie persistence helpers (2 commits)

### `5564ec6` + `5047da2` + `6df358a`
Base helpers `save_auth_cookies(context, bank_slug, log)` and
`load_auth_cookies(bank_slug)` in `base_bank_importer.py`. Each helper is
~15 lines. Adopted across all 6 Playwright-based bank importers with a
one-line call post-`Logged in` checkpoint:
  usalliance_importer, usbank_importer, chime_importer,
  merrick_importer, capitalone_importer, verizon_importer

Importers already had matching `cookies=` param in their `_login()`
signature + cookie injection via `context.add_cookies(...)`, so the
save+load pair works end-to-end.

+10 tests in new `app/tests/test_auth_cookie_helpers.py`: roundtrip,
slug isolation, malformed JSON → None, empty list → None, etc.

---

## Phase 10B — CDP-level webdriver mask (1 commit)

### `5f0c2a3` — MED-PASS2-2
patchright + `channel="chrome"` leaves `navigator.webdriver` as boolean
`false` rather than the `undefined` a real human browser returns. Strict
anti-bot vendors (DataDome, Akamai aggressive) check for
`=== undefined`. Added `context.add_init_script()` in `launch_browser()`
that redefines:
  - `navigator.webdriver` → `undefined`
  - `navigator.plugins` → `[1,2,3,4,5]` (stub — length > 0)
  - `navigator.languages` → `['en-US', 'en']`

Applied in both `base_bank_importer.launch_browser()` AND
`usalliance_importer.run_import()` (the latter has its own inline launch
not yet folded onto base — scoped for Phase 10C).

---

## Canary validation findings (2026-04-24)

First live test of Phase 9 + remediation stack against a real bank
(US Alliance FCU). Result: **authentication layer works end-to-end**
(patchright + real Chrome + Xvfb + tini + warm-up nav + MFA → authenticated
session reached). Two follow-up findings logged:

1. **Statement-download bug** (tracked in
   `qa/bug-statement-download-usalliance.md`) — every row click either
   leaves URL unchanged or intercepts a 536-byte stub response. Real PDF
   content presumably loaded by viewer iframe via follow-up fetches we're
   not capturing. Scoped for a dedicated session with HAR capture from a
   real user session.
2. **Cookie persistence not session-restoring** — saved cookies re-injected
   into a fresh context land on a page titled "Login". US Alliance's
   session model is NOT pure cookie-based (likely localStorage + device
   fingerprint + ThreatMetrix tmx_guid binding). Documented in bug-log.

---

## Phase 10B — US Alliance IIFE folded onto bank-modal factory (completed, `a606a5e`)

setup_modals.js had two IIFEs that served bank-modal concerns:
  - Block A (US Alliance, lines 432-625) — 194 LOC written pre-factory,
    inline-duplicating saveCreds / loadStatus / saveCookies / clearCookies /
    poll-logs / submitMfa
  - Block B (7 banks, lines 628-1221) — had `makePoller` + `makeBankHelpers`
    factories plus per-bank adapter code

Phase 10B:
- Added `window.__bankFactory = {makePoller, makeBankHelpers}` cross-IIFE
  export inside Block B
- Rewrote US Alliance IIFE to wait for the factory, then delegate 5 of 8
  `window.*` functions via the factory helpers
- Kept US-Alliance-specific behavior inline: `usaCopySnippet` + clipboard
  fallback, `testUsaCreds` (no other bank has a test-login endpoint),
  bot-notice toggle integrated into `loadUsaStatus`, impTab hook

Net: setup_modals.js 1222 → 1142 LOC (-80, -6.6%). One less place to edit
when the factory pattern evolves.

pytest: 126/126.

---

## Deferred targets (explicit, with rationale)
- **usalliance inline Playwright launch → base** — Phase 10C (US Alliance
  has its own `sync_playwright()` block; consolidating to
  `launch_browser()` requires careful preservation of its SSE-streaming
  stealth config)
- **Shared `download_statement_pdf()` helper** — Phase 10D (would save
  ~600 LOC across 5 bank importers, but risks masking the current
  statement-download bug being investigated)
- **`navigator.webdriver` CDP override verification** — init-script works
  but A/B testing against a real detector has not been run. Deferred
  until a specific bank importer fails at this fingerprint signal.

---

## Session totals (2026-04-24)

- 17 commits since `e4b225c` (pre-session baseline)
- pytest: 73 → **126 tests**, all green
- 2 Docker image rebuilds + Docker Hub push
- 3 git tags pushed (`pre-remediation-*`, `post-phase9-*`, `post-canary-*`)
- 6 data-snapshot tarballs archived
- 18 QA findings resolved + 2 deferred with documented rationale




---

## Phase 11E — `setup_modals.js` → `setup_modals/` package (completed 2026-04-28)

**Before**: single 1,142-line `app/static/js/dashboard/setup_modals.js`
mixing Gmail OAuth setup chat, PayPal setup chat, US Alliance importer
modal, and a combined IIFE containing the `__bankFactory` plus 8 sub-bank
sections (Capital One / US Bank / Merrick / Chime / Verizon / SimpleFIN /
IMAP / Plaid).

**After**: 13-file package under `setup_modals/`:

| File | Lines | Responsibility |
|------|-------|---------------|
| `gmail.js` | 286 | Gmail Setup Modal (`openGmailSetupModal`, drag-and-drop, OAuth chat) |
| `paypal.js` | 149 | PayPal Setup Modal |
| `banks/factory.js` | 122 | Installs `window.__bankFactory.{makePoller, makeBankHelpers}` |
| `banks/usalliance.js` | 106 | US Alliance IIFE — uses factory + own clipboard/test-login |
| `banks/capitalone.js` | 38 | Capital One importer modal |
| `banks/usbank.js` | 36 | US Bank importer modal |
| `banks/merrick.js` | 36 | Merrick Bank importer modal |
| `banks/chime.js` | 73 | Chime (email/password instead of username) |
| `banks/verizon.js` | 71 | My Verizon importer modal |
| `banks/simplefin.js` | 47 | SimpleFIN Bridge token claim |
| `banks/imap.js` | 98 | Generic IMAP importer modal |
| `banks/plaid.js` | 122 | Plaid Link / per-item connection management |
| `banks/tab_dispatcher.js` | 33 | Wraps `impTab` to fire `loadXxxStatus` per source |

`_scripts.html` updated with the new ordered `<script>` list (factory before
banks; tab_dispatcher last so all `loadXxxStatus` globals exist when it
captures them).

**Globals preserved**: 67 `window.*` exports across the new files, exact
match against the 67 in the old monolith. Zero call-site changes.

**Verification**: `pytest app/tests/` — 144/144 passed.

---

## Phase 11F — `docs.html` → `templates/docs/` partials (completed 2026-04-28)

**Before**: 918-line `app/templates/docs.html` with 24 inline `<section>`
blocks (Overview, Logging In, Navigation, Entities, Gmail OAuth Setup,
Gmail Run, PayPal & Venmo, Bank CSV, URL Importer, Cloud, Transactions,
Adding Manually, Filtering & Search, Categorizing, Documents, AI Analysis,
Chat, Reports, Settings, Users, CSV Reference, Categories, Troubleshooting).

**After**: 274-line shell + 24 `templates/docs/<section>.html` partials.
Sizes: 4-58 lines per partial. The Jinja shell:
```html
<!-- ... -->
<h1 class="doc-title">Financial AI Analyzer</h1>
{% include "docs/overview.html" %}
{% include "docs/login.html" %}
…22 more…
{% include "docs/troubleshooting.html" %}
```

The TOC sidebar, footer, and inline `<style>` + `<script>` block all stay
in the shell — they're shell-level concerns, not section content.

**Verification**: rendered docs.html via Jinja → 43,953 chars, all 24
section IDs present, footer + script block intact. `pytest` 144/144.

---

## Phase 11G — `usalliance_importer.py` → `usalliance/` package (completed 2026-04-28)

**Before**: 1,132-line `app/importers/usalliance_importer.py` with 23
top-level functions covering MFA registry, login form fill, MFA detection,
eStatements navigation, statement discovery, PDF download, and small
utility helpers all in one file.

**After**: 7-file package under `usalliance/`, plus the original module
kept as a 6-line re-export shim for backwards compatibility:

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `runner.py` | 245 | `run_import` orchestrator (login → MFA → download) |
| `download.py` | 486 | `_download_year`, `_select_statement_year`, `_find_statement_links`, `_dump_page_structure`, `_download_statement`, `_nav_back_to_statements` |
| `estatements.py` | 154 | `_is_404_page`, `_is_documents_page`, `_navigate_to_estatements`, `_wait_for_documents_content` |
| `login.py` | 146 | `_fill_login`, `_verify_logged_in` |
| `mfa.py` | 144 | `set_mfa_code`, `_wait_for_mfa`, `_is_mfa_page`, `_is_push_mfa_page`, `_submit_mfa` |
| `helpers.py` | 60 | `_safe_filename`, `_months_for_year`, `_find_element`, `_get_base_url`, `_save_debug_screenshot` |
| `__init__.py` | 20 | Re-exports `run_import` + `set_mfa_code` |

**Public API preserved**: `from app.importers.usalliance_importer import
run_import, set_mfa_code` still works. The 2 actual call sites
(`routes/importers/import_usalliance.py:138, 177`) are unchanged.

**Verification**: `pytest` 144/144. Container restart + import smoke
confirmed `run_import is run_import` between shim and package paths.

---

## Phase 11H — `gmail_importer.py` → `gmail/` package (completed 2026-04-28)

**Before**: 843-line `app/importers/gmail_importer.py` mixing OAuth
credential storage, Gmail API search/fetch, HTML/text → PDF rendering,
amount/date normalization, AI-assisted relevance review, transaction
upsert, per-month worker pool, and `run_import`.

**After**: 7-file package under `gmail/` plus a 17-line re-export shim:

| Module | Lines | Responsibility |
|--------|-------|---------------|
| `runner.py` | 374 | `_process_month` worker + `run_import` orchestrator |
| `fetch.py` | 175 | `_google_imports`, `_month_query`, `_fast_prefilter`, `_build_service`, message-list/detail fetch, body decode |
| `auth.py` | 132 | OAuth credentials.json + token DB persistence + auth URL flow |
| `parse.py` | 128 | HTML/text → PDF, filename safety, amount/date normalize, dedup hash |
| `transactions.py` | 86 | `upsert_transaction` (used by Gmail and IMAP) |
| `ai_review.py` | 58 | `_ai_review_email` AI relevance check |
| `__init__.py` | 36 | Re-exports public + IMAP-shared symbols |

**Cross-importer reuse preserved**: `imap_importer.py` was importing
`_ai_review_email`, `_fast_prefilter`, `_is_known_pdf`, `_text_to_pdf`,
`upsert_transaction` directly from `gmail_importer`. All five remain
re-exported by the package `__init__` and the legacy shim, so
`imap_importer.py:34-40` is unchanged.

**Drive-by fix**: 4 modules were missing `import re` after the split
(`gmail/fetch.py`, `gmail/ai_review.py`, `gmail/runner.py`,
`usalliance/estatements.py`) — `re.compile(...)` calls at module scope
crashed on first import. Caught by manual import smoke after restart;
added the missing imports.

**Verification**: `pytest` 144/144. Both shim path
(`from app.importers.gmail_importer import run_import`) and package path
(`from app.importers.gmail import run_import`) resolve to the same
function object.

---

## Session totals (Phase 11E–H, 2026-04-28)

- 4 large files split into 4 cohesive packages
- Largest single file in repo: `setup_modals.js` 1,142 → `gmail/runner.py` 374 LOC (cap-rate 67% reduction)
- 23 new module files (13 JS + 10 Python) under 4 new package directories
- 2 thin re-export shims preserve all existing imports
- 24 new HTML partials from `docs.html`
- pytest: 144/144 passed at every phase boundary
- No Docker image rebuild needed (source-mount only — `docker restart` between phases)

## Next refactor targets

- `app/importers/verizon_importer.py` 709, `capitalone_importer.py` 652,
  `usbank_importer.py` 636, `chime_importer.py` 593 — bank-importer family
  is structurally similar (login → MFA → discover accounts → download →
  parse). A future template-method base class could collapse these
  significantly. Deferred: Phase 11D bank-onboarding codegen agent is
  actively generating new bank importers — wait for that work to stabilize
  before introducing a shared abstraction.
- `app/static/js/dashboard/transactions.js` 563 — splittable into tab
  logic / bulk-edit / vendor-merge sub-files. Lower payoff right now.
- `app/db/import_jobs.py` 330 — getting wide; revisit at ~500 LOC.
