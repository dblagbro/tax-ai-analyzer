# `/app/data/` — runtime data directory

This directory is the Docker volume mount (`docker_tax_ai_data` → `/app/data`).
It survives container recreate. **Do not commit anything here to git.**

## Canonical files

| Path | What |
|---|---|
| `financial_analyzer.db` | Primary SQLite DB. Holds users, entities, analyzed_documents, transactions, transaction_links, import_jobs, activity_log, pdf_content_hashes, mileage_log, plaid_items, pending_banks, bank_recordings, generated_importers, llm_proxy_endpoints, chat_sessions, chat_messages, settings, daemon_heartbeats (when added). Schema initialized by `app.db.core.init_db()`. |
| `usage.db` | LLM cost-tracking SQLite DB. Holds the `llm_usage` table populated by `app.llm_usage_tracker.log_usage()`. Separate from the primary DB so cost queries don't lock app transactions. |
| `.flask_secret_key` | Flask session signing key. Generated on first boot if missing. |
| `tax_analyzer_state.json` | Persisted analyzer state (last-poll timestamps, in-progress IDs, etc.) — survives restart so we don't re-analyze docs Paperless already processed. |
| `gmail_credentials.json` | Google OAuth client credentials (operator-provisioned). |
| `gmail_token.json` | Refreshable OAuth token (auto-written after successful Gmail auth). |
| `chrome_profiles/` | Per-bank persistent Chrome user-data dirs for bank importers (cookies, localStorage, fingerprint state). |
| `onboarding/` | Uploaded HAR recordings + DOM snapshots from the Bank-Onboarding Wizard. |

## What's NOT here

Several legacy DB filenames have been cleaned up:
- `app.db` — never had real schema, leftover from very early scaffolding.
- `chat.db` — chat data lives in `financial_analyzer.db` (`chat_sessions`, `chat_messages`).
- `llm_usage.db` — renamed to `usage.db` during Phase 12 migration. The
  old filename was a stale 0-byte file until the post-Phase-14 QA cleanup.
- `tax_ai.db`, `tax_analyzer.db` — early scaffolding names never used.

If you encounter any of these as 0-byte files in this directory, they
can be safely deleted — no code path references them.

## Diagnostics

```bash
# Check schema of either DB
docker exec tax-ai-analyzer sqlite3 /app/data/financial_analyzer.db ".schema"
docker exec tax-ai-analyzer sqlite3 /app/data/usage.db ".tables"

# Tail recent llm_usage entries
docker exec tax-ai-analyzer sqlite3 /app/data/usage.db \
  "SELECT ts, provider, model, operation, success, cost_class FROM llm_usage ORDER BY ts DESC LIMIT 20"

# Disk usage of the volume
docker exec tax-ai-analyzer du -sh /app/data/*
```

## Backups

Tarballs live at `/mnt/s/router_and_LAN/backups/www1/manual/tax-ai-data-*.tar.gz`
on the Docker host. See `qa/runbook.md §4` for restore procedure.
