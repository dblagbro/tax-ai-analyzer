# Bug Log — Post-Phase-9 Regression Pass (2026-04-24)

**Pass type:** Deep regression + release hardening
**Build under test:** `dblagbro/tax-ai-analyzer:2026-04-24-qa-remediated` (rebuilt twice during this pass — xvfb-run → entrypoint script, then `-ac` added to Xvfb)
**Commits covered:** `2af3853`, `f729e64`, `f6174e2` (Phases 6/7/8 refactor + QA remediation + Phase 9 Playwright)
**Baseline:** prior `qa/bug-log.md` (all CRIT/HIGH/MED-1..7 + LOW-1..4 + ENH-1/4/6 remediated)

---

## NEW — Release-blocker class

### CRIT-NEW-1 — App daemon never starts under `xvfb-run` wrapper (Phase 9 regression)

- **Area:** Docker entrypoint
- **Severity:** **CRITICAL** (release blocker; prevents container from serving any request)
- **Evidence:** After Phase 9 rebuild, `docker ps` shows container "Up", port 8012 published, Xvfb running — but `ps -ef` inside container shows only `xvfb-run` shell + `Xvfb` process. `python -m app.main` never forked. `docker logs` returns empty. Any HTTP request to `http://localhost:8012/tax-ai-analyzer/` returns `status: 000` (connection refused / empty reply, curl exit 56). Multiple `docker restart tax-ai-analyzer` cycles reproduced the hang.
- **Reproduction:** The image from commit `f6174e2` with `CMD ["xvfb-run", "-a", "--server-args=...", "python", "-m", "app.main"]`. Start container, wait 30s, attempt any HTTP request.
- **Root cause:** Debian's `/usr/bin/xvfb-run` script uses `trap : USR1` + `wait` to synchronize on Xvfb's ready signal. In this container, the signal is either not delivered or the trap doesn't interrupt `wait` — the shell hangs at `wait` and never reaches `"$@"` (the Python command). Xvfb is up but the wrapped app is never exec'd.
- **Fix applied (this pass):** Replaced `xvfb-run` with a custom `docker-entrypoint.sh` that starts Xvfb in the background, polls for its Unix socket, `export DISPLAY=:99`, then `exec python -m app.main`. Dockerfile `CMD` updated.
- **Status:** **FIXED** — verified `python -m app.main` now runs as PID 1; Flask serving on 8012.

### CRIT-NEW-2 — Xvfb rejects client connections without `-ac` flag (Phase 9 regression)

- **Area:** Browser automation / Xvfb
- **Severity:** **CRITICAL** (prevents any bank importer from launching Chrome; every Playwright import would `TargetClosedError`)
- **Evidence:** With the entrypoint fix from CRIT-NEW-1 live, `patchright.chromium.launch(headless=False, channel='chrome')` still failed with `BrowserType.launch: Target page, context or browser has been closed` and browser logs "Looks like you launched a headed browser without having a XServer running." `xdpyinfo -display :99` returned "unable to open display :99" even though `/tmp/.X11-unix/X99` existed. Xvfb was up but refusing clients because no Xauthority cookie was configured.
- **Root cause:** My entrypoint script started `Xvfb :99 -screen 0 1280x900x24 -nolisten tcp` without `-ac` (disable access control) or a matching Xauthority cookie. `xvfb-run` normally generates one; our replacement didn't.
- **Fix applied (this pass):** Added `-ac` to the Xvfb launch in `docker-entrypoint.sh`. `-ac` disables all X server access control — safe inside the container network boundary.
- **Status:** **FIXED** — verified `patchright.chromium.launch(headless=False, channel='chrome')` now returns a real Chrome browser with UA `Chrome/147.0.0.0`.

### CRIT-NEW-3 — Open redirect in `/login?next=...` (pre-existing; still blocking)

- **Area:** `app/routes/auth.py:login()`
- **Severity:** **CRITICAL** (phishing vector; classic open-redirect)
- **Evidence:** `curl -i -X POST -d "username=admin&password=admin" "http://localhost:8012/tax-ai-analyzer/login?next=https%3A%2F%2Fevil.com%2F"` returns `Location: https://evil.com/`. Server blindly trusts the `next` query param.
- **Attack shape:** An attacker sends `https://<app>/login?next=https://phishing.site/fake-bank` to a victim; after legitimate login, victim is redirected to attacker-controlled URL that can impersonate a continuation of the app.
- **Fix:** validate `next` at the auth route — accept only paths starting with `/` and belonging to this app's URL prefix. Reject any value containing `://` or starting with `//`.
- **Status:** UNFIXED — release blocker for any public deploy. Acceptable risk for LAN-only single-user, but trivial to fix.

---

## NEW — High

### HIGH-NEW-1 — `ADMIN_INITIAL_PASSWORD` env var not set in production compose

