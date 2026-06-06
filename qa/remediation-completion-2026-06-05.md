# Post-Phase-14 remediation close-out (2026-06-05)

Execution report for the 6-batch plan defined in `qa/risk-controlled-fix-plan.md`.
Captures what landed, what's still pending, and the verification evidence
for each batch.

## Snapshot

| Metric | Pre-remediation | Post-remediation |
|---|---|---|
| HEAD | `8eab99d` | `76be477` |
| Pytest passing | 242 | **256** (+14 net) |
| Pytest skipped | 30 | 30 (unchanged) |
| Open findings | 13 (from `bug-log-post-phase14.md`) | 2 deferred + 1 operator-blocked |
| Schema migrations | — | +1 table (`daemon_heartbeats`) |
| Live LLM functionality | broken (401) | **still broken** (CRIT-POST14-1 operator gate) |

Backup tags pushed: `pre-batch1-2026-06-05` … `pre-batch6-2026-06-05`.
Data tarball at `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-06-05_pre-batch5.tar.gz`.

## Batch-by-batch results

### Batch 1 — Cluster URL switch (CRIT-POST14-1 + HIGH-POST14-1)

**Status**: code-side complete (`52ca829`); operator step still required.

**Code shipped**:
- `PUBLIC_LLM_PROXY2_URL` constant flipped to `https://www.voipguru.org/llm-proxy/v1`.
- Existing `_LOCAL_HOST_FRAGMENTS` substring matcher already includes
  `llm-proxy2`, so the boot migration's URL-rewrite loop will pick up
  the live row and migrate it to the new URL automatically once the
  container next restarts with the new env.
- `README.md`, `qa/runbook.md`, `architecture.md` all updated; new
  "Cluster-split note (2026-06-05)" runbook subsection.
- 1 new test: `test_cluster_split_old_url_rewrites_to_new`.

**Operator action still required** (BLOCKING live LLM recovery):
1. Provision a new `llmp-*` key at `https://www.voipguru.org/llm-proxy/keys`
   (separate session cookie `llmproxy_clone_session`).
2. Edit `/home/dblagbro/docker/.env`:
   - `LLM_PROXY2_KEY=<new-key>`
   - `LLM_API_KEY=` (clear — HIGH-POST14-1)
   - `LLM_PROXY2_URL=https://www.voipguru.org/llm-proxy/v1` (optional)
3. `docker restart tax-ai-analyzer`. Migration auto-rewrites the URL
   and the api_key on the live `llm_proxy_endpoints` row.

**Verification probe** (run AFTER operator step):
```python
from app.llm_client.proxy_manager import build_anthropic_client
from app import db
ep = db.llm_proxy_list_endpoints(include_disabled=True)[0]
assert ep["url"] == "https://www.voipguru.org/llm-proxy/v1"
client = build_anthropic_client(ep, lmrh_hint="task=classification, cost=economy")
r = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=8,
                            messages=[{"role":"user","content":"OK"}])
print(r.content[0].text)  # → "OK"
```

### Batch 2 — Validator + schema test (HIGH-POST14-2 + LOW-POST14-3)

**Status**: complete (`7331236`).

- Validator now accepts `email` as alternative credential param (chime_importer no longer false-rejected).
- Schema-init test now verifies 7 Phase 11/14 columns on `generated_importers`.
- 3 new tests; full suite 245 passing.

### Batch 3 — Dispatcher 400/404 + orphan DB cleanup (LOW-POST14-2 + MED-POST14-2)

**Status**: complete (`1bbe2ab` + `d1cb116`).

- Auto-import dispatcher `_resolve_importer` now returns `(mod, error, http_status)`; invalid-shape slugs return 400 with diagnostic, unknown valid slugs return 404.
- Deleted 5 orphan 0-byte `.db` files from `/app/data/`: `app.db`, `chat.db`, `tax_ai.db`, `tax_analyzer.db`, `llm_usage.db`. Pre-grep audit confirmed zero code references.
- New `qa/data-layout.md` documents canonical DB layout.
- 1 new test (`test_auto_dispatcher_400s_invalid_slug_shape`) + 1 enhanced (`test_auto_dispatcher_404s_unknown_slug` now also asserts error message body).
- 247 passing.

