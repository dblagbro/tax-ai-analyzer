# QA Notes — tax-ai-analyzer

Observations, open questions, and ideas captured during the 2026-04-24 QA pass.
These are NOT bugs — they are items worth revisiting, tracking, or deciding on.

## Environment
- Build sha at start of pass: `sha256:b0d42ef1` (commit `e4b225c`)
- Live DB state at pass start:
  - transactions: 3777 (3774 Gmail, 1 OFX, 2 bank_csv)
  - analyzed_documents: 4574
  - transaction_links: 664
  - activity_log: 4906
  - users: 2 (admin, dblagbro)
  - entities: 4
  - mileage_log: 0
  - plaid_items: 0

## Running order
1. discovery — complete
2. planning — complete
3. design — complete
4. execution — complete
5. defect logging — complete (`qa/bug-log.md`)
6. remediation planning — complete (`qa/remediation-plan.md`)
7. backup planning — complete (`qa/backup-plan.md`)
8. release-readiness report — complete (`qa/release-readiness-report.md`)
9. pause for user review — PENDING (current state)

## Open questions / areas I want the user's input on

1. **HIGH-1 CSRF — choose remedy.** Full Flask-WTF (thorough, touches every form) vs
   one-line `SESSION_COOKIE_SAMESITE=Lax` (blocks most practical CSRF, ~zero risk).
   My recommendation: SameSite=Lax now, revisit full CSRF if this ever gets
   exposed beyond LAN.
2. **HIGH-2 admin/admin — choose remedy.** (a) Require `ADMIN_INITIAL_PASSWORD` env var
   at first boot, refuse to seed a default admin without it, OR (b) keep default seeding
   but force password change on first login. Option (a) is simpler; option (b) is
   smoother for new users. My recommendation: (a) — we're the only deployer.
3. **Release-blocker delta for this deploy.** Classification assumes single-user
   LAN-only deploy. Confirm that's correct — if this tool is ever going public,
   HIGH-1 becomes a blocker too.
4. **MED-3 orphan cleanup.** One orphan row in `transaction_links`. Confirm you want
   it deleted vs preserved for forensic purposes (link_id is in `qa/bug-log.md`).
5. **MED-6 pinning.** Pin exact versions or minor-range? Exact is safer, minor-range
   is easier to patch. My recommendation: exact with quarterly refresh.
6. **MED-7 image tag.** Use date-based tag (`2026-04-23-playwright-step1`) or
   git-sha tag (`e4b225c`)? Date is human-readable; sha is authoritative. My
   recommendation: combined `YYYY-MM-DD-<short-sha>`.
7. **LOW-3 tf-cat dead refs.** Confirm `tf-cat` is truly dead (nothing on a feature
   branch is about to land that re-uses these).

## Surfaces I did NOT test (and why)

| Surface | Why skipped |
|---|---|
| Live Playwright bank logins (US Bank, USAA, Chase, BofA) | Would risk triggering account lockouts and contacting real banks. US Bank credentials were rejected on job 61 — suspected account lock, unresolved until user verifies. |
| Live LLM provider calls | Cost + rate-limit concerns; LLM output quality is not a QA-pass deliverable. |
| Real Plaid `/link/token/create` and `/item/public_token/exchange` | Requires live Plaid credentials; error paths were exercised but no real handshake. |
| Real Dropbox OAuth callback | Covered in code audit (HIGH-3, state param unused). No live roundtrip performed. |
| IMAP with a real mailbox | Error paths only — no live IMAP server test. |
| Backup/restore round-trip with a real tarball | Planned for remediation — logged as ENH-5. |
| Concurrent SQLite writes under 10+ threads | Single-user product; concurrency is theoretical. Logged as ENH-3. |
| Mobile / small-viewport UI | Product is single-user desktop-only by design. |
| Load / stress (wrk, k6, ab) | Not a release-readiness gate for a single-user tool. |
| Paperless-ngx ↔ tax-ai-analyzer integration | Separate product boundary; paperless analyzer has its own test suite. |

## Test artifacts (scripts saved in /tmp)

- `/tmp/qa_sec.py` — master security probe (sections [1] path traversal, [2] SQLi,
  [3] XSS reflection, [4] file upload abuse, [5] auth bypass probes, [6] credential
  enumeration on `/api/settings`, [7] CSRF form-post without token, [8] data-integrity
  mileage abuse)
- `/tmp/qa_html_crawler.py` — static HTML audit (duplicate IDs, orphan
  `getElementById()` refs, unescaped `innerHTML` interpolations, dangling form-label
  linkage, `tf-cat` dead class refs)
- `/tmp/qa_db_audit.py` — FK integrity + orphan row finder + NULL-where-expected scan
- `/tmp/qa_endpoint_matrix.py` — exercises every `@app.route` in authed + unauthed +
  inactive-user contexts and records status code + content-type
- `/tmp/qa_static.py` — grep-based AST scan: dead imports, unused fixtures, TODO density,
  `print()` in application code, missing `@requires_auth` on write routes

All were retained under `/tmp/` for the duration of the pass. Can be moved into
`qa/scripts/` if we want them committed as permanent regression harnesses
(recommended for §§1, 2, 6, 7, 8 of `qa_sec.py` in particular).

## Misc observations (not bugs)

- `app/tests/test_session_smoke.py` is the only test file that runs in CI by default.
  The other `app/tests/*.py` files exist but aren't all invoked by the smoke runner.
  Candidate for CI expansion (tracked in ENH-1 and ENH-6).
- `app/static/js/transactions.js` has a 230-line `renderRow()` function that's a
  prime target for bug density — no obvious defect found, but flagged for future
  split.
- Gmail date backfill touched 3774 rows without a dry-run flag. Fine this time;
  any future mass-migration should ship with `--dry-run` by default (tracked as
  process note, not a bug).
- Accountant token generation uses `secrets.token_urlsafe(24)` — 192 bits, fine.
  Just noting for completeness.
- `/api/export/*` exporters all stream instead of buffering — good practice,
  no memory blow-up even on the 3777-row tx set.

## Baseline performance (informational)

On the live container (b0d42ef1):
- `/api/transactions?limit=100`: p50 ~85 ms, p95 ~180 ms
- `/api/dashboard/summary`: p50 ~140 ms, p95 ~320 ms
- `/api/export/csv` (full year): ~2.1 s for 3777 rows
- `/login` POST: p50 ~340 ms (bcrypt cost 12) — acceptable

No performance regressions observed vs last pass.
