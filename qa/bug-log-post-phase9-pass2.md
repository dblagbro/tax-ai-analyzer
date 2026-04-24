# Bug Log — Post-Phase-9 Pass #2 (2026-04-24 ~23:40 EDT)

**Pass type:** Deep regression + release hardening (skeptical re-probe)
**Scope delta vs Pass #1:** added file-upload abuse, concurrent-rate-limit stress, SSE lifecycle, Patchright init-script round-trip, SQLite WAL concurrency, backup restore simulation, export pipeline end-to-end, accountant token revocation, CSRF cross-origin, X-Forwarded-For spoofing.

**State at start:**
- Git: `f6174e2` HEAD. Working tree has 2 tracked modifications (Dockerfile), 1 untracked file (docker-entrypoint.sh), 3 untracked QA markdowns — none committed.
- Container: running image `dblagbro/tax-ai-analyzer:2026-04-24-qa-remediated` (rebuilt twice during Pass #1), PID 1 = `python -m app.main`, Xvfb :99 running.
- pytest: 73/73.

---

## NEW this pass

### CRIT-PASS2-1 — Rate limiter bypassed by X-Forwarded-For spoofing

- **Area:** `app/routes/auth.py:login()` `_rate_limited(client_ip)`
- **Severity:** **HIGH→CRITICAL** (for any public deploy — effectively no rate limit on login)
- **Evidence:** 15 sequential bad-login attempts, each with a random `X-Forwarded-For: 10.<rand>.<rand>.<rand>` header → 15 × HTTP 200 (not 429). The limiter is keyed on a CLIENT-SUPPLIED header value, which rotates per request, so no key ever reaches the 10-attempt threshold.
- **Root cause:** `client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()` blindly trusts `X-Forwarded-For`. `ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)` is NOT configured with `x_for=1`, so Werkzeug doesn't even attempt to normalize XFF from a trusted upstream.
- **Fix:**
  - Option A (right fix): only honor `X-Forwarded-For` when the immediate `REMOTE_ADDR` is in a `TRUSTED_PROXIES` allow-list (nginx upstream IPs).
  - Option B (minimal): use `request.remote_addr` unconditionally. Loses accuracy behind nginx, gains simplicity. Since the product is LAN single-user, good enough.
- **Status:** UNFIXED.

### CRIT-PASS2-2 — Prior QA probes leaked live XSS payloads into production DB (test hygiene)

- **Area:** QA test cleanup (mine), not app code
- **Severity:** **MEDIUM** (data integrity / process defect)
- **Evidence:** SELECT * FROM entities showed:
  - `id=5, slug='_script_alert_1___script_', name='<script>alert(1)</script>', color='red" onclick="alert(1)"'`
  - `id=6, slug='qa_test_ent', name='QA-test-ent', color='#123456'`
  Both were test entities I created during Pass #1 probes. Cleanup queries used literal slugs (`xss-test`, `qa-test-ent`) but the server-side normalizer (`re.sub(r"[^\w]", "_", ...)`) mangled them to the slugs above. My `DELETE WHERE slug='…'` missed.
- **Impact:** Entity table polluted; if the dashboard had rendered these without client-side escape, they'd have executed. MED-2's `escColor()` masked them at render time — but a DB backup + forensic review would see these.
- **Fix applied (this pass):** Deleted both entities + cascading tax_years cleanup. Verified 4 real entities remain.
- **Process fix:** future QA probes should use a dedicated `qa_probe_*` prefix AND delete by `id IN (SELECT id FROM entities WHERE created_at > <probe_start>)` instead of relying on slug matching.
- **Status:** **FIXED** this pass.

### HIGH-PASS2-1 — Export download endpoint 404s for ALL 8 formats

- **Area:** `app/routes/export_.py:api_export_download()`
- **Severity:** HIGH (feature is fully broken — zero exports downloadable via the official API)
- **Evidence:** After successful POST to `/api/export/2024/personal` generating `export_2024_personal.qbo`, `export_2024_personal.ofx`, `transactions_2024_personal.csv` etc. — GETs to `/api/export/2024/personal/download/{csv,json,iif,qbo,ofx,txf,pdf,zip}` all return 404.
- **Root cause:** Download route looks for file `<entity_slug>_<year><ext>` (e.g. `personal_2024.csv`) but the generator writes `export_<year>_<slug>.<ext>` and `transactions_<year>_<slug>.csv`. Filename conventions don't match.
- **Fix:** Either (a) rename the download path construction to match generator output (`export_{year}_{slug}{ext}`) or (b) rename the generator to match download (`{slug}_{year}{ext}`). Option (a) is safer — doesn't rewrite existing files.
- **Status:** UNFIXED.

### MED-PASS2-1 — `/api/documents/<id>` returns HTTP 200 + stub for nonexistent documents

- **Area:** `app/routes/documents.py:api_document_detail()` (lines 167-186)
- **Severity:** MEDIUM (API contract bug — clients can't tell missing from present)
- **Evidence:** GET `/api/documents/99999999` → `{"doc_id": 99999999}` with HTTP 200. GET `/api/documents/0` → `{"doc_id": 0}` with HTTP 200. Negative/non-integer IDs correctly return `{"error":"not found"}` but that's Werkzeug type-coercion, not the route's logic.
- **Root cause:** The route merges `paperless_doc` + `db_rec`, both empty for nonexistent IDs, then returns `jsonify({**empty, **empty, "doc_id": doc_id})` — which is always truthy/JSON.
- **Fix:**
  ```python
  if not paperless_doc and not db_rec:
      return jsonify({"error": "document not found"}), 404
  return jsonify({**paperless_doc, **db_rec, "doc_id": doc_id})
  ```
- **Status:** UNFIXED.

### LOW-PASS2-1 — Zombie `chrome_crashpad` processes accumulate inside container

- **Area:** Process hygiene
- **Severity:** LOW (each zombie consumes ~0 resources but a process-table slot)
- **Evidence:** `ps -ef` inside running container showed `[chrome_crashpad] <defunct>` PIDs (38, 40) left over from my Pass #1 patchright probes. Each Chrome spawn creates a crashpad helper that exits; if nothing reaps it, it stays as a zombie.
- **Root cause:** `python -m app.main` is PID 1. It doesn't run as a PID-1-aware init that reaps all descendant zombies. Every Chrome launch via patchright leaves ≥1 zombie.
- **Fix options:**
  - Use `tini` or `dumb-init` as PID 1 to reap zombies automatically.
  - Explicitly call `browser.close()` + `context.close()` + `pw.stop()` on every importer exit path (some paths may miss this on exception).
- **Status:** UNFIXED.

### LOW-PASS2-2 — Idle-time log volume ~305 B/s (~26 MB/day)

- **Area:** Paperless document-scan daemon (`app.paperless_client` httpx calls)
- **Severity:** LOW (operational hygiene — untouched logs are fine for the single-user tool, but disk growth is real)
- **Evidence:** `docker logs` byte count delta over 10s of idle (no user activity, no import): **3049 bytes** — roughly 305 B/s or 26 MB/day. The vast majority: `INFO httpx: HTTP Request: GET http://tax-paperless-web:8000/api/documents/?page=N&page_size=100 "HTTP/1.1 200 OK"`.
- **Fix options:**
  - Reduce log level from INFO to WARNING for httpx.
  - Add `logging.getLogger("httpx").setLevel(logging.WARNING)` somewhere during app init.
- **Status:** UNFIXED.

### MED-PASS2-2 — Patchright `add_init_script` does NOT mask `navigator.webdriver`

- **Area:** `base_bank_importer.launch_browser()` — and the Phase 9 plan's MED-NEW-4 workaround
- **Severity:** MEDIUM (fingerprint leak; previously identified; this pass confirmed the obvious workaround doesn't work)
- **Evidence:** Launched real Chrome via `pw.chromium.launch(headless=False, channel="chrome", ...)`, called `context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")`, navigated to `https://www.google.com/`, evaluated `navigator.webdriver` → still returns `False` with `typeof: boolean`. The init script did not override.
- **Root cause:** patchright likely re-installs the webdriver property after init scripts run, OR Chrome 147+ enforces the property at a deeper level than user-land JS can override.
- **Fix options:**
  - Try a Chrome launch arg instead: `--disable-blink-features=AutomationControlled` (already present but incomplete).
  - Use CDP `Page.addScriptToEvaluateOnNewDocument` with `runImmediately=true` via `CDPSession`.
  - Accept as residual risk — the strongest anti-bot check (CDP Runtime.Enable) IS patched, so this is a secondary signal.
- **Status:** UNFIXED.

### HARDENING-PASS2-1 — `Dockerfile`, `docker-entrypoint.sh`, and 3 QA docs ARE UNCOMMITTED

- **Area:** git discipline
- **Severity:** HIGH (any `git reset --hard` or accidental `git clean -fd` loses the CRIT-NEW-1/2 fixes and forces a re-diagnosis next time)
- **Evidence:** `git status -s` shows:
  ```
  M Dockerfile
  ?? docker-entrypoint.sh
  ?? qa/bug-log-post-phase9.md
  ?? qa/qa-notes-post-phase9.md
  ?? qa/remediation-plan-post-phase9.md
  ```
  The CRIT-fixed image exists locally but the fix CODE only lives in the working tree.
- **Fix:** Commit these plus this pass's findings.
- **Status:** UNCOMMITTED (user asked for QA-only this pass; commit decision deferred).

---

## RE-VERIFIED from Pass #1 (all still unfixed as reported; no sneaky self-fixes)

| Pass #1 ID | Status |
|---|---|
| CRIT-NEW-3 (open redirect) | ✅ STILL BROKEN — `Location: https://evil.com/` |
| HIGH-NEW-1 (ADMIN_INITIAL_PASSWORD unset) | ✅ STILL UNSET in container env |
| MED-NEW-1 (entity id shape bug) | ✅ STILL RETURNS nested dict as id |
| MED-NEW-2 (entity color server XSS) | ✅ STILL ACCEPTS `javascript:alert(1)` verbatim |
| MED-NEW-3 (entities.js registry) | ✅ 0 `registerTabLoader`, 1 monkey-patch |
| MED-NEW-4 (navigator.webdriver leak) | ✅ init-script workaround confirmed ineffective (new MED-PASS2-2) |
| MED-NEW-5 (rate limiter LAN bypass) | ✅ no bypass; limiter still fires locally from 127.0.0.1 |
| LOW-NEW-1 (`ENV DISPLAY` missing) | ✅ Dockerfile still doesn't set ENV |

---

## Positive confirmations (security & correctness)

- Full pytest: **73/73** passing
- Dashboard render: **14 tabs** + **16 import partials** + **15 static JS modules** all served HTTP 200
- SQLite integrity: `PRAGMA integrity_check = ok`; 3777 tx / 4574 docs / 0 orphan links
- SQLite WAL under 10 parallel readers + 1 writer: **0 errors, 0 lock timeouts**
- Backup tarball restore simulation: extracted → queried → counts match (3777 tx / 4574 docs / 2 users / 4 entities / first entity=Personal) ✓
- Rate limiter fires correctly when keyed on real `remote_addr` (only broken with XFF spoof)
- Security headers: all 5 present on every response (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy)
- `Set-Cookie: session=...; Secure; HttpOnly; Path=/tax-ai-analyzer; SameSite=Lax` — all 4 attributes present
- Credential mask (CRIT-1): 0 leaks
- Mileage validation (MED-1): 8/8 bad inputs rejected with specific messages
- Bulk-edit SQLi whitelist: rejects non-whitelisted column names
- Bulk-edit happy path: mutation persists, DB verified
- Empty `ids` on bulk-edit: rejected with proper error
- Wrong HTTP method (POST on GET-only, DELETE on GET-only): 405 (correct)
- Unauth access to admin routes: 302 redirect to login (correct)
- Accountant token generate → anon-consume → revoke → re-consume → 302 (correct behavior)
- Path traversal probes: 302 / 404 (no file leak)
- Patchright real-Chrome launches successfully: `Chrome/147.0.0.0` UA, headful under Xvfb
- 76 GET routes probed: **0 returned 5xx**
- SSE endpoint returns `text/event-stream` content-type correctly
- SSE stream aborts cleanly (no leaked python processes after curl abort)
- Concurrent 20-way bad-login burst: rate limiter does eventually fire, lock serializes correctly (no deadlock)

---

## Surfaces NOT exercised (acknowledged)

- Playwright browser-level UI click-through (still only curl-based HTML crawl)
- Live bank importers (US Bank account status gate)
- Live LLM calls
- OAuth roundtrips (Gmail, Dropbox, Plaid)
- IMAP with a real mailbox
- Multi-user concurrent load against rate limiter
- Disk-full / volume-out-of-space failure modes
- Network partition / nginx-upstream-down failure modes
- Chrome memory leak over many import runs
- Fresh DB bootstrap (would catch HIGH-NEW-1 ADMIN_INITIAL_PASSWORD gate)
