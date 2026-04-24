# Test Plan — tax-ai-analyzer QA Pass 2026-04-24

**Author:** QA pass (senior-SDET-role, AI-assisted)
**Trigger:** Post-feature-session regression hardening + release-readiness validation
**Classification:** Deep regression + release hardening + security/permissions validation + exploratory + operational

## Scope
Everything under `/home/dblagbro/docker/tax-ai-analyzer/` as of commit `e4b225c`. Excludes live external API calls (bank sites, LLM providers) to avoid cost/lockout side-effects.

## Entry criteria (met)
- Container `tax-ai-analyzer` running on `dblagbro/tax-ai-analyzer:latest` (sha256:b0d42ef1)
- Session smoke suite passing (6/6)
- Git tree clean at `e4b225c`
- Backup exists: `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-04-23_2004-step1pause.tar.gz`

## Exit criteria
- All planned test phases executed
- All findings logged in `bug-log.md` with severity + reproduction
- `remediation-plan.md` written, grouped by severity
- `backup-plan.md` written covering fix cycle
- Explicit pause requested for review before remediation

## Test phases

| # | Phase | Method | Artifacts |
|---|---|---|---|
| 1 | Discovery | Filesystem + doc inventory | test-plan.md (this) |
| 2 | Static analysis | grep, ast, import audit, dead-code + TODO scan | bug-log.md entries |
| 3 | API endpoint matrix | Flask test_client; live curl for thread-backed jobs | bug-log.md entries |
| 4 | Auth / permission boundaries | Matrix of authed / unauthed / admin / inactive on sensitive routes | bug-log.md entries |
| 5 | UI structural audit | HTML crawler (extended with a11y + duplicate IDs + form-label linkage) | bug-log.md entries |
| 6 | Database integrity | SQL audit of FK, orphans, NULL where not expected, bad migrations | bug-log.md entries |
| 7 | Security deep checks | Path traversal probes, SQLi attempts, XSS in user-controlled fields, file-upload abuse | bug-log.md entries |
| 8 | Operational | Background threads, disk, backup round-trip, rollback dry-run | bug-log.md entries |
| 9 | Exploratory | Chat SSE, tax review, folder manager, AI costs — surfaces untouched recently | bug-log.md entries + qa-notes.md |
| 10 | Documentation | Write remediation-plan.md, backup-plan.md, release-readiness-report.md | all files |

## Critical user journeys to verify
1. **Login → dashboard loads → logout.** Most common path.
2. **Add transaction manually + attach receipt.** Recently added feature, file upload path.
3. **Transactions tab → Unmatched sub-tab → manual link.** Cross-source dedup UI.
4. **Transactions tab → Vendors sub-tab → merge vendors.** Data mutation at scale.
5. **Transactions tab → bulk edit category/entity on N rows.** Data mutation at scale.
6. **Mileage tab → add entry → CSV export.** New feature end-to-end.
7. **Reports tab → YoY compare → download CSV.** Aggregation surface.
8. **Settings tab → generate accountant token → visit /accountant in incognito.** Auth-with-token.
9. **Import Hub → Plaid tab → save settings → start sync (no real creds).** Error-path coverage.
10. **Import Hub → IMAP tab → save settings → test connection (no real creds).** Error-path coverage.
11. **Exports — all 8 formats for a real entity/year.** Regression of exporters.
12. **Activity log → filter/paginate.** New UI coverage.

## Known risks and focus areas
- **Shipped 14 new features, 20 fixes in one session** — high churn = high chance of latent bugs
- **Playwright importers** not tested live for all 5 banks
- **Password leak in HTML dump** (fixed; verify no other similar leaks)
- **Bulk edit endpoints** (whitelisted but worth SQLi probing)
- **Accountant token authn** (new orthogonal auth path — bypass potential)
- **Gmail date backfill** touched 3774 rows — validate no data regression
- **SQLite WAL under concurrent thread access** — race conditions

## Assumptions
- No real LLM API calls during this pass
- No live bank logins
- Admin session available via `admin` / `admin` default
- Volume mount bind delivers code changes without rebuild

## Severity model
- **Critical**: data loss, credential leak, auth bypass, crash-on-load
- **High**: broken common feature, remote exploit, crash on common path
- **Medium**: degraded feature with workaround, UX failure
- **Low**: cosmetic, edge case
- **Enhancement**: gap not caused by a defect (missing test, missing doc, missing feature)

## Coverage matrix
See separate table in the main assistant response. Key uncovered areas explicitly:
- Live Playwright runs (blocked on user)
- LLM output quality (not a QA scope issue)
- Multi-user concurrent UI use (single-user tool)
- Mobile/responsive UI (single-user desktop-only tool by design)

## Out-of-scope for this pass
- Live bank logins
- Load/stress testing
- Multi-user concurrency (product is single-user)
- Real-user UX (user did their own click-through previously)
