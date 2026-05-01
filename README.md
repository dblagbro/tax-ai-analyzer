# Financial AI Analyzer

AI-powered financial document management and tax preparation tool. Organizes documents, categorizes transactions, and exports in multiple formats — all with Claude AI.

**Version 1.0.0** &nbsp;|&nbsp; Copyright © 2026 Devin Blagbrough

---

## Features

- **Multi-source import** — Local folders, Gmail, Google Drive, Dropbox, PayPal, IMAP, Plaid, SimpleFIN, and Playwright-based bank scrapers (US Alliance, US Bank, Capital One, Chime, Merrick, Verizon)
- **AI document analysis** — Claude-powered OCR, categorization, vendor/amount/date extraction, all routed through an LMRH-aware proxy chain (Phase 12)
- **Vision AI fallback** — When OCR fails, Claude reads document images directly
- **Bank-Onboarding Wizard** *(Phase 11A-F)* — Submit a new bank, upload a HAR recording + narration, AI codegen drafts a Playwright importer, AST-validate it, approve, auto-deploy to disk, and expose under `/api/import/auto/<slug>/*` — all without leaving the dashboard. Re-iterate via "Regenerate with feedback".
- **LLM Routing Admin** *(Phase 13)* — Manage proxy endpoints (priority, breaker reset, live test, key tail-only display) and per-task LMRH hint overrides directly in the dashboard.
- **Anti-detection browser stack** — patchright + real Google Chrome by default; per-bank flip to Camoufox (hardened Firefox) and/or residential proxy egress for sites that defeat Chromium-based scrapers.
- **Duplicate detection** — Automatic near-duplicate flagging and PDF content-hash deduplication across imports
- **Multi-entity support** — Manage Personal, LLC, Corp, DBA entities in a hierarchy
- **Transaction tracking** — Import bank CSVs, OFX/QFX files with AI categorization
- **AI Chat** — Natural language Q&A about your finances, exportable as PDF; multi-turn Tax Review mode (streaming, routed through proxy chain with cascade=auto for quality-per-dollar)
- **Tax Review** — AI acts as a tax accountant, streaming questions and flags for any year; compare against filed 1040 data
- **Filed Tax Returns** — Enter actual 1040 data (income, AGI, deductions, refund) for year-over-year comparison
- **Folder Manager** — Browse, rename, and queue local document folders for import with AI-assisted rename suggestions
- **AI Cost Tracking** — Per-model token and cost breakdown for all LLM calls (proxy-pool calls + direct-vendor fallback)
- **Multiple export formats** — CSV, PDF report, OFX/QFX, TurboTax TXF, QuickBooks IIF, JSON, ZIP bundle
- **Elasticsearch integration** — Optional vector search across all documents

## Quick Start

### Requirements
- Docker & Docker Compose
- Claude API key (Anthropic)
- Optional: Paperless-ngx instance for document storage

### Setup

1. **Copy the example env file:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env`** with your values:
   ```env
   LLM_API_KEY=sk-ant-...                     # Anthropic API key
   PAPERLESS_API_BASE_URL=http://paperless:8000
   PAPERLESS_API_TOKEN=your_token_here
   SECRET_KEY=change_me_random_string
   ```

3. **Add to your `docker-compose.yml`:**
   ```yaml
   tax-ai-analyzer:
     image: dblagbro/tax-ai-analyzer:latest
     container_name: tax-ai-analyzer
     ports:
       - "8012:8012"
     volumes:
       - tax_ai_data:/app/data
       - /mnt/s/documents/tax-organizer/export:/app/export
       - /mnt/s/documents/tax-organizer/media:/paperless/media:ro
       - /consume:/consume
     env_file: .env
     deploy:
       resources:
         limits:
           cpus: '4.0'
           memory: 4G

   volumes:
     tax_ai_data:
   ```

4. **Start the container:**
   ```bash
   docker compose up -d tax-ai-analyzer
   ```

5. **Open in browser:** `http://localhost:8012/tax-ai-analyzer/`

   Default credentials: `admin` / `admin` (change immediately in Settings → Users)

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_API_KEY` | ✅ | Anthropic (Claude) API key — used as direct-SDK fallback when the proxy pool is exhausted |
| `LLM_MODEL` | | Model name (default: `claude-sonnet-4-6`). Note: with LMRH routing, the proxy picks the model from `task=`/`cost=` hints; this only matters for the direct-SDK fallback path |
| `LLM_PROXY2_KEY` | | `llmp-*` key for the LMRH-aware proxy chain (preferred). Falls back to `LLM_PROXY_KEY` for back-compat |
| `LLM_PROXY2_URL` | | Public URL of the proxy. Defaults to `https://www.voipguru.org/llm-proxy2/v1`. Local-access URLs are auto-rewritten — never use `localhost`/`host.docker.internal`/internal docker names |
| `PAPERLESS_API_BASE_URL` | | Paperless-ngx API URL |
| `PAPERLESS_API_TOKEN` | | Paperless-ngx API token |
| `SECRET_KEY` / `FLASK_SECRET_KEY` | ✅ | Flask secret key (generate a random string) |
| `ADMIN_INITIAL_PASSWORD` | ✅ on fresh DB | Seeds the admin user on first boot. Refused if shorter than 12 chars |
| `ELASTICSEARCH_URL` | | Elasticsearch URL for vector search |
| `ELASTICSEARCH_PASSWORD` | | Elasticsearch password |
| `CONSUME_PATH` | | Path where PDFs are dropped for Paperless consumption |
| `ENTITIES` | | Comma-separated entity slugs (default: `personal,voipguru,martinfeld_ranch`) |
| `URL_PREFIX` | | URL prefix if behind a reverse proxy (e.g. `/tax-ai-analyzer`) |
| `BROWSER_ENGINE` | | Default Playwright engine: `chrome` (patchright) or `firefox` (Camoufox). Per-bank override via `<slug>_browser_engine` setting |
| `PROXY_URL` | | Default residential proxy URL for Playwright. Per-bank override via `<slug>_proxy_url` setting. Format: `http://user:pass@host:port` or `socks5://...` |

