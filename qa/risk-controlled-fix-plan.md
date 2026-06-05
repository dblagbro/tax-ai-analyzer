# Risk-controlled remediation plan — post-Phase 14 (2026-06-05)

This is the consolidated, execution-ready plan for the 13 findings from
`qa/bug-log-post-phase14.md`. It adds the dimensions a pure remediation list
doesn't carry: **pre-fix backup**, **rollback expectation**, **per-batch
retest**, and **batch ordering** that minimizes blast radius if anything
goes wrong.

**Planning-only. No fixes implemented here.** Execute batch-by-batch with
explicit go/no-go between each.

## 1. Inputs consulted

| Source | Date | Used for |
|---|---|---|
| `qa/bug-log-post-phase14.md` | 2026-06-05 | Authoritative finding list (13 items) |
| `qa/test-plan-post-phase14.md` | 2026-06-05 | Coverage gaps + recommended new tests |
| `qa/qa-notes-post-phase14.md` | 2026-06-05 | Environment quirks + false-positive lessons |
| `qa/remediation-plan-post-phase14.md` | 2026-06-05 | Prior remediation outline (this doc supersedes for execution) |
| `architecture.md` | 2026-05-04 | Subsystem boundaries + load-bearing structures |
| `refactor-log.md` | 2026-05-04 | Phase history for blast-radius reasoning |
| `qa/backup-plan.md` | 2026-04-23 | Backup naming + verification conventions |
| `qa/runbook.md` | 2026-05-04 | Restart + restore procedures |
| `design.md` | — | Not present in the repo. Architectural decisions live in `architecture.md` + `refactor-log.md`. |

## 2. Findings grouped by subsystem and root-cause area

| Subsystem | Findings | Root cause |
|---|---|---|
| **LLM proxy chain** | CRIT-POST14-1, HIGH-POST14-1, ENH-POST14-1 | External event: 2026-06-05 cluster split broke our key; doc-vs-env drift |
| **Codegen pipeline** | HIGH-POST14-2, ENH-POST14-2 | Validator's credential-param check is too strict (assumes `username`); prompt mirrors the assumption |
| **Observability** | MED-POST14-1, MED-POST14-3 | Health endpoint inspects `threading.enumerate()` of the calling process; `paperless_configured` predicate diverges from real connectivity |
| **Data hygiene** | MED-POST14-2 | Orphan `.db` files from earlier refactors clutter `/app/data/` |
| **Auth / rate limiting** | MED-POST14-4 | In-process rate-limit dict shares one bucket across loopback test clients and real localhost users |
| **Web API ergonomics** | LOW-POST14-1, LOW-POST14-2 | Hyphen/underscore drift between URL paths and JS tab keys; dispatcher emits "not found" for shape errors |
| **Test coverage** | LOW-POST14-3 | Schema-init test doesn't enumerate Phase 11/14 columns |
| **Phase-14 integration risk** | ENH-POST14-3 | All 5 importers refactored but no live-bank validation since |

## 3. Local fixes vs architectural fixes

| Item | Type | Why |
|---|---|---|
| CRIT-POST14-1 | **Local + operator** | Env-var + DB row swap + one constant change |
| HIGH-POST14-1 | **Operator only** | `.env` edit |
| HIGH-POST14-2 | **Local** | Two lines in `importer_validator.py` + one new test |
| MED-POST14-1 | **Architectural** | New `daemon_heartbeats` table + daemon write loop + health-read query — touches schema, daemon, route |
| MED-POST14-2 | **Local** | Grep audit + `rm` + README |
| MED-POST14-3 | **Local** | Single predicate fix in `stats.py` |
| MED-POST14-4 | **Local** | Env-gated branch in `auth.py` |
| LOW-POST14-1 | **Doc only** | One section in `architecture.md` |
| LOW-POST14-2 | **Local** | Three error-message changes in `import_auto.py` |
| LOW-POST14-3 | **Local** | Extend one list in `test_session_smoke.py` |
| ENH-POST14-1 | **Triggered** | No code — observation post-CRIT fix |
| ENH-POST14-2 | **Local** | Edit `SYSTEM_PROMPT` string |
| ENH-POST14-3 | **Triggered + operator** | Real-bank run (cannot automate from this session) |

