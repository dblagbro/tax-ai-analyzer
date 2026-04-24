# QA Notes — Post-Phase-9 (2026-04-24)

## Environment
- Host: tmrwww01, Ubuntu 22.04.5
- Rebuilt image this pass twice:
  1. After xvfb-run → docker-entrypoint.sh swap (CRIT-NEW-1 fix)
  2. After adding `-ac` to Xvfb + `COPY tools/` (CRIT-NEW-2 + HIGH-NEW-2 fix)
- Running image: `dblagbro/tax-ai-analyzer:2026-04-24-qa-remediated` (new SHA after rebuild; old `93126135781d` superseded)
- DB state identical: 3777 tx, 4574 docs, 663 links, 0 orphans, integrity_check=ok
- Xvfb :99 running under app process; Chrome 147.0.0.0 launchable via patchright

## Things learned the hard way this pass

1. **`xvfb-run` is broken in Docker without tty.** Used everywhere in production browser-automation tutorials, but the SIGUSR1 sync-on-ready dance hangs under Docker (no controlling terminal, or something in how `/bin/sh` handles `trap`). Replacing it with an explicit background-start + `exec` pattern is more reliable AND easier to debug.

2. **Xvfb needs `-ac` OR an Xauthority cookie.** Without either, clients launched in a different process context (patchright, headful Chrome) can't connect even though the Unix socket is visible. `-ac` is safe inside the container network boundary.

3. **`docker exec` doesn't inherit env from PID 1.** So `docker exec <container> python3 -c "playwright launch headful"` fails with "no XServer" even when the app's PID 1 has `DISPLAY=:99`. Either pass `-e DISPLAY=:99` or set `ENV DISPLAY=:99` in Dockerfile (recommended).

4. **Rate limiter's IP tracking resets on container restart.** In-memory `_login_fail_log: dict[str, deque[float]]` with a `threading.Lock`. If you hit >10 bad logins from one IP during QA, you're locked out for 5 minutes OR until the container restarts.

5. **`ps -ef` is a great canary.** If PID 1 is the process-manager wrapper and the actual app isn't in the tree, the container is "running" by Docker's definition but actively NOT serving. Worth building into a healthcheck.

6. **`curl` stores `HttpOnly` cookies with the `#HttpOnly_` prefix in its cookie jar file.** A naive `grep -v "^#"` filters them out of a listing. The cookies still work, but debug output misleads.

## Open questions / decisions for the user

1. **CRIT-NEW-3 open redirect** — is this LAN-only or ever public? If public, blocker.
2. **HIGH-NEW-1 env var** — what value for `TAX_AI_ADMIN_PASSWORD`? Recommend a generated 16-char random, stored in `.env` (gitignored).
3. **MED-NEW-4 `navigator.webdriver`** — do we proactively mask with an `add_init_script`, or wait until an actual bank importer is observed to fail on this signal?

## Surfaces covered this pass

- ✅ Container health + process tree + startup logs
- ✅ Full pytest suite (73/73)
- ✅ Static JS asset serving (15 modules, all HTTP 200)
- ✅ Dashboard HTML render (14 tabs, all present)
- ✅ Import Hub partials (16 source panels, all present)
- ✅ Credential-mask re-probe (CRIT-1)
- ✅ Mileage validation matrix (MED-1) — 8 bad inputs + 1 valid
- ✅ Rate limiter (MED-5) — 10 pass, 11-12 429
- ✅ Security headers (MED-4) — 5 of 5 present
- ✅ SameSite=Lax + HttpOnly + Secure cookie attributes (HIGH-1)
- ✅ Unauth endpoint redirect behavior
- ✅ DB integrity + orphan scan (MED-3)
- ✅ Backup tarball integrity
- ✅ Path traversal probes (2 probes, both rejected)
- ✅ SQL-injection whitelist probe (bulk-edit rejected non-whitelisted column)
- ✅ Server-side XSS on entity color (NEW finding: accepted)
- ✅ Open redirect on /login (NEW finding: vulnerable)
- ✅ Patchright headless + headful + channel=chrome browser launch
- ✅ Navigator.webdriver leak check
- ✅ `/app/tools/` presence (NEW finding: missing, then fixed)
- ✅ ADMIN_INITIAL_PASSWORD env-var wiring (NEW finding: missing)
- ✅ SSE endpoint content-type check
- ✅ Tab-loader registry coverage (NEW finding: entities.js missing, but functional)

## Surfaces NOT tested (and why)

(Same as prior pass + this session's additional exclusions.)

- Live bank importers against real banks — user account lockout check outstanding for US Bank; can't safely test the others without live credential exposure
- LLM API calls — cost/rate-limit
- OAuth roundtrips (Gmail, Dropbox) — external service dependency
- IMAP with a real mailbox — external
- Load/stress — single-user product
- Multi-user concurrency — single-user product
- Backup/restore end-to-end — verified tarball integrity but did not restore into a fresh instance
- Click-through Playwright test in a real browser — curl-only HTML crawl; a real browser would catch JS console errors and late-bound rendering bugs

## Test artifacts

- `qa/bug-log-post-phase9.md` — this pass's findings (11 new items, 14 re-verified)
- `qa/remediation-plan-post-phase9.md` — ordered fix list + retest scope
- `qa/qa-notes-post-phase9.md` — this file

Saved scripts used during the pass (stayed in /tmp, can be promoted to `app/tests/` if desired):
- Security header curl probe
- Mileage validation matrix
- Rate-limit 12-probe
- Patchright headful launch probe
- Entity shape probe
