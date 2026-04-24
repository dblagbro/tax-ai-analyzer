# Remediation Plan — Post-Phase-9 Pass #2 (2026-04-24)

**Input:** `qa/bug-log-post-phase9-pass2.md`
**Unfixed from Pass #1:** CRIT-NEW-3, HIGH-NEW-1, MED-NEW-1..5, LOW-NEW-1..2 (still valid)
**New this pass:** CRIT-PASS2-1, HIGH-PASS2-1, MED-PASS2-1, MED-PASS2-2, LOW-PASS2-1, LOW-PASS2-2, HARDENING-PASS2-1

## Release-blocker classification

| Issue | Blocker for LAN-only? | Blocker for public? |
|---|---|---|
| CRIT-NEW-3 (open redirect) | NO | **YES** |
| CRIT-PASS2-1 (rate-limit XFF bypass) | NO | **YES** |
| HIGH-NEW-1 (admin env var) | NO currently, YES on fresh deploy | **YES** |
| HIGH-PASS2-1 (export download broken) | **YES** | **YES** — core feature is entirely non-functional |
| MED-PASS2-1 (doc 404 missing) | NO | NO |
| MED-NEW-1..2 (entity bugs) | NO | NO |
| MED-NEW-3 (registry inconsistency) | NO | NO |
| MED-PASS2-2 (webdriver leak) | NO | NO |
| LOW-PASS2-1 (zombie chrome) | NO | NO |
| LOW-PASS2-2 (log volume) | NO | NO |
| HARDENING-PASS2-1 (uncommitted changes) | **YES (process risk)** | **YES** |

## Ordered quick wins (most bang-per-LOC)

1. **Commit the uncommitted** (HARDENING-PASS2-1) — git add + commit the Dockerfile/entrypoint/QA docs. ~1 min. Eliminates biggest operational risk.

2. **HIGH-PASS2-1 — export download path fix** (~3 LOC):
   ```python
   # app/routes/export_.py:85 — change filename construction
   filename = f"export_{year}_{entity_slug}{ext}"   # was: f"{entity_slug}_{year}{ext}"
   ```
   Handle CSV/PDF special cases (`transactions_{year}_{slug}.csv`, `summary_{year}_{slug}.pdf`) with a dispatch dict.

3. **CRIT-NEW-3 — open redirect guard** (~8 LOC):
   ```python
   def _safe_next(next_url):
       if not next_url: return None
       if next_url.startswith("/") and not next_url.startswith("//"):
           return next_url
       return None
   # login(): return redirect(_safe_next(request.args.get("next")) or _url("/"))
   ```

4. **CRIT-PASS2-1 — rate-limiter IP resolution** (~5 LOC):
   ```python
   TRUSTED_PROXIES = {"127.0.0.1", "::1"}  # plus any nginx upstream IP
   def _client_ip():
       if request.remote_addr in TRUSTED_PROXIES:
           fwd = request.headers.get("X-Forwarded-For", "")
           return fwd.split(",")[0].strip() or request.remote_addr
       return request.remote_addr or ""
   # In login(): client_ip = _client_ip()
   ```

5. **HIGH-NEW-1 — wire env var** (compose edit, ~2 lines in 2 files):
   ```yaml
   # docker-compose.yml (parent)
   services:
     tax-ai-analyzer:
       environment:
         ADMIN_INITIAL_PASSWORD: ${TAX_AI_ADMIN_PASSWORD}
   # .env
   TAX_AI_ADMIN_PASSWORD=<generated-16-char>
   ```

6. **MED-PASS2-1 — doc 404** (~3 LOC):
   ```python
   if not paperless_doc and not db_rec:
       return jsonify({"error": "document not found"}), 404
   ```

7. **MED-NEW-1 — entity shape** (~1 LOC):
   ```python
   # app/routes/entities.py:46
   return jsonify({"id": eid["id"], "name": name, "slug": slug}), 201
   ```