## 4. Execution batches (ordered for risk minimization)

Six batches, each with its own backup, fix, rollback, and retest scope.
Go/no-go between batches.

---

### Batch 1 — Restore production LLM functionality (BLOCKER)

**Scope**: CRIT-POST14-1 (proxy 401) + HIGH-POST14-1 (`LLM_API_KEY` still set)

**Why first**: System has zero LLM functionality right now. Every other
fix happens against a still-broken stack, which makes any cross-test
involving LLM impossible.

**Pre-fix backup**:

```
# Tag the current "known-broken-but-stable" state
git tag pre-batch1-2026-06-05 HEAD -m "before LLM cluster switch"
git push origin pre-batch1-2026-06-05

# Snapshot data volume (no schema change in this batch, but cheap insurance)
sudo docker run --rm -v docker_tax_ai_data:/data:ro \
  -v /mnt/s/router_and_LAN/backups/www1/manual:/backup alpine \
  tar -czf /backup/tax-ai-data-2026-06-05_pre-batch1.tar.gz -C /data .

# Snapshot .env (it contains secrets — encrypt or move to a private path)
sudo cp /home/dblagbro/docker/.env /home/dblagbro/docker/.env.pre-batch1.bak
sudo chmod 600 /home/dblagbro/docker/.env.pre-batch1.bak
```

**Fix steps**:
1. (Operator) Provision a new key at `https://www.voipguru.org/llm-proxy/keys`
   with name `tax-ai-analyzer`. Capture the `llmp-*` prefix.
2. (Operator) Edit `/home/dblagbro/docker/.env`:
   - `LLM_PROXY2_KEY=<new-key>`
   - `LLM_PROXY2_URL=https://www.voipguru.org/llm-proxy/v1` (note: no "2" in URL)
   - `LLM_API_KEY=` (empty per HIGH-POST14-1)
3. (Code) Update `PUBLIC_LLM_PROXY2_URL` in `app/db/core.py` from
   `https://www.voipguru.org/llm-proxy2/v1` → `https://www.voipguru.org/llm-proxy/v1`.
4. (Code) Sweep `architecture.md` + `runbook.md` for `/llm-proxy2/` references
   → switch to `/llm-proxy/` where they refer to our endpoint.
5. (Ops) `docker restart tax-ai-analyzer`. Boot migration in `_migrate(conn)`
   will rewrite the existing `llm_proxy_endpoints.url` + `api_key` to match
   the new env.
6. (Verify) `docker exec tax-ai-analyzer python3 -c "from app import db; print(db.llm_proxy_list_endpoints(include_disabled=True))"` — URL and key tail match the new values.

**Rollback expectation**:
- If smoke test fails: revert `.env` from `.env.pre-batch1.bak`, `git checkout pre-batch1-2026-06-05 -- app/db/core.py architecture.md qa/runbook.md`, restart container. Boot migration will rewrite the DB row back to the old (also-broken) state.
- The data volume snapshot is for catastrophic corruption only; no schema change here.

**Retest scope after batch**:
- Full `pytest app/tests/` — 242/242 + 30 skipped
- Live probe: one `task=classification` call via `proxy_call.call_chat` — assert 2xx + non-empty `LLM-Capability` response header
- Live probe: one `task=codegen` call via `proxy_call.call_anthropic_messages` — assert 2xx + check `cost_class` extraction
- Live probe: one streaming `chat` SSE round-trip from the dashboard tab
- Re-fetch `/api/health/extended` from a real HTTP login — `overall.status` should be `healthy` (not `degraded`)
- Verify CrossFamilySubstitution exception NOT raised during these probes
- If `provider-hint=claude-oauth,anthropic,anthropic-direct;require` 503s on the new cluster, that's a Batch 1B sub-task: ping ops for the canonical provider_type names, possibly add `anthropic-max-gmail`/`anthropic-max-vg` to the comma-list.

**Go/no-go**: Don't proceed to Batch 2 until at least one real LLM call succeeds.

---

### Batch 2 — Codegen path correctness (HIGH)

**Scope**: HIGH-POST14-2 (validator accepts `email`) + LOW-POST14-3 (schema test column coverage)

**Why batch them**: Both are codegen/validator path correctness. Both add tests. Failure of either is detected by the same test invocation.

