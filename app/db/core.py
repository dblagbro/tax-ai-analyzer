"""Core SQLite connection and schema initialisation."""
import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "financial_analyzer.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'standard',
            created_at TEXT DEFAULT (datetime('now')),
            last_login TEXT,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            type TEXT DEFAULT 'personal',
            tax_id TEXT,
            color TEXT DEFAULT '#1a3c5e',
            created_at TEXT DEFAULT (datetime('now')),
            archived INTEGER DEFAULT 0,
            metadata_json TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS tax_years (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id),
            year TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(entity_id, year)
        );

        CREATE TABLE IF NOT EXISTS analyzed_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paperless_doc_id INTEGER UNIQUE,
            entity_id INTEGER REFERENCES entities(id),
            tax_year TEXT,
            title TEXT,
            doc_type TEXT,
            category TEXT,
            vendor TEXT,
            amount REAL,
            date TEXT,
            confidence REAL,
            analyzed_at TEXT DEFAULT (datetime('now')),
            extracted_json TEXT DEFAULT '{}',
            paperless_tags_applied INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER REFERENCES entities(id),
            tax_year TEXT,
            source TEXT NOT NULL,
            source_id TEXT,
            date TEXT,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            vendor TEXT,
            description TEXT,
            category TEXT,
            doc_type TEXT,
            pdf_path TEXT,
            paperless_doc_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            metadata_json TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS import_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            entity_id INTEGER REFERENCES entities(id),
            status TEXT DEFAULT 'pending',
            started_at TEXT,
            completed_at TEXT,
            count_imported INTEGER DEFAULT 0,
            count_skipped INTEGER DEFAULT 0,
            error_msg TEXT,
            config_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS importer_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            name TEXT NOT NULL,
            credentials_json TEXT NOT NULL,
            entity_id INTEGER REFERENCES entities(id),
            created_at TEXT DEFAULT (datetime('now')),
            last_used TEXT,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            entity_id INTEGER REFERENCES entities(id),
            tax_year TEXT,
            title TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model_used TEXT,
            tokens_used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            detail TEXT,
            entity_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS url_pollers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            entity_id INTEGER REFERENCES entities(id),
            tax_year TEXT,
            auth_type TEXT DEFAULT 'none',
            auth_config_json TEXT DEFAULT '{}',
            poll_interval_hours INTEGER DEFAULT 24,
            last_polled TEXT,
            last_checksum TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Phase 12: LLM proxy endpoint pool (ported from paperless-ai-analyzer
        -- v3.9.0). Each row is one llm-proxy node; the proxy_manager picks the
        -- best healthy one per call and applies a circuit breaker to bad ones.
        CREATE TABLE IF NOT EXISTS llm_proxy_endpoints (
            id         TEXT PRIMARY KEY,
            label      TEXT NOT NULL,
            url        TEXT NOT NULL,
            api_key    TEXT NOT NULL,
            version    INTEGER NOT NULL DEFAULT 1,  -- 1 = Bearer, 2 = x-api-key
            priority   INTEGER NOT NULL DEFAULT 10,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_llm_proxy_priority
            ON llm_proxy_endpoints(enabled, priority ASC);
    """)
    conn.commit()
    _migrate(conn)
    _seed_llm_proxy_endpoints(conn)
    conn.close()
    logger.info("Database initialized")


def _migrate(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(analyzed_documents)").fetchall()}
    if "title" not in existing:
        conn.execute("ALTER TABLE analyzed_documents ADD COLUMN title TEXT")
        conn.commit()

    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if "edited" not in msg_cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN edited INTEGER DEFAULT 0")
        conn.commit()
    if "edit_of_id" not in msg_cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN edit_of_id INTEGER DEFAULT NULL")
        conn.commit()

    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    if "deleted" not in sess_cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN deleted INTEGER DEFAULT 0")
        conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gmail_processed_messages (
            message_id  TEXT PRIMARY KEY,
            gmail_id    TEXT,
            processed_at TEXT DEFAULT (datetime('now')),
            status      TEXT DEFAULT 'imported',
            entity_slug TEXT,
            year        TEXT,
            subject     TEXT,
            sender      TEXT
        );

        CREATE TABLE IF NOT EXISTS filed_tax_returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER REFERENCES entities(id),
            tax_year TEXT NOT NULL,
            filing_status TEXT DEFAULT 'single',
            agi REAL,
            wages_income REAL,
            business_income REAL,
            other_income REAL,
            total_income REAL,
            total_deductions REAL,
            taxable_income REAL,
            total_tax REAL,
            refund_amount REAL,
            amount_owed REAL,
            preparer_name TEXT,
            preparer_firm TEXT,
            filed_date TEXT,
            notes TEXT,
            form_data_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(entity_id, tax_year)
        );

        CREATE TABLE IF NOT EXISTS import_job_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL,
            line        TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_import_job_logs_job_id ON import_job_logs(job_id);

        CREATE TABLE IF NOT EXISTS chat_session_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            shared_with_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            can_write INTEGER DEFAULT 0,
            shared_by_user_id INTEGER REFERENCES users(id),
            shared_at TEXT DEFAULT (datetime('now')),
            UNIQUE(session_id, shared_with_user_id)
        );

        CREATE TABLE IF NOT EXISTS user_entity_access (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            access_level TEXT DEFAULT 'read',
            granted_by INTEGER REFERENCES users(id),
            granted_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, entity_id)
        );

        CREATE TABLE IF NOT EXISTS pdf_content_hashes (
            sha256      TEXT PRIMARY KEY,
            first_seen  TEXT DEFAULT (datetime('now')),
            source      TEXT,
            filename    TEXT,
            entity_slug TEXT,
            year        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pdf_hashes_source ON pdf_content_hashes(source);
    """)
    conn.commit()

    ent_cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
    for col, defn in [
        ("parent_entity_id", "INTEGER REFERENCES entities(id)"),
        ("display_name", "TEXT"),
        ("sort_order", "INTEGER DEFAULT 0"),
    ]:
        if col not in ent_cols:
            conn.execute(f"ALTER TABLE entities ADD COLUMN {col} {defn}")
            conn.commit()

    ad_cols = {r[1] for r in conn.execute("PRAGMA table_info(analyzed_documents)").fetchall()}
    if "is_duplicate" not in ad_cols:
        conn.execute("ALTER TABLE analyzed_documents ADD COLUMN is_duplicate INTEGER DEFAULT 0")
        conn.commit()

    ij_cols = {r[1] for r in conn.execute("PRAGMA table_info(import_jobs)").fetchall()}
    if "created_at" not in ij_cols:
        conn.execute("ALTER TABLE import_jobs ADD COLUMN created_at TEXT DEFAULT NULL")
        conn.commit()

    # transaction_links — cross-source event matching
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transaction_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id      INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
            doc_id      INTEGER REFERENCES analyzed_documents(id) ON DELETE CASCADE,
            link_type   TEXT NOT NULL DEFAULT 'match',
            confidence  REAL NOT NULL DEFAULT 0.0,
            created_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(txn_id, doc_id)
        );
        CREATE INDEX IF NOT EXISTS idx_txnlinks_txn ON transaction_links(txn_id);
        CREATE INDEX IF NOT EXISTS idx_txnlinks_doc ON transaction_links(doc_id);

        CREATE TABLE IF NOT EXISTS plaid_items (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id          TEXT UNIQUE NOT NULL,
            institution_id   TEXT,
            institution_name TEXT,
            access_token     TEXT NOT NULL,
            cursor           TEXT DEFAULT '',
            entity_id        INTEGER REFERENCES entities(id),
            last_sync        TEXT,
            status           TEXT DEFAULT 'active',
            created_at       TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_plaid_item_id ON plaid_items(item_id);

        CREATE TABLE IF NOT EXISTS mileage_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id       INTEGER REFERENCES entities(id),
            tax_year        TEXT,
            date            TEXT NOT NULL,
            miles           REAL NOT NULL,
            purpose         TEXT DEFAULT '',
            from_location   TEXT DEFAULT '',
            to_location     TEXT DEFAULT '',
            business        INTEGER DEFAULT 1,
            vehicle         TEXT DEFAULT '',
            odometer_start  REAL,
            odometer_end    REAL,
            notes           TEXT DEFAULT '',
            rate_per_mile   REAL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_mileage_entity ON mileage_log(entity_id);
        CREATE INDEX IF NOT EXISTS idx_mileage_year ON mileage_log(tax_year);
        CREATE INDEX IF NOT EXISTS idx_mileage_date ON mileage_log(date);
    """)
    conn.commit()

    # vendor_normalized on transactions for fuzzy matching
    txn_cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "vendor_normalized" not in txn_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN vendor_normalized TEXT DEFAULT ''")
        conn.commit()

    # cross_source_duplicate on analyzed_documents
    if "cross_source_duplicate" not in ad_cols:
        conn.execute(
            "ALTER TABLE analyzed_documents ADD COLUMN cross_source_duplicate INTEGER DEFAULT 0"
        )
        conn.commit()

    # Phase 12 cleanup: per ops 2026-04-30, llm-proxy-manager (v1) is
    # permanently decommissioned. Delete any legacy v1 rows so the chain
    # never tries to route through a dead host. We do this every boot
    # (idempotent — DELETE WHERE removes 0 rows on a clean DB).
    try:
        cur = conn.execute("DELETE FROM llm_proxy_endpoints WHERE version = 1")
        if cur.rowcount:
            logger.info(f"Removed {cur.rowcount} legacy v1 llm-proxy endpoint(s) per ops directive")
            conn.commit()
    except Exception as e:
        # Table might not exist yet on the very first boot — fine.
        logger.debug(f"v1 cleanup skipped: {e}")

    # Per Devin's directive (2026-04-30): no local-access URLs for LLM/proxy
    # calls. Rewrite any row whose URL is local to the public llm-proxy2 URL.
    # Also swap in the new v2 key if LLM_PROXY2_KEY is set in env (matches the
    # ops "key issuance + naming" provisioning model — the operator drops a
    # new key in env, we pick it up on next boot).
    import os
    new_key = os.environ.get("LLM_PROXY2_KEY", "").strip()
    try:
        rows = conn.execute(
            "SELECT id, url, api_key FROM llm_proxy_endpoints"
        ).fetchall()
        rewrites = 0
        for r in rows:
            updates = {}
            if _is_local_access_url(r["url"]):
                updates["url"] = PUBLIC_LLM_PROXY2_URL
            if new_key and r["api_key"] != new_key:
                updates["api_key"] = new_key
            if updates:
                cols = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(
                    f"UPDATE llm_proxy_endpoints SET {cols} WHERE id = ?",
                    (*updates.values(), r["id"]),
                )
                rewrites += 1
        if rewrites:
            conn.commit()
            logger.info(
                f"Rewrote {rewrites} llm-proxy endpoint URL(s)/key(s) "
                f"to canonical values per ops directive"
            )
    except Exception as e:
        logger.debug(f"llm-proxy URL/key normalization skipped: {e}")

    # ── Phase 11: bank-onboarding queue (admin-curated user submissions) ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pending_banks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            slug            TEXT UNIQUE NOT NULL,
            display_name    TEXT NOT NULL,
            login_url       TEXT NOT NULL,
            statements_url  TEXT DEFAULT '',
            platform_hint   TEXT DEFAULT '',  -- e.g. 'lumin_digital', 'q2', 'fis', 'unknown'
            submitted_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
              -- pending | recording | recorded | processing | generated | approved | rejected | live
            notes           TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pendingbanks_status ON pending_banks(status);
        CREATE INDEX IF NOT EXISTS idx_pendingbanks_slug   ON pending_banks(slug);

        CREATE TABLE IF NOT EXISTS bank_recordings (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_bank_id   INTEGER REFERENCES pending_banks(id) ON DELETE CASCADE,
            har_path          TEXT,                -- on-disk path under data/onboarding/
            narration_text    TEXT DEFAULT '',
            dom_snapshot_path TEXT,
            byte_size         INTEGER DEFAULT 0,
            captured_at       TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_bankrec_pending ON bank_recordings(pending_bank_id);

        CREATE TABLE IF NOT EXISTS generated_importers (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_bank_id   INTEGER REFERENCES pending_banks(id) ON DELETE CASCADE,
            recording_id      INTEGER REFERENCES bank_recordings(id) ON DELETE SET NULL,
            source_code       TEXT NOT NULL,       -- the generated <bank>_importer.py
            test_code         TEXT DEFAULT '',     -- generated test (optional)
            llm_model         TEXT DEFAULT '',
            llm_tokens_in     INTEGER DEFAULT 0,
            llm_tokens_out    INTEGER DEFAULT 0,
            generation_notes  TEXT DEFAULT '',     -- LLM's own commentary about confidence
            approved_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            approved_at       TEXT,
            generated_at      TEXT DEFAULT (datetime('now')),
            validation_status TEXT DEFAULT '',     -- 'pass' | 'syntax_error' | 'shape_error' | ''
            validation_notes  TEXT DEFAULT '',
            deployed_path     TEXT DEFAULT '',     -- on-disk path to the deployed .py module
            deployed_at       TEXT,
            deployed_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            parent_id         INTEGER REFERENCES generated_importers(id) ON DELETE SET NULL,
            feedback_text     TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_genimp_pending ON generated_importers(pending_bank_id);
    """)
    conn.commit()

    # Belt-and-braces ALTER for DBs created before the validation columns existed
    gen_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(generated_importers)"
    ).fetchall()}
    if "validation_status" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN validation_status TEXT DEFAULT ''"
        )
        conn.commit()
    if "validation_notes" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN validation_notes TEXT DEFAULT ''"
        )
        conn.commit()
    if "deployed_path" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN deployed_path TEXT DEFAULT ''"
        )
        conn.commit()
    if "deployed_at" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN deployed_at TEXT"
        )
        conn.commit()
    if "deployed_by" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN deployed_by INTEGER"
        )
        conn.commit()
    if "parent_id" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN parent_id INTEGER"
        )
        conn.commit()
    if "feedback_text" not in gen_cols:
        conn.execute(
            "ALTER TABLE generated_importers ADD COLUMN feedback_text TEXT DEFAULT ''"
        )
        conn.commit()


