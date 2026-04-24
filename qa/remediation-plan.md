# Remediation Plan — tax-ai-analyzer

**Derived from:** `qa/bug-log.md` (2026-04-24 QA pass)
**Status:** Not started. Awaiting user review before implementation.

## Release-blocker classification

| Issue | Release-blocker? | Rationale |
|---|---|---|
| CRIT-1 (settings leaks credentials) | **YES** | If this app were ever exposed beyond trusted LAN, full credential dump via a single authenticated API call. On a single-user home install, still leaks to DevTools/browser extensions/screenshare. |
| HIGH-1 (no CSRF) | YES for public deploy; NO for LAN-only | Depends on threat model. SameSite=Lax mitigates most practical CSRF on LAN. |
| HIGH-2 (admin/admin default) | **YES** for any deploy | Even LAN-exposed. Trivial to fix (env-var requirement). |
| HIGH-3 (Dropbox CSRF) | NO (low real-world impact; user initiates flow) | But cheap to fix. |
| HIGH-4 (no permission test) | NO | Coverage gap; no evidence of defect. |
| MED-1..7 | NO | Quality improvements, not blockers. |
| LOW-1..4 | NO | Cosmetic / dead code. |
| ENH-1..12 | NO | Process / test gap. |

## Groups by subsystem + severity

### Group A — Credential safety (CRIT + HIGH-2)
- [CRIT-1] mask all credential values in `/api/settings`
- [HIGH-2] enforce initial admin password via env var OR force-change-on-first-login
**Retest after fix**: re-run `qa/scripts/qa_sec.py` sections [6] and new sections for credential-mask enumeration. Verify `api_settings_get()` returns `***` for every key ending in `_password`, `_secret`, `_token`, `_key`.

### Group B — CSRF / session hardening (HIGH-1, HIGH-3, MED-4, MED-5)
- [HIGH-1] CSRF tokens OR SameSite=Lax (decide with user)
- [HIGH-3] Verify OAuth state on Dropbox callback
- [MED-4] Add security headers via `@app.after_request`
- [MED-5] Rate limit `/login` + bulk endpoints
**Retest after fix**: smoke test full login→dashboard→logout flow; test all previously-working forms still submit; add CSRF-rejection tests to `test_session_smoke.py`.

### Group C — Data integrity (MED-1, MED-3)
- [MED-1] Validate date format and finite miles in mileage API
- [MED-3] One-off orphan cleanup; document FK pragma requirement for direct DB access; consider weekly orphan-sweep job
**Retest after fix**: re-run `qa/scripts/qa_sec.py` section [8]; add date-validation + isfinite tests.

### Group D — UI hardening (MED-2)
- [MED-2] Wrap entity name/color/slug in `esc()` in entity-card HTML
**Retest after fix**: extend HTML crawler with "innerHTML interpolation without esc" grep.

### Group E — Dev hygiene / reproducibility (MED-6, MED-7, LOW-1, LOW-2, LOW-3, LOW-4)
- [MED-6] Pin requirements.txt exactly
- [MED-7] Stop using `:latest` in compose — use dated tags
- [LOW-1] Convert `diag_usalliance.py` to logger or relocate
- [LOW-2] Move diag script outside `app/`
- [LOW-3] Delete unused `tf-cat` references
- [LOW-4] Add linter for dead `getElementById()` refs
**Retest after fix**: `pytest app/tests/` full pass; fresh `docker compose build` works; HTML crawler picks up the new linter.

### Group F — Test coverage (all ENH-*)
- [ENH-1] Add non-admin user fixture
- [ENH-2] Document manual-integration-test checklist per importer
- [ENH-3] Concurrent SQLite write test
- [ENH-5] Backup/restore round-trip test
- [ENH-7/8] Bulk-edit + upload boundary tests
- Others: prioritize as needed
**Retest**: expanded smoke suite must stay green.

## Quick wins (one or two lines each)
1. [CRIT-1] replace mask list with suffix-based predicate — ~10 lines in `api_settings_get`
2. [MED-1] add `isfinite` + `strptime` guards in `api_mileage_create` — ~5 lines
3. [LOW-1] swap `print` → `logger.info` in `diag_usalliance.py` — mechanical
4. [LOW-3] delete 4 dead `tf-cat` refs — mechanical
5. [MED-7] change compose `image:` tag to `2026-04-23-playwright-step1` — one line
6. [HIGH-2 minimal] refuse to create default admin if `ADMIN_INITIAL_PASSWORD` env var is unset — ~8 lines in `ensure_default_data`

## Local fix vs architectural fix
- **Local**: CRIT-1, HIGH-3, HIGH-4, MED-1, MED-2, MED-3, MED-4, MED-5, MED-6, MED-7, all LOW, all ENH
- **Architectural**:
  - HIGH-1 (CSRF) — touches every form template + every POST route, or pivots on SameSite=Lax (one-line)
  - HIGH-2 depending on chosen remedy

## Risky changes requiring extra caution
- **HIGH-1 (full CSRF)** — adding Flask-WTF + injecting `csrf_token()` into every form is a widespread change; regress risk across every write endpoint. If chosen, do as its own commit with dedicated smoke pass.
- **CRIT-1 mask refactor** — may accidentally mask a key the UI NEEDS to read (e.g. `plaid_client_id` displayed in the Plaid tab). Confirm the "save a field starting with `***` skips write" pattern holds.
- **HIGH-2 env-var gating** — may break first-time bootstrap for anyone who hasn't set the env var; document clearly.

## Dependencies between fixes
- MED-6 (pin requirements) should land before MED-7 (dated tag) so the tagged image has pinned deps.
- Group B (CSRF) should land before extended security-headers rollout to avoid test churn.
- No other hard dependencies.

## Ordered execution suggestion (if user approves going ahead)
1. **Backup + baseline** per `qa/backup-plan.md`
2. **Group A — Credential safety** (CRIT-1 + HIGH-2) — release-blocker class
3. **Smoke pass** after Group A
4. **Group C — Data integrity** (MED-1 + MED-3) — quick, safe
5. **Group D — UI hardening** (MED-2)
6. **Group E — Hygiene batch** (MED-6, MED-7, LOW-1..4)
7. **Group B — CSRF** (HIGH-1 etc.) — risky, solo commit
8. **Group F — Coverage expansion** — iterative

## Retest scope
- After Group A: full session_smoke suite + specific CRIT-1 test.
- After each group: full session_smoke suite.
- After Group B: manual regression of every write-path UI flow (bulk edit, settings save, login, accountant-token generate, vendor merge, mileage add).
- Before declaring done: `docker compose build` from scratch, fresh DB init, all 6/6 session_smoke tests pass, endpoint-matrix check shows no new 5xx.