8. **MED-NEW-2 — entity color server validation** (~4 LOC paired with #7):
   ```python
   color = (data.get("color") or "#1a3c5e").strip()
   if not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
       return jsonify({"error": "color must be hex (#abc or #aabbcc)"}), 400
   ```

9. **MED-NEW-3 — entities.js registry** (~5 LOC): replace monkey-patch IIFE with `registerTabLoader("entities", loadEntityTree);`.

10. **LOW-NEW-1 — `ENV DISPLAY=:99`** in Dockerfile.

11. **LOW-PASS2-1 — tini as PID 1** (~2 Dockerfile lines):
    ```dockerfile
    RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
    ENTRYPOINT ["/usr/bin/tini", "--"]
    CMD ["/usr/local/bin/docker-entrypoint.sh"]
    ```

12. **LOW-PASS2-2 — log level** (~2 LOC in `main.py`):
    ```python
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    ```

## Architectural vs local

All 12 above are **local** patches. Only CRIT-PASS2-1 edges into "should be a shared middleware" (the IP resolution should probably live in a single module used by any future rate-limiter + abuse-throttling code), but for this product a private helper is fine.

## Risky changes requiring extra care

- **HIGH-PASS2-1 export filename change** — existing generated files in `/app/export/<year>/` may have the OLD naming. Need to decide: (a) rename existing files during migration, (b) make the download route try both naming conventions, or (c) require users to re-generate. Option (b) is safest.

- **CRIT-PASS2-1 TRUSTED_PROXIES list** — if the app is deployed behind a different proxy (e.g. traefik, caddy), the upstream IP may be a container network IP (e.g. `172.18.0.1`). Need to document this config OR auto-detect from request chain.

- **LOW-PASS2-1 tini** — changes PID 1 semantics. Some background threads may behave differently under tini's signal forwarding. Test: kill -15 the container and confirm graceful shutdown.

## Dependencies between fixes

- #1 (commit) first — gives a stable baseline for all subsequent fixes.
- #2, #3, #4, #6 independent of each other.
- #7 and #8 pair naturally (same route, same commit).
- #9 can go anytime.
- #10 blocks nothing.
- #11 and #12 can go anytime.

## Retest scope after each fix

| Fix | Retest |
|---|---|
| #1 | `git log --oneline -5` — confirm commits landed |
| #2 | `curl -b jar -o out.csv /api/export/2024/personal/download/csv` → 200, file non-empty |
| #3 | `curl -i -X POST "...?next=https://evil.com/"` → Location is NOT evil.com |
| #4 | 20 bad logins with random X-Forwarded-For → the 11th+ return 429 |
| #5 | `docker exec printenv ADMIN_INITIAL_PASSWORD` → value present |
| #6 | `curl /api/documents/99999999` → 404 |
| #7 | `curl -X POST /api/entities` with valid body → response.id is integer |
| #8 | `curl -X POST /api/entities` with `"color":"javascript:x"` → 400 |
| #9 | `grep registerTabLoader app/static/js/dashboard/entities.js` → 1 match |
| #10 | `docker exec printenv DISPLAY` → `:99` |
| #11 | Run US Alliance import; verify no `[chrome_crashpad] <defunct>` in `ps -ef` after |
| #12 | Log size delta over 60s idle — confirm <50 B/s |

## Full post-remediation retest

- `pytest app/tests/` — 73/73 (or 74+ after new regression tests added)
- Re-run the entire Pass #2 probe set — all unfixed items should flip to green
- Rebuild image + restart → Flask + Xvfb + Chrome all launch
- Trigger a US Alliance import (working canary) — should complete
- Optional: trigger all 8 export formats, download each, confirm each non-empty

## Recommended NEW automated tests (to prevent regression)

- `test_auth_boundaries.py::TestOpenRedirect::test_next_param_rejects_external` — CRIT-NEW-3
- `test_auth_boundaries.py::TestRateLimit::test_xff_spoof_does_not_bypass` — CRIT-PASS2-1
- `test_export.py::test_all_8_formats_downloadable_after_generate` — HIGH-PASS2-1
- `test_documents.py::test_get_nonexistent_returns_404` — MED-PASS2-1
- `test_entities.py::test_create_returns_integer_id` — MED-NEW-1
- `test_entities.py::test_create_rejects_non_hex_color` — MED-NEW-2
- `test_session_smoke.py::TestTabRegistry::test_all_tabs_registered` — MED-NEW-3
- `test_bootstrap.py::test_fresh_db_refuses_without_admin_password_env` — HIGH-NEW-1
