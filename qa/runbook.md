# Operational Runbook — tax-ai-analyzer

Captures the hard-won knowledge from the 2026-04-23/24 remediation + canary
session. Use this when something breaks or before a maintenance event.

---

## 1. Quick health check

```sh
# Container alive?
docker ps --filter name=tax-ai-analyzer --format '{{.Status}}\t{{.Image}}'

# Flask actually serving (not just "Up")?
curl -sI http://localhost:8012/tax-ai-analyzer/login | head -1
# Expect: HTTP/1.1 200 OK

# Process tree healthy?
docker exec tax-ai-analyzer ps -ef | head -8
# Expect: PID 1 = tini, child = python -m app.main, grandchild = Xvfb :99

# Run the test suite
docker exec tax-ai-analyzer python3 -m pytest app/tests/ -q
# Expect: 126 passed (or higher)
```

---

## 2. Single-container restart (safe, never touches volumes)

```sh
# Code-only changes (under app/, profiles/, app/static/)
sudo docker restart tax-ai-analyzer

# Image-changing edits (Dockerfile, requirements.txt, docker-entrypoint.sh)
cd /home/dblagbro/docker
sudo docker compose build tax-ai-analyzer
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

**NEVER** run `docker compose down` — it stops the entire stack including
Paperless. The repo's `CLAUDE.md` calls this out as a HARD LIMIT.

---

## 3. "Container Up but nothing is serving"

Symptom: `docker ps` shows `Up X minutes` but `curl` returns connection
refused / curl exit 56.

Diagnosis:
```sh
docker exec tax-ai-analyzer ps -ef | head
```

Outcomes:
- **Only `tini` + `Xvfb`, no python**: entrypoint failed before Flask
  started. Check `docker logs tax-ai-analyzer --tail 80` for the cause.
  Common: stale `/tmp/.X11-unix/X99` socket from a prior Xvfb.
- **`tini` + `python` + Xvfb all running**: Flask is up, problem is
  upstream (nginx, network, port mapping). Check
  `docker port tax-ai-analyzer 8012`.
- **Only `xvfb-run` (no python, no Xvfb)**: this means the image is from
  before commit `1bba238`. Old `xvfb-run` wrapper hangs in Docker. Pull
  the latest image.

Hard fix when Xvfb is the problem:
```sh
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
# This recreates the container with a fresh /tmp, which clears any
# stale X11 socket from a prior Xvfb.
```

---

## 4. Restore data from backup tarball

Tarballs live at `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-*.tar.gz`.

```sh
# 1. Stop writers (analysis daemon)
docker exec tax-ai-analyzer curl -s -X POST http://localhost:5000/api/admin/pause_threads || true

# 2. Pick a tarball to restore
TARBALL=/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-04-24_1634-session-close-post-canary.tar.gz

# 3. Verify the tarball before destroying live data
sudo tar tzf $TARBALL | head
# Expect: _snapshot_*.db, usage.db, tax_analyzer_state.json, etc.

# 4. Extract over the live volume
docker stop tax-ai-analyzer
sudo tar xzf $TARBALL -C /var/lib/docker/volumes/docker_tax_ai_data/_data/

# 5. The snapshot DB is named _snapshot_<timestamp>.db — rename to financial_analyzer.db
sudo mv /var/lib/docker/volumes/docker_tax_ai_data/_data/_snapshot_*.db \
        /var/lib/docker/volumes/docker_tax_ai_data/_data/financial_analyzer.db

# 6. Restart
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
sleep 5

# 7. Verify counts match expected post-restore state
docker exec tax-ai-analyzer python3 -c "
import sqlite3
c = sqlite3.connect('/app/data/financial_analyzer.db')
print('tx:', c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0])
print('docs:', c.execute('SELECT COUNT(*) FROM analyzed_documents').fetchone()[0])
print('integrity:', c.execute('PRAGMA integrity_check').fetchone())"
```

---

## 5. Rotate `ADMIN_INITIAL_PASSWORD` (only matters on fresh-DB bootstrap)

The `ensure_default_data()` gate refuses to seed an admin user without this
env var on a pristine DB. If the existing DB already has users, the value
is ignored — but it's still wise to set a fresh secret periodically.

```sh
# Generate a new secret
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")

# Update .env (gitignored)
sed -i "s|^TAX_AI_ADMIN_PASSWORD=.*|TAX_AI_ADMIN_PASSWORD=$NEW|" /home/dblagbro/docker/.env

# Recreate the container so it picks up the new env value
cd /home/dblagbro/docker
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

Note: rotating this does NOT change any existing admin user's password —
that's a separate flow under Settings → Users → Reset Password. The env
var only seeds on first-ever boot.

---

## 6. Image tags + Docker Hub

Naming convention: `dblagbro/tax-ai-analyzer:YYYY-MM-DD-<label>`.

Active tags as of session-close:
| Tag | Purpose |
|---|---|
| `2026-04-24-qa-remediated` | Current production (matches HEAD `9b3f623`) |
| `pre-remediation-2026-04-24_0107` | Nuclear rollback target |
| `2026-04-23-playwright-step1` | Pre-Phase-9 reference |
| `latest` | Stale — DO NOT use; use a dated tag |