### Batch 4 — Observability + dev rate-limit (MED-POST14-3 + MED-POST14-4)

**Status**: complete (`8fc59f6`).

- `paperless_configured` predicate now mirrors `paperless_client.py`'s actual fallback chain: `db.get_setting("paperless_token") or config.PAPERLESS_API_TOKEN`. Env-only configs no longer report false.
- `_rate_limited()` now has env-gated bypass: `DEV_BYPASS_RATELIMIT_LOOPBACK=1` skips the limit for `127.0.0.1`/`::1` only. Public IPs still limited. Default off in prod.
- 3 new tests (paperless env-var honesty; default-off loopback limited; bypass-on skips loopback). Existing XFF test hardened to reset rate-limit state.
- 250 passing.

### Batch 5 — Daemon heartbeat architecture (MED-POST14-1)

**Status**: complete (`0aaac29`). **The only architectural fix in the wave.**

- New `daemon_heartbeats(name TEXT PK, ts TEXT)` table — idempotent CREATE TABLE IF NOT EXISTS in `init_db`.
- New `app/db/daemons.py`: `record_heartbeat(name)` (INSERT OR REPLACE upsert), `get_heartbeats(expected_intervals, default=600s)`.
- Both daemons (`analysis_daemon`, `_daily_dedup`) write heartbeats each cycle, wrapped in their own try/except so a heartbeat failure cannot kill the daemon thread.
- `/api/health/extended` reports `expected_present` as `(in-threads OR heartbeat-fresh)` — eliminates the false-positive degraded report from cross-process callers (test_client, monitoring scripts).
- Pre-batch data tarball: `tax-ai-data-2026-06-05_pre-batch5.tar.gz` (3.7MB).
- 6 new tests (round-trip, upsert-not-append, stale-marked-dead, fresh-alive, default-interval, endpoint integration).
- 256 passing.

### Batch 6 — Codegen prompt + naming docs (LOW-POST14-1 + ENH-POST14-2)

**Status**: complete (`76be477`).

- Codegen `SYSTEM_PROMPT` instructs the model: first credential parameter is `username` for password-login banks OR `email` for email-login banks (e.g. Chime, Robinhood). Pick whichever the HAR shows.
- `architecture.md` now documents the URL hyphen vs JS-key underscore convention (`/folder-manager` URL → `folder_manager` JS key, both intentional, `loadTab()` substitutes between them).
- No new tests (pure doc/prompt change). 256 passing.

## Pytest delta summary

```
Pre-remediation:           242 passed, 30 skipped (242 total active)
Post-Batch-2:              245 passed, 30 skipped  (+3)
Post-Batch-1 code-side:    246 passed, 30 skipped  (+1)
Post-Batch-3:              247 passed, 30 skipped  (+1)
Post-Batch-4:              250 passed, 30 skipped  (+3)
Post-Batch-5:              256 passed, 30 skipped  (+6)
Post-Batch-6:              256 passed, 30 skipped  (+0 — doc/prompt only)
```

## Findings status (vs bug-log-post-phase14.md)

| ID | Title | Status |
|---|---|---|
| CRIT-POST14-1 | Proxy chain 401 | **code-side fixed; operator step pending** (provision new key + edit `.env`) |
| HIGH-POST14-1 | `LLM_API_KEY` still set | **deferred to operator** (operator clears `.env` in same edit as CRIT-POST14-1) |
| HIGH-POST14-2 | Validator rejects email-cred | **fixed** (Batch 2) |
| MED-POST14-1 | Health endpoint process-local | **fixed** (Batch 5) |
| MED-POST14-2 | Orphan empty DBs | **fixed** (Batch 3) |
| MED-POST14-3 | `paperless_configured` lies | **fixed** (Batch 4) |
| MED-POST14-4 | Rate limiter loopback bucket | **fixed** (Batch 4) |
| LOW-POST14-1 | Hyphen vs underscore | **fixed** (Batch 6, docs) |
| LOW-POST14-2 | Dispatcher 400 vs 404 | **fixed** (Batch 3) |
| LOW-POST14-3 | Schema test column gap | **fixed** (Batch 2) |
| ENH-POST14-1 | `cost_class` verification | **deferred** until live proxy works (Batch 1 operator step) |
| ENH-POST14-2 | Codegen prompt email-aware | **fixed** (Batch 6) |
| ENH-POST14-3 | Real-bank retest | **deferred** (user-triggered, no automation available) |

