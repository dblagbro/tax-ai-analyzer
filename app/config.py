"""Central configuration — environment variables + runtime database settings."""
import os
import logging

logger = logging.getLogger(__name__)

# ── Core paths ────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
EXPORT_PATH = os.environ.get("EXPORT_PATH", "/app/export")
CONSUME_PATH = os.environ.get("CONSUME_PATH", "/consume")
PROFILES_DIR = os.environ.get("PROFILES_DIR", "/app/profiles")

# ── Paperless ─────────────────────────────────────────────────────────────────
PAPERLESS_API_BASE_URL = os.environ.get("PAPERLESS_API_BASE_URL", "http://tax-paperless-web:8000")
PAPERLESS_API_TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "")
# Public-facing URL for document preview links (nginx proxy path)
PAPERLESS_WEB_URL = os.environ.get("PAPERLESS_WEB_URL", "/tax-paperless")

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# ── Elasticsearch (optional) ──────────────────────────────────────────────────
ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
ELASTICSEARCH_PASSWORD = os.environ.get("ELASTICSEARCH_PASSWORD", "")

# ── Web ───────────────────────────────────────────────────────────────────────
WEB_PORT = int(os.environ.get("WEB_PORT", "8012"))
URL_PREFIX = os.environ.get("URL_PREFIX", "/tax-ai-analyzer").rstrip("/")
FLASK_SECRET_KEY_FILE = os.path.join(DATA_DIR, ".flask_secret_key")

# ── Gmail (merged from gmail-collector) ───────────────────────────────────────
GMAIL_TOKEN_FILE = os.path.join(DATA_DIR, "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.path.join(DATA_DIR, "gmail_credentials.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_YEARS = [
    y.strip()
    for y in os.environ.get("GMAIL_YEARS", "2022,2023,2024,2025").split(",")
]
GMAIL_SEARCH_TERMS = os.environ.get(
    "GMAIL_SEARCH_TERMS",
    "receipt invoice statement payment billing 1099 W-2 tax order",
).split()
ACCOUNTANT_EMAIL_DOMAIN = os.environ.get("ACCOUNTANT_EMAIL_DOMAIN", "iactaxes.com")

# ── Default entities ──────────────────────────────────────────────────────────
# Root entity first — DBAs reference it by slug via parent_slug key (resolved at boot)
DEFAULT_ENTITIES = [
    {
        "name": "Devin Blagbrough",
        "slug": "devin_blagbrough",
        "type": "person",
        "display_name": "Devin Blagbrough (Personal)",
        "color": "#1a3c5e",
        "sort_order": 0,
    },
    {
        "name": "Personal",
        "slug": "personal",
        "type": "dba",
        "display_name": "Personal (DBA of Devin Blagbrough)",
        "parent_slug": "devin_blagbrough",
        "color": "#1a3c5e",
        "sort_order": 10,
    },
    {
        "name": "VoIPGuru",
        "slug": "voipguru",
        "type": "dba",
        "display_name": "VoIPGuru (DBA of Devin Blagbrough)",
        "parent_slug": "devin_blagbrough",
        "color": "#0d6efd",
        "sort_order": 20,
    },
    {
        "name": "Martinfeld Ranch",
        "slug": "martinfeld_ranch",
        "type": "dba",
        "display_name": "Martinfeld Ranch (DBA of Devin Blagbrough)",
        "parent_slug": "devin_blagbrough",
        "color": "#198754",
        "sort_order": 30,
    },
]
DEFAULT_TAX_YEARS = [str(y) for y in range(2015, 2027)]

# ── Polling ───────────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

# ── Legacy compat (kept so old imports don't break) ──────────────────────────
ENTITIES = [e["slug"] for e in DEFAULT_ENTITIES]
TAX_YEARS = DEFAULT_TAX_YEARS
STATE_FILE = os.path.join(DATA_DIR, "tax_analyzer_state.json")


def get_flask_secret_key() -> str:
    """Return persistent Flask secret key, generating if needed."""
    import secrets

    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(FLASK_SECRET_KEY_FILE):
        with open(FLASK_SECRET_KEY_FILE) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(FLASK_SECRET_KEY_FILE, "w") as f:
        f.write(key)
    return key


def get_db_setting(db_conn, key: str, default: str = "") -> str:
    """Read a runtime setting from the database."""
    try:
        row = db_conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_db_setting(db_conn, key: str, value: str):
    """Write a runtime setting to the database."""
    db_conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value),
    )
    db_conn.commit()