Push a new tag to Docker Hub:
```sh
sudo docker tag dblagbro/tax-ai-analyzer:2026-04-24-qa-remediated \
                dblagbro/tax-ai-analyzer:2026-MM-DD-<label>
sudo docker push dblagbro/tax-ai-analyzer:2026-MM-DD-<label>
```

Roll back to an older image:
```sh
# Edit the compose image tag
sed -i 's|^.*image: dblagbro/tax-ai-analyzer:.*$|    image: dblagbro/tax-ai-analyzer:pre-remediation-2026-04-24_0107|' \
    /home/dblagbro/docker/docker-compose.yml
# Recreate
cd /home/dblagbro/docker
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

---

## 7. Bank importer troubleshooting

### Patchright + real Chrome won't launch (`Missing X server or $DISPLAY`)
Almost always Xvfb dying after `docker-entrypoint.sh` exec'd into python.
Symptoms in `ps -ef`: no `Xvfb :99` process. Fix:
```sh
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

### MFA push not arriving on user's phone
Cause is bank-side — not our code. Verify on phone:
1. Open the bank's mobile app
2. Settings → Notifications → enabled
3. iOS/Android system Settings → bank app → notifications → enabled
4. Manually log in to the bank's app once to "wake" the device registration

If the bank app can log in manually but the importer can't get a push,
the registered MFA device has likely changed. Re-register.

### Login succeeds but statement download yields 0 bytes / 536-byte stub
This is the known US Alliance statement-download bug. See
`qa/bug-statement-download-usalliance.md`. Workaround: log in to the
bank's mobile app, download statements manually, drop into
`/consume/personal/<year>/`. Paperless will OCR + ingest.

### Account lockout suspected
1. Stop firing import attempts immediately. Each adds another bad-login
   to the bank's record.
2. Manually log in via a real browser at the bank's website.
3. If "Your account has been locked": call the bank's recovery line +
   reset password.
4. Save the new password in Settings → `<bank>` credentials.
5. Then retry the importer.

### Cookie auto-save not skipping MFA on next run
Two failure modes:
- **Cookies not saved**: check `db.get_setting('<bank>_cookies')` is
  non-empty. If empty, the prior login failed before reaching the
  `Logged in` checkpoint where `save_auth_cookies()` runs.
- **Cookies saved but session rejected**: the bank's session model isn't
  pure cookie-based (US Alliance is known to do this — see Finding A in
  `bug-statement-download-usalliance.md`). The importer falls back to
  full credential login + MFA. Not a bug.

---

## 8. Known dangerous operations to avoid

| Action | Why dangerous |
|---|---|
| `docker compose down` | Stops the entire stack incl. Paperless |
| `docker compose down -v` | DESTROYS volumes. Total data loss. |
| `docker volume rm docker_tax_ai_data` | Same as above. Live data goes away. |
| `git push --force` to main | Lose commits. |
| Editing `/var/lib/docker/volumes/docker_tax_ai_data/_data/financial_analyzer.db` while container running | SQLite WAL corruption. Stop container first. |
| Setting `SESSION_COOKIE_SECURE=False` | Cookie no longer requires HTTPS — a rollback risk. |
| Removing `_safe_next()` guard in `app/routes/auth.py` | Re-opens CRIT-NEW-3 open redirect. |
| Editing `_STEALTH_ARGS` and forgetting it's shared | Affects 6 bank importers at once. |

---

## 9. Routine release-readiness checklist

Before a release/deploy:
- [ ] `pytest app/tests/` — 126/126 (or higher)
- [ ] `git status` — clean
- [ ] `git log --oneline origin/main..HEAD` — empty (nothing unpushed)
- [ ] `docker ps` — container Up, image is the dated tag (not `:latest`)
- [ ] `curl -sI .../login` — HTTP 200
- [ ] `qa/release-readiness-report-final.md` — verdict reflects current commit
- [ ] Backup tarball + git tag + Docker Hub push for rollback
- [ ] Smoke a US Alliance canary import (proves auth stack still works)

---

## 9b. LLM proxy chain (Phase 12 + 13)

### Manage proxy endpoints via admin UI
Sidebar → **LLM Routing** → Proxy endpoints panel.
- "Test" button: live round-trip with task=classification, cost=economy. Shows latency + the model the proxy actually picked.
- "Reset" button: clears circuit-breaker state (visible when an endpoint has failures or is tripped).
- "Disable" / "Enable": pulls an endpoint out of rotation without deleting.
- Priority is inline-editable.
- API keys are NEVER returned in full from the API — only last 4 chars shown.

### Manage per-task LMRH hint overrides
Sidebar → **LLM Routing** → Per-task LMRH hints panel.
- One row per task in `app/llm_client/lmrh.py:TASK_PRESETS`.
- Override input shows the default as a placeholder. Empty value clears the override.
- Save persists to `db.set_setting(f"lmrh.hint.{task}")`.