**Tally**: 10 fixed in code, 2 deferred to operator (CRIT-POST14-1 + HIGH-POST14-1 — single edit), 1 deferred until proxy works (ENH-POST14-1), 1 user-triggered (ENH-POST14-3).

## Image artifacts

| Artifact | Status |
|---|---|
| Local image rebuild | done — image ID `9bf6c3fd1eed` (4.22GB), built 2026-06-05 20:23 EDT |
| Docker Hub push `dblagbro/tax-ai-analyzer:phase-14-remediated-2026-06-05` | **done** — digest `sha256:8d3ea582abf12653…` (2026-06-06 00:42 UTC) |
| Docker Hub push `dblagbro/tax-ai-analyzer:latest` | **done** — same digest `sha256:8d3ea582abf12653…` |

Verify Hub post-push:
```bash
curl -sf "https://hub.docker.com/v2/repositories/dblagbro/tax-ai-analyzer/tags/?page_size=5" \
  | python3 -c "import sys,json;[print(t['name'],t['last_updated'][:19]) for t in json.load(sys.stdin)['results']]"
```

## Rollback levers (available)

| Anchor | Use case |
|---|---|
| `pre-batch1-2026-06-05` … `pre-batch6-2026-06-05` git tags | Per-batch revert |
| `phase-14-2026-05-01` git tag | Full revert to pre-QA HEAD |
| `dblagbro/tax-ai-analyzer:phase-14-2026-05-01` (Docker Hub, `sha256:54c469a947bb9eea…`) | Image rollback |
| `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-06-05_pre-batch5.tar.gz` | Data volume rollback (only batch with schema change) |

## What's NOT covered

- **Live LLM round-trip**: still blocked on Batch 1 operator step. No real LLM call has succeeded since the cluster split memo of 2026-06-05.
- **Real-bank retest** (ENH-POST14-3): the 5 Phase-14-refactored importers (merrick, chime, verizon, capitalone, usbank) have not been exercised against live banks since the orchestrator refactor. Closures pass structural tests but real-bank integration is the only proof. Recommended first re-test: merrick (no MFA, simplest closure shape).
- **Playwright browser UI**: no automated browser-driven UI tests possible from this environment.

## Recommended next steps

1. **Operator** (highest priority): complete Batch 1's pending step. New `llmp-*` key on `/llm-proxy/`, edit `.env`, restart container. Probe with the verification snippet above.
2. **Once live LLM works**: run the deferred ENH-POST14-1 probe (fire ~10 mixed-task calls, query `SELECT cost_class, COUNT(*) FROM llm_usage GROUP BY cost_class`).
3. **At user signal**: trigger ENH-POST14-3 real-bank retest (merrick first).
4. **Memory refresh**: when this is fully closed, update `tax_ai_resume_2026_05_01.md` to point at HEAD `76be477` + the new Docker Hub tag.

## Cross-batch invariants — held

- ✅ One commit per batch (Batch 3 has 2 commits because the gitignore relocate was incidental, not a logical "second batch").
- ✅ Pytest stayed green between every batch.
- ✅ Only Batch 5 touched schema (idempotent CREATE TABLE IF NOT EXISTS — never destructive).
- ✅ No `.env` touched by this session — all env changes are operator-gated.
- ✅ No `compose down`, no force-push to main, no destructive volume ops.

## Effort accounting

Plan estimate was ~4.5 hours code + 10 min operator.
Actual: ~2 hours of focused code work + image build + Hub push (operator step pending).