- **Area:** `docker-compose.yml` + `ensure_default_data()` gate (HIGH-2 fix from prior pass)
- **Severity:** **HIGH** (latent — only triggers on a data volume reset or fresh deploy)
- **Evidence:** `docker exec tax-ai-analyzer printenv | grep ADMIN` returns nothing. Parent-repo `docker-compose.yml` has no `ADMIN_INITIAL_PASSWORD` entry under the `tax-ai-analyzer` service. The HIGH-2 fix raises `RuntimeError` from `ensure_default_data()` if the env var is unset AND no users exist — which means a pristine DB boot crashes the app startup.
- **Fix:** Add `ADMIN_INITIAL_PASSWORD: ${TAX_AI_ADMIN_PASSWORD}` under `services.tax-ai-analyzer.environment` in compose, with a default in `.env`.
- **Status:** UNFIXED — currently harmless because the DB has 2 existing users, but a `docker volume rm docker_tax_ai_data` + restart would fail to boot.

### HIGH-NEW-2 — `/app/tools/` not present inside container (Phase 8 regression)

- **Area:** `Dockerfile` COPY directives
- **Severity:** **HIGH** (operational — diag script unreachable)
- **Evidence:** Phase 8 moved `app/diag_usalliance.py` → `tools/diag_usalliance.py` on the host, but `Dockerfile` only did `COPY app/ ./app/` + `COPY profiles/ ./profiles/`. `docker exec tax-ai-analyzer ls /app/tools/` returned "No such file or directory". Anyone trying to run the diag inside the container failed.
- **Fix applied (this pass):** Added `COPY tools/ ./tools/` to Dockerfile. Verified `/app/tools/diag_usalliance.py` present after rebuild.
- **Status:** **FIXED**.

---

## NEW — Medium

### MED-NEW-1 — Entity create API returns malformed JSON shape (pre-existing)

- **Area:** `app/routes/entities.py:46` + `app/db/entities.py:create_entity()`
- **Severity:** MEDIUM (client-breaking; explains many possible downstream UI bugs)
- **Evidence:** `POST /api/entities` returns `{"id": {full_entity_object}, "name": "...", "slug": "..."}`. The `id` field is a dict, not an integer. Confirmed reproduction:
  ```
  response: {"id":{"archived":0,"color":"#123456",...,"id":6,...}, ...}
  ```
- **Root cause:** `db.create_entity()` was refactored in Phase 2 to return `dict(row)` (the whole entity), but the route at `entities.py:46` still writes `{"id": eid, ...}` assuming `eid` is an integer. One caller missed when the DB function's return type changed.
- **Fix:** `entities.py:46` should be `{"id": eid["id"], "name": name, "slug": slug}` — OR change the route to return the full entity dict under a different key (e.g. spread: `{**eid, "status": "created"}`).
- **Status:** UNFIXED.

### MED-NEW-2 — Server-side accepts XSS/CSS-injection in entity `color`

- **Area:** `app/routes/entities.py:api_entities_create()`
- **Severity:** MEDIUM (defense-in-depth; MED-2 client-side `escColor()` blocks exploitation on the dashboard, but DB is polluted)
- **Evidence:** POST with `"color": "red\" onclick=\"alert(1)\""` → 201 Created, DB row stores the full injection string verbatim. Any future consumer that doesn't `escColor()` would execute it.
- **Fix:** Validate `color` server-side — reject non-hex (`^#[0-9a-fA-F]{3,8}$`) values with 400.
- **Status:** UNFIXED.

### MED-NEW-3 — `entities.js` lacks `registerTabLoader` call (inconsistency from Phase 9 lazy-load refactor)

- **Area:** `app/static/js/dashboard/entities.js`
- **Severity:** MEDIUM (functional but inconsistent — relies on legacy monkey-patch)
- **Evidence:** `grep -c 'registerTabLoader("entities"' app/static/js/dashboard/*.js` → 0. The entities tab still auto-loads via a pre-existing IIFE that monkey-patches `window.sw`: `window.sw = function(tab){ orig(tab); if (tab==='entities') loadEntityTree(); };`. Works, but inconsistent with the Phase 9 registry pattern.
- **Fix:** Replace the monkey-patch IIFE in `entities.js` with `registerTabLoader("entities", loadEntityTree);` + remove the IIFE.
- **Status:** UNFIXED.

### MED-NEW-4 — `navigator.webdriver` returns `false` (detectable)

- **Area:** `patchright` + real Chrome launch config
- **Severity:** MEDIUM (fingerprint; depends on which anti-bot vendor an importer hits)
- **Evidence:** `page.evaluate('navigator.webdriver')` returns `False` (boolean), not `undefined` as a real human browser does. `navigator.webdriver === undefined` evaluates to `False`. Some anti-bot scripts (DataDome, Akamai in aggressive mode) check for `=== undefined` as a humanity test.
- **Root cause:** patchright replaces playwright-stealth at the driver level but does not fully mask `navigator.webdriver`; real Chrome still exposes it as `false` under automation.
- **Fix options:**
  - Add `--disable-blink-features=AutomationControlled` (already present — helps but incomplete)
  - Inject init script to redefine `navigator.webdriver` to `undefined` on every page: `context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")`
