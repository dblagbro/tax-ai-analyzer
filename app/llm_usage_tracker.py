"""
LLM API usage tracker for Financial AI Analyzer.

Tracks all API calls to Anthropic and OpenAI in a SQLite database,
computes costs, and provides usage statistics.

Thread-safe via threading.RLock.
"""
import sqlite3
import os
import threading
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Database setup ────────────────────────────────────────────────────────────

_USAGE_DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "usage.db")
_lock = threading.RLock()
_initialized = False


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_USAGE_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(_USAGE_DB_PATH), exist_ok=True)
        conn = _get_connection()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT DEFAULT (datetime('now')),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                operation TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                success INTEGER DEFAULT 1,
                doc_id INTEGER,
                error_msg TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
            CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage(model);
            CREATE INDEX IF NOT EXISTS idx_llm_usage_operation ON llm_usage(operation);
        """)
        conn.commit()
        conn.close()
        _initialized = True


# ── Pricing table (USD per 1M tokens) ────────────────────────────────────────
# Format: "provider:model" -> (input_price, output_price)
_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "anthropic:claude-opus-4-6":             (15.00,  75.00),
    "anthropic:claude-sonnet-4-6":           ( 3.00,  15.00),
    "anthropic:claude-sonnet-4-5-20250929":  ( 3.00,  15.00),
    "anthropic:claude-haiku-4-5-20251001":   ( 0.80,   4.00),
    "anthropic:claude-3-5-sonnet-20241022":  ( 3.00,  15.00),
    "anthropic:claude-3-5-haiku-20241022":   ( 0.80,   4.00),
    "anthropic:claude-3-haiku-20240307":     ( 0.25,   1.25),
    "anthropic:claude-3-opus-20240229":      (15.00,  75.00),
    # OpenAI
    "openai:gpt-4o":                         ( 2.50,  10.00),
    "openai:gpt-4o-mini":                    ( 0.15,   0.60),
    "openai:gpt-4-turbo":                    (10.00,  30.00),
    "openai:gpt-4":                          (30.00,  60.00),
    "openai:gpt-3.5-turbo":                  ( 0.50,   1.50),
}

# Default fallback pricing if model not in table
_DEFAULT_INPUT_PRICE = 3.00
_DEFAULT_OUTPUT_PRICE = 15.00


def compute_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Compute USD cost for a given model and token counts."""
    key = f"{provider.lower()}:{model.lower()}"
    in_price, out_price = _PRICING.get(key, (_DEFAULT_INPUT_PRICE, _DEFAULT_OUTPUT_PRICE))
    cost = (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
    return round(cost, 8)


# ── Logging ───────────────────────────────────────────────────────────────────

def log_usage(
    provider: str,
    model: str,
    operation: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    success: bool = True,
    doc_id: Optional[int] = None,
    error_msg: str = "",
):
    """
    Record a single API call.

    Args:
        provider: "anthropic" or "openai"
        model: Model identifier string
        operation: Logical operation name (e.g. "analyze_document", "chat")
        input_tokens: Prompt/input token count
        output_tokens: Completion/output token count
        cost: Computed cost in USD
        success: Whether the call succeeded
        doc_id: Optional Paperless document ID being processed
        error_msg: Error message if success=False
    """
    _init_db()
    total = input_tokens + output_tokens
    with _lock:
        conn = _get_connection()
        try:
            conn.execute(
                """
                INSERT INTO llm_usage
                    (provider, model, operation, input_tokens, output_tokens,
                     total_tokens, cost_usd, success, doc_id, error_msg)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    provider, model, operation, input_tokens, output_tokens,
                    total, cost, 1 if success else 0, doc_id, error_msg or None,
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to log LLM usage: {e}")
        finally:
            conn.close()


# ── Statistics ────────────────────────────────────────────────────────────────

def get_stats(days: int = 30) -> dict:
    """
    Return usage statistics for the last N days.

    Returns dict with:
      total_calls, total_tokens, total_cost_usd,
      by_model: {model: {calls, input_tokens, output_tokens, cost_usd}},
      by_operation: {operation: {calls, tokens, cost_usd}},
      by_day: [{date, calls, tokens, cost_usd}],
      success_rate: float (0.0-1.0),
      period_days: int
    """
    _init_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        conn = _get_connection()
        try:
            # Overall totals
            row = conn.execute(
                """
                SELECT
                    COUNT(*) as calls,
                    COALESCE(SUM(input_tokens),0) as in_tok,
                    COALESCE(SUM(output_tokens),0) as out_tok,
                    COALESCE(SUM(total_tokens),0) as total_tok,
                    COALESCE(SUM(cost_usd),0) as cost,
                    COALESCE(SUM(success),0) as successes
                FROM llm_usage WHERE ts >= ?
                """,
                (cutoff,),
            ).fetchone()

            total_calls = row["calls"]
            total_tokens = row["total_tok"]
            total_cost = round(row["cost"], 6)
            success_rate = (row["successes"] / total_calls) if total_calls else 1.0

            # By model
            model_rows = conn.execute(
                """
                SELECT model, provider,
                    COUNT(*) as calls,
                    COALESCE(SUM(input_tokens),0) as in_tok,
                    COALESCE(SUM(output_tokens),0) as out_tok,
                    COALESCE(SUM(cost_usd),0) as cost
                FROM llm_usage WHERE ts >= ?
                GROUP BY model, provider
                ORDER BY cost DESC
                """,
                (cutoff,),
            ).fetchall()

            by_model = {}
            for r in model_rows:
                by_model[r["model"]] = {
                    "provider": r["provider"],
                    "calls": r["calls"],
                    "input_tokens": r["in_tok"],
                    "output_tokens": r["out_tok"],
                    "cost_usd": round(r["cost"], 6),
                }

            # By operation
            op_rows = conn.execute(
                """
                SELECT operation,
                    COUNT(*) as calls,
                    COALESCE(SUM(total_tokens),0) as tokens,
                    COALESCE(SUM(cost_usd),0) as cost
                FROM llm_usage WHERE ts >= ?
                GROUP BY operation
                ORDER BY cost DESC
                """,
                (cutoff,),
            ).fetchall()

            by_operation = {
                r["operation"]: {
                    "calls": r["calls"],
                    "tokens": r["tokens"],
                    "cost_usd": round(r["cost"], 6),
                }
                for r in op_rows
            }

            # By day
            day_rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) as day,
                    COUNT(*) as calls,
                    COALESCE(SUM(total_tokens),0) as tokens,
                    COALESCE(SUM(cost_usd),0) as cost
                FROM llm_usage WHERE ts >= ?
                GROUP BY day
                ORDER BY day ASC
                """,
                (cutoff,),
            ).fetchall()

            by_day = [
                {
                    "date": r["day"],
                    "calls": r["calls"],
                    "tokens": r["tokens"],
                    "cost_usd": round(r["cost"], 6),
                }
                for r in day_rows
            ]

            return {
                "period_days": days,
                "total_calls": total_calls,
                "total_tokens": total_tokens,
                "total_cost_usd": total_cost,
                "success_rate": round(success_rate, 4),
                "by_model": by_model,
                "by_operation": by_operation,
                "by_day": by_day,
            }
        finally:
            conn.close()


def get_recent_calls(limit: int = 20) -> list:
    """
    Return the most recent LLM API calls.

    Each item is a dict with:
      id, ts, provider, model, operation, input_tokens, output_tokens,
      total_tokens, cost_usd, success, doc_id, error_msg
    """
    _init_db()
    with _lock:
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM llm_usage ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_total_cost(days: int = None) -> float:
    """Return total cost in USD, optionally limited to the last N days."""
    _init_db()
    with _lock:
        conn = _get_connection()
        try:
            if days is not None:
                cutoff = (datetime.utcnow() - timedelta(days=days)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) as total FROM llm_usage WHERE ts >= ?",
                    (cutoff,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) as total FROM llm_usage"
                ).fetchone()
            return round(row["total"], 6)
        finally:
            conn.close()


def get_pricing_table() -> dict:
    """Return a copy of the pricing table for display."""
    result = {}
    for key, (in_price, out_price) in _PRICING.items():
        provider, model = key.split(":", 1)
        result[model] = {
            "provider": provider,
            "input_per_1m": in_price,
            "output_per_1m": out_price,
        }
    return result
