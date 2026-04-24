# Release-Readiness Report — tax-ai-analyzer

**Pass date:** 2026-04-24
**Classification:** Deep regression + release hardening + security/permissions + exploratory + operational
**Build under test:** `dblagbro/tax-ai-analyzer@sha256:b0d42ef1`, commit `e4b225c`
**Test environment:** live container on `tmrwww01`, bind-mounted source, admin session
**Tester:** QA pass (senior-SDET role, AI-assisted)

---

## 1. Verdict

**NOT RELEASE-READY** — 1 critical and 1 high-severity issue block release under
the current severity model. Both are small, local fixes. All other findings are
quality/hardening work that can land post-release if desired.

### Blockers

| ID | Title | Effort |
|---|---|---|
| CRIT-1 | `/api/settings` leaks credential values (plaintext importer passwords, OAuth tokens, accountant token) to any authenticated DevTools session | ~10 lines |
| HIGH-2 | Default `admin`/`admin` seeded with no forced change | ~8 lines |

**HIGH-1 (no CSRF) is a blocker IFF this deploys publicly.** For single-user LAN
only, SameSite=Lax is a one-line mitigation.

### Non-blockers (summary counts)

- 2 high (HIGH-3 Dropbox OAuth state, HIGH-4 no inactive-user coverage)
- 7 medium (data validation, security headers, rate limiting, pinning, tag hygiene)
- 4 low (cosmetic, dead code)
- 12 enhancement / coverage gaps

Full details: `qa/bug-log.md`.

---

## 2. Scope actually covered

### Phases executed

| Phase | Method | Result |
|---|---|---|
| 1 Discovery | FS + doc inventory | `qa/test-plan.md` produced |
| 2 Static analysis | grep/ast/import audit | Dead-code + `print()` findings |
| 3 API endpoint matrix | 203 routes × {authed, unauthed} | `/tmp/qa_endpoint_matrix.py` |
| 4 Auth / permission boundaries | admin / user / inactive / unauthed matrix | HIGH-4 gap logged |
| 5 UI structural audit | HTML crawler + a11y + dup-IDs | LOW-4, MED-2 found |
| 6 DB integrity | FK / orphans / NULLs | MED-3 found (1 orphan) |
| 7 Security deep checks | path traversal, SQLi, XSS, file-upload abuse, CSRF probe, credential enum | CRIT-1, HIGH-1, HIGH-3 found |
| 8 Operational | threads, disk, backup sanity | MED-7 found |
| 9 Exploratory | chat SSE, tax review, folder manager, AI costs | No new defects |
| 10 Documentation | test-plan / bug-log / notes / remediation / backup / this report | All written |

### Critical user journeys (§test-plan.md)

| # | Journey | Result |
|---|---|---|
| 1 | Login → dashboard → logout | PASS |
| 2 | Add tx + receipt | PASS |
| 3 | Unmatched → manual link | PASS |
| 4 | Vendor merge | PASS |
| 5 | Bulk edit N rows | PASS (whitelist holds vs SQLi probe) |
| 6 | Mileage add → CSV export | **FAIL** — accepts invalid date + infinite miles (MED-1) |
| 7 | Reports → YoY → CSV | PASS |
| 8 | Accountant token → /accountant | PASS |
| 9 | Plaid settings save (no creds) | PASS |
| 10 | IMAP settings save (no creds) | PASS |
| 11 | 8 export formats | PASS (all stream, no buffer blow-up) |
| 12 | Activity log filter/paginate | PASS |

### Surfaces explicitly NOT tested (§`qa-notes.md`)

Live Playwright bank sessions, live LLM, real Plaid, real Dropbox OAuth,
IMAP with a real mailbox, backup/restore roundtrip, concurrent multi-thread
writes, mobile viewport, load/stress, paperless cross-product integration.

---

## 3. Security posture summary

| Area | State | Notes |
|---|---|---|
| AuthN | OK | bcrypt cost 12, session cookies, `@requires_auth` on all write endpoints |
| AuthZ | Gaps | HIGH-4: no test confirming inactive user is denied |
| Credential storage | **Broken** | CRIT-1: readable plaintext via `/api/settings` |
| CSRF | **Missing** | HIGH-1; SameSite=None makes it worse |
| XSS | Minor gap | MED-2: entity name/color/slug un-escaped in innerHTML |
| SQLi | OK | all queries parameterized; bulk-edit column whitelist holds |
| Path traversal | OK | no `../` sneaks through receipt / attachment endpoints |
| File upload | OK | size + mime-type + extension checks present |
| Default credentials | **Broken** | HIGH-2: admin/admin seeded |
| Rate limiting | Missing | MED-5; mitigates brute-force on `/login` |
| Security headers | Missing | MED-4: no CSP / X-Frame-Options / HSTS |
| Secrets-in-logs | OK | spot-check showed no plaintext logging (separate from CRIT-1 which is an API response issue) |

---

## 4. Data integrity

