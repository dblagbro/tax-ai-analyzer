# QA Notes — Post-Phase-9 Pass #2 (2026-04-24)

## Observations only a second pass caught

Pass #1 hit 11 findings. Pass #2 hit 7 more — 4 of them security-class. The lesson: a single pass on a high-churn session is insufficient. A skeptical *second* pass, especially targeting surfaces that the first pass didn't touch (export, doc detail, rate-limiter with header spoofing, zombie-process hygiene), is where the real defects emerged.

Biggest value-add of Pass #2:
- **CRIT-PASS2-1 (XFF rate-limit bypass)** — completely defeats MED-5 under any public deploy
- **HIGH-PASS2-1 (export download broken)** — core product feature zero-functional via API
- **MED-PASS2-1 (doc 404 missing)** — would have caused hard-to-diagnose UI bugs downstream
- **CRIT-PASS2-2 (XSS entities leaked into prod DB)** — self-inflicted during Pass #1 probes

## Lessons about QA hygiene (self-reflection on Pass #1 mistakes)

1. **My own QA probes polluted production data.** Creating entities with literal slugs like `xss-test` doesn't survive the server-side slug normalizer. When I tried to clean up with `DELETE FROM entities WHERE slug='xss-test'`, the slug had been mangled to `_script_alert_1___script_` and my delete missed. **Fix going forward**: always use a `qa_probe_<timestamp>` prefix AND a post-probe catch-all `DELETE FROM entities WHERE created_at > <probe_start>`.

2. **My test entities 5 and 6 containing live XSS strings sat in the prod DB for ~30 minutes.** The client-side `escColor()` + `esc()` guards (MED-2 fix) prevented exploitation on the dashboard, but if ANY consumer of those rows (a future report, an export, a backup viewer) didn't escape, it would execute. **This is a process defect in my QA methodology, not in the app.**

3. **Smoke tests passing ≠ app serving.** Pass #1 ran `pytest app/tests/` → 73/73 right before finding that HTTP requests returned 000. pytest uses `flask_app.test_client()` which is an in-process Flask call — it never exercises the actual daemon, network, Xvfb, or docker exec boundary. **Going forward: add a cheap HTTP-level liveness probe to the test suite** (e.g., `test_healthz_returns_non_000` using `requests.get` against `http://localhost:8012/...`).

4. **Docker's "Up X minutes" status is a lie.** It only means the entrypoint process is alive — NOT that the app inside it is serving. Every CI-style smoke check should include an HTTP assertion, not a container-status check.

5. **Browser Secure cookies over HTTP are platform-specific.** curl + HTTP + Secure cookie = no re-send; Chrome over localhost + Secure = re-send works; Chrome over LAN-IP + HTTP + Secure = no re-send. If users ever access the app via a LAN IP rather than localhost, they can't log in. Worth surfacing in deployment docs.

## Things I verified twice

These hold under Pass #2 scrutiny:
- `Set-Cookie: ...; Secure; HttpOnly; SameSite=Lax; Path=/tax-ai-analyzer` — full 4-attribute set
- 5 security headers present on every response
- Credential mask leaks=0
- Mileage validation rejects all 8 bad inputs
- SQLite integrity=ok; 0 orphan links; tx/doc counts preserved
- Backup tarball restores cleanly (queries return correct counts)
- Rate limiter fires at attempt 11 when IP is real (127.0.0.1 or X-Forwarded-For absent)
- Wrong HTTP methods → 405 (correct)
- Accountant token: generate → use → revoke → re-use is 302 (correct state machine)

## Environment quirks

- **Xvfb needs `-ac`** — not `xvfb-run`'s defaults. Docker entrypoint bootstraps this manually.
- **DISPLAY is a PID 1 local** — `docker exec <container> <cmd>` doesn't inherit. Pass `-e DISPLAY=:99` or set `ENV DISPLAY=:99` in Dockerfile.
- **Patchright init scripts don't override `navigator.webdriver`** — expect `false` (boolean) under real Chrome 147 via patchright. CDP-level override needed for full masking.
- **`curl` stores HttpOnly cookies under `#HttpOnly_<host>` prefix** — naive `grep -v "^#"` filters them out.
- **`ProxyFix` in `web_ui.py` uses `x_proto=1, x_host=1, x_port=1` but NOT `x_for=1`** — X-Forwarded-For is not normalized; any route that reads it directly trusts whatever the client sent.

## Discovered surface gaps vs test-plan.md

Things not yet in any automated test:
- Health endpoint liveness over HTTP
- Export download end-to-end (would have caught HIGH-PASS2-1 at PR time)
- Doc detail 404 path (would have caught MED-PASS2-1)
- Rate-limiter IP resolution under proxy conditions
- Entity create response shape
- SSE connection lifecycle + thread cleanup
- Fresh-DB bootstrap with/without ADMIN_INITIAL_PASSWORD

## Test artifacts this pass

All in `qa/`:
- `bug-log-post-phase9-pass2.md` — 7 new findings + 8 re-verified + positive-confirmations + surfaces-not-tested
- `remediation-plan-post-phase9-pass2.md` — 12 ordered fixes with retest scope
- `qa-notes-post-phase9-pass2.md` — this file

Saved-in-place scripts (still in /tmp history, not yet committed):
- Bulk-edit probe (SQLi whitelist + happy-path mutation + revert)
- Export pipeline probe (all 8 formats)
- Accountant token flow (generate → anon-consume → revoke → reuse)
- Concurrent rate-limiter stress (20 parallel)
- SQLite WAL concurrency (10 readers + 1 writer)
- XFF rate-limit bypass probe

## Final net verdict

Working image runs correctly for single-user LAN. **Not release-ready for any public or multi-user deploy** until at least:
- HIGH-PASS2-1 (exports)
- CRIT-NEW-3 (open redirect)
- CRIT-PASS2-1 (rate-limit XFF bypass)
- HARDENING-PASS2-1 (commit uncommitted work)

are addressed. The remaining 15 findings are quality and hardening targets that can be sequenced as scope allows.
