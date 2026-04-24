# Remediation Plan — Post-Phase-9 Regression (2026-04-24)

**Source:** `qa/bug-log-post-phase9.md`
**Fix classification:**
- Already fixed in this pass: CRIT-NEW-1 (xvfb-run → entrypoint), CRIT-NEW-2 (Xvfb `-ac`), HIGH-NEW-2 (`tools/` COPY)
- Outstanding: CRIT-NEW-3, HIGH-NEW-1, MED-NEW-1..5, LOW-NEW-1..2

## Release-blocker status

| Issue | Blocker? | Rationale |
|---|---|---|
| CRIT-NEW-1 | **WAS** — now fixed | app daemon didn't start; HTTP 000 on everything |
| CRIT-NEW-2 | **WAS** — now fixed | every bank import would fail browser launch |
| CRIT-NEW-3 (open redirect) | **YES** for public deploy / NO for LAN | phishing vector via `?next=https://evil.com` |
| HIGH-NEW-1 (no admin env var) | NO currently / YES on fresh deploy | latent; existing DB has users so it hides |
| HIGH-NEW-2 | **WAS** — now fixed | operational, not user-facing |
| MED-NEW-1 (entity API shape) | NO | client-breaking but not security |
| MED-NEW-2 (XSS accepted) | NO | defense-in-depth; client-side mask holds |
| MED-NEW-3 (entities.js registry) | NO | functional via legacy monkey-patch |
| MED-NEW-4 (webdriver leak) | NO | only hits if Akamai/DataDome checks this exact path |
| MED-NEW-5 (rate-limiter test fragility) | NO | QA workflow papercut |
| LOW-NEW-1, LOW-NEW-2 | NO | cosmetic |

## Quick wins (≤15 minutes each)

1. **CRIT-NEW-3 — open redirect guard** (~8 LOC):
   ```python
   def _safe_next(next_url):
       if not next_url: return None
       if next_url.startswith("/") and not next_url.startswith("//"):
           return next_url
       return None
   # in login():
   nxt = _safe_next(request.args.get("next"))
   return redirect(nxt or _url("/"))
   ```

2. **HIGH-NEW-1 — wire env var** (~3 lines):
   - `docker-compose.yml`: add `ADMIN_INITIAL_PASSWORD: ${TAX_AI_ADMIN_PASSWORD}` under `services.tax-ai-analyzer.environment`
   - `.env`: add `TAX_AI_ADMIN_PASSWORD=<12+chars>`
   - Restart tax-ai-analyzer (noop unless volume reset — safe)

3. **MED-NEW-1 — entity create shape** (~1 line):
   - `app/routes/entities.py:46`: `{"id": eid["id"], "name": name, "slug": slug}`

4. **MED-NEW-2 — reject bad color server-side** (~4 lines in `api_entities_create`):
   ```python
   color = (data.get("color") or "#1a3c5e").strip()
   if not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
       return jsonify({"error": "color must be hex (#abc or #aabbcc)"}), 400
   ```

5. **MED-NEW-3 — consistent registry** (~5 lines):
   - `entities.js`: replace monkey-patch IIFE with `registerTabLoader("entities", loadEntityTree);`

6. **LOW-NEW-1 — `ENV DISPLAY=:99`** (~1 line in Dockerfile).

## Local fix vs architectural

**All local** — no architectural rework needed. Everything above is a surgical edit to one file or env.

## Risky changes requiring caution

- **HIGH-NEW-1 env var rollout**: verify existing `tax-ai-analyzer` container survives restart with the new env var. Existing DB has users, so `ensure_default_data()` won't trigger the gate; the env var is ignored. But worth smoke-testing once deployed.
- **MED-NEW-2 color validator**: confirm no existing entity in DB has an invalid color that would break edits. Check: `sqlite3 SELECT color, id FROM entities WHERE color NOT REGEXP '^#[0-9a-fA-F]{3,8}$';` (or equivalent Python).

## Dependencies between fixes

- None. All 6 quick-wins are independent.

## Ordered execution suggestion

1. **CRIT-NEW-3** (release-blocker; cheapest; user impact biggest)
2. **HIGH-NEW-1** (latent but easily tested; no-op on existing deploy)
3. **MED-NEW-1** (API contract bug; affects client code)
4. **MED-NEW-2** (pairs naturally with MED-NEW-1 on the same route)
5. **MED-NEW-3** (registry consistency; same module as the 11 sibling tabs)
6. **LOW-NEW-1** (environment polish)
7. **Retest scope**: smoke (73/73) + re-run the 8 re-verified items from bug-log-post-phase9.md RE-VERIFIED table + 1 new regression test for open-redirect guard.

## Retest scope

- `python3 -m pytest app/tests/` — full suite, must stay at 73/73 (or 74 after adding open-redirect regression test).
- Open-redirect re-probe: `curl -i -X POST -d "username=admin&password=admin" "http://.../login?next=https://evil.com/"` → expect `Location: /tax-ai-analyzer/` (not `https://evil.com/`).
- Entity create re-probe: `POST /api/entities` with valid + invalid color payloads → expect proper shape + color validation.
- Fresh volume bootstrap simulation (manual): `docker volume rm` + restart → should either seed admin from env var or log clear error asking to set it.

## New automated tests recommended

- `test_auth_boundaries.py::TestOpenRedirect::test_next_param_sanitized` — covers CRIT-NEW-3
- `test_entities.py::test_create_returns_integer_id` — covers MED-NEW-1
- `test_entities.py::test_create_rejects_bad_color` — covers MED-NEW-2
- `test_session_smoke.py::TestJs::test_all_14_tabs_have_registerTabLoader` — covers MED-NEW-3 (and prevents future drift)
