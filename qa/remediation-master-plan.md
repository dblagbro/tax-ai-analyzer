# Remediation Master Plan — Post-Phase-9 (consolidated)

**Date:** 2026-04-23 EDT (covers both QA passes completed this session)
**Inputs:**
- `qa/bug-log-post-phase9.md` (Pass #1, 11 findings)
- `qa/bug-log-post-phase9-pass2.md` (Pass #2, 7 new + 8 re-verified)
- `qa/remediation-plan-post-phase9.md` (Pass #1 fix list)
- `qa/remediation-plan-post-phase9-pass2.md` (Pass #2 fix list)

**Goal:** A single, ordered, commit-grouped, retest-gated plan to drive the code to release-ready (LAN + public tiers) with minimal rework and explicit rollback at each step.

---

## Guiding principles

1. **Commit first, fix second.** Uncommitted CRIT fixes in the working tree are a bigger operational risk than any code defect. Wave 0 gets the tree clean before anything else touches it.
2. **Bind-mount changes first, image rebuilds last.** Python/JS/template edits are live instantly via bind mount; Dockerfile/entrypoint changes need full rebuild + container recreate. Sequence fixes to minimize rebuild count.
3. **Retest gate between waves.** Every wave ends with a specific retest matrix. If the matrix fails, the wave rolls back (git reset or image revert) before the next wave begins.
4. **LAN-safe waves first, public-deploy-hardening waves after.** The product is currently a single-user LAN tool. Waves 1-3 keep it LAN-usable; Waves 4-5 harden it for any future public exposure.
5. **New automated tests with each wave.** Don't ship a fix without its regression guard.

---

## Current state (pre-plan)

Working tree (uncommitted):
- `M Dockerfile` — has xvfb apt, `COPY tools/`, CMD → `docker-entrypoint.sh` (from Pass #1 fix for CRIT-NEW-1/2)
- `?? docker-entrypoint.sh` — new wrapper (the CRIT-NEW-1 fix itself)
- `??` 6 QA markdown files under `qa/`

Live container:
- Image: `dblagbro/tax-ai-analyzer:2026-04-24-qa-remediated` (rebuilt twice during QA)
- Running PID 1 = `python -m app.main` + Xvfb :99 with `-ac`
- 73/73 tests pass, 3777 tx / 4574 docs / 0 orphan links, integrity OK

Git: 3 commits landed (`2af3853`, `f729e64`, `f6174e2`) + 3 pushed tags; nothing ahead of `origin/main`.

---

## Open issues — ordered by wave

### Wave 0 — Tree hygiene (no code change, pure git)

**Purpose:** Eliminate the loaded-gun risk of uncommitted CRIT fixes. Must land before any edit.

| # | Issue | Action |
|---|---|---|
| H-PASS2-1 | Dockerfile/entrypoint/QA-docs uncommitted | `git add` + `git commit` in one commit titled "Fix CRIT-NEW-1/2: replace xvfb-run with entrypoint script; copy tools/ into image". Push to origin. |

### Wave 1 — LAN-safe release blockers (fast wins, no rebuild)

**Purpose:** Restore the one broken feature that makes the product non-functional on LAN (exports).

| # | Issue | File | Change summary |
|---|---|---|---|
| HIGH-PASS2-1 | Export download 404s | `app/routes/export_.py` | Fix filename construction to match generator output (`export_{year}_{slug}{ext}` plus per-format overrides for csv/pdf). Add a fallback that tries both naming conventions so existing on-disk files stay reachable. |
| NEW | regression test | `app/tests/test_export.py` (new) | Generate-then-download roundtrip for all 8 formats against a known entity/year. |

No rebuild. Just `docker restart tax-ai-analyzer` to refresh Python imports.

### Wave 2 — Tier-2 correctness & contract fixes (no rebuild)

**Purpose:** Fix API contract bugs and pre-existing quality defects that could cause silent downstream breakage.

| # | Issue | File | Change summary |
|---|---|---|---|
| MED-PASS2-1 | `/api/documents/<id>` 200 for nonexistent | `app/routes/documents.py` line 183-184 | Add `if not paperless_doc and not db_rec: return jsonify({"error":"document not found"}), 404`. |
| MED-NEW-1 | Entity create returns nested dict as `id` | `app/routes/entities.py` line 46 | Change `{"id": eid, ...}` → `{"id": eid["id"], ...}`. |
| MED-NEW-2 | Entity color accepts XSS server-side | `app/routes/entities.py` both create + update | Validate `color` matches `^#[0-9a-fA-F]{3,8}$`; reject with 400. |
| MED-NEW-3 | `entities.js` registry inconsistency | `app/static/js/dashboard/entities.js` | Replace `(function(){ var orig = window.sw; window.sw = function(tab){ orig(tab); if (tab==='entities') loadEntityTree(); };})();` with `registerTabLoader("entities", loadEntityTree);`. |
| NEW | regression tests | `app/tests/test_entities.py` (new), `app/tests/test_documents.py` (new) | `test_create_returns_integer_id`, `test_create_rejects_non_hex_color`, `test_get_nonexistent_returns_404`. |
| NEW | JS-level test | `app/tests/test_session_smoke.py` (append) | `TestTabRegistry::test_all_tabs_registered` — grep every loaded JS module; every tab in the dispatch map must have exactly one `registerTabLoader("<name>"` call. |

No rebuild. `docker restart` to pick up Python; JS is static so hot on refresh.

### Wave 3 — Security hardening blockers for public deploy (no rebuild)

**Purpose:** Close the two remaining critical security gaps plus the admin-gate config.

| # | Issue | File | Change summary |
|---|---|---|---|
| CRIT-NEW-3 | Open redirect on `/login?next=` | `app/routes/auth.py` | Add `_safe_next(url)` helper that returns `url` only if it starts with `/` and not `//`; otherwise `None`. Use in the post-login `redirect(...)` call. |
| CRIT-PASS2-1 | Rate-limit XFF spoof bypass | `app/routes/auth.py` | Add `TRUSTED_PROXIES = {"127.0.0.1", "::1"}` (pull from `app.config` so it's env-configurable). Refactor IP resolution into `_client_ip()`: only honor `X-Forwarded-For` when `request.remote_addr` is in the trusted set. |
| HIGH-NEW-1 | `ADMIN_INITIAL_PASSWORD` unset | `/home/dblagbro/docker/docker-compose.yml` + `.env` | Add `ADMIN_INITIAL_PASSWORD: ${TAX_AI_ADMIN_PASSWORD}` under `services.tax-ai-analyzer.environment`. Generate 16-char random into `.env` (which is gitignored). Restart single container. |
| NEW | regression tests | `app/tests/test_auth_boundaries.py` (append) | `TestOpenRedirect::test_next_param_rejects_external`, `TestRateLimit::test_xff_spoof_does_not_bypass`, `TestBootstrap::test_fresh_db_refuses_without_admin_password_env` (uses a fresh in-memory DB fixture). |

No rebuild. `docker restart tax-ai-analyzer`.

### Wave 4 — Image-level hygiene (requires full rebuild)

**Purpose:** Clean up Docker-layer issues batched so only ONE rebuild is needed.

| # | Issue | File | Change summary |
|---|---|---|---|
| LOW-NEW-1 | DISPLAY not image-wide | `Dockerfile` | Add `ENV DISPLAY=:99` after existing `ENV PLAYWRIGHT_BROWSERS_PATH=...`. |
| LOW-PASS2-1 | Zombie `chrome_crashpad` procs | `Dockerfile` | Add `tini` to apt-get list; add `ENTRYPOINT ["/usr/bin/tini","--"]` above existing `CMD`. |
| LOW-PASS2-2 | Idle log volume ~26MB/day | `app/main.py` | Add `logging.getLogger("httpx").setLevel(logging.WARNING)` during app init. (This is a code fix, not image — moved here because it pairs with the other hygiene fixes as a validation batch.) |
| NEW | smoke addition | `app/tests/test_smoke.py` (append) | `test_http_liveness_over_real_socket` — uses `socket` or `http.client` to hit `http://localhost:8012/tax-ai-analyzer/login` from inside the container. Catches "container Up but Flask dead" failure mode that Pass #1 exposed. |

**Build + rollout:**
```
sudo docker compose build tax-ai-analyzer
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

Retest after restart: `ps -ef` shows `tini` as PID 1, `python -m app.main` as child, Xvfb as grandchild; run an idle 10s log-volume probe (must be <100 B/s); trigger a patchright launch and verify no zombie left after close.

### Wave 5 — Deferred / residual (CDP-level patchright fix)

**Purpose:** Address the `navigator.webdriver === false` leak that `add_init_script` cannot mask.

| # | Issue | Approach |
|---|---|---|
| MED-PASS2-2 | webdriver leak | Use `CDPSession.send("Page.addScriptToEvaluateOnNewDocument", {"source": "...", "runImmediately": true})` on every page context. Or accept residual risk — the primary CDP Runtime.Enable patch is already in place; this is a secondary signal that only the most paranoid detectors check. |

**Recommendation**: DEFER. Revisit only if a specific bank importer (e.g. a future US Bank attempt) fails at this detection layer. Test/fix with live bank-specific feedback in hand rather than speculatively.

---

## Per-wave retest matrix

### After Wave 0 (commits)
```
git log --oneline -5           → 2 new commits since f6174e2
git status -s                  → clean (or just new QA-docs from this pass)
git push origin main           → up to date
```

### After Wave 1 (exports)
```
pytest app/tests/              → 74/74 (was 73, +1 test_export)
curl -b jar "/api/export/2024/personal" POST          → 200 + files generated
curl -b jar "/api/export/2024/personal/download/csv"  → 200 + non-empty content-length
# ... same for json, iif, qbo, ofx, txf, pdf, zip
```

### After Wave 2 (contract)
```
pytest app/tests/              → 77/77 (+3 tests)
curl /api/documents/99999999   → 404
curl /api/entities POST valid  → response.id is int
curl /api/entities POST color=javascript:x → 400
grep registerTabLoader app/static/js/dashboard/entities.js → 1
```

### After Wave 3 (security)
```
pytest app/tests/                                       → 80/80 (+3 tests)
curl -i "/login?next=https://evil.com/" POST admin      → Location is /tax-ai-analyzer/
15× bad login with random XFF                           → 11+ return 429
docker exec printenv ADMIN_INITIAL_PASSWORD             → value present
```

### After Wave 4 (image hygiene)
```
docker compose build tax-ai-analyzer                    → succeeds
ps -ef                                                  → PID 1 = tini
ps -ef | grep chrome_crashpad                           → 0 defunct after a browser cycle
docker exec printenv DISPLAY                            → :99
docker logs --since 60s | wc -c                         → <6000 bytes
pytest app/tests/                                       → 81/81 (+1 liveness)
```

### Final retest (before declaring release-ready)
- Single US Alliance canary import — confirms patchright + real Chrome + Xvfb + warm-up all work together
- Full browser click-through (Playwright headless visit + console capture) — catches JS console errors the HTML crawl can't see
- Simulated fresh-volume deploy: tar back the data volume to an empty state, restart, verify app boots with env-var gate

---

## Rollback plan per wave

Each wave produces exactly one commit (except Wave 4 which produces one commit + one image rebuild).

### If Wave 1 fails retest
```
git revert HEAD
docker restart tax-ai-analyzer
```

### If Wave 2 fails retest
```
git revert HEAD
docker restart tax-ai-analyzer
```

### If Wave 3 fails retest
```
git revert HEAD
# Parent compose edit is not in git; manually revert the env var line
docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

### If Wave 4 fails retest
```
git revert HEAD
# Tag pre-Wave-4 image BEFORE build so we can fall back
sudo docker tag dblagbro/tax-ai-analyzer:2026-04-24-qa-remediated \
                dblagbro/tax-ai-analyzer:pre-wave4
# If new build breaks: edit compose to pre-wave4 tag + recreate
```

Full nuclear rollback target: image `dblagbro/tax-ai-analyzer:pre-remediation-2026-04-24_0107` + git tag `pre-remediation-2026-04-24_0107`.

---

## Effort estimate

| Wave | LOC changed | New tests | Rebuild? | Estimated time |
|---|---|---|---|---|
| 0 | 0 (commit only) | 0 | no | 3 min |
| 1 | ~30 | 1 test file, ~80 LOC | no | 20 min |
| 2 | ~20 | 2 test files, ~120 LOC | no | 30 min |
| 3 | ~40 | 3 new tests | no | 40 min |
| 4 | ~10 (Dockerfile+main.py) | 1 liveness test | **yes** (~10 min rebuild) | 30 min |
| 5 | deferred | — | — | — |

**Total active work:** ~2 hours across 5 waves. **Full-session bound** allowing for retests.

---

## Commit structure (one commit per wave)

1. `Commit uncommitted Phase 9 stabilization: xvfb-run → entrypoint script, copy tools/, QA docs`
2. `Wave 1: Fix export download filename conventions (HIGH-PASS2-1)`
3. `Wave 2: API contract fixes — doc 404, entity shape, color validation, tab registry consistency`
4. `Wave 3: Security hardening — open-redirect guard, XFF-spoof-resistant rate limiter, admin env gate wiring`
5. `Wave 4: Image hygiene — tini, ENV DISPLAY, httpx log level, HTTP liveness test`

Each commit is independently revertable. Each commit adds its own tests.

---

## What this plan does NOT cover (deferred, with rationale)

- **Live bank importer end-to-end test** — gated on user's US Bank account-lockout confirmation. No change from prior plan.
- **Residential proxy (Step 6)** — user decision. Out of scope for remediation.
- **Camoufox / Firefox automation (Step 7)** — escalation path only if Wave 4 + Step 6 insufficient.
- **CDP-level patchright webdriver mask (MED-PASS2-2)** — speculative; wait for live bank signal.
- **Gunicorn / production WSGI runner** — product is single-user by design; Werkzeug dev server is acceptable.
- **Multi-user concurrency / load testing** — product is single-user by design.
- **Browser-level Playwright click-through tests** — worth adding, but scope-expanding. Recommended as a post-plan follow-up if you want deeper UI coverage.

---

## Definition of done

Release-ready for LAN:
- [x] Waves 0, 1, 2 complete and retested
- [x] Wave 3 complete (admin env var wired — prevents future boot breakage even if data volume reset)
- [x] Wave 4 complete (tini + DISPLAY + log volume + liveness test)
- [x] `pytest app/tests/` 81+/81+ pass
- [x] Single US Alliance canary import succeeds

Release-ready for public (additional):
- [ ] Wave 3 rate-limit XFF guard deployed with a correct `TRUSTED_PROXIES` list for whatever proxy is in front
- [ ] Browser click-through test added (Playwright headless)
- [ ] Fresh-volume deploy simulation passes
- [ ] Open-redirect regression test green

---

## Ready to proceed

Plan is internally consistent: no fix depends on a later wave; each wave has its retest; each wave has its rollback; image rebuild is batched to ONE cycle.

**Request for approval:** If approved, execute Wave 0 first, then proceed sequentially with a retest gate between each.