# ── LLM proxy endpoint pool helpers (Phase 12) ────────────────────────────────


# Public URL for llm-proxy2. Per Devin's directive (2026-04-30): NEVER use
# local-access URLs (localhost, host.docker.internal, internal docker names)
# for LLM/AI/proxy calls. Always use the public URL — even if the proxy is
# on the same host. We normalize any local URL we see (in env, in seed,
# in DB rows) to this canonical value.
PUBLIC_LLM_PROXY2_URL = "https://www.voipguru.org/llm-proxy2/v1"

_LOCAL_HOST_FRAGMENTS = (
    "localhost", "127.0.0.1", "host.docker.internal", "::1",
    "llm-proxy2", "llm-proxy-manager",  # internal docker names
)


def _is_local_access_url(url: str) -> bool:
    """True if url uses a local-access hostname that violates the public-URL
    rule for LLM/proxy calls."""
    if not url:
        return True
    u = url.lower()
    return any(frag in u for frag in _LOCAL_HOST_FRAGMENTS)


def _normalize_llm_proxy_url(url: str) -> str:
    """Normalize a proxy URL to the public form.

    If the URL is local-access (per Devin's directive), substitute the public
    llm-proxy2 URL. If it's already public, pass through unchanged.
    """
    if _is_local_access_url(url):
        return PUBLIC_LLM_PROXY2_URL
    return url