- 3777 transactions, 4574 analyzed documents, 664 transaction_links, 4906 activity rows.
- 1 orphan row in `transaction_links` pointing to a deleted transaction (MED-3).
- No other orphans, no NULLs where NOT NULL expected, all FKs walk cleanly via
  `get_connection()`.
- SQLite in WAL mode, `PRAGMA integrity_check;` returns `ok`.
- Mileage table accepted `float('inf')` + non-ISO dates during QA probe (MED-1).

---

## 5. Operational readiness

| Concern | State |
|---|---|
| Background threads (analysis-daemon, dedup-scheduler) | Running; exit cleanly on container stop |
| Disk usage | `data/` 1.4 GB; uploads/ 890 MB; healthy |
| Docker image stability | Using `:latest` mutable tag (MED-7) — not reproducible |
| Dependency pinning | Not pinned (MED-6) |
| Healthcheck | `GET /healthz` → 200; responds in < 30 ms |
| Single-container restart | Works via `docker compose up -d --force-recreate --no-deps tax-ai-analyzer` |
| Backup/restore | Manual tarball works; not scripted end-to-end (ENH-5) |
| Startup logs | Clean; no tracebacks on cold start |

---

## 6. Coverage metrics

| Artifact | Count | Notes |
|---|---|---|
| Routes exercised | 203 / 203 | `/tmp/qa_endpoint_matrix.py` |
| HTML templates crawled | 47 | duplicate-ID and innerHTML audits |
| DB tables audited | 21 / 21 | FK + orphan + NULL scan |
| Python modules grep'd for `print()` in app code | 1 finding | LOW-1 (`diag_usalliance.py`) |
| Dead `getElementById()` refs | 3 | LOW-4 |
| Dead `tf-cat` CSS class refs | 4 | LOW-3 |
| Session-smoke suite | 6 / 6 pass |
| Other `app/tests/*.py` | Not uniformly invoked by CI | Tracked as ENH-1, ENH-6 |

---

## 7. Recommendations

### Must fix before release (under single-user LAN threat model)
1. CRIT-1: suffix-based credential mask in `api_settings_get()`.
2. HIGH-2: refuse to seed default admin when `ADMIN_INITIAL_PASSWORD` env var is unset.

### Must fix before any public / multi-user exposure
3. HIGH-1: full CSRF (Flask-WTF) — or interim: `SESSION_COOKIE_SAMESITE=Lax` one-liner.
4. MED-4: minimal security headers (`X-Frame-Options`, `X-Content-Type-Options`,
   a conservative CSP, HSTS if TLS-terminated upstream).
5. MED-5: per-IP rate limit on `/login` (and bulk endpoints).

### Should fix in the next sprint
6. MED-1: mileage date + isfinite validation.
7. MED-2: `esc()` on entity name/color/slug innerHTML interpolation.
8. MED-3: one-off orphan cleanup.
9. MED-6 + MED-7: pinned deps + dated image tag.
10. HIGH-3: verify Dropbox OAuth `state` param.

### Nice to have
11. LOW-1..4: cosmetics, dead code.
12. ENH-1..12: coverage expansion.

### Process recommendations
- Promote `session_smoke` → `full_smoke` that invokes all `app/tests/*.py`, not just
  the session file. Tracked as ENH-6.
- Add `qa_sec.py` sections [1], [2], [6], [7], [8] into `app/tests/` as committed
  regression harnesses. They caught CRIT-1 and MED-1 this pass.
- Adopt the dated-tag image scheme from MED-7 as a permanent release discipline.

---

## 8. Mandatory review gate

**Per QA prompt: no fix phase may begin until backup/snapshot planning has been
completed and reviewed.**

Deliverables ready for user review:
- [x] `qa/test-plan.md`
- [x] `qa/bug-log.md`
- [x] `qa/qa-notes.md`
- [x] `qa/remediation-plan.md`
- [x] `qa/backup-plan.md`
- [x] `qa/release-readiness-report.md` (this file)

**PAUSING for user review. No remediation work will begin until the user responds.**

Open decisions required from user:
1. Approve release-blocker classification (CRIT-1 + HIGH-2; HIGH-1 conditional).
2. Pick HIGH-1 remedy: full Flask-WTF CSRF vs. SameSite=Lax one-liner.
3. Pick HIGH-2 remedy: env-var gate vs. force-change-on-first-login.
4. Confirm MED-3 orphan row can be deleted.
5. Confirm order of execution from `remediation-plan.md` §Ordered Execution.
6. Confirm baseline tarball at
   `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-04-23_2004-step1pause.tar.gz`
   is acceptable or a fresh Checkpoint-0 snapshot should be taken first (I recommend fresh).

---

## 9. Sign-off

Pass status: **COMPLETE — AWAITING USER REVIEW**
Next action: await user decisions on open items §8, then execute `remediation-plan.md`
in the order listed, gated by `backup-plan.md` checkpoints.
