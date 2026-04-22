"""Shared mutable state for in-process import jobs and chat streams.

All globals here are intentionally module-level singletons — they survive
across requests in the same process and are the coordination point between
the request that starts a job and the request that polls for logs/status.
"""
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# In-memory log store per job_id — capped at 2000 lines
_job_logs: dict = {}
_job_logs_lock = threading.Lock()

# Stop signals for active chat streams: session_id → threading.Event
_chat_stop_events: dict = {}
_chat_stop_lock = threading.Lock()

# Stop signals for import jobs: job_id → threading.Event
_job_stop_events: dict = {}
_job_stop_lock = threading.Lock()

# Set to True while an analysis pass is running (prevents concurrent runs)
_is_analyzing: bool = False


def append_job_log(job_id: int, msg: str) -> None:
    from app import db
    ts = datetime.utcnow().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    logger.info("job#%d: %s", job_id, msg)
    with _job_logs_lock:
        if job_id not in _job_logs:
            _job_logs[job_id] = []
        _job_logs[job_id].append(entry)
        if len(_job_logs[job_id]) > 2000:
            _job_logs[job_id] = _job_logs[job_id][-2000:]
    try:
        db.append_import_job_log(job_id, entry)
    except Exception:
        pass