def _seed_llm_proxy_endpoints(conn):
    """Seed the llm_proxy_endpoints table on first boot.

    Runs only if the table is empty AND a key is configured (LLM_PROXY2_KEY
    preferred, LLM_PROXY_KEY for back-compat). Inserts a single primary endpoint:
      1. llm-proxy2 (v2, priority 10) — public URL only.

    Per ops 2026-04-30: v1 (llm-proxy-manager) is permanently decommissioned —
    do NOT seed it.

    The URL is locked to the public form regardless of env. Local-access URLs
    are explicitly rejected to keep LLM traffic visible/observable on the
    public proxy boundary.
    """
    import os, time, uuid as _uuid
    existing = conn.execute("SELECT COUNT(*) FROM llm_proxy_endpoints").fetchone()[0]
    if existing:
        return
    # Prefer the v2 key; fall back to the legacy var name for back-compat.
    key = (os.environ.get("LLM_PROXY2_KEY", "").strip()
           or os.environ.get("LLM_PROXY_KEY", "").strip())
    if not key:
        logger.info("LLM proxy seeding skipped: no LLM_PROXY2_KEY / LLM_PROXY_KEY set")
        return
    raw_url = os.environ.get("LLM_PROXY2_URL", PUBLIC_LLM_PROXY2_URL)
    proxy2_url = _normalize_llm_proxy_url(raw_url)
    if proxy2_url != raw_url:
        logger.warning(
            f"LLM_PROXY2_URL was local-access ({raw_url!r}); "
            f"normalized to public {proxy2_url!r} per ops directive"
        )
    seeds = [
        (str(_uuid.uuid4()), "llm-proxy2 (v2)", proxy2_url, key, 2, 10, 1, time.time()),
    ]
    conn.executemany(
        "INSERT INTO llm_proxy_endpoints "
        "(id, label, url, api_key, version, priority, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        seeds,
    )
    conn.commit()
    logger.info(f"Seeded {len(seeds)} LLM proxy endpoints (v1 dropped per ops)")


