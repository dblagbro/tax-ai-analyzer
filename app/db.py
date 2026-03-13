"""SQLite database layer for Financial AI Analyzer."""
import sqlite3
import os
import hashlib
import secrets
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "financial_analyzer.db")


def get_connection() -> sqlite3.Connection:
    """Return a thread-safe SQLite connection with row_factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    conn.executescript("""
        -- Users
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

        -- Entities (Personal, VoIPGuru, Martinfeld Ranch, etc.)
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

        -- Tax years per entity
        CREATE TABLE IF NOT EXISTS tax_years (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id),
            year TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(entity_id, year)
        );

        -- Analyzed documents (from Paperless)
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

        -- Imported transactions (PayPal, Venmo, Bank CSV, etc.)
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

        -- Import jobs (runs of importers)
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
            config_json TEXT DEFAULT '{}'
        );

        -- Importer credentials (Gmail OAuth token, PayPal keys, etc.)
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

        -- Chat sessions
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            entity_id INTEGER REFERENCES entities(id),
            tax_year TEXT,
            title TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        -- Chat messages
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model_used TEXT,
            tokens_used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Runtime settings (key-value)
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        -- Activity log
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            detail TEXT,
            entity_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- URL pollers
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
    # Migrations — add columns that may not exist in older DBs
    _migrate(conn)
    conn.close()
    logger.info("Database initialized")


def _migrate(conn):
    """Apply incremental schema migrations."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(analyzed_documents)").fetchall()}
    if "title" not in existing:
        conn.execute("ALTER TABLE analyzed_documents ADD COLUMN title TEXT")
        conn.commit()
        logger.info("Migration: added analyzed_documents.title")

    # Chat message edits
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if "edited" not in msg_cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN edited INTEGER DEFAULT 0")
        conn.commit()
    if "edit_of_id" not in msg_cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN edit_of_id INTEGER DEFAULT NULL")
        conn.commit()

    # Chat session soft-delete + vector embed flag
    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    if "deleted" not in sess_cols:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN deleted INTEGER DEFAULT 0")
        conn.commit()

    # Gmail processed message tracking (fast dedup)
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
    """)
    conn.commit()

    # Persistent import job logs (survive container restarts)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS import_job_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL,
            line        TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_import_job_logs_job_id ON import_job_logs(job_id);
    """)
    conn.commit()

    # Entity hierarchy: parent_entity_id + display_name
    ent_cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
    if "parent_entity_id" not in ent_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN parent_entity_id INTEGER REFERENCES entities(id)")
        conn.commit()
        logger.info("Migration: added entities.parent_entity_id")
    if "display_name" not in ent_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN display_name TEXT")
        conn.commit()
        logger.info("Migration: added entities.display_name")
    if "sort_order" not in ent_cols:
        conn.execute("ALTER TABLE entities ADD COLUMN sort_order INTEGER DEFAULT 0")
        conn.commit()

    # New tables for sharing and entity-level access control
    conn.executescript("""
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
    """)
    conn.commit()


# ── Password utilities ────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False


# ── User operations ───────────────────────────────────────────────────────────

def create_user(username: str, password: str, email: str = "", role: str = "standard") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, email, password_hash, role) VALUES(?,?,?,?)",
            (username, email or None, _hash_password(password), role),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_by_username(username: str):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def authenticate_user(username: str, password: str):
    """Return user row if credentials valid, else None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
        if row and _verify_password(password, row["password_hash"]):
            conn.execute(
                "UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],)
            )
            conn.commit()
            return row
        return None
    finally:
        conn.close()


def list_users():
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id,username,email,role,created_at,last_login,active "
            "FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()


def update_user(user_id: int, **kwargs):
    conn = get_connection()
    try:
        allowed = {"email", "role", "active", "username"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if "password" in kwargs:
            fields["password_hash"] = _hash_password(kwargs["password"])
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id))
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id: int):
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def user_count() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    finally:
        conn.close()


# ── Entity operations ─────────────────────────────────────────────────────────

