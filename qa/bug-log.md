# Bug Log — tax-ai-analyzer

**Pass date:** 2026-04-24
**Build:** commit `e4b225c`, image sha256:b0d42ef1
**Scope:** See `qa/test-plan.md`

Template (copy-paste):
```
### [ID] <SEV> — <Title>
- **Category**: ...
- **Area**: ...
- **Repro**: ...
- **Expected**: ...
- **Actual**: ...
- **Evidence**: ...
- **Suspected cause**: ...
- **Fix direction**: ...
- **Status**: open
```

---

## CRITICAL

### [CRIT-1] Critical — /api/settings leaks credential values in plaintext
- **Category**: confirmed defect, security
- **Area**: `app/routes/settings.py::api_settings_get` (lines ~20-40)
- **Date**: 2026-04-24
- **Environment**: running container, authed as admin
- **Repro**:
  1. Log in as admin via `/login`
  2. `curl` (or open in browser DevTools) `/tax-ai-analyzer/api/settings`
  3. Observe JSON response
- **Expected**: all sensitive credential values masked (e.g. `"***1234"` or `null`) regardless of which settings key stored them
- **Actual**: `usbank_password`, `usalliance_password`, `imap_password`, `accountant_token`, `gmail_oauth_token`, `plaid_secret` all returned with **full raw value**
- **Evidence**:
  ```
  Does /api/settings leak credential values to the UI?
    llm_api_key:         not returned
    paperless_token:     not returned
    plaid_secret:        (empty string)
    usbank_password:     LEAKS (12 chars)
    usalliance_password: LEAKS (12 chars)
    accountant_token:    LEAKS (32 chars)
    gmail_oauth_token:   LEAKS (649 chars)
  ```
- **Suspected cause**: The mask list at `api_settings_get` only contains `("llm_api_key", "paperless_token", "smtp_pass", "dropbox_token", "s3_secret_key")`. All the importer credential keys and OAuth tokens were added in later sessions without updating this list.
- **Fix direction**:
  1. Replace mask list with a `should_mask(key)` predicate that checks suffixes: `_password`, `_secret`, `_token`, `_key` (minus allowlisted non-secrets like `plaid_client_id`).
  2. Add a test that enumerates all sensitive suffixes and asserts they're masked on readback.
  3. Double-check: the `api_settings_save` pre-existing pattern ("if value starts with `***`, skip update") will continue working — the skip-on-masked pattern only needs the read side masking everything consistently.
- **Status**: open — documented for remediation; do NOT fix in this phase.

---

## HIGH

### [HIGH-1] High — No CSRF protection on any state-mutating POST
- **Category**: security, hardening
- **Area**: Flask app globally (`app/web_ui.py`)
- **Date**: 2026-04-24
- **Repro**: grep shows no `csrf`, `CSRFProtect`, or `flask_wtf` in `app/` (except Dropbox OAuth state, which is also unverified on callback — see HIGH-3).
- **Expected**: CSRF tokens required on: `/login`, `/api/settings`, `/api/transactions/bulk`, `/api/vendors/merge`, `/api/accountant/token`, `/api/mileage`, `/api/transactions/<id>/attach`, all import-related POSTs.
- **Actual**: No CSRF validation anywhere. Combined with `SESSION_COOKIE_SAMESITE = "None"` (set in `app/web_ui.py:30`) any page the admin visits while logged in could issue cross-site POSTs that would succeed.
- **Evidence**:
  ```
  app/cloud_adapters/dropbox_adapter.py:109:    csrf_token_session_key="dropbox_csrf",
  (only Dropbox OAuth helper mentions it — and it's not verified)
  ```