def llm_proxy_list_endpoints(include_disabled: bool = False) -> list[dict]:
    """Return endpoints ordered by priority ASC.

    By default returns only enabled endpoints (what the proxy_manager
    pool consumes). Pass include_disabled=True for the admin UI.
    """
    conn = get_connection()
    try:
        if include_disabled:
            rows = conn.execute(
                "SELECT id, label, url, api_key, version, priority, enabled "
                "FROM llm_proxy_endpoints ORDER BY priority ASC, label ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, label, url, api_key, version, priority, enabled "
                "FROM llm_proxy_endpoints WHERE enabled = 1 ORDER BY priority ASC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def llm_proxy_add_endpoint(label: str, url: str, api_key: str,
                           version: int = 2, priority: int = 10,
                           enabled: bool = True) -> str:
    """Insert a new endpoint. Returns the generated UUID."""
    import time, uuid as _uuid
    eid = str(_uuid.uuid4())
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO llm_proxy_endpoints "
            "(id, label, url, api_key, version, priority, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, label, url, api_key, int(version), int(priority),
             1 if enabled else 0, time.time())
        )
        conn.commit()
    finally:
        conn.close()
    return eid


def llm_proxy_update_endpoint(eid: str, **kwargs) -> bool:
    """Update one or more fields on an endpoint. Allowed fields:
    label, url, api_key, version, priority, enabled."""
    allowed = {"label", "url", "api_key", "version", "priority", "enabled"}
    cols = [k for k in kwargs if k in allowed]
    if not cols:
        return False
    sets = ", ".join(f"{c} = ?" for c in cols)
    vals = [kwargs[c] for c in cols]
    vals.append(eid)
    conn = get_connection()
    try:
        cur = conn.execute(f"UPDATE llm_proxy_endpoints SET {sets} WHERE id = ?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def llm_proxy_delete_endpoint(eid: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM llm_proxy_endpoints WHERE id = ?", (eid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
