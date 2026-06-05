# Bug log — post-Phase 14 regression validation (2026-06-05)

This log captures defects, hardening opportunities, and observations from a deep regression validation conducted against HEAD `8eab99d` (Phase 14 — full bank-importer family + cost_class + Phase 13.5 substitution-guard + Phase 11 bank-onboarding + cluster-split fallout).

Prior logs (`bug-log.md`, `bug-log-post-phase9.md`, `bug-log-post-phase9-pass2.md`) cover pre-Phase-11 surface area and are not duplicated here.

Severity levels: **critical** | **high** | **medium** | **low** | **enhancement**.

---

## CRIT-POST14-1 — LLM proxy chain returns 401 on every call

**Date**: 2026-06-05
**Area**: `app/llm_client/proxy_manager.py`, env `LLM_PROXY2_KEY`, DB `llm_proxy_endpoints.api_key`
**Severity**: **critical**
**Environment**: live container, `LLM_PROXY2_URL=https://www.voipguru.org/llm-proxy2/v1`

**Repro**:
```python
from app import db
from app.llm_client.proxy_manager import build_anthropic_client
ep = db.llm_proxy_list_endpoints(include_disabled=True)[0]
client = build_anthropic_client(ep, lmrh_hint='task=codegen, cost=premium, provider-hint=claude-oauth,anthropic,anthropic-direct;require')
client.messages.create(model='claude-haiku-4-5-20251001', max_tokens=8, messages=[{'role':'user','content':'OK'}])
```

**Expected**: 2xx response with claude-haiku output.
**Actual**: `AuthenticationError: Error code: 401 - {'detail': 'Invalid or disabled API key'}` — 31ms.

**Evidence**: live probe during this QA pass; reproducible. Affects every LMRH-shape call (hard or soft hint, any task).

**Likely cause**: cluster split memo of 2026-06-05 — `/llm-proxy2/` is the compliance-locked fleet; our `llmp-*` key (`llmp-2HjkGnX…Zuc8`) may have been disabled, removed, or never synced to the post-split fleet. Combined with the memo's recommendation that we move to `/llm-proxy/` (full-catalog) instead.

**Recommended fix** (operator+code):
1. Operator: provision a new `llmp-*` key on `/llm-proxy/` (visit `https://www.voipguru.org/llm-proxy/keys`).
2. Code: update `LLM_PROXY2_KEY` env to the new key; update `PUBLIC_LLM_PROXY2_URL` constant in `app/db/core.py` from `https://www.voipguru.org/llm-proxy2/v1` → `https://www.voipguru.org/llm-proxy/v1`; the URL normalizer + boot migration will rewrite the existing endpoint row to match.
3. Smoke test the chain end-to-end (one haiku call) post-rotation.

**Status**: open; blocks all real LLM traffic.

---

## HIGH-POST14-1 — `LLM_API_KEY` still set despite "account closed" docs

**Date**: 2026-06-05
**Area**: `/home/dblagbro/docker/.env`, runbook §LLM proxy chain
**Severity**: high
**Environment**: live container

**Repro**:
```bash
docker exec tax-ai-analyzer printenv LLM_API_KEY  # returns sk-ant-api03-... (108 chars)
```

**Expected** (per `qa/runbook.md` and memory `tax_ai_resume_2026_05_01.md`): `LLM_API_KEY` is empty because the Anthropic billing account was closed.

**Actual**: env still has the 108-char `sk-ant-api03-…` key set.

**Likely cause**: docs were updated but the `.env` change to clear the key was never applied.

**Recommended fix**: clear `LLM_API_KEY` in `/home/dblagbro/docker/.env`, restart container. With the proxy chain currently 401-broken (CRIT-POST14-1), the direct-SDK fallback would activate on every call — and would itself fail if the account is truly closed. Verifying which is true (closed vs still-credited) is the first step.

**Status**: open. Operator action.

---

## HIGH-POST14-2 — Validator rejects email-credential importers as `shape_error`

**Date**: 2026-06-05
**Area**: `app/ai_agents/importer_validator.py` — `REQUIRED_RUN_IMPORT_PARAMS`
**Severity**: high (will impact any codegen for an email-login bank)

**Repro**:
```python
from app.importers import chime_importer
from app.ai_agents.importer_validator import validate
src = open(chime_importer.__file__).read()
validate(src)
# → ('shape_error', "run_import() missing parameters: ['username']")
```

