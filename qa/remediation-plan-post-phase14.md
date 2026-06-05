# Remediation plan — post-Phase 14 (2026-06-05)

Prioritized by severity, blast radius, and operator-vs-code split.

## Release blockers (must fix before declaring "ready")

### 1. CRIT-POST14-1 — Proxy chain 401-broken
**Owner**: operator (Devin) + me (code)
**Sequence**:
1. Operator: log in to `https://www.voipguru.org/llm-proxy/keys` (note the separate session cookie `llmproxy_clone_session` per the cluster-split memo), create a new `tax-ai-analyzer` key on the full-catalog cluster, copy the `llmp-*` value.
2. Operator: update `/home/dblagbro/docker/.env` — set `LLM_PROXY2_KEY=<new-key>` and `LLM_PROXY2_URL=https://www.voipguru.org/llm-proxy/v1` (note: no "2" in the URL path).
3. Me: update `PUBLIC_LLM_PROXY2_URL` constant in `app/db/core.py` from `https://www.voipguru.org/llm-proxy2/v1` → `https://www.voipguru.org/llm-proxy/v1`. The URL normalizer will rewrite local URLs to the new public form; the boot migration's `_migrate(conn)` ALTER + UPDATE block already rewrites api_key when `LLM_PROXY2_KEY` changes.
4. Me: also expand the `_LOCAL_HOST_FRAGMENTS` set if needed — currently rewrites `llm-proxy2` internal name; should also catch `llm-proxy-manager` (already does). No change needed for the public-URL switch.
5. Operator: `docker restart tax-ai-analyzer`. Boot migration applies.
6. Me: smoke test — fire one classification call via `proxy_call.call_chat`, assert 2xx + `LLM-Capability` header present. Pytest full suite still passes.
7. Me: refresh `architecture.md` LMRH section + `runbook.md` to reference `/llm-proxy/` consistently. Add a "cluster-split" entry to `refactor-log.md`.

**Estimated work**: 30 min once operator supplies the key.

### 2. HIGH-POST14-1 — Clear `LLM_API_KEY`
**Owner**: operator
**Action**: edit `/home/dblagbro/docker/.env`, set `LLM_API_KEY=`. Restart container.
**Verification**: `docker exec tax-ai-analyzer printenv LLM_API_KEY` returns empty. Any direct-SDK fallback path either raises a clear `RuntimeError("no Anthropic API key configured")` (bank_codegen) or fails at SDK construction with a clear message.

**Estimated work**: 2 min.

## Quick wins (1-hour or less, no operator dependency)

### 3. HIGH-POST14-2 — Validator accepts `email` as credential param
**Owner**: me
**Patch**:
```python
# app/ai_agents/importer_validator.py
REQUIRED_RUN_IMPORT_PARAMS = ("password", "years", "consume_path", "entity_slug", "job_id")
CRED_PARAM_ALTERNATIVES = ("username", "email")

# in _check_shape, replace the current `miss_params` block:
fn = found_funcs.get("run_import")
if fn:
    param_names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    miss_required = [p for p in REQUIRED_RUN_IMPORT_PARAMS if p not in param_names]
    if not any(c in param_names for c in CRED_PARAM_ALTERNATIVES):
        miss_required.append(f"one of {CRED_PARAM_ALTERNATIVES}")
    if miss_required:
        return f"run_import() missing parameters: {miss_required}"
```

**Tests to add**: one for `chime_importer.__file__` → `validate()` returns `pass`. One for a fixture without `username` AND without `email` → returns `shape_error`.

**Estimated work**: 20 min.

### 4. LOW-POST14-3 — Schema test covers Phase 11/14 columns
**Owner**: me
**Patch**: extend `test_session_smoke.py::test_fresh_init_creates_new_schema`'s `checks` list:
```python
checks = [
    ("transactions", "vendor_normalized"),
    ("analyzed_documents", "cross_source_duplicate"),
    ("analyzed_documents", "is_duplicate"),
    ("import_jobs", "created_at"),
    # New Phase 11/14 columns:
    ("generated_importers", "validation_status"),
    ("generated_importers", "validation_notes"),
    ("generated_importers", "deployed_path"),
    ("generated_importers", "deployed_at"),
    ("generated_importers", "deployed_by"),
    ("generated_importers", "parent_id"),
    ("generated_importers", "feedback_text"),
    ("llm_usage", "cost_class"),  # in usage.db, not financial_analyzer.db
]
```

**Estimated work**: 10 min.

### 5. LOW-POST14-2 — Auto-import dispatcher returns 400 on invalid-shape slug
**Owner**: me
**Patch**: `app/routes/importers/import_auto.py` — modify `_resolve_importer`:
```python
def _resolve_importer(slug: str):
    if not _SAFE_SLUG_RE.match(slug):
        return None, "invalid slug format (must match [a-z][a-z0-9_]*)"  # currently "invalid slug"
    ...
```
And update each route handler to map "invalid slug format" → 400, "bank not found" → 404.

