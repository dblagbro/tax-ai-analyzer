# QA notes — post-Phase 14 deep regression (2026-06-05)

Environment observations, false-positive lessons, and risk areas that didn't rise to bug-log severity but are worth remembering.

## Environment quirks

### `app.test_client()` runs in a separate Python process when invoked via `docker exec python3 -c '...'`
- Implication: anything that inspects `threading.enumerate()` (e.g. `/api/health/extended`) will return that ad-hoc process's threads, NOT the live Flask app's threads.
- Implication: in-process state (rate-limiter dict, breaker state, daemon registry) is NOT shared.
- For accurate live-state inspection, hit Flask via real HTTP (`urllib.request.urlopen("http://localhost:8012/...")`).
- This caught me during this pass — initial health probe falsely reported `status: degraded` because the test_client process had no daemons.

### Recent timestamps in the live container suggest the system clock has drifted across the conversation
Logged events: 2026-05-23, 2026-06-01, 2026-06-05. The conversation started at "2026-04-30" per system reminders. Suggests either: (a) tickless container clock that follows host time, or (b) gaps between sessions span weeks of real wall-clock. Whichever — don't assume timestamps are "today" without re-reading the live clock.

### `LLM_API_KEY` env is still set despite "account closed" doc claim
108-char `sk-ant-api03-...` value present in container env. Runbook says it should be empty. Behavior diverges from documented expectation. See HIGH-POST14-1.

### Paperless intermittent unreachability
Analysis daemon hit "Connection refused" multiple times across 2026-05-23 → 2026-06-05. Currently reachable (302 from `tax-paperless-web:8000`). Suggests `tax-paperless-web` had restart cycles during gap periods. Container is healthy now.

### Disk at 84% (host volume)
`/dev/sda2` (the host mount backing `/app/data`) is at 313G / 392G. The container's own `/app/data` is only 15MB — the 84% is shared with other Docker volumes on the host. Worth monitoring but not a tax-ai-specific risk.

## False positives encountered during this pass

1. **Health endpoint reports `status: degraded`** — only happens via test_client; live process is fine. See env quirk #1.

2. **`Cost-by-Tier` empty in AI Costs UI** — expected because the proxy chain is 401-broken and the live cost_class column has zero rows since the v3.0.50 cost_class dim was wired. Not a bug; will populate once proxy works.

3. **`paperless_configured: false` in health features** — actually a real medium-severity bug (MED-POST14-3), but initially looked like a false positive. The integration works at the HTTP layer; only the feature-flag predicate is wrong.

4. **15 bad-login probe locked out the rate limiter** — expected behavior per Wave 3 design. Tests sharing this process inherit the limit. Container restart clears it.

5. **My initial schema audit looked at `financial_analyzer.db` for `llm_usage` table** — wrong DB; llm_usage lives in `usage.db` (separate). Schema is fine; my probe was misplaced.

## Real risks that didn't rise to bug-log

### Refactor blast radius for the 5 Phase-14 importers
All 5 importers now delegate to `run_bank_import` with closures capturing the live `(page, context)` from the orchestrator. Tests pass at structure level (`test_run_bank_import.py`). But no real bank has been imported through the refactored code. Bugs in closure param ordering, missing kwargs, or stale references would only surface at runtime against a real bank.

Mitigation: when the user next runs ANY live bank, run it carefully and watch logs for argument-shape errors. Merrick (simplest) or US Alliance (best-known) are the lowest-risk first re-tests.

### Codegen prompt revision unvalidated against real LLM
Codegen `SYSTEM_PROMPT` was updated to instruct the model to emit `run_bank_import`-shaped output. Validator detects legacy shape as `pattern_warning` (non-blocking). But no real codegen call has been fired with the new prompt — the synthetic E2E from 2026-04-30 predates this change.

Mitigation: when the proxy is back up, the next codegen call will be the validation. If the model produces legacy-shape output despite the new prompt, the validator will flag it visibly (badge in admin UI).

### conftest.py is autouse=True, scope=session
The fixture redirects `tracker._USAGE_DB_PATH` to a temp dir for the entire test session. This means:
- Any test that reads from the production `usage.db` during a pytest run gets zero rows.
- Any test that expects historical data from `llm_usage` will fail.
- Cleanup runs at session end; if pytest is killed mid-run, the temp dir may leak (minor).

Audit: searched `app/tests/` for queries against historical `llm_usage` — found none. Currently safe.

### LMRH `provider-hint` comma-list assumes `claude-oauth` is on the target cluster
Per the 2026-06-05 cluster-split memo, `/llm-proxy2/` has NO Anthropic providers (compliance-locked). Our hint `provider-hint=claude-oauth,anthropic,anthropic-direct;require` will 503 against that cluster (per the memo's stated behavior). The current 401 we're seeing means we never get to the `provider-hint` check — auth fails first.

When we switch to `/llm-proxy/` (full catalog), the comma-list values may need to be updated — the memo lists "Anthropic-Max-Gmail" and "Anthropic-Max-VG" as the current Anthropic provider_types. Worth a probe on day-1 to confirm `;require` doesn't 503 there too.

### Substitution-guard semantics interact subtly with cluster split
Our `CrossFamilySubstitution` exception triggers on `chosen-because=cross-family-fallback`. Per the memo, the compliance cluster substitutes silently with `X-Compliance-Substitution: true` (a DIFFERENT header). If we accidentally end up on the compliance cluster post-switch, our guard won't catch it.

Mitigation: extend `_detect_substitution` (proxy_call.py) to ALSO check for `X-Compliance-Substitution: true`. Track as ENH item if we ever route through the compliance cluster.

## Test pollution observed

None in this pass. The conftest fixture (added 2026-05-04) is doing its job — pytest runs leak 0 rows into the production `usage.db`. Verified by counting rows before/after a full test suite run earlier in the session.

## Code-quality observations

1. **`PUBLIC_LLM_PROXY2_URL` is misleadingly named** post-cluster-split. The constant points at `/llm-proxy2/` but we want `/llm-proxy/`. Worth renaming or commenting carefully when the switch happens.

2. **`importer_validator._check_phase14_pattern` returns `pattern_warning`** but the validator's docstring says `status ∈ {"pass", "syntax_error", "shape_error", "import_error", "pattern_warning"}`. Test that the deploy + approve gates correctly skip blocking on `pattern_warning` (we have one such test — `test_pattern_warning_unblocked_in_deploy_gate`). Good.

3. **`run_bank_import` accepts no_op closures** — if a caller passes lambdas that do nothing, the loop iterates over (None × years) and returns `{imported:0, skipped:0, errors:0}` with no error. By design (Phase-14 tests rely on this), but a developer testing the wiring might think the orchestrator silently swallowed a real failure.

4. **No explicit test for `proxy_call.NoProxyAvailable` being raised when the pool is empty AND every endpoint is held by breaker** — covered by `test_streaming_client_raises_when_pool_empty` for the streaming helper, but not for `call_chat` / `call_anthropic_messages` specifically. Marginal.

## What was NOT tested this pass

- Real browser UI flows (Playwright unavailable)
- Real LLM calls (proxy 401-broken)
- Real bank imports (no consent + creds for live runs)
- Multi-user concurrency (single-user app today)
- Backup/restore from the tarball at `/mnt/s/router_and_LAN/backups/`
- Long-running stress (24h soak)
- I18n / encoding edge cases in vendor names, narration text, etc.
- Mobile dashboard rendering
- Different browsers (only Flask test_client + curl)