**Expected**: chime_importer is a real, production, in-use Phase-14-shaped importer — should pass validation.

**Actual**: validator's `REQUIRED_RUN_IMPORT_PARAMS = ('username', 'password', ...)` is hardcoded to `username`. Chime uses `email` (because that's how Chime's auth actually works). Any codegen output for an email-login bank (e.g. Chime, Robinhood, several neobanks) would be rejected as a shape_error → blocking the approve+deploy gate.

**Likely cause**: validator written for the common case before chime was refactored. Domain inconsistency overlooked.

**Recommended fix**: relax the credential-param check to accept either `username` OR `email` as the first auth parameter:
```python
REQUIRED_RUN_IMPORT_PARAMS = ("password","years","consume_path","entity_slug","job_id")
REQUIRED_CRED_PARAM_ALTERNATIVES = {"username","email"}
# In _check_shape: ensure at least one of {username, email} is present
```
Add a regression test using chime_importer source to assert `pass`.

**Status**: open.

---

## MED-POST14-1 — `/api/health/extended` thread inspection is process-local

**Date**: 2026-06-05
**Area**: `app/routes/stats.py:260-272`
**Severity**: medium (false-positive degraded status when accessed from non-daemon process)

**Description**: The health endpoint calls `threading.enumerate()` which lists threads of the **CURRENT** process. When the endpoint runs in the live Flask process, this correctly enumerates `MainThread + analysis-daemon + dedup-scheduler + request-handling-thread`. When called via `app.test_client()` from a separate Python process (e.g. ad-hoc scripts, unit tests, monitoring probes), it only sees that process's threads → reports `expected_present: {analysis-daemon: false, dedup-scheduler: false}` → overall `status=degraded` incorrectly.

**Repro** (false positive):
```python
from app.web_ui import app
c = app.test_client()
with c.session_transaction() as s: s['_user_id']='1'; s['_fresh']=True
r = c.get('/tax-ai-analyzer/api/health/extended')
# → 'overall': {'problems': [...], 'status': 'degraded'} even when the live process is healthy
```

**Recommended fix**: the health endpoint should track daemon presence via a heartbeat in the DB (each iteration of `analysis_daemon()` writes a `daemon_heartbeat` row with `(name, ts)`; the endpoint reads "last heartbeat < N seconds ago"). That way the check works regardless of which process serves the request.

Alternative: explicitly document in code comments + endpoint response that `threads` is process-local.

**Status**: open (no urgency — affects monitoring scripts, not user-visible behavior).

---

## MED-POST14-2 — Five orphan empty `.db` files in `/app/data/`

**Date**: 2026-06-05
**Area**: `/app/data/` filesystem
**Severity**: medium (clutter; could mask real DB issues during diagnostics)

**Listing**:
```
-rw-r--r-- 1 root root 0 Apr 30 14:33 app.db
-rw-r--r-- 1 root root 0 Mar 12 23:04 chat.db
-rw-r--r-- 1 root root 0 May  5 00:46 llm_usage.db
-rw-r--r-- 1 root root 0 Mar 11 23:48 tax_ai.db
-rw-r--r-- 1 root root 0 Mar 12 22:03 tax_analyzer.db
```

Likely orphans from earlier refactors. The actual DBs in use are `financial_analyzer.db` and `usage.db`. Notably `llm_usage.db` is interesting — the tracker now writes to `usage.db`, but a stale `llm_usage.db` (also 0 bytes) sits next to it.

**Risk**: a future developer running `sqlite3 llm_usage.db` for diagnostics will see an empty DB and conclude the tracker is broken. Or worse, code somewhere may still reference the old path and silently write to nothing.

**Recommended fix**: 
1. Grep the codebase for references to the old paths to confirm nothing still uses them
2. Delete the 0-byte files from `/app/data/`
3. Document the canonical paths in `architecture.md` or a `data/README.md`

**Status**: open.

---

## MED-POST14-3 — `paperless_configured: false` in health despite full configuration

**Date**: 2026-06-05
**Area**: `app/routes/stats.py` (feature detection logic)
**Severity**: medium (misleading health signal)

**Evidence**:
```
PAPERLESS_API_BASE_URL=http://tax-paperless-web:8000  (env set)
PAPERLESS_API_TOKEN=6a6113ccbb4205eb...  (env set)
/api/ → reachable (302 from API root, expected)
/api/health/extended features.paperless_configured = false
```