### Rotate the `llmp-*` proxy key
1. Provision a new key in the llm-proxy2 admin UI (separate project on `https://www.voipguru.org/llm-proxy2/`)
2. Update `LLM_PROXY2_KEY` in `/home/dblagbro/docker/.env`
3. `docker restart tax-ai-analyzer` — boot migration auto-rewrites the api_key on every existing endpoint row
4. Hit "Test" in the admin UI to confirm the new key round-trips

### Hard rule: no local-access URLs for LLM/proxy
Always public URL: `https://www.voipguru.org/llm-proxy2/v1`. NEVER `localhost`, `host.docker.internal`, internal docker names. The app has a defense-in-depth normalizer that rewrites local URLs to public on every boot, but don't rely on it — fix the env at the source.

### Diagnose a `CrossFamilySubstitution` exception
Raised when `tax-review` or `codegen` got a `chosen-because=cross-family-fallback` response from the proxy (rare under our `;require` config — should normally fail-fast at the proxy with 503 instead).
1. Check the WARNING log line — full `LLM-Capability` string is included.
2. If `requested-model` differs from `served-model`, the proxy substituted across families.
3. The `provider-hint=claude-oauth,anthropic,anthropic-direct;require` LMRH hint should have prevented this — if it didn't, our hint may have a typo or the proxy fleet's provider_type values changed. Run `docker exec tax-ai-analyzer python3 -c "from app.llm_client.lmrh import build_lmrh_header; print(build_lmrh_header('codegen'))"` to confirm the hint shape.
4. Caller falls through to direct vendor SDK on this exception; no user-visible failure unless that fallback also dies.

### Cache tokens reporting 0 on prompt-cached calls
**Expected.** `claude-oauth` (Pro Max OAuth path) doesn't surface cache fields to API callers — savings are recorded server-side. Don't chase. Query the proxy's activity log if you need real cache numbers:

```bash
curl ".../api/monitoring/activity?api_key_id=885b4635c8653425&event_type=llm_request&since=$(date -u -d '1 hour ago' +%FT%TZ)" \
  -H "Cookie: ..." \
  | jq '.[] | select(.metadata.requested_model != .metadata.served_model)'
```

Non-zero values in our local logs mean we routed to `anthropic-direct` (held in reserve, exposes the fields).

## 9c. Bank-Onboarding Wizard (Phase 11A-F)

### Submit a new bank
Sidebar → **Bank Queue** → "Submit a new bank" panel.
- Required: display_name + login_url (must be http(s)://).
- Optional: statements_url, platform_hint, notes.

### Capture a HAR recording
1. Open the bank's site in a real Chrome window
2. DevTools (F12) → Network tab → enable "Preserve log"
3. Log in + navigate to statements + click one statement
4. Right-click in Network tab → "Save all as HAR with content"
5. Upload the .har via the bank's detail panel + write a narration describing what you did

### Generate the importer
1. Click "⚡ Generate importer (Claude)" — ~30-60s
2. Review the validation badge (pass / syntax_error / shape_error / import_error)
3. Click "View" to see source code + validation notes in a new tab
4. Click "↻ Regen" if the draft needs corrective feedback (Phase 11F regenerate-with-feedback loop)

### Approve + deploy
1. Click "Approve" (gated on validation_status=pass; force=1 to override)
2. Click "Deploy" — writes `app/importers/<slug>_importer.py` with a deploy-marker first line
3. Bank moves to status="live"
4. New bank reachable at `/api/import/auto/<slug>/{credentials,cookies,status,mfa,start}`

### Switch a bank to Camoufox / proxy
Sidebar → **Bank Queue** → click bank → "Anti-detection (per-bank overrides)" panel.
- Engine dropdown: chrome (default) / firefox (Camoufox)
- Proxy URL input: `http://user:pass@proxy.example.com:8080` or `socks5://...` (provider-agnostic)

### Undeploy
Detail panel → click "Undeploy" — removes `app/importers/<slug>_importer.py`. Only works on files carrying the auto-deploy marker (hand-written importers are protected).

## 10. Files and where they live

| What | Where |
|---|---|
| Code | `/home/dblagbro/docker/tax-ai-analyzer/app/` (bind-mounted into container) |
| Data | Docker volume `docker_tax_ai_data` (NOT bind-mounted) |
| Backups | `/mnt/s/router_and_LAN/backups/www1/manual/` |
| Compose | `/home/dblagbro/docker/docker-compose.yml` (NOT in git) |
| Secrets | `/home/dblagbro/docker/.env` (gitignored) |
| QA docs | `qa/` inside repo (in git) |
| Bug log of record | `qa/bug-log.md` + `qa/bug-log-post-phase9*.md` (canonical findings) |
| Architecture | `architecture.md` (current as of Phase 13 / 2026-05-01) |
| Refactor history | `refactor-log.md` (current as of `af25088`) |
| LMRH spec (external) | https://www.voipguru.org/llm-proxy2/lmrh.md |