- **Suspected cause**: Framework choice — Flask without Flask-WTF doesn't provide CSRF by default.
- **Fix direction**:
  1. Add `flask-wtf` to requirements and enable `CSRFProtect(app)` on all blueprints.
  2. Inject `{{ csrf_token() }}` into `_head.html` once; expose as a global JS variable; fetch() helper in `_scripts.html` adds `X-CSRFToken` header automatically.
  3. Exempt pure OAuth callback routes and the accountant-portal token-auth path.
  4. Alternative (lighter): change `SESSION_COOKIE_SAMESITE = "Lax"` (default). Since the tool is self-hosted single-admin, SameSite=Lax + session cookie blocks most cross-site POSTs without needing a framework change. Confirm nothing in the product needs cross-site cookie (I don't see iframe/embed use).
- **Status**: open

### [HIGH-2] High — Default admin/admin user with no forced password change
- **Category**: security, defect
- **Area**: `app/db/activity.py::ensure_default_data` (line 119)
- **Date**: 2026-04-24
- **Repro**:
  1. Start container against empty DB volume.
  2. App auto-creates user `admin` with password `admin`.
  3. Nothing prevents continued use or logs in to unused account.
- **Expected**: Either no default user (operator must create on first run), or force password change on first login.
- **Actual**: User `admin / admin` always exists on fresh installs. Log says "CHANGE THIS IMMEDIATELY" but the warning is silent for the user — there's no UI affordance reminding them.
- **Evidence**:
  ```python
  if user_count() == 0:
      create_user("admin", "admin", "admin@localhost", "admin")
      logger.info("Created default admin user (password: admin) — CHANGE THIS IMMEDIATELY")
  ```
- **Suspected cause**: Design convenience for first-run.
- **Fix direction**:
  1. Require operator to set an env var `INITIAL_ADMIN_PASSWORD`, else refuse to create the default user and print setup instructions.
  2. OR: set `user.must_change_password=1` on bootstrap and force a password-change screen after first login.
  3. At minimum: show a prominent in-app banner on every page if `username == "admin"` AND password hash matches `"admin"`.
- **Status**: open

### [HIGH-3] High — Dropbox OAuth state not verified on callback (CSRF in OAuth flow)
- **Category**: security
- **Area**: `app/cloud_adapters/dropbox_adapter.py::complete_auth` (line ~140)
- **Date**: 2026-04-24
- **Repro**: examine the `complete_auth` signature — it takes only `code` and `redirect_uri`, doesn't accept `state`. `get_auth_url_with_state` stores `session['dropbox_csrf'] = state` but it's never read back.
- **Expected**: callback URL must accept `state` from query string and verify against `session['dropbox_csrf']`.
- **Actual**: state is generated, stored, and never checked. OAuth CSRF possible.
- **Evidence**: grep `'dropbox_csrf'` finds only set, no get.
- **Suspected cause**: callback flow was refactored without wiring state verification.
- **Fix direction**: in `complete_auth`, accept `state: str` parameter, compare to `session['dropbox_csrf']`, raise if mismatch; clear on success.
- **Status**: open

### [HIGH-4] High — No inactive-user login block verified
- **Category**: likely defect needing confirmation, coverage gap
- **Area**: `app/auth.py::authenticate_user` — relies on `active=1` filter in SQL
- **Date**: 2026-04-24
- **Repro**: none (both users in DB are admin+active; can't disprove without second user). Code path LOOKS correct: `get_user_by_username` filters `active=1`, and `authenticate_user` does the same.
- **Expected**: user with `active=0` cannot log in.
- **Actual**: cannot verify without a non-admin inactive user in the DB.
- **Fix direction**: create one integration test that (a) creates a standard user, (b) sets active=0, (c) verifies login rejected. Same for `/api/users` admin-required enforcement.
- **Status**: open — coverage gap, likely not a defect

---

## MEDIUM

### [MED-1] Medium — Mileage API accepts invalid dates and infinite miles
- **Category**: confirmed defect, data integrity
- **Area**: `app/db/mileage.py::add_mileage`, `app/routes/mileage.py::api_mileage_create`
- **Date**: 2026-04-24
- **Repro**:
  ```
  POST /api/mileage {"date":"2024-02-30", "miles":10}   → 201 created
  POST /api/mileage {"date":"not-a-date", "miles":10}   → 201 created, tax_year="not-"
  POST /api/mileage {"date":"2024-01-01", "miles":inf}  → 201 created, miles=inf in DB
  ```
- **Expected**: 400 on invalid date format, 400 on non-finite miles.
- **Actual**: all three accepted. Row written to DB as-is.
- **Evidence**: three bad rows were created during QA probing (cleaned up in same pass).
- **Suspected cause**:
  - Route validates miles > 0 but doesn't `math.isfinite(...)`. Python's `float('inf') > 0` is True.
  - Route doesn't validate date via `datetime.strptime(...)` — it just `date[:10]`'s and trusts.
- **Fix direction**:
  1. In `api_mileage_create`: `from math import isfinite; if not isfinite(miles) or miles <= 0: 400`.
  2. Validate date: `datetime.strptime(date, "%Y-%m-%d")` → 400 on ValueError.
  3. `tax_year` should fall back to current year if date is invalid, not first 4 chars of garbage.
- **Status**: open

### [MED-2] Medium — innerHTML interpolation without esc() on entity-card fields
- **Category**: UX / hardening (self-XSS)
- **Area**: `app/templates/dashboard/_scripts.html:195`
- **Date**: 2026-04-24
- **Repro**: create an entity with `name = '<script>alert(1)</script>'` via the Entities tab → reload dashboard → observe alert fires.
- **Expected**: entity name is HTML-escaped when rendered into the dashboard entity cards.
- **Actual**: `e.color`, `e.name`, `sl` interpolated raw into template literal that's assigned to `innerHTML`.
- **Evidence**:
  ```js
  g.innerHTML = ents.length ? ents.map(([sl,e])=>`<div class="entity-card" style="border-left-color:${e.color||'#1a3c5e'};cursor:pointer" title="Filter by ${e.name||sl}" onclick="jumpToEntity(${e.id})">
  ```
- **Suspected cause**: Missing `esc()` wrapper, which is used elsewhere (e.g. `esc(t.vendor)` in txn renderer).
- **Fix direction**: wrap `e.color`, `e.name`, `sl` with `esc(...)` (the app's existing escape helper in `_scripts.html`).
- **Status**: open

### [MED-3] Medium — Orphan row in transaction_links
- **Category**: data integrity
- **Area**: `transaction_links` table
- **Date**: 2026-04-24
- **Repro**: `SELECT * FROM transaction_links WHERE doc_id NOT IN (SELECT id FROM analyzed_documents)` returns 1 row (id=4, txn_id=3798 → doc_id=4758 missing).
- **Expected**: `ON DELETE CASCADE` on doc_id cleans up links when doc is deleted.
- **Actual**: one orphan exists. Likely residue from earlier QA test cleanup that deleted an analyzed_document directly in SQL, outside of the cascade path.
- **Suspected cause**: FK enforcement is off on raw `sqlite3.connect(...)` (default SQLite behavior); `get_connection()` enables `foreign_keys=ON` but direct-DB manipulation scripts bypass it.
- **Fix direction**:
  1. One-off cleanup: `DELETE FROM transaction_links WHERE doc_id NOT IN (SELECT id FROM analyzed_documents) OR txn_id NOT IN (SELECT id FROM transactions)`.
  2. Document in a runbook that any direct SQLite manipulation MUST enable `PRAGMA foreign_keys = ON` at connect time.
  3. Consider adding a weekly scheduled orphan-sweep job alongside the existing dedup scheduler.
- **Status**: open

### [MED-4] Medium — No security HTTP headers
- **Category**: hardening
- **Area**: `app/web_ui.py`
- **Date**: 2026-04-24
- **Repro**: `curl -I` any page → missing `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, `Strict-Transport-Security`, `Referrer-Policy`.
- **Expected**: Sensible defaults set by middleware or `@app.after_request`.
- **Actual**: None.
- **Impact**: Clickjacking, MIME sniffing, minor. Low-risk for single-admin LAN tool.
- **Fix direction**: add an `@app.after_request` handler that injects sensible defaults. Or `flask-talisman`.
- **Status**: open

### [MED-5] Medium — No rate limiting on login or any endpoint
- **Category**: hardening
- **Area**: `app/routes/auth.py::login`, all endpoints
- **Date**: 2026-04-24
- **Expected**: login endpoint rate-limited (e.g. 5/minute per IP); bulk endpoints have sanity limits.
- **Actual**: no limits.
- **Impact**: Brute force on login. Automated abuse of bulk endpoints.
- **Fix direction**: `flask-limiter` on `/login`, `/api/transactions/bulk`, `/api/vendors/merge`.
- **Status**: open

### [MED-6] Medium — Pip dependencies not fully pinned
- **Category**: reproducibility, hardening
- **Area**: `requirements.txt`
- **Date**: 2026-04-24
- **Evidence**: Several deps use `>=` instead of `==`: `plaid-python>=24.0.0`, `playwright-stealth>=2.0.0`, `pdfplumber>=0.11.0`.
- **Impact**: Docker builds drift over time; "it worked yesterday" failures.
- **Fix direction**: pin every top-level dependency to exact versions; add Renovate/Dependabot for managed updates.
- **Status**: open

### [MED-7] Medium — Docker image tag `latest` is mutable
- **Category**: hardening
- **Area**: `docker-compose.yml` (`image: dblagbro/tax-ai-analyzer:latest`)
- **Evidence**: Compose references `:latest`, which is re-published and changes remote digest.
- **Impact**: Pulls on the same day can produce different containers. Complicates rollback.
- **Fix direction**: pin to a dated tag in compose (we already dated `2026-04-23-playwright-step1`); update with each deploy.
- **Status**: open

---

## LOW

### [LOW-1] Low — `print()` statements in production-code paths
- **Category**: observability hygiene
- **Area**: `app/diag_usalliance.py` (20+ `print()` calls)
- **Impact**: Output lands in docker logs without timestamp/level; pollutes structured log scanning.
- **Fix direction**: the file appears to be a one-off diagnostic; convert to `logger.info` OR move outside `app/` into a `scripts/` directory and exclude from image.
- **Status**: open

### [LOW-2] Low — `diag_usalliance.py` lives in `app/` but isn't wired into anything
- **Category**: dead code / clarity
- **Area**: `app/diag_usalliance.py`
- **Impact**: Confusion; it looks like a route but is a standalone script.
- **Fix direction**: move to `scripts/diagnostic/` outside the app package; document in README.
- **Status**: open

### [LOW-3] Low — `tf-cat` ID referenced by JS but not in any template
- **Category**: dead code
- **Area**: `app/templates/dashboard/_scripts.html` references `document.getElementById('tf-cat')` 4 times; no element has `id="tf-cat"` anywhere.
- **Impact**: defensive `?.value || ''` means no runtime error, but the branch is dead.
- **Fix direction**: delete the 4 references OR add the `tf-cat` element if there was a missed intent.
- **Status**: open (noticed in prior QA pass, carried forward)

### [LOW-4] Low — 12 `getElementById()` calls target IDs not in any static template
- **Category**: dead-code / fragility
- **Area**: `app/templates/dashboard/_scripts.html`: `ee-tax-id`, `fo-cov-in`, `fo-cov-out`, `fo-cov-total`, `fo-coverage-body`, `fo-coverage-summary`, `fo-queue-btn`, `fo-year`
- **Notes**: all live in `_modal_paypal.html` which is probably OK (modal only rendered on open). `gm-typing` and `pp-typing` are dynamically created. Worth noting as a fragility vector — if the modal template changes, JS silently breaks.
- **Fix direction**: add a lightweight linter/grep check that runs in CI to catch dead `getElementById` references. Current HTML crawler catches unresolved onclick handlers but not getElementById.
- **Status**: open (enhancement)

---

## ENHANCEMENT / COVERAGE GAPS

### [ENH-1] No standard (non-admin) user for permission tests
- Cannot validate `@admin_required` enforcement on routes without a standard user. Fix: add a fixture that creates/tears down a standard user during tests.

### [ENH-2] No automated test for live Playwright or live API importers
- All 6 Playwright importers (US Alliance, Capital One, US Bank, Merrick, Chime, Verizon) are untested at the live-integration layer. Plaid + SimpleFIN + IMAP also have no live-integration test. Unavoidable without test accounts, but should be tracked.

### [ENH-3] No concurrent-access SQLite test
- WAL mode is on and `check_same_thread=False`, but no test exercises concurrent reads + writes.

### [ENH-4] No monitor / alerting on `/api/health/extended`
- Endpoint exists but no external system polls it.

### [ENH-5] No backup/restore round-trip test
- Backups are created (WAL-checkpointed tarballs), but the restore path has never been exercised. Write a `tests/test_backup_roundtrip.py` that extracts, opens, compares row counts.

### [ENH-6] Docker image not pinned by sha256 digest in compose
- Already tracked as MED-7 but worth tagging as an enhancement for process hygiene.

### [ENH-7] No negative test for bulk edit SQL whitelist
- `_BULK_ALLOWED_FIELDS` is tested for updates but the per-field type coercion (entity_id → int) isn't fully exercised.

### [ENH-8] Endpoints that accept file uploads — no upload-bomb test
- `/api/transactions/<id>/attach` has a 50MB per-file guard; app has `MAX_CONTENT_LENGTH = 64MB`. Not tested at boundary.

### [ENH-9] Accountant portal token has no rotation / expiry
- Generated token is stable until explicitly revoked. No audit log of generation events (actually, there IS — `db.log_activity("accountant_portal_generated")`). No automatic expiry.

### [ENH-10] No client-side validation for numeric form inputs
- Mileage miles, bulk edit entity_id, YoY years — all only server-validated. Bad input shows generic error.

### [ENH-11] No feature flags / gradual rollout
- Recent feature shipments go to "all" immediately. No way to enable-per-user.

### [ENH-12] Activity log has no retention policy
- Grows indefinitely. 4906 rows in 2 months = ~30k/year. Not urgent, but worth a TTL.

---

## Carried forward from previous sessions (verified still present)

### [MED-?] Gmail importer 3774 rows backfilled — spot-check for silent regressions
- `transactions.date` was RFC 2822 for 3774 rows; backfilled to ISO on 2026-04-23. 0 orphans, 0 bad dates, 0 tax_year mismatches in the current DB — the backfill held. No new bug, but worth re-asserting.

### Earlier fixes verified still in place
- `/api/import/gmail/status` — no longer 500 (jsonify import)
- `_tab_entities.html` Cancel buttons — use `closeM`, not `closeModal`
- HTML dump password scrubbing — all `value="..."` attrs say `"[REDACTED]"`
- US Alliance config ported into `base_bank_importer.launch_browser()` (Step 1 from Playwright anti-detect rollout)
