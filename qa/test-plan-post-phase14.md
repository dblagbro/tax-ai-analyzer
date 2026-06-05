# Test plan — post-Phase 14 (2026-06-05 baseline)

Supersedes `test-plan.md` (2026-04-23) for any test surface added or modified since Phase 11.

## Test pyramid current state

| Layer | Coverage | Test files |
|---|---|---|
| **Unit / mocked integration** | strong (272 collected, 242 pass + 30 skipped host-JS) | 18 files in `app/tests/` |
| **HTTP smoke (in-process)** | strong (every tab renders, all admin routes auth-checked) | `test_session_smoke.py`, `test_smoke.py` |
| **Live LLM round-trip** | **0** — proxy chain currently 401-broken; only synthetic-LLM E2E ran (2026-04-30) | manual probes only |
| **Live bank importer** | **0** post-Phase-14 — refactored code never exercised against a real bank | n/a |
| **Concurrency** | minimal (1 ad-hoc probe in this QA pass) | n/a |
| **Browser UI (Playwright)** | **0** — environment can't drive a real browser at the dashboard | n/a |

## Test surfaces and current owner

| Surface | Coverage method | Gaps |
|---|---|---|
| HTTP routes (232 total) | `test_session_smoke.py::TestRouteMatrix` walks every GET → no 5xx | POST/PUT routes not enumerated; payload-fuzzing minimal |
| Auth boundaries | `test_auth_boundaries.py` (20 tests) — rate limiter, _safe_next, ProxyFix gating | Add: TRUST_PROXY_HEADERS-on-vs-off scenarios |
| Codegen pipeline | `test_bank_codegen*.py`, `test_importer_validator.py`, `test_importer_deployer.py` (39 tests combined) | One real codegen call (synthetic Acme E2E, 2026-04-30) but no real-HAR validation |
| Bank importer family (Phase 14) | `test_run_bank_import.py` (9 tests — orchestrator branches + 5 importer structure smoke) | No live-bank integration; closures haven't been exercised end-to-end |
| LLM proxy chain | `test_lmrh.py` (37 tests — header build, breaker, fallback, cost_class extraction) | Live cluster behavior un-probed since cluster split |
| AI Costs accounting | `test_lmrh.py::test_log_usage_persists_cost_class` + UI render via Jinja smoke | No UI-level interaction (Playwright gap) |
| Admin REST | `test_llm_proxies.py` (12 tests — CRUD, breaker reset, hints) | All exercised via test_client; no real-HTTP integration |
| Bank-onboarding flow | `test_bank_codegen.py` + `test_bank_codegen_regenerate.py` (18 tests) | HAR-upload via real multipart not exercised |
| Schema / migrations | `test_session_smoke.py::TestFreshDbInit` (1 test, 4 columns checked) | 7 Phase-11/14 columns not in checks (LOW-POST14-3) |
| Streaming routes | none | `chat`, `tax-review`, `helpers` SSE all bypass any test |

## Recommended new tests

### Unit-level (no live deps)

1. **Validator accepts `email` as credential param** (HIGH-POST14-2 regression guard) — fixture using `chime_importer.__file__` as source asserts `validate()` returns `pass`, not `shape_error`.

2. **Schema columns audit expansion** (LOW-POST14-3) — extend `test_fresh_init_creates_new_schema` checks list with the 7 columns (validation_status, validation_notes, deployed_path, deployed_at, deployed_by, parent_id, feedback_text).

3. **Auto-import dispatcher rejects invalid slugs at the regex layer** (LOW-POST14-2) — assert 400, not 404, for `with-dash`, `UpperCase`, `1starts_with_digit`.

4. **Settings credential mask covers JSON-shaped tokens** — assert `gmail_oauth_token`'s masked output doesn't leak more than 4 chars, even when those last 4 chars are JSON syntax (`y"]}` is the current observed output — it's actually safe, but the visual makes it look weird; add a comment or unit test confirming).

5. **Daemon heartbeat alternative for `/api/health/extended`** (MED-POST14-1) — design + test a DB-backed heartbeat that the analysis daemon writes each cycle; health endpoint reads it instead of in-process `threading.enumerate()`.

### Integration (single live LLM call once proxy is fixed)

6. **Live proxy chain round-trip** — fire one cheap call (`task=classification, cost=economy`) and assert:
   - 2xx response
   - `LLM-Capability` header present
   - if `cost_class` dim is in capability → `llm_usage` row has matching value
   - if `cost_class` absent → log a known-limitation note

7. **Live codegen via proxy with prompt caching** — fire `bank_codegen.generate_importer()` against a synthetic small HAR; assert `cache_creation_input_tokens > 0` on first call AND `cache_read_input_tokens > 0` on a follow-up call within 5 min. (Note: claude-oauth may return 0 — see runbook §Cache token reporting.)

### Real-bank integration (gated on user signal)

8. **Phase 14 refactor end-to-end with US Alliance** — given user has saved cookies, trigger a no-MFA refresh import. Walk the closure chain, capture log output, verify standard `{imported, skipped, errors}` shape.

9. **Codegen against real US Alliance HAR** — upload a captured HAR via the dashboard, kick off codegen, validate output against `usalliance_importer.py` reference patterns.

## Retest scope per fix area

| Fix | Retest scope |
|---|---|
| CRIT-POST14-1 proxy key | All operations: codegen (1 call), classification (1 call), summary (1 call), chat streaming (1 conn), tax-review streaming (1 conn), gmail/ai_review (skip — no Gmail in flight) |
| HIGH-POST14-1 LLM_API_KEY empty | Verify direct-SDK fallback path raises clean error; verify proxy chain still works as primary |
| HIGH-POST14-2 validator email | Re-run `test_importer_validator.py` + add chime fixture test |
| MED-POST14-1 health endpoint heartbeat | Verify health correctly reports from any caller process |
| MED-POST14-2 orphan DBs | grep verify no code references; delete; restart container; full pytest |
| MED-POST14-3 paperless_configured | After fix, `/api/health/extended → features.paperless_configured` matches reality |
| MED-POST14-4 rate limiter conftest | Conftest fixture clears rate-limit log; rerun the rate-limit boundary tests |
| LOW-POST14-1/2/3 | Refresh the smoke crawler tests |
| ENH-POST14-1/2/3 | Bake into the work that triggers each |

## Known testing limitations from this environment

1. **No real browser available** — Playwright UI tests against the dashboard can't be driven from this session.
2. **No real bank credentials available** — Playwright bank importers can't be exercised end-to-end without user consent + saved credentials.
3. **LLM calls are budget-sensitive** — every probe burns proxy quota; in this session the proxy was 401-broken so cost was $0 but normally real calls would accumulate.
4. **conftest.py redirects llm_usage to a temp DB** — any test that writes to llm_usage now reads from the temp DB; if a test wants to read from the live db, it must explicitly override.

## Process improvements

- **Pin a "test surface inventory" doc** to memory so a future QA pass can compare against this 2026-06-05 baseline.
- **Add a smoke-test script** (`qa/smoke.sh`) that runs the 30 probes in this session in 1 minute and prints a green/red summary.
- **Auto-archive bug-log files** under `qa/archive/` so the current log stays focused on "open since latest pass."