**Estimated work**: 15 min.

### 6. MED-POST14-2 — Delete orphan empty DB files
**Owner**: me (after grep audit)
**Sequence**:
1. `grep -rn "app.db\|chat.db\|tax_ai.db\|tax_analyzer.db" app/ --include="*.py"` — if zero hits, safe to delete.
2. `docker exec tax-ai-analyzer rm /app/data/app.db /app/data/chat.db /app/data/tax_ai.db /app/data/tax_analyzer.db /app/data/llm_usage.db`
3. Add `data/README.md` documenting which DBs are canonical (`financial_analyzer.db`, `usage.db`).

**Estimated work**: 15 min.

## Hardening (medium effort, low urgency)

### 7. MED-POST14-3 — `paperless_configured` predicate reconciliation
**Owner**: me
**Investigation needed**: find the predicate in `app/routes/stats.py` (or wherever `features` dict is built); reconcile env-var vs DB-setting checks.

**Estimated work**: 30 min including the test.

### 8. MED-POST14-1 — Daemon heartbeat for `/api/health/extended`
**Owner**: me
**Design**:
- Add a `daemon_heartbeats(name TEXT PRIMARY KEY, ts TEXT)` table.
- `analysis_daemon()` and `_daily_dedup()` write `(name, datetime('now'))` each iteration.
- `/api/health/extended` reads each row; if `ts > N seconds ago` → alive, else missing/dead.
- N defaults to `2× POLL_INTERVAL` for analysis, `25h` for dedup.

**Tests**: heartbeat written by mocked daemon; health endpoint reports both states.

**Estimated work**: 90 min.

### 9. MED-POST14-4 — Rate limiter bypass for loopback in dev mode
**Owner**: me (or operator depending on framing)
**Patch**:
```python
# app/routes/auth.py
def _ratelimited(ip):
    if os.environ.get("DEV_BYPASS_RATELIMIT_LOOPBACK") == "1" and ip in ("127.0.0.1", "::1"):
        return False
    # existing logic
```

**Estimated work**: 20 min.

### 10. LOW-POST14-1 — Document hyphen vs underscore convention
**Owner**: me
**Patch**: append to `architecture.md`:
```
## URL / tab-key naming convention

- URL paths use hyphens: `/tax-ai-analyzer/folder-manager`, `/ai-costs`, `/tax-review`.
- JS tab-loader keys + Jinja `id="tab-<key>"` use underscores: `folder_manager`, `ai_costs`, `tax_review`.
- The `loadTab()` JS helper substitutes hyphens for underscores when navigating.
- Don't search for one expecting to find the other.
```

**Estimated work**: 5 min.

## Future work / hold for trigger

### 11. ENH-POST14-1 — Verify `cost_class` populates post-proxy-fix
Trigger: CRIT-POST14-1 resolved.
Action: fire 10 calls across mixed tasks; query `SELECT cost_class, COUNT(*) FROM llm_usage GROUP BY cost_class`. If all `unknown`, ping llm-proxy2 ops to confirm `cost_class` emission rules.

### 12. ENH-POST14-2 — Codegen prompt mentions `email` alternative
Trigger: ship with HIGH-POST14-2 fix.
Action: update `SYSTEM_PROMPT` in `bank_codegen.py` to say "first credential parameter is `username` OR `email`, use whichever the bank actually accepts."

### 13. ENH-POST14-3 — Real bank retest after Phase 14
Trigger: user signals "go" for a live bank import.
Action: run merrick (no MFA, simplest) first to validate closure wiring; then US Alliance for the MFA path. Watch logs for argument-shape errors.

## Architectural / non-local fixes

### MED-POST14-1 daemon heartbeat (#8) is the only one that touches schema + daemon code together. All other items are local.

## Recommended retest scope after fixes

| Fix wave | Retest |
|---|---|
| **Wave A** (#1, #2 — CRIT/HIGH) | Full pytest (272), live proxy probe (1 classification + 1 codegen), one streaming chat round-trip, smoke each dashboard tab |
| **Wave B** (#3, #4, #5 — quick wins) | Pytest only (242 + new tests for #3, #4) + spot-check the changed routes |
| **Wave C** (#6, #7, #8, #9 — hardening) | Pytest + `/api/health/extended` check + restart-and-verify cycle |
| **Wave D** (#10 — doc) | n/a (no runtime change) |
| **Wave E** (#11, #12, #13 — triggered) | Per-item; see triggers above |

## Effort summary

- Critical/High waves: **~50 min code** + operator (key provisioning, .env edit, container restart)
- Quick wins: **~60 min total** (#3 #4 #5 #6 #10)
- Hardening: **~3 hours total** (#7 #8 #9)
- **Total to ship a clean post-Phase-14 release: ~5 hours of focused work + 1 operator step.**