**Pre-fix backup**:
```
git tag pre-batch2-2026-06-05 HEAD
git push origin pre-batch2-2026-06-05
# No data backup needed — code + tests only.
```

**Fix steps**:
1. (Code) `app/ai_agents/importer_validator.py`:
   - Split `REQUIRED_RUN_IMPORT_PARAMS` into `REQUIRED_RUN_IMPORT_PARAMS` (fixed list minus `username`) + `CRED_PARAM_ALTERNATIVES = ("username", "email")`
   - In `_check_shape`, after the fixed-list check, assert at least one of the alternatives is present
2. (Tests) Add `test_email_cred_importer_passes` in `test_importer_validator.py` using a fixture that mirrors `chime_importer.py`'s shape
3. (Tests) Extend `test_session_smoke.py::TestFreshDbInit.test_fresh_init_creates_new_schema` `checks` list with the 7 Phase 11/14 columns + `("llm_usage", "cost_class")` (note: latter lives in `usage.db`, not `financial_analyzer.db` — may need a separate sub-test)
4. (Verify) `pytest app/tests/test_importer_validator.py app/tests/test_session_smoke.py -v`
5. (Verify) `pytest app/tests/` — overall 244/244 (+2 from the new tests)

**Rollback expectation**:
- Single-commit batch. `git revert <commit>` returns to the pre-batch state.
- Container hot-reloads bind-mounted Python — no restart, no DB change.

**Retest scope after batch**:
- Full pytest (now 244/244 expected)
- Validator probe: feed `chime_importer.py` source into `validate()` — assert returns `pass` (was `shape_error` before)
- Schema probe: re-run the new fresh-init test against a tmp DB — assert all listed columns present

---

### Batch 3 — API ergonomics + data hygiene (MED/LOW)

**Scope**: LOW-POST14-2 (slug 400 vs 404) + MED-POST14-2 (orphan empty `.db` files)

**Why batch them**: Both are low-blast-radius cleanup. Different subsystems but each is a single small change. Bundling reduces commit overhead.

**Pre-fix backup**:
```
git tag pre-batch3-2026-06-05 HEAD
git push origin pre-batch3-2026-06-05

# DB hygiene — re-snapshot since we're deleting files
sudo docker run --rm -v docker_tax_ai_data:/data:ro \
  -v /mnt/s/router_and_LAN/backups/www1/manual:/backup alpine \
  tar -czf /backup/tax-ai-data-2026-06-05_pre-batch3.tar.gz -C /data .

# Verify the orphan files are actually empty + unreferenced before delete
grep -rn "app\.db\|chat\.db\|tax_ai\.db\|tax_analyzer\.db\|llm_usage\.db" \
  /home/dblagbro/docker/tax-ai-analyzer/app/ \
  --include="*.py" | grep -v __pycache__ > /tmp/orphan-db-refs.txt
# If /tmp/orphan-db-refs.txt is empty, files are safe to delete.
# If not, DO NOT delete — investigate each reference.
```

**Fix steps**:
1. (Code) `app/routes/importers/import_auto.py`:
   - Modify `_resolve_importer` to return distinct error strings (`"invalid slug format"` vs `"bank not found"`)
   - Each route handler maps `"invalid slug format"` → 400, `"bank not found"` → 404
2. (Tests) Update `test_run_bank_import.py` (or new test file) to assert:
   - `with-dash` → 400 not 404
   - `UpperCase` → 400 not 404
   - `valid_slug_unknown` → 404 still
3. (Cleanup) After confirming `/tmp/orphan-db-refs.txt` is empty:
   - `docker exec tax-ai-analyzer rm /app/data/app.db /app/data/chat.db /app/data/tax_ai.db /app/data/tax_analyzer.db /app/data/llm_usage.db`
4. (Doc) Write `app/data/README.md` listing canonical DBs (`financial_analyzer.db`, `usage.db`) and what each holds.
5. (Verify) Full pytest 246/246.