## Entity Types

The system supports a full entity hierarchy:

```
Person (SSN, DOB, address)
├── DBA (trade name, registration #)
└── LLC / Corp (EIN, employer ID)
    └── DBA (trade name)
```

Use **Admin → Entities** to manage your entity tree. The **Merge / Acquire** feature consolidates two entities, moving all records from source to target (useful for M&A events or corrections).

## Import Sources

| Source | Auth Method | Notes |
|--------|-------------|-------|
| Local Folder | None | Recursive scan of accessible server path |
| Gmail | OAuth2 | Connect via in-app flow; imports financial emails as PDFs |
| IMAP | Username + Password | Yahoo / iCloud / Outlook / AOL / generic |
| Google Drive | OAuth2 | Import from Drive folder |
| Dropbox | OAuth2 | Import from Dropbox path |
| Amazon S3 | Access keys | Bucket + prefix |
| PayPal | API credentials | Client ID + Secret |
| Plaid | OAuth-style access tokens | 12,000+ institutions via Plaid Link SDK |
| SimpleFIN | Bridge token | 16,000+ institutions, beta-bridge.simplefin.org |
| US Alliance FCU, US Bank, Capital One, Chime, Merrick, Verizon | Username + Password (+ MFA) | Playwright browser automation with patchright; cookie auto-save reuses sessions |
| **Auto-deployed banks** | (varies) | Banks added via the **Bank-Onboarding Wizard** (admin tab) get a Playwright importer auto-generated from a HAR recording, AST-validated, deployed to disk, and exposed under `/api/import/auto/<slug>/*` |

## Export Formats

All exports are per-entity, per-year:

| Format | File | Purpose |
|--------|------|---------|
| CSV | `transactions_YYYY_entity.csv` | Spreadsheet import |
| PDF Report | `summary_YYYY_entity.pdf` | Accountant-ready summary |
| OFX/QFX | `export_YYYY_entity.ofx` | Quicken / QB Online |
| TurboTax TXF | `export_YYYY_entity.txf` | TurboTax direct import |
| QuickBooks IIF | `export_YYYY_entity.iif` | QB Desktop import |
| JSON | `export_YYYY_entity.json` | Full machine-readable export |
| ZIP Bundle | `tax_YYYY_entity_complete.zip` | All formats + source PDFs |

## Nginx Reverse Proxy

```nginx
location /tax-ai-analyzer/ {
    auth_basic "Tax Organizer";
    auth_basic_user_file /etc/nginx/tax-organizer.htpasswd;
    proxy_pass http://tax-ai-analyzer:8012/;
    client_max_body_size 100M;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Backfill Existing Documents

If you have existing tax documents, use the backfill script to import them:

```bash
./backfill_existing_docs.sh
```

This copies PDFs from your existing tax folder into the Paperless consume directory, organized by year.

## Development

```bash
# Build locally
docker build -t dblagbro/tax-ai-analyzer:latest .

# Run with live code reload (bind mount app/)
docker run -p 8012:8012 \
  -v $(pwd)/app:/app/app \
  -v tax_ai_data:/app/data \
  --env-file .env \
  dblagbro/tax-ai-analyzer:latest
```

## CI/CD

GitHub Actions automatically builds and pushes to Docker Hub on every push to `main`.

Required repository secrets:
- `DOCKERHUB_USERNAME` — Docker Hub username
- `DOCKERHUB_TOKEN` — Docker Hub access token

## License

Copyright © 2026 Devin Blagbrough. All rights reserved.
