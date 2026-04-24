# Backup & Snapshot Plan — tax-ai-analyzer fix cycle

**Purpose:** Protect the working state before remediation begins. Every group in
`remediation-plan.md` has a paired backup checkpoint and a rollback path.

**Baseline exists:** `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-2026-04-23_2004-step1pause.tar.gz`
(captured before the Playwright Step 1 session, still valid for this QA pass).

---

## 1. What to back up

| Asset | Path (inside container / host) | Why |
|---|---|---|
| SQLite DB + WAL + SHM | `/app/data/tax.db*` | Live user data: 3777 tx, 4574 analyzed docs, 664 links, 4906 activity rows |
| Uploaded receipts | `/app/data/uploads/` | User-uploaded attachments |
| Importer state | `/app/data/state/` | Gmail cursors, Plaid tokens, OFX history |
| Application config | `/app/data/config.json`, `.env` | Secret keys, OAuth state |
| Docker image SHA | `sha256:b0d42ef1` (current) | Exact reproducible build |
| Git commit | `e4b225c` | Source-level rollback point |
| Compose file | `/home/dblagbro/docker/docker-compose.yml` | Runtime wiring |

**Exclusions:** `__pycache__/`, `*.pyc`, container logs older than 24 h,
paperless corpus (separate product).

---

## 2. Snapshot checkpoints

Follow this sequence. Do NOT skip — each checkpoint is the rollback target for
the next group.

### Checkpoint 0 — Pre-remediation full snapshot (MANDATORY before any fix)

```bash
# Stop writes by quiescing background threads — do NOT bring the stack down
docker exec tax-ai-analyzer curl -s -X POST http://localhost:5000/api/admin/pause_threads
sleep 3

# Host-side tar of the mounted data volume
TS=$(date -u +%Y-%m-%d_%H%M)
sudo tar czf "/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-${TS}-pre-remediation.tar.gz" \
    -C /home/dblagbro/docker/tax-ai-analyzer data/

# Git tag
cd /home/dblagbro/docker/tax-ai-analyzer && git tag -a "pre-remediation-${TS}" -m "QA pass 2026-04-24 pre-fix baseline"

# Docker image pin — tag the current :latest to a dated tag so future pulls don't overwrite
docker tag dblagbro/tax-ai-analyzer:latest dblagbro/tax-ai-analyzer:pre-remediation-${TS}

# Resume threads
docker exec tax-ai-analyzer curl -s -X POST http://localhost:5000/api/admin/resume_threads
```

**Verify:** tarball size > 40 MB, `tar tzf` lists `data/tax.db`, `data/uploads/`,
git tag is visible in `git tag --list`, docker image list shows the new tag.

### Checkpoint A — After Group A (Credential Safety)

Smallest-impact, release-blocker fixes. Back up before AND after.

```bash
TS=$(date -u +%Y-%m-%d_%H%M)
sudo tar czf "/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-${TS}-post-groupA.tar.gz" \
    -C /home/dblagbro/docker/tax-ai-analyzer data/
git tag -a "post-groupA-${TS}" -m "After CRIT-1 + HIGH-2 fixes"
```

Rollback = git reset to `pre-remediation-${TS}`, restore tarball over `data/`.

### Checkpoint C — After Group C (Data Integrity, MED-1 + MED-3)

MED-3 involves a one-off orphan cleanup that touches `transaction_links`.
**Dry-run first** then backup.

```bash
# 1. Dry-run orphan cleanup (outputs count + IDs, does not delete)
docker exec tax-ai-analyzer python3 /app/tools/cleanup_orphans.py --dry-run

# 2. Only if dry-run output matches QA finding (1 orphan row, link_id=<known>), snapshot
TS=$(date -u +%Y-%m-%d_%H%M)
sudo tar czf "/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-${TS}-pre-groupC.tar.gz" \
    -C /home/dblagbro/docker/tax-ai-analyzer data/

# 3. Apply cleanup
docker exec tax-ai-analyzer python3 /app/tools/cleanup_orphans.py --apply

# 4. Post snapshot
TS2=$(date -u +%Y-%m-%d_%H%M)
sudo tar czf "/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-${TS2}-post-groupC.tar.gz" \
    -C /home/dblagbro/docker/tax-ai-analyzer data/
```

### Checkpoint E — Before Group E (Dev Hygiene, MED-6 + MED-7)

**Image-level changes.** Tag the current image BEFORE rebuild so we can revert.

```bash
docker tag dblagbro/tax-ai-analyzer:latest dblagbro/tax-ai-analyzer:pre-groupE-$(date -u +%Y-%m-%d)
# Now safe to modify requirements.txt, rebuild, update compose tag
```

Rollback = `docker compose up -d --force-recreate --no-deps tax-ai-analyzer`
with the compose `image:` pointing back at the pre-groupE tag.

### Checkpoint B — Before Group B (CSRF — widespread write-path change)

Highest-risk remediation group. Full snapshot + extended smoke required.

```bash
TS=$(date -u +%Y-%m-%d_%H%M)
sudo tar czf "/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-${TS}-pre-groupB.tar.gz" \
    -C /home/dblagbro/docker/tax-ai-analyzer data/
git tag -a "pre-groupB-${TS}" -m "Before CSRF rollout — high-risk change"

# Extra: export every user's account+entities as JSON for human-readable diff
docker exec tax-ai-analyzer python3 /app/tools/export_profile_snapshot.py > \
    "/mnt/s/router_and_LAN/backups/www1/manual/profile-snapshot-${TS}.json"
```

