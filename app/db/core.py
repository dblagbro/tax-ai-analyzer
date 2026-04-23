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
    """)
    conn.commit()
    _migrate(conn)
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