**Rollback expectation**:
- Code: `git revert` works as in Batch 2.
- Data: re-extract the deleted files from the tarball if needed (they're empty anyway — restoration is cosmetic).

**Retest scope after batch**:
- Pytest (246 expected)
- Curl probe: `/api/import/auto/with-dash/status` → 400
- Curl probe: `/api/import/auto/nonexistent_bank/status` → 404
- File listing: `docker exec tax-ai-analyzer ls /app/data/` — only canonical DBs + onboarding/ + chrome_profiles/ + json state files
- Container restart cycle — verify no code path tried to write to a deleted DB (would crash)

---

### Batch 4 — Observability + dev hygiene (MED)

**Scope**: MED-POST14-3 (`paperless_configured` predicate) + MED-POST14-4 (rate-limiter loopback bypass)

**Why batch them**: Both affect the dev/QA loop without changing user-visible behavior in prod. Independent root causes.

**Pre-fix backup**:
```
git tag pre-batch4-2026-06-05 HEAD
git push origin pre-batch4-2026-06-05
# Code-only; no data snapshot needed
```

**Fix steps**:
1. (Code) `app/routes/stats.py`:
   - Locate the `features` dict assembly
   - Change `paperless_configured` predicate from whatever it currently checks (likely `db.get_setting("paperless_api_url")`) to `bool(os.environ.get("PAPERLESS_API_BASE_URL") and os.environ.get("PAPERLESS_API_TOKEN"))`
2. (Code) `app/routes/auth.py`:
   - Add env-gated bypass in `_ratelimited(ip)`: if `DEV_BYPASS_RATELIMIT_LOOPBACK=1` AND `ip in {"127.0.0.1","::1"}` → return False
3. (Tests) `test_auth_boundaries.py` — add assertions for both branches (env set, env unset).
4. (Verify) full pytest. Health probe via real HTTP — `paperless_configured: true`.

**Rollback expectation**:
- Single-commit batch; revert via `git revert`.
- No env-var change required (the new env-gate defaults off).

**Retest scope after batch**:
- Pytest (now 248+ expected)
- `/api/health/extended` features section — `paperless_configured: true`
- Rate-limit probe — with default env, behaviour unchanged. With `DEV_BYPASS_RATELIMIT_LOOPBACK=1`, no 429s on 20 bad-login probes.

---

### Batch 5 — Daemon heartbeat architecture (MED, architectural)

**Scope**: MED-POST14-1 (`/api/health/extended` thread inspection is process-local)

**Why on its own**: This is the only **architectural** fix in the list — touches schema (new table), daemon code (write loop), and route handler (read query). Larger blast radius; deserves its own commit + retest cycle.

**Pre-fix backup**:
```
git tag pre-batch5-2026-06-05 HEAD
git push origin pre-batch5-2026-06-05

# Schema change — full data snapshot
sudo docker run --rm -v docker_tax_ai_data:/data:ro \
  -v /mnt/s/router_and_LAN/backups/www1/manual:/backup alpine \
  tar -czf /backup/tax-ai-data-2026-06-05_pre-batch5.tar.gz -C /data .

# Confirm pytest baseline before any change
docker exec tax-ai-analyzer python3 -m pytest app/tests/ -q | tail -3
```

**Fix steps**:
1. (Schema) `app/db/core.py`:
   - Add `CREATE TABLE IF NOT EXISTS daemon_heartbeats (name TEXT PRIMARY KEY, ts TEXT NOT NULL DEFAULT (datetime('now')))` to the executescript in `init_db`
   - Idempotent — no ALTER needed since this is a fresh table
2. (DB helper) New `app/db/daemons.py`:
   - `record_heartbeat(name)` — `INSERT OR REPLACE INTO daemon_heartbeats(name, ts) VALUES(?, datetime('now'))`
   - `get_heartbeats()` → list of `{name, ts, seconds_since}` dicts
3. (Daemon code) `app/main.py`:
   - In `analysis_daemon()` loop body: after the work block, `db.record_heartbeat("analysis-daemon")`
   - In `_daily_dedup()` loop body: same with `"dedup-scheduler"`
4. (Route) `app/routes/stats.py:api_health_extended`:
   - Replace `threading.enumerate()` block with `db.get_heartbeats()` lookup
   - Compute alive = `seconds_since < threshold_per_daemon` (analysis → `2 * POLL_INTERVAL`, dedup → `90000` seconds = 25h)
   - Keep `threads.alive` for backwards compat but mark it `[process-local; informational]`
5. (Tests) New `app/tests/test_daemon_heartbeats.py`:
   - Test record + read round-trip
   - Test stale-heartbeat detection (mock `datetime`)
   - Test health endpoint returns correct alive/dead based on heartbeat ages
6. (Docs) Update `architecture.md` background-threads section to document the heartbeat pattern.
7. (Verify) Restart container. Wait for first dedup-scheduler cycle (immediately on startup) + first analysis-daemon cycle (within POLL_INTERVAL seconds). `/api/health/extended` reports both alive.

**Rollback expectation**:
- Code revert is straightforward (`git revert`).
- Schema rollback: the new table is empty; just `DROP TABLE daemon_heartbeats` if cleanup desired (otherwise leave; harmless).
- If the daemon write block has a bug that crashes the daemon: the daemon thread dies silently (Python doesn't bubble exceptions out of threads by default). Mitigation: wrap `record_heartbeat` calls in their own try/except inside the daemon loop body, so a heartbeat write failure can't kill the work loop.

**Retest scope after batch**:
- Full pytest (now 250+ expected)
- Restart container, then 60s later: `/api/health/extended` reports `analysis-daemon: alive` and `dedup-scheduler: alive`
- Force a daemon failure (e.g. corrupt the heartbeat code temporarily): health reports the named daemon as `dead` with `seconds_since > threshold`
- Probe from test_client (the old false-positive case): health now reports alive based on heartbeat row, NOT on `threading.enumerate()` — no false-positive

---

### Batch 6 — Documentation + triggered enhancements

**Scope**: LOW-POST14-1 (hyphen vs underscore docs) + ENH-POST14-1 (cost_class verification post-Batch-1) + ENH-POST14-2 (codegen prompt mentions email)

**Why last**: No runtime risk. Each is small. Batch them for one commit.

**Pre-fix backup**:
```
git tag pre-batch6-2026-06-05 HEAD
git push origin pre-batch6-2026-06-05
# Code+doc only; no data snapshot
```

**Fix steps**:
1. (Doc) `architecture.md` — append "URL / tab-key naming convention" subsection.
2. (Investigation) Verify cost_class is populated post-Batch-1 by querying `usage.db` for the last 24h of rows. If `cost_class` is mostly `unknown`, draft a question for llm-proxy2 ops about emission rules.
3. (Code) `app/ai_agents/bank_codegen.py:SYSTEM_PROMPT`:
   - In the "_login" closure documentation block, change `def run_import(username, password, ...)` to "first credential parameter is named `username` for password-login banks OR `email` for email-login banks (e.g. Chime). Use whichever matches the bank's actual auth flow."
4. (Verify) full pytest. Validator with new chime-style fixture still passes (regression guard against Batch 2 being undone).

**Rollback expectation**:
- Doc-only / prompt-only changes. `git revert` is trivial.

**Retest scope after batch**:
- Pytest unchanged
- Optional: a real codegen call against a synthetic email-login HAR to verify the prompt produces email-using output (LLM cost: ~$0.30)

---

### Triggered: ENH-POST14-3 — Real-bank retest after Phase 14

**Trigger**: User signals "go" on a live bank import.

**Not part of any batch** because cannot complete without operator action.

**Backup**: ensure `pre-claude-strip-2026-05-01` + `phase-14-2026-05-01` tags exist as recovery anchors (already do).

**Suggested first run**: `merrick` (no MFA, simplest closure shape). If healthy, re-test `usalliance` (has saved cookies + MFA dance).

**Retest scope**: watch logs for argument-shape errors. Confirm `{imported, skipped, errors}` shape returned. Verify no exception crosses the `run_bank_import` boundary unexpectedly.

## 5. Batch sequence rationale

```
Batch 1 (CRIT+HIGH ops) ────────────► restores LLM functionality
                │
                ▼
Batch 2 (HIGH+LOW correctness) ─────► validator no longer false-rejects email banks
                │
                ▼
Batch 3 (LOW+MED hygiene) ──────────► dispatcher returns right code, /app/data cleaned
                │
                ▼
Batch 4 (MED dev/observability) ────► paperless predicate honest; dev rate-limit fixed
                │
                ▼
Batch 5 (MED architectural) ────────► daemon heartbeat — non-rollback-risky thanks to standalone table
                │
                ▼
Batch 6 (LOW doc + ENH) ────────────► finish line; doc sync + prompt nudge
                │
                ▼
[Triggered]    ENH-POST14-3 — when user runs a live bank
```

## 6. Cross-batch invariants

- **Pytest must stay green between every batch**: 242 → 244 (Batch 2) → 246 (Batch 3) → 248 (Batch 4) → 250 (Batch 5) → 250 (Batch 6).
- **No batch removes or modifies an existing test** (only adds).
- **No batch touches `.env` except Batch 1**.
- **No batch touches schema except Batch 5**.
- **Each batch produces exactly one commit** (plus `.env` edit for Batch 1).
- **Each batch creates one `pre-batch<N>-2026-06-05` git tag for fast revert**.

## 7. Rollback levers available (already in place)

| Anchor | Purpose |
|---|---|
| `phase-14-2026-05-01` | Pre-QA-pass HEAD; safe target for full-batch revert |
| `pre-claude-strip-2026-05-01` | Historical reference (DO NOT roll back to this — re-introduces Claude attribution) |
| `dblagbro/tax-ai-analyzer:phase-14-2026-05-01` (Docker Hub, digest `sha256:54c469a947bb9eea...`) | Image rollback if any batch corrupts the build |
| `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-05-01_phase14.tar.gz` | Data volume rollback (3.7 MB) |
| New `pre-batch<N>` tags + `.env.pre-batch1.bak` + per-batch tarball | Per-batch granularity |

## 8. Hard "do not do" list during this remediation

- **Don't change `LLM_API_KEY` to a non-empty value** to "test if Anthropic still works" — operator confirmed account is closed; testing would either burn unknown credit or fail authentication.
- **Don't run `docker compose down`** — fleet rule, stops the whole stack including Paperless.
- **Don't force-push to `main`** without a backup tag pointing at the current HEAD (we just spent considerable effort rewriting history; another rewrite is unwanted).
- **Don't expand `_STRICT_PROVIDER_TASKS` in the substitution-guard** during this remediation — the cluster split makes that a moving target; revisit after Batch 1 settles.
- **Don't add `;require` to non-strict task LMRH hints** — current observation is that even `claude-oauth,anthropic,anthropic-direct;require` is now 503-prone on the wrong cluster. We need to live-verify on `/llm-proxy/` before committing to `;require` policies anywhere new.

## 9. Effort summary

| Batch | Risk | Code (mins) | Operator (mins) | Total (incl. retest) |
|---|---|---|---|---|
| 1 — Cluster switch | High blast radius | 15 | 10 | 45 |
| 2 — Validator + schema test | Low | 30 | 0 | 40 |
| 3 — Dispatcher + orphan DBs | Low | 25 | 0 | 35 |
| 4 — Observability + rate-limit | Low | 25 | 0 | 30 |
| 5 — Daemon heartbeat | Medium (architectural) | 70 | 0 | 100 |
| 6 — Doc + prompt | Trivial | 15 | 0 | 20 |
| **Total** | | **180 min** | **10 min** | **270 min (~4.5 hours)** |

ENH-POST14-3 (real-bank retest) is not included — it's user-triggered and untimed from here.

## 10. Success criteria for declaring "post-Phase-14 remediation complete"

- All 13 findings from `bug-log-post-phase14.md` either fixed, deferred with documented trigger, or explicitly accepted as low-priority.
- `pytest app/tests/` green at the new baseline (~250 tests).
- One successful live LLM round-trip through the proxy chain post-Batch-1.
- `/api/health/extended` reports `status: healthy` from a real HTTP login.
- Docker Hub `phase-14-remediated-2026-06-XX` tag pushed (whatever date the last batch lands).
- New backup tarball at `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-06-XX_post-remediation.tar.gz`.
- Memory file `tax_ai_resume_2026_05_01.md` refreshed with the post-remediation HEAD + the batch completion summary.
- This file itself moves from `qa/risk-controlled-fix-plan.md` to `qa/risk-controlled-fix-plan-COMPLETED-2026-06-XX.md` (archived).

---

**Planning ends here. No implementation until you approve the batch sequence and authorize Batch 1.**