---

## 3. Rollback procedures

### 3a. Code-only rollback (most groups)

```bash
cd /home/dblagbro/docker/tax-ai-analyzer
git reset --hard <tag>
docker restart tax-ai-analyzer       # code picks up via bind mount
```

### 3b. Data rollback

```bash
# Stop writers
docker exec tax-ai-analyzer curl -s -X POST http://localhost:5000/api/admin/pause_threads

# Restore tarball over data/ (bind-mounted, so container sees it immediately)
sudo rm -rf /home/dblagbro/docker/tax-ai-analyzer/data.rollback-staging
sudo mkdir /home/dblagbro/docker/tax-ai-analyzer/data.rollback-staging
sudo tar xzf <backup-tarball> -C /home/dblagbro/docker/tax-ai-analyzer/data.rollback-staging
sudo mv /home/dblagbro/docker/tax-ai-analyzer/data /home/dblagbro/docker/tax-ai-analyzer/data.BAD-$(date -u +%s)
sudo mv /home/dblagbro/docker/tax-ai-analyzer/data.rollback-staging/data /home/dblagbro/docker/tax-ai-analyzer/data

# Restart single container — NEVER use docker compose down
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

### 3c. Image rollback

```bash
# Edit docker-compose.yml image: line back to dblagbro/tax-ai-analyzer:pre-groupE-<date>
sudo docker compose up -d --force-recreate --no-deps tax-ai-analyzer
```

### 3d. Full rollback (nuclear — if multi-group fix corrupts state)

1. Code: `git reset --hard pre-remediation-${TS}`
2. Data: restore `pre-remediation-${TS}` tarball (3b above)
3. Image: revert compose to `pre-remediation-${TS}` tag (3c above)
4. Single-container recreate
5. Run `session_smoke` suite to confirm baseline restored

---

## 4. Verification after every rollback

Mandatory 6-point check:

1. `curl -s http://localhost:5000/healthz` → 200
2. `docker exec tax-ai-analyzer pytest app/tests/test_session_smoke.py -q` → 6/6 pass
3. `SELECT COUNT(*) FROM transactions` matches pre-snapshot count (3777)
4. `SELECT COUNT(*) FROM analyzed_documents` matches (4574)
5. `SELECT COUNT(*) FROM activity_log` ≥ pre-snapshot count (activity is monotonic)
6. Login with admin/dblagbro → dashboard renders → tx list loads → logout

If any fail: stop, escalate, do not proceed.

---

## 5. Retention policy

- **Pre-remediation snapshot**: keep indefinitely (release-readiness artifact).
- **Per-group snapshots**: keep for 30 days after remediation completes, then prune.
- **Image dated tags**: keep the 3 most recent.
- **Git tags**: keep indefinitely (cheap).

---

## 6. Backup integrity tests

Run once before starting remediation:

```bash
# Test 1: tarball is readable and contains expected paths
tar tzf <latest-tarball> | grep -q 'data/tax.db$' || echo "FAIL: tx.db missing"
tar tzf <latest-tarball> | grep -q 'data/uploads/' || echo "FAIL: uploads missing"

# Test 2: SQLite file inside tarball is not corrupt
mkdir -p /tmp/bk-verify && tar xzf <latest-tarball> -C /tmp/bk-verify data/tax.db
sqlite3 /tmp/bk-verify/data/tax.db "PRAGMA integrity_check;" | grep -q "^ok$" || echo "FAIL: db corrupt"
rm -rf /tmp/bk-verify

# Test 3: git tag resolves
git rev-parse pre-remediation-${TS} >/dev/null || echo "FAIL: git tag missing"
```

All three must print nothing (empty = pass). Any "FAIL" = redo the backup.

---

## 7. Gaps this plan does NOT cover

- **External OAuth state** — Dropbox/Gmail tokens. If those rotate during remediation,
  restoring an older DB may use stale tokens. Mitigation: re-auth both flows manually
  after any full data rollback.
- **Docker volume state not bind-mounted** — the compose file uses bind mounts for
  `data/`, so this is actually fine, but confirm in `docker-compose.yml` that no
  named volumes hold writable user data.
- **Cross-machine restore** — tarballs are captured from `tmrwww01`. Restoring on a
  different host requires matching path `/home/dblagbro/docker/tax-ai-analyzer/data/`
  or compose override.
- **Live Playwright sessions** — if a scraping job is mid-flight, rollback of `state/`
  may leave orphaned cookies. Mitigation: pause importers before snapshotting.

---

## 8. Ordered sequence (to satisfy the QA prompt's "backup before fix" rule)

1. Run Checkpoint 0 (MANDATORY)
2. Backup integrity tests (§6) — must all pass
3. Begin Group A remediation
4. Checkpoint A post-backup
5. Smoke pass
6. Begin Group C → Checkpoint C → smoke
7. Begin Group D → snapshot → smoke
8. Checkpoint E → Group E → rebuild → smoke
9. Checkpoint B → Group B (solo commit) → full manual regression
10. Group F coverage expansion (iterative; no data risk)
11. Final post-fix snapshot → generate release-readiness-report.md
