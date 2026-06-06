"""Daemon heartbeat helpers (Phase 14B / post-Phase-14 QA).

Background threads (analysis-daemon, dedup-scheduler) write a heartbeat row
each iteration so the health endpoint can verify they're alive WITHOUT
relying on `threading.enumerate()` of the current process — which gives a
false-negative when called from any process that isn't the daemon-bearing
main process (e.g. via Flask test_client during ad-hoc probes).

Schema:
    CREATE TABLE daemon_heartbeats (name TEXT PRIMARY KEY, ts TEXT NOT NULL);

Usage:
    # In each daemon's main loop, after a work cycle:
    db.record_heartbeat("analysis-daemon")

    # In the health endpoint:
    hb = db.get_heartbeats()  # → {"analysis-daemon": {"ts": "...", "seconds_since": 23, "alive": True}, ...}

A daemon is considered alive if its heartbeat is no older than its expected
poll interval × 2 (slack for slow cycles). Each caller passes the expected
interval; default 600 seconds (10 min) for unknown daemons.

Failures here MUST NOT kill the daemon thread — every call is wrapped in
a try/except in the daemon loop body.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.core import get_connection

logger = logging.getLogger(__name__)


def record_heartbeat(name: str) -> None:
    """Upsert a heartbeat row for ``name`` with the current UTC timestamp.

    INSERT OR REPLACE is the idempotent semantic: each daemon has at most
    one row (PK on name). Updates the timestamp without growing the table.
    """
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daemon_heartbeats(name, ts) "
            "VALUES(?, datetime('now'))",
            (name,),
        )
        conn.commit()
    finally:
        conn.close()


def get_heartbeats(
    expected_intervals: Optional[dict] = None,
    default_interval: int = 600,
) -> dict:
    """Return all heartbeat rows with `seconds_since` and an alive verdict.

    Args:
      expected_intervals: optional dict {name: max_seconds_between_beats}.
        Used to compute `alive`. Pass {"dedup-scheduler": 86400 * 2} for
        once-a-day daemons to avoid false-dead reports.
      default_interval: used for any daemon not in expected_intervals.

    Returns:
      {
        "analysis-daemon": {
          "ts": "2026-06-05 23:45:01",
          "seconds_since": 12,
          "alive": True,
        },
        ...
      }

    Returns empty dict if the table doesn't exist (pre-migration) — caller
    should treat that as "unknown" rather than "all dead."
    """
    expected_intervals = expected_intervals or {}
    conn = get_connection()
    try:
        try:
            rows = conn.execute(
                "SELECT name, ts FROM daemon_heartbeats"
            ).fetchall()
        except Exception as e:
            # Pre-migration DBs: table doesn't exist
            logger.debug(f"daemon_heartbeats table missing: {e}")
            return {}
    finally:
        conn.close()

    out = {}
    now = datetime.utcnow()
    for row in rows:
        name, ts_str = row["name"], row["ts"]
        try:
            # SQLite datetime('now') format: "YYYY-MM-DD HH:MM:SS"
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            secs = int((now - ts).total_seconds())
        except Exception:
            secs = -1  # unparseable — mark unknown
        threshold = expected_intervals.get(name, default_interval)
        alive = (secs >= 0) and (secs <= threshold)
        out[name] = {
            "ts": ts_str,
            "seconds_since": secs,
            "alive": alive,
            "threshold_seconds": threshold,
        }
    return out