- **Status:** UNFIXED — only matters when we hit a detector that specifically checks this. US Alliance working (per prior session) despite this.

### MED-NEW-5 — Rate limiter has no local-IP bypass (test fragility)

- **Area:** `app/routes/auth.py:_rate_limited()`
- **Severity:** MEDIUM (QA workflow; not a security issue)
- **Evidence:** Any QA/curl probe that does >10 bad logins trips the rate limiter for the IP, then legitimate probes for the next 5 minutes return 429. I hit this once this pass, had to `docker restart` to clear.
- **Fix options (pick one):**
  - Allow bypass when `TESTING=true` in Flask config
  - Skip rate limiting for `127.0.0.1` / `::1`
  - Use a smaller window (e.g. 60s) for LAN deploys
- **Status:** UNFIXED.

---

## NEW — Low

### LOW-NEW-1 — `DISPLAY` env var not inherited by `docker exec` subshells

- **Area:** Docker entrypoint
- **Severity:** LOW (test ergonomics only — runtime threads inherit DISPLAY correctly)
- **Evidence:** `docker exec tax-ai-analyzer python3 -c "<patchright launch>"` fails unless you explicitly pass `-e DISPLAY=:99`. The entrypoint's `export DISPLAY=:99` is only in PID 1's environ.
- **Impact:** Minor — production importer threads are children of PID 1 so they inherit correctly. Only QA manual probes need the `-e` flag.
- **Fix:** Add `ENV DISPLAY=:99` to Dockerfile so it's in the image-wide environment.
- **Status:** UNFIXED (cosmetic).

### LOW-NEW-2 — `/login` POST hits default werkzeug dev server (still development mode)

- **Area:** Production deployment config
- **Severity:** LOW (already logged as MED-NEW in prior pass via werkzeug's own warning, but worth re-flagging post-refactor)
- **Evidence:** Startup log: `WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.`
- **Fix:** Add `gunicorn app.web_ui:app -b 0.0.0.0:8012` as the actual production runner, or accept risk as single-user LAN tool.
- **Status:** UNFIXED (product is single-user LAN by design; flag for completeness).

---

## RE-VERIFIED (from prior bug-log, still passing)

| Prior ID | Verdict |
|---|---|
| CRIT-1 (credential mask) | ✅ still fixed — leaks=0, all 4 sensitive keys masked |
| HIGH-1 (SameSite=Lax) | ✅ `Set-Cookie: ...; SameSite=Lax; HttpOnly; Secure` |
| HIGH-2 (admin env gate) | ✅ code fix in place (but see HIGH-NEW-1: env var not wired in compose) |
| HIGH-3 (Dropbox state) | ✅ code fix in place; live roundtrip still unexercised |
| HIGH-4 (inactive user) | ✅ covered by new `TestInactiveUser` test |
| MED-1 (mileage validation) | ✅ 8/8 bad inputs rejected with specific messages |
| MED-2 (entity esc/escColor) | ✅ `dashboard.js` uses `escColor(e.color)` + `esc(e.name||sl)` + `Number(e.id)\|\|0` |
| MED-3 (orphan links) | ✅ 0 orphans tx-side + 0 doc-side; integrity_check=ok |
| MED-4 (security headers) | ✅ CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy all present |
| MED-5 (rate limit /login) | ✅ 10 fails pass → 11th+ HTTP 429 (but see MED-NEW-5: test fragility) |
| MED-6 (pinned deps) | ✅ all 5 `>=` specs replaced with `==` exact pins |
| MED-7 (dated image tag) | ✅ compose image `2026-04-24-qa-remediated` |
| LOW-1/2 (diag script relocated) | ✅ present at `tools/diag_usalliance.py` (and FIXED HIGH-NEW-2 to get it into the container) |
| ENH-1 + ENH-6 (test fixtures + full_smoke) | ✅ `test_auth_boundaries.py` with 10 tests + `pytest.ini` |

---

## Surfaces NOT tested this pass (explicit acknowledgement)

- Live US Bank / Chase / BofA importer runs (user account verification outstanding)
- Live LLM API calls (cost/rate-limit)
- Live Plaid `/link/token/create` handshake
- Live Dropbox / Gmail OAuth flows
- IMAP against a real mailbox
- Backup/restore end-to-end roundtrip (integrity-check only, no restore-into-fresh-instance test)
- Multi-user concurrent load
- Mobile / small-viewport UI
- Browser UI click-through via Playwright (only curl-based HTML crawl)
