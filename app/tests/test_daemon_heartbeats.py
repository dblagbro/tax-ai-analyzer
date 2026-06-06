"""Tests for the Phase 14B daemon heartbeat (post-Phase-14 QA, MED-POST14-1).

Without these, /api/health/extended threading.enumerate() is process-local
and gives false-degraded reports when called from any non-daemon-bearing
process (e.g. test_client probes, monitoring scripts running ad-hoc).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def test_record_and_read_round_trip():
    from app.db import record_heartbeat, get_heartbeats

    record_heartbeat("test-daemon-roundtrip")
    hb = get_heartbeats()
    assert "test-daemon-roundtrip" in hb
    entry = hb["test-daemon-roundtrip"]
    assert "ts" in entry
    assert "seconds_since" in entry
    assert entry["seconds_since"] >= 0
    # Just-written heartbeat should be alive (well under any reasonable threshold)
    assert entry["alive"] is True

    # Cleanup
    from app.db.core import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM daemon_heartbeats WHERE name=?",
                 ("test-daemon-roundtrip",))
    conn.commit()
    conn.close()


def test_record_is_upsert_not_append():
    """Writing the same heartbeat twice updates the row in place — doesn't
    grow the table. Primary key on `name` enforces single-row semantics."""
    from app.db import record_heartbeat
    from app.db.core import get_connection

    for _ in range(5):
        record_heartbeat("test-upsert")
    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM daemon_heartbeats WHERE name=?",
            ("test-upsert",)
        ).fetchone()[0]
        assert count == 1, f"expected 1 row after 5 upserts, got {count}"
    finally:
        conn.execute("DELETE FROM daemon_heartbeats WHERE name=?",
                     ("test-upsert",))
        conn.commit()
        conn.close()


def test_stale_heartbeat_marked_dead():
    """A heartbeat older than its threshold should be marked alive=False."""
    from app.db import get_heartbeats
    from app.db.core import get_connection

    # Insert a heartbeat with a manually-old timestamp
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO daemon_heartbeats(name, ts) "
        "VALUES(?, datetime('now', '-2 hours'))",
        ("test-stale",)
    )
    conn.commit()
    conn.close()

    try:
        # threshold 60s — 2 hours is well past it
        hb = get_heartbeats(expected_intervals={"test-stale": 60})
        assert "test-stale" in hb
        assert hb["test-stale"]["alive"] is False
        assert hb["test-stale"]["seconds_since"] > 60
    finally:
        conn = get_connection()
        conn.execute("DELETE FROM daemon_heartbeats WHERE name=?", ("test-stale",))
        conn.commit()
        conn.close()


def test_fresh_heartbeat_under_threshold_alive():
    from app.db import record_heartbeat, get_heartbeats
    from app.db.core import get_connection

    record_heartbeat("test-fresh")
    try:
        hb = get_heartbeats(expected_intervals={"test-fresh": 3600})
        assert hb["test-fresh"]["alive"] is True
        assert hb["test-fresh"]["seconds_since"] < 60
    finally:
        conn = get_connection()
        conn.execute("DELETE FROM daemon_heartbeats WHERE name=?", ("test-fresh",))
        conn.commit()
        conn.close()


def test_default_interval_when_unknown_daemon():
    from app.db import record_heartbeat, get_heartbeats

    record_heartbeat("test-default-interval")
    try:
        # No interval mapping → default 600s
        hb = get_heartbeats()
        assert "test-default-interval" in hb
        assert hb["test-default-interval"]["threshold_seconds"] == 600
        assert hb["test-default-interval"]["alive"] is True
    finally:
        from app.db.core import get_connection
        conn = get_connection()
        conn.execute("DELETE FROM daemon_heartbeats WHERE name=?",
                     ("test-default-interval",))
        conn.commit()
        conn.close()


def test_health_endpoint_uses_heartbeats():
    """Integration: the /api/health/extended endpoint must report a daemon
    as present if its heartbeat is fresh, EVEN IF threading.enumerate() in
    the calling process doesn't list it. This is the whole point of the
    Phase 14B change."""
    from app.web_ui import app
    from app.db import record_heartbeat
    from app.db.core import get_connection

    record_heartbeat("analysis-daemon")  # pretend daemon just ran
    record_heartbeat("dedup-scheduler")

    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = "1"; s["_fresh"] = True

    try:
        r = client.get("/tax-ai-analyzer/api/health/extended")
        assert r.status_code == 200
        threads = r.get_json()["threads"]
        # Both should be marked present via the heartbeat path even though
        # this test_client process doesn't run them in threading.enumerate()
        assert threads["expected_present"]["analysis-daemon"] is True
        assert threads["expected_present"]["dedup-scheduler"] is True
        # The full heartbeats payload should also be exposed
        assert "heartbeats" in threads
        assert threads["heartbeats"]["analysis-daemon"]["alive"] is True
    finally:
        conn = get_connection()
        conn.execute("DELETE FROM daemon_heartbeats WHERE name IN ('analysis-daemon','dedup-scheduler')")
        conn.commit()
        conn.close()