**Likely cause**: the feature-detection predicate probably checks something other than env vars (maybe `db.get_setting("paperless_api_url")` — a DB-stored setting that's never populated). Conflict between env-var-driven config and setting-driven config.

**Recommended fix**: identify which predicate sets `paperless_configured` and reconcile it with the actual reachability check.

**Status**: open. Low urgency since the underlying integration WORKS (analysis daemon was reaching paperless until 2026-06-05 15:14:49; the connection-refused/timeout errors were transient outages).

---

## MED-POST14-4 — Rate limiter triggers at 11 attempts (correct), but cookie-less probes from `127.0.0.1` share the bucket

**Date**: 2026-06-05
**Area**: `app/routes/auth.py:_client_ip()`, ProxyFix gating
**Severity**: medium (test-suite + ad-hoc probes can lock out legitimate localhost logins)

**Repro**: 15 bad-credential POSTs to `/login` from `app.test_client()`:
```
status codes = [200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 429, 429, 429, 429, 429]
```

10 attempts → 200 with login-page (auth fail), then 5× 429. **Behavior is correct per spec** (Wave 3 design — 10 fails per 5 min). But:

1. **Persistence across legitimate auths**: any user trying to log in immediately after a test-client run sees 429 until the in-memory window expires. Container restart clears the bucket.
2. **All `127.0.0.1`-origin requests share one bucket** — ProxyFix is gated on `TRUST_PROXY_HEADERS` (not set in dev), so all requests look like they're from `127.0.0.1`.

**Recommended hardening**:
1. Add a settings entry `RATE_LIMIT_SKIP_LOOPBACK = true` (default off in prod) that bypasses the limiter for `127.0.0.1` requests when dev-mode is on.
2. Or: bake the conftest fixture to clear the rate-limit log between test runs (the rate-limit state is process-local; if we share the live process with tests, this matters).

**Status**: open. Low operational risk in production (proper proxy headers + TRUST_PROXY_HEADERS=1 fix it). Affects QA workflow today.

---

## LOW-POST14-1 — Dashboard tab path inconsistency: hyphens vs underscores

**Date**: 2026-06-05
**Area**: `app/routes/pages.py` URL prefixes vs JS tab registry keys
**Severity**: low (cosmetic)

**Observation**: The dashboard SPA uses underscored tab keys (`bank_onboarding`, `llm_routing`, `folder_manager`, `ai_costs`, `tax_review`) in `registerTabLoader()` calls, but the page-route URLs use hyphens (`/ai-costs`, `/tax-review`, `/folder-manager`). The `loadTab()` JS handler does the substitution.

**Risk**: subtle confusion for developers — searching for `tax-review` finds routes but not JS handlers, and vice versa.

**Recommended fix**: standardize on one form (hyphens in URLs is web-conventional; keep underscores in JS for JS-conventional identifier-style). Just document the convention in `architecture.md`.

**Status**: open. Low priority.

---

## LOW-POST14-2 — Auto-import dispatcher emits `bank not found` for invalid slug forms

**Date**: 2026-06-05
**Area**: `app/routes/importers/import_auto.py`
**Severity**: low (UX clarity)

**Repro**:
```
GET /api/import/auto/with-dash/status  → 404 {"error": "bank not found"}
GET /api/import/auto/../etc/status     → 404 {"error": "bank not found"}
GET /api/import/auto/UpperCase/status  → 404 {"error": "bank not found"}
```

**Observation**: All these are returning "bank not found" but the actual reason is "invalid slug shape" (regex `^[a-z][a-z0-9_]*$` rejects them). For path-traversal cases the misleading error is fine (we don't want to confirm structure to an attacker), but for a legitimate developer mistyping a slug with a dash, "bank not found" is misleading vs the correct "invalid slug (must match [a-z][a-z0-9_]*)".

**Recommended fix**: return `400 {"error": "invalid slug format"}` for regex-fails, keep `404 {"error": "bank not found"}` for valid-shape-but-unknown-slug.

**Status**: open. Minor.

---

## LOW-POST14-3 — `tests/` test file lacks coverage for the seven recent `generated_importers` columns

**Date**: 2026-06-05 (per schema-audit subagent)
**Area**: `app/tests/test_session_smoke.py:38-63` (`test_fresh_init_creates_new_schema`)
**Severity**: enhancement / hardening

**Observation**: the fresh-init test verifies 4 historical columns but not the 7 Phase-11/14 additions: `validation_status`, `validation_notes`, `deployed_path`, `deployed_at`, `deployed_by`, `parent_id`, `feedback_text`.

**Risk**: a future ALTER statement collision (reserved word, typo) would not be caught by automated tests until exercised in admin actions.

**Recommended fix**: append the 7 columns to the `checks` list in `test_fresh_init_creates_new_schema`.

**Status**: open.

---

## ENH-POST14-1 — `cost_class` index never populated by live calls

**Date**: 2026-06-05
**Area**: `app/llm_usage_tracker.py` — `cost_class` column + bucket reporting
**Severity**: enhancement

**Observation**: The new `cost_class` infrastructure (proxy header parsing → tracker column → AI Costs UI breakdown) is wired end-to-end. But because the proxy chain is currently 401-broken (CRIT-POST14-1) AND because the proxy hasn't been observed to emit `cost_class=` in `LLM-Capability` even in successful probes during this session, the column will stay empty (or all-`unknown`) until ops confirms when the dim is emitted.

**Recommended action**: once CRIT-POST14-1 is resolved, fire ~10 real calls of mixed task types (analysis, codegen, chat), then query `SELECT cost_class, COUNT(*) FROM llm_usage GROUP BY cost_class` and confirm we're getting non-`unknown` values. If still all-unknown, ask llm-proxy2 ops whether `cost_class` is opt-in or available only on specific provider_types.

**Status**: open. Not blocking.

---

## ENH-POST14-2 — Codegen prompt doesn't tell the model about `email`-credential banks

**Date**: 2026-06-05
**Area**: `app/ai_agents/bank_codegen.py:SYSTEM_PROMPT`
**Severity**: enhancement (paired with HIGH-POST14-2)

**Observation**: The prompt hardcodes `def run_import(username, password, ...)` as the required signature, even though chime_importer (a reference implementation) uses `email, password`. If a user uploads a HAR for an email-login bank, the codegen will emit `username` and the validator's hardcoded list will pass, but the operator UI labels (input placeholders, credentials form) will say "username" when the bank actually wants email.

**Recommended fix**: update prompt to "first credential parameter is `username` OR `email` (use whichever the bank actually accepts — read the HAR)". Loosen validator per HIGH-POST14-2.

**Status**: open.

---

## ENH-POST14-3 — No retest of all Playwright importers after Phase 14 refactor against real banks

**Date**: 2026-06-05
**Area**: 5 refactored importers (merrick, chime, verizon, capitalone, usbank)
**Severity**: enhancement / hardening

**Observation**: Phase 14 converted all 5 importers to delegate to `run_bank_import`. Structural tests pass (`test_run_bank_import.py`, 8 tests). But no real-bank import has been run against the refactored code. The closures' parameter shapes are slightly different from the inline pattern, and integration bugs (typos, off-by-one in args, MFA token routing) could be lurking.

**Risk profile**: the closures look syntactically clean and the orchestrator's tests cover happy/error paths. But a real-bank run is the only validation that proves the closures wire arguments correctly.

**Recommended retest**: trigger US Alliance with the existing cookies + saved credentials (US Alliance is gated by user, but USAlliance is the most-recently-tested bank). Or run merrick (no MFA, simplest) against the live site if creds are available. If none feasible, at minimum exercise each importer's `_login_fn(page, context)` closure with a mocked page object verifying expected `find_element` / `human_click` calls.

**Status**: open. Cannot complete in this QA session without user action.

---

## Summary

| Severity | Count |
|---|---|
| Critical | 1 (CRIT-POST14-1) |
| High | 2 (HIGH-POST14-1, HIGH-POST14-2) |
| Medium | 4 (MED-POST14-1 … MED-POST14-4) |
| Low | 3 (LOW-POST14-1 … LOW-POST14-3) |
| Enhancement | 3 (ENH-POST14-1 … ENH-POST14-3) |
| **Total** | **13** |

No NEW regressions introduced by Phase 14 itself — the orchestrator + codegen prompt changes look solid structurally. The bulk of findings are infrastructure (CRIT/HIGH) or hardening (MED/LOW/ENH).