def create_entity(
    name: str,
    slug: str,
    entity_type: str = "personal",
    description: str = "",
    tax_id: str = "",
    color: str = "#1a3c5e",
    parent_entity_id: int = None,
    display_name: str = None,
    metadata_json: str = "{}",
    sort_order: int = 0,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO entities(name,slug,type,description,tax_id,color,"
            "parent_entity_id,display_name,metadata_json,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, slug, entity_type, description, tax_id, color,
             parent_entity_id, display_name or name, metadata_json, sort_order),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_entity(entity_id: int = None, slug: str = None):
    conn = get_connection()
    try:
        if entity_id is not None:
            return conn.execute(
                "SELECT * FROM entities WHERE id=?", (entity_id,)
            ).fetchone()
        if slug is not None:
            return conn.execute(
                "SELECT * FROM entities WHERE slug=?", (slug,)
            ).fetchone()
        return None
    finally:
        conn.close()


def list_entities(include_archived: bool = False):
    conn = get_connection()
    try:
        if include_archived:
            return conn.execute("SELECT * FROM entities ORDER BY name").fetchall()
        return conn.execute(
            "SELECT * FROM entities WHERE archived=0 ORDER BY name"
        ).fetchall()
    finally:
        conn.close()


def update_entity(entity_id: int, **kwargs):
    conn = get_connection()
    try:
        allowed = {
            "name", "description", "type", "tax_id", "color", "archived",
            "metadata_json", "parent_entity_id", "display_name", "sort_order",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE entities SET {sets} WHERE id=?", (*fields.values(), entity_id))
        conn.commit()
    finally:
        conn.close()


def merge_entities(source_id: int, target_id: int) -> dict:
    """Reassign all records from source entity to target entity, then archive source."""
    conn = get_connection()
    try:
        counts = {}
        for table, col in [
            ("transactions", "entity_id"),
            ("analyzed_documents", "entity_id"),
            ("import_jobs", "entity_id"),
            ("chat_sessions", "entity_id"),
            ("tax_years", "entity_id"),
            ("url_pollers", "entity_id"),
            ("importer_credentials", "entity_id"),
        ]:
            cur = conn.execute(
                f"UPDATE {table} SET {col}=? WHERE {col}=?", (target_id, source_id)
            )
            if cur.rowcount:
                counts[table] = cur.rowcount
        # Move children entities to new parent
        conn.execute(
            "UPDATE entities SET parent_entity_id=? WHERE parent_entity_id=?",
            (target_id, source_id),
        )
        # Archive source
        conn.execute("UPDATE entities SET archived=1 WHERE id=?", (source_id,))
        conn.commit()
        return counts
    finally:
        conn.close()


def get_entity_tree() -> list:
    """Return entities as a tree (parents with children list)."""
    rows = list_entities(include_archived=False)
    by_id = {r["id"]: dict(r) | {"children": []} for r in rows}
    roots = []
    for eid, ent in by_id.items():
        pid = ent.get("parent_entity_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(ent)
        else:
            roots.append(ent)
    return roots


# ── Tax year operations ───────────────────────────────────────────────────────

def ensure_tax_year(entity_id: int, year: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tax_years WHERE entity_id=? AND year=?", (entity_id, year)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO tax_years(entity_id,year) VALUES(?,?)", (entity_id, year)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_tax_years(entity_id: int = None):
    conn = get_connection()
    try:
        if entity_id is not None:
            return conn.execute(
                "SELECT * FROM tax_years WHERE entity_id=? ORDER BY year DESC", (entity_id,)
            ).fetchall()
        return conn.execute(
            "SELECT DISTINCT year FROM tax_years ORDER BY year DESC"
        ).fetchall()
    finally:
        conn.close()


def update_tax_year_status(entity_id: int, year: str, status: str, notes: str = ""):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tax_years SET status=?, notes=? WHERE entity_id=? AND year=?",
            (status, notes, entity_id, year),
        )
        conn.commit()
    finally:
        conn.close()


# ── Analyzed document operations ──────────────────────────────────────────────

def mark_document_analyzed(
    paperless_doc_id: int,
    entity_id: int,
    tax_year: str,
    doc_type: str,
    category: str,
    vendor: str,
    amount: float,
    date: str,
    confidence: float,
    extracted_json: str = "{}",
    title: str = None,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO analyzed_documents
                (paperless_doc_id,entity_id,tax_year,title,doc_type,category,vendor,
                 amount,date,confidence,extracted_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(paperless_doc_id) DO UPDATE SET
                entity_id=excluded.entity_id,
                tax_year=excluded.tax_year,
                title=excluded.title,
                doc_type=excluded.doc_type,
                category=excluded.category,
                vendor=excluded.vendor,
                amount=excluded.amount,
                date=excluded.date,
                confidence=excluded.confidence,
                extracted_json=excluded.extracted_json,
                analyzed_at=datetime('now')
            """,
            (
                paperless_doc_id, entity_id, tax_year, title, doc_type, category,
                vendor, amount, date, confidence, extracted_json,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_analyzed_doc_ids() -> set:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT paperless_doc_id FROM analyzed_documents").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def get_analyzed_documents(
    entity_id: int = None,
    tax_year: str = None,
    category: str = None,
    limit: int = 500,
):
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("d.entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("d.tax_year=?")
            params.append(tax_year)
        if category:
            where.append("d.category=?")
            params.append(category)
        w = f"WHERE {' AND '.join(where)}" if where else ""
        return conn.execute(
            f"""
            SELECT d.*, e.name as entity_name, e.slug as entity_slug, e.color as entity_color
            FROM analyzed_documents d
            LEFT JOIN entities e ON e.id=d.entity_id
            {w} ORDER BY d.date DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()


def get_financial_summary(entity_id: int = None, tax_year: str = None) -> dict:
    """Return income/expense/deduction totals.

    Dedup logic applied to avoid inflated totals from email imports:
    1. Exclude statement doc_types (credit_card_statement, bank_statement,
       mortgage_statement) — their amounts represent balances/totals, not
       individual transactions; the actual charges are captured as receipts.
    2. Deduplicate by (vendor, amount, year-month) so that multiple email
       notifications about the same payment (scheduled/confirmed/reminder)
       only count once per calendar month.
    """
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("tax_year=?")
            params.append(tax_year)
        # Exclude statement types — they show balances, not individual transactions
        where.append(
            "doc_type NOT IN ('credit_card_statement','bank_statement','mortgage_statement')"
        )
        w = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT category, COUNT(*) as count, COALESCE(SUM(amount),0) as total
            FROM (
                -- Deduplicate: same vendor+amount within the same calendar month
                -- counts as one transaction (handles payment scheduled/confirmed/reminder)
                SELECT category,
                       MAX(amount) as amount
                FROM analyzed_documents {w}
                  AND amount IS NOT NULL AND amount > 0
                GROUP BY
                    COALESCE(vendor,''),
                    amount,
                    strftime('%Y-%m', COALESCE(date,'2000-01')),
                    category
            ) deduped
            GROUP BY category
            """,
            params,
        ).fetchall()
        result = {
            "income": 0.0,
            "expense": 0.0,
            "deduction": 0.0,
            "asset": 0.0,
            "other": 0.0,
            "counts": {},
        }
        for r in rows:
            cat = r["category"] or "other"
            result[cat] = result.get(cat, 0.0) + (r["total"] or 0.0)
            result["counts"][cat] = r["count"]
        result["net"] = result["income"] - result["expense"] - result["deduction"]
        return result
    finally:
        conn.close()


def set_paperless_tags_applied(paperless_doc_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE analyzed_documents SET paperless_tags_applied=1 WHERE paperless_doc_id=?",
            (paperless_doc_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── Transaction operations ────────────────────────────────────────────────────

def upsert_transaction(
    source: str,
    source_id: str,
    entity_id: int,
    tax_year: str,
    date: str,
    amount: float,
    vendor: str,
    description: str,
    category: str = "",
    doc_type: str = "",
    pdf_path: str = "",
    metadata_json: str = "{}",
) -> int:
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM transactions WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO transactions
                (source,source_id,entity_id,tax_year,date,amount,vendor,description,
                 category,doc_type,pdf_path,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source, source_id, entity_id, tax_year, date, amount, vendor,
                description, category, doc_type, pdf_path, metadata_json,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_transactions(
    entity_id: int = None,
    tax_year: str = None,
    source: str = None,
    limit: int = 500,
):
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("t.entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("t.tax_year=?")
            params.append(tax_year)
        if source:
            where.append("t.source=?")
            params.append(source)
        w = f"WHERE {' AND '.join(where)}" if where else ""
        return conn.execute(
            f"""
            SELECT t.*, e.name as entity_name, e.color as entity_color
            FROM transactions t
            LEFT JOIN entities e ON e.id=t.entity_id
            {w} ORDER BY t.date DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()


def get_transaction(transaction_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT t.*, e.name as entity_name FROM transactions t "
            "LEFT JOIN entities e ON e.id=t.entity_id WHERE t.id=?",
            (transaction_id,),
        ).fetchone()
    finally:
        conn.close()


def update_transaction(transaction_id: int, **kwargs):
    conn = get_connection()
    try:
        allowed = {
            "entity_id", "tax_year", "date", "amount", "vendor", "description",
            "category", "doc_type", "pdf_path", "paperless_doc_id", "metadata_json",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE transactions SET {sets} WHERE id=?", (*fields.values(), transaction_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_transaction_summary(entity_id: int = None, tax_year: str = None) -> dict:
    conn = get_connection()
    try:
        where, params = [], []
        if entity_id is not None:
            where.append("entity_id=?")
            params.append(entity_id)
        if tax_year:
            where.append("tax_year=?")
            params.append(tax_year)
        w = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT source, COUNT(*) as count, COALESCE(SUM(amount),0) as total
            FROM transactions {w}
            GROUP BY source
            """,
            params,
        ).fetchall()
        return {r["source"]: {"count": r["count"], "total": r["total"]} for r in rows}
    finally:
        conn.close()


# ── Import job operations ─────────────────────────────────────────────────────

def create_import_job(
    source_type: str, entity_id: int = None, config_json: str = "{}"
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO import_jobs(source_type,entity_id,status,config_json) VALUES(?,?,?,?)",
            (source_type, entity_id, "pending", config_json),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_import_job(job_id: int, **kwargs):
    conn = get_connection()
    try:
        allowed = {
            "status", "started_at", "completed_at",
            "count_imported", "count_skipped", "error_msg",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE import_jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        conn.commit()
    finally:
        conn.close()


def list_import_jobs(source_type: str = None, limit: int = 50):
    conn = get_connection()
    try:
        where = "WHERE source_type=?" if source_type else ""
        params = [source_type] if source_type else []
        return conn.execute(
            f"SELECT j.*, e.name as entity_name FROM import_jobs j "
            f"LEFT JOIN entities e ON e.id=j.entity_id "
            f"{where} ORDER BY j.id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()


def get_import_job(job_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT j.*, e.name as entity_name FROM import_jobs j "
            "LEFT JOIN entities e ON e.id=j.entity_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


# ── Credentials operations ────────────────────────────────────────────────────

def save_credential(
    source_type: str,
    name: str,
    credentials_json: str,
    entity_id: int = None,
) -> int:
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM importer_credentials WHERE source_type=? AND name=?",
            (source_type, name),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE importer_credentials "
                "SET credentials_json=?,last_used=datetime('now') WHERE id=?",
                (credentials_json, existing["id"]),
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO importer_credentials(source_type,name,credentials_json,entity_id) "
            "VALUES(?,?,?,?)",
            (source_type, name, credentials_json, entity_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_credential(source_type: str, name: str = None):
    conn = get_connection()
    try:
        if name:
            return conn.execute(
                "SELECT * FROM importer_credentials "
                "WHERE source_type=? AND name=? AND active=1",
                (source_type, name),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM importer_credentials "
            "WHERE source_type=? AND active=1 ORDER BY id LIMIT 1",
            (source_type,),
        ).fetchone()
    finally:
        conn.close()


def list_credentials(source_type: str = None):
    conn = get_connection()
    try:
        if source_type:
            return conn.execute(
                "SELECT id,source_type,name,entity_id,created_at,last_used,active "
                "FROM importer_credentials WHERE source_type=? ORDER BY name",
                (source_type,),
            ).fetchall()
        return conn.execute(
            "SELECT id,source_type,name,entity_id,created_at,last_used,active "
            "FROM importer_credentials ORDER BY source_type,name"
        ).fetchall()
    finally:
        conn.close()


def delete_credential(credential_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE importer_credentials SET active=0 WHERE id=?", (credential_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ── Chat operations ───────────────────────────────────────────────────────────

def create_chat_session(
    user_id: int,
    entity_id: int = None,
    tax_year: str = None,
    title: str = "New Chat",
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO chat_sessions(user_id,entity_id,tax_year,title) VALUES(?,?,?,?)",
            (user_id, entity_id, tax_year, title),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_chat_session(session_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT s.*, e.name as entity_name FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id WHERE s.id=?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def update_chat_session_title(session_id: int, title: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE chat_sessions SET title=?,updated_at=datetime('now') WHERE id=?",
            (title, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_chat_sessions(user_id: int, entity_id: int = None):
    conn = get_connection()
    try:
        if entity_id is not None:
            return conn.execute(
                "SELECT s.*, e.name as entity_name FROM chat_sessions s "
                "LEFT JOIN entities e ON e.id=s.entity_id "
                "WHERE s.user_id=? AND s.entity_id=? ORDER BY s.updated_at DESC",
                (user_id, entity_id),
            ).fetchall()
        return conn.execute(
            "SELECT s.*, e.name as entity_name FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id "
            "WHERE s.user_id=? ORDER BY s.updated_at DESC LIMIT 50",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def delete_chat_session(session_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM chat_session_shares WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def search_chat_sessions(user_id: int, query: str, is_admin: bool = False) -> list:
    """Full-text search over session titles and message content."""
    conn = get_connection()
    try:
        q = f"%{query}%"
        if is_admin:
            rows = conn.execute(
                "SELECT DISTINCT s.*, e.name as entity_name FROM chat_sessions s "
                "LEFT JOIN entities e ON e.id=s.entity_id "
                "LEFT JOIN chat_messages m ON m.session_id=s.id "
                "WHERE s.deleted=0 AND (s.title LIKE ? OR m.content LIKE ?) "
                "ORDER BY s.updated_at DESC LIMIT 50",
                (q, q),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT s.*, e.name as entity_name FROM chat_sessions s "
                "LEFT JOIN entities e ON e.id=s.entity_id "
                "LEFT JOIN chat_messages m ON m.session_id=s.id "
                "LEFT JOIN chat_session_shares sh ON sh.session_id=s.id AND sh.shared_with_user_id=? "
                "WHERE s.deleted=0 AND (s.user_id=? OR sh.shared_with_user_id IS NOT NULL) "
                "AND (s.title LIKE ? OR m.content LIKE ?) "
                "ORDER BY s.updated_at DESC LIMIT 50",
                (user_id, user_id, q, q),
            ).fetchall()
        return rows
    finally:
        conn.close()


def list_chat_sessions(user_id: int, entity_id: int = None, include_shared: bool = True):
    """List sessions owned by user plus sessions shared with them."""
    conn = get_connection()
    try:
        base_select = (
            "SELECT DISTINCT s.*, e.name as entity_name, "
            "CASE WHEN s.user_id=? THEN 0 ELSE 1 END as is_shared "
            "FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id "
        )
        share_join = "LEFT JOIN chat_session_shares sh ON sh.session_id=s.id AND sh.shared_with_user_id=? "
        where = "WHERE s.deleted=0 AND (s.user_id=? OR sh.shared_with_user_id IS NOT NULL) "
        order = "ORDER BY s.updated_at DESC LIMIT 100"

        if entity_id is not None:
            where += "AND s.entity_id=? "
            rows = conn.execute(
                base_select + share_join + where + order,
                (user_id, user_id, user_id, entity_id),
            ).fetchall()
        else:
            rows = conn.execute(
                base_select + share_join + where + order,
                (user_id, user_id, user_id),
            ).fetchall()
        return rows
    finally:
        conn.close()


def truncate_messages_from(session_id: int, from_message_id: int):
    """Delete all messages in session at or after the given message id."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM chat_messages WHERE session_id=? AND id>=?",
            (session_id, from_message_id),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=datetime('now') WHERE id=?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def share_chat_session(session_id: int, shared_with_user_id: int,
                       shared_by_user_id: int, can_write: bool = False):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO chat_session_shares"
            "(session_id, shared_with_user_id, shared_by_user_id, can_write) VALUES(?,?,?,?)",
            (session_id, shared_with_user_id, shared_by_user_id, 1 if can_write else 0),
        )
        conn.commit()
    finally:
        conn.close()


def unshare_chat_session(session_id: int, shared_with_user_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM chat_session_shares WHERE session_id=? AND shared_with_user_id=?",
            (session_id, shared_with_user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_chat_shares(session_id: int) -> list:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT sh.*, u.username, u.email FROM chat_session_shares sh "
            "JOIN users u ON u.id=sh.shared_with_user_id WHERE sh.session_id=?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


# ── Entity access control ─────────────────────────────────────────────────────

def get_user_entity_access(user_id: int) -> list:
    """Return list of entity_ids the user has explicit access to."""
    conn = get_connection()
    try:
        return [r["entity_id"] for r in conn.execute(
            "SELECT entity_id FROM user_entity_access WHERE user_id=?", (user_id,)
        ).fetchall()]
    finally:
        conn.close()


def set_user_entity_access(user_id: int, entity_id: int,
                           access_level: str, granted_by: int):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO user_entity_access"
            "(user_id, entity_id, access_level, granted_by) VALUES(?,?,?,?)",
            (user_id, entity_id, access_level, granted_by),
        )
        conn.commit()
    finally:
        conn.close()


def revoke_user_entity_access(user_id: int, entity_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM user_entity_access WHERE user_id=? AND entity_id=?",
            (user_id, entity_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_entity_access(entity_id: int) -> list:
    """Return all users with access to a given entity."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT ua.*, u.username, u.email, u.role FROM user_entity_access ua "
            "JOIN users u ON u.id=ua.user_id WHERE ua.entity_id=?",
            (entity_id,),
        ).fetchall()
    finally:
        conn.close()


def get_chat_messages(session_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def append_chat_message(
    session_id: int,
    role: str,
    content: str,
    model_used: str = "",
    tokens_used: int = 0,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO chat_messages(session_id,role,content,model_used,tokens_used) "
            "VALUES(?,?,?,?,?)",
            (session_id, role, content, model_used, tokens_used),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=datetime('now') WHERE id=?", (session_id,)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_settings() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def delete_setting(key: str):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()


# ── URL poller operations ─────────────────────────────────────────────────────

def create_url_poller(
    name: str,
    url: str,
    entity_id: int = None,
    tax_year: str = None,
    auth_type: str = "none",
    auth_config_json: str = "{}",
    poll_interval_hours: int = 24,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO url_pollers(name,url,entity_id,tax_year,auth_type,"
            "auth_config_json,poll_interval_hours) VALUES(?,?,?,?,?,?,?)",
            (name, url, entity_id, tax_year, auth_type, auth_config_json, poll_interval_hours),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_url_pollers(active_only: bool = True):
    conn = get_connection()
    try:
        where = "WHERE p.active=1" if active_only else ""
        return conn.execute(
            f"SELECT p.*, e.name as entity_name FROM url_pollers p "
            f"LEFT JOIN entities e ON e.id=p.entity_id {where} ORDER BY p.name"
        ).fetchall()
    finally:
        conn.close()


def update_url_poller_poll(poller_id: int, checksum: str):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE url_pollers SET last_polled=datetime('now'),last_checksum=? WHERE id=?",
            (checksum, poller_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_url_poller(poller_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM url_pollers WHERE id=?", (poller_id,))
        conn.commit()
    finally:
        conn.close()


# ── Activity log ──────────────────────────────────────────────────────────────

def log_activity(
    action: str, detail: str = "", user_id: int = None, entity_id: int = None
):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activity_log(action,detail,user_id,entity_id) VALUES(?,?,?,?)",
            (action, detail, user_id, entity_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_recent_activity(limit: int = 50):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT a.*, u.username, e.name as entity_name FROM activity_log a "
            "LEFT JOIN users u ON u.id=a.user_id "
            "LEFT JOIN entities e ON e.id=a.entity_id "
            "ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def ensure_default_data():
    """Create default admin user and entities if DB is empty."""
    from app.config import DEFAULT_ENTITIES, DEFAULT_TAX_YEARS

    if user_count() == 0:
        create_user("admin", "admin", "admin@localhost", "admin")
        logger.info(
            "Created default admin user (password: admin) — CHANGE THIS IMMEDIATELY"
        )

    for ent in DEFAULT_ENTITIES:
        existing = get_entity(slug=ent["slug"])
        # Resolve parent_slug → parent_entity_id
        parent_id = None
        if ent.get("parent_slug"):
            parent_row = get_entity(slug=ent["parent_slug"])
            if parent_row:
                parent_id = parent_row["id"]
        if not existing:
            _result = create_entity(
                name=ent["name"],
                slug=ent["slug"],
                entity_type=ent.get("type", "personal"),
                color=ent.get("color", "#1a3c5e"),
                parent_entity_id=parent_id,
                display_name=ent.get("display_name", ent["name"]),
                sort_order=ent.get("sort_order", 0),
            )
            eid = _result["id"] if isinstance(_result, dict) else _result
            for year in DEFAULT_TAX_YEARS:
                ensure_tax_year(eid, year)
            logger.info(f"Created entity: {ent['name']}")
        elif parent_id and not existing["parent_entity_id"]:
            # Backfill parent linkage for pre-existing entities
            update_entity(existing["id"], parent_entity_id=parent_id,
                          display_name=ent.get("display_name", existing["name"]),
                          sort_order=ent.get("sort_order", 0))


# ---------------------------------------------------------------------------
# Compatibility aliases (web_ui.py naming convention)
# ---------------------------------------------------------------------------

def get_entities(include_archived: bool = False) -> list:
    rows = list_entities(include_archived=include_archived)
    return [dict(r) for r in rows]


def get_entity_dict(entity_id=None, slug: str = None) -> dict:
    row = get_entity(entity_id=entity_id, slug=slug)
    return dict(row) if row else None


def archive_entity(entity_id) -> bool:
    try:
        conn = get_connection()
        conn.execute("UPDATE entities SET archived=1 WHERE id=?", (int(entity_id),))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def get_transactions(entity_id=None, year=None, source=None, category=None,
                     limit: int = 100, offset: int = 0) -> list:
    eid = int(entity_id) if entity_id else None
    rows = list_transactions(entity_id=eid, tax_year=year, source=source, limit=limit + offset)
    items = [dict(r) for r in rows]
    if category:
        items = [r for r in items if r.get("category") == category]
    return items[offset:offset + limit]


def count_transactions(entity_id=None, year=None, source=None, category=None) -> int:
    conn = get_connection()
    conditions = []
    params = []
    if entity_id:
        conditions.append("entity_id=?")
        params.append(int(entity_id))
    if year:
        conditions.append("tax_year=?")
        params.append(str(year))
    if source:
        conditions.append("source=?")
        params.append(source)
    if category:
        conditions.append("category=?")
        params.append(category)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    row = conn.execute(f"SELECT COUNT(*) FROM transactions {where}", params).fetchone()
    conn.close()
    return row[0] if row else 0


def add_transaction(data: dict) -> dict:
    import json as _json
    conn = get_connection()
    try:
        entity_id = int(data.get("entity_id")) if data.get("entity_id") else None
        cur = conn.execute(
            """INSERT INTO transactions
               (source,source_id,entity_id,tax_year,date,amount,vendor,description,
                category,doc_type,pdf_path,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("source", "manual"),
                data.get("source_id") or secrets.token_hex(8),
                entity_id,
                data.get("year") or data.get("tax_year", ""),
                data.get("date", ""),
                float(data.get("amount", 0)),
                data.get("vendor") or data.get("description", "")[:50],
                data.get("description", ""),
                data.get("category", ""),
                data.get("doc_type", ""),
                data.get("pdf_path", ""),
                _json.dumps(data.get("raw") or data.get("metadata") or {}),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_entity(entity_id=None, slug: str = None):
    """Override to return dict."""
    conn = get_connection()
    try:
        if entity_id is not None:
            row = conn.execute("SELECT * FROM entities WHERE id=?", (int(entity_id),)).fetchone()
        elif slug is not None:
            row = conn.execute("SELECT * FROM entities WHERE slug=?", (slug,)).fetchone()
        else:
            return None
        return dict(row) if row else None
    finally:
        conn.close()


def create_entity(name: str, slug: str = None, entity_type: str = "personal",
                  description: str = "", tax_id: str = "", color: str = "#1a3c5e",
                  parent_entity_id: int = None, display_name: str = None,
                  metadata_json: str = "{}", sort_order: int = 0) -> dict:
    """Override to auto-generate slug and return dict."""
    import re as _re
    if not slug:
        slug = _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO entities(name,slug,type,description,tax_id,color,"
            "parent_entity_id,display_name,metadata_json,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, slug, entity_type, description, tax_id, color,
             parent_entity_id, display_name or name, metadata_json, sort_order),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM entities WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def update_entity(entity_id, **kwargs) -> dict:
    """Override to return entity dict after update."""
    conn = get_connection()
    try:
        allowed = {"name", "slug", "description", "type", "tax_id", "color", "archived",
                   "metadata_json", "years", "parent_entity_id", "display_name", "sort_order"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE entities SET {sets} WHERE id=?", (*fields.values(), int(entity_id)))
            conn.commit()
        row = conn.execute("SELECT * FROM entities WHERE id=?", (int(entity_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_import_jobs(source_type=None, limit: int = 50) -> list:
    return [dict(r) for r in list_import_jobs(source_type=source_type, limit=limit)]


def create_import_job(source_type: str, entity_id=None, params: dict = None, config_json: str = None) -> int:
    """Override to accept params dict."""
    import json as _json
    eid = int(entity_id) if entity_id else None
    cfg = config_json or _json.dumps(params or {})
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO import_jobs(source_type,entity_id,status,config_json) VALUES(?,?,?,?)",
            (source_type, eid, "pending", cfg),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_import_job(job_id: int, **kwargs):
    """Override to accept message/progress kwargs."""
    mapping = {"message": "error_msg", "progress": "count_imported"}
    conn = get_connection()
    try:
        allowed = {
            "status", "started_at", "completed_at",
            "count_imported", "count_skipped", "error_msg",
        }
        fields = {}
        for k, v in kwargs.items():
            key = mapping.get(k, k)
            if key in allowed:
                fields[key] = v
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE import_jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        conn.commit()
    finally:
        conn.close()


def get_import_job(job_id: int) -> dict:
    """Override to return dict."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT j.*, e.name as entity_name FROM import_jobs j "
            "LEFT JOIN entities e ON e.id=j.entity_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_chat_sessions(user_id: int, entity_id=None) -> list:
    return [dict(r) for r in list_chat_sessions(user_id=user_id, entity_id=entity_id)]


def add_chat_message(session_id, role: str, content: str):
    append_chat_message(session_id=int(session_id), role=role, content=content)


def get_chat_session(session_id) -> dict:
    """Override to return dict with messages embedded."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT s.*, e.name as entity_name FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id WHERE s.id=?",
            (int(session_id),),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        msgs = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at",
            (int(session_id),),
        ).fetchall()
        d["messages"] = [dict(m) for m in msgs]
        return d
    finally:
        conn.close()


def create_chat_session(user_id: int, entity_id=None, year: str = None,
                        tax_year: str = None, title: str = "New Chat") -> dict:
    """Override to accept year kwarg and return dict."""
    ty = year or tax_year
    eid = int(entity_id) if entity_id else None
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO chat_sessions(user_id,entity_id,tax_year,title) VALUES(?,?,?,?)",
            (user_id, eid, ty, title),
        )
        conn.commit()
        row = conn.execute(
            "SELECT s.*, e.name as entity_name FROM chat_sessions s "
            "LEFT JOIN entities e ON e.id=s.entity_id WHERE s.id=?",
            (cur.lastrowid,),
        ).fetchone()
        d = dict(row) if row else {}
        d["messages"] = []
        return d
    finally:
        conn.close()


def get_settings() -> dict:
    return get_all_settings()


def save_settings(updates: dict):
    for key, value in updates.items():
        set_setting(key, str(value) if value is not None else "")


def get_activity_log(limit: int = 50) -> list:
    rows = get_recent_activity(limit=limit)
    return [dict(r) for r in rows]


def log_activity(msg: str, detail: str = "", user_id=None, entity_id=None):
    """Override: accept single message string (common call pattern)."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO activity_log(action,detail,user_id,entity_id) VALUES(?,?,?,?)",
            (msg, detail, user_id, entity_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


# ── Gmail dedup tracking ───────────────────────────────────────────────────────

def is_gmail_message_processed(message_id: str) -> bool:
    """O(1) check — returns True if Message-ID is already in the processed table."""
    if not message_id:
        return False
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM gmail_processed_messages WHERE message_id=? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_gmail_message(
    message_id: str,
    gmail_id: str,
    status: str,
    entity_slug: str = "",
    year: str = "",
    subject: str = "",
    sender: str = "",
):
    """Upsert a processed Gmail message record."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gmail_processed_messages"
            "(message_id, gmail_id, status, entity_slug, year, subject, sender)"
            " VALUES (?,?,?,?,?,?,?)",
            (message_id, gmail_id, status, entity_slug, year,
             subject[:200] if subject else "", sender[:200] if sender else ""),
        )
        conn.commit()
    finally:
        conn.close()


def gmail_processed_stats() -> dict:
    """Return counts by status for the gmail_processed_messages table."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM gmail_processed_messages GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


def delete_import_job(job_id: int) -> bool:
    """Delete an import job record and its logs. Returns True if deleted."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM import_job_logs WHERE job_id=?", (job_id,))
        cur = conn.execute("DELETE FROM import_jobs WHERE id=?", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def append_import_job_log(job_id: int, line: str) -> None:
    """Persist a single log line for a job."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO import_job_logs (job_id, line) VALUES (?, ?)",
            (job_id, line),
        )
        conn.commit()
    finally:
        conn.close()


def get_import_job_logs(job_id: int, offset: int = 0) -> tuple[list[str], int]:
    """Return (lines[offset:], total_count) from persistent log store."""
    conn = get_connection()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM import_job_logs WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT line FROM import_job_logs WHERE job_id=? ORDER BY id LIMIT -1 OFFSET ?",
            (job_id, offset),
        ).fetchall()
        return [r[0] for r in rows], total
    finally:
        conn.close()
