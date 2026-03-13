"""
Per-entity document processing state.

Tracks which Paperless document IDs have been analyzed for each entity,
stored as JSON files at /app/data/state_{entity_slug}.json.

This exists alongside the SQLite analyzed_documents table — the state files
provide a fast, lock-free way to check processed IDs without hitting the DB
on every polling cycle.
"""
import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _state_path(entity_slug: str = "default") -> str:
    data_dir = os.environ.get("DATA_DIR", "/app/data")
    return os.path.join(data_dir, f"state_{entity_slug}.json")


def _load(entity_slug: str = "default") -> dict:
    path = _state_path(entity_slug)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load state file {path}: {e}")
    return {"processed_ids": [], "last_seen_id": 0, "last_run": None, "error_ids": []}


def _save(state: dict, entity_slug: str = "default"):
    path = _state_path(entity_slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)  # Atomic replace to avoid partial writes


# ── Read operations ───────────────────────────────────────────────────────────

def is_processed(doc_id: int, entity_slug: str = "default") -> bool:
    """Return True if this doc_id has been successfully analyzed for entity."""
    return doc_id in _load(entity_slug).get("processed_ids", [])


def is_error(doc_id: int, entity_slug: str = "default") -> bool:
    """Return True if this doc_id previously errored during analysis."""
    return doc_id in _load(entity_slug).get("error_ids", [])


def get_last_seen_id(entity_slug: str = "default") -> int:
    """Return the highest doc_id seen for this entity (for incremental polling)."""
    return _load(entity_slug).get("last_seen_id", 0)


def get_processed_ids(entity_slug: str = "default") -> list:
    """Return list of all processed doc_ids for this entity."""
    return list(_load(entity_slug).get("processed_ids", []))


def get_error_ids(entity_slug: str = "default") -> list:
    """Return list of doc_ids that encountered errors during analysis."""
    return list(_load(entity_slug).get("error_ids", []))


def get_state_info(entity_slug: str = "default") -> dict:
    """Return full state dict including counts and timestamps."""
    state = _load(entity_slug)
    return {
        "entity_slug": entity_slug,
        "processed_count": len(state.get("processed_ids", [])),
        "error_count": len(state.get("error_ids", [])),
        "last_seen_id": state.get("last_seen_id", 0),
        "last_run": state.get("last_run"),
        "state_file": _state_path(entity_slug),
    }


# ── Write operations ──────────────────────────────────────────────────────────

def mark_processed(doc_id: int, entity_slug: str = "default"):
    """
    Mark a document as successfully processed.
    Also removes it from error_ids if it was previously failing.
    """
    state = _load(entity_slug)
    processed = state.setdefault("processed_ids", [])
    error_ids = state.setdefault("error_ids", [])

    if doc_id not in processed:
        processed.append(doc_id)

    # Remove from error list if it was previously erroring
    if doc_id in error_ids:
        error_ids.remove(doc_id)

    state["last_seen_id"] = max(state.get("last_seen_id", 0), doc_id)
    state["last_run"] = datetime.utcnow().isoformat()
    _save(state, entity_slug)


def mark_error(doc_id: int, entity_slug: str = "default"):
    """
    Mark a document as having errored during analysis.
    It will NOT be retried unless reset_errors() is called.
    """
    state = _load(entity_slug)
    error_ids = state.setdefault("error_ids", [])

    if doc_id not in error_ids:
        error_ids.append(doc_id)

    state["last_seen_id"] = max(state.get("last_seen_id", 0), doc_id)
    _save(state, entity_slug)


def update_last_seen(doc_id: int, entity_slug: str = "default"):
    """Update the last_seen_id without marking the doc as processed."""
    state = _load(entity_slug)
    state["last_seen_id"] = max(state.get("last_seen_id", 0), doc_id)
    state["last_run"] = datetime.utcnow().isoformat()
    _save(state, entity_slug)


# ── Reset operations ──────────────────────────────────────────────────────────

def reset_entity(entity_slug: str):
    """
    Completely reset the state for an entity (clears all processed/error IDs).
    The next polling cycle will reprocess all documents.
    """
    path = _state_path(entity_slug)
    if os.path.exists(path):
        os.remove(path)
    logger.info(f"State reset for entity: {entity_slug}")


def reset_errors(entity_slug: str = "default"):
    """Clear the error_ids list so errored documents will be retried."""
    state = _load(entity_slug)
    state["error_ids"] = []
    _save(state, entity_slug)
    logger.info(f"Error IDs cleared for entity: {entity_slug}")


def unmark_document(doc_id: int, entity_slug: str = "default"):
    """
    Remove a single doc_id from both processed and error lists.
    Useful for forcing a specific document to be reprocessed.
    """
    state = _load(entity_slug)
    processed = state.get("processed_ids", [])
    error_ids = state.get("error_ids", [])
    if doc_id in processed:
        processed.remove(doc_id)
    if doc_id in error_ids:
        error_ids.remove(doc_id)
    state["processed_ids"] = processed
    state["error_ids"] = error_ids
    _save(state, entity_slug)
    logger.info(f"Removed doc {doc_id} from state for entity: {entity_slug}")


# ── Legacy compat (old state.py used analyzed_ids / mark_analyzed) ────────────

def is_analyzed(doc_id: int, entity_slug: str = "default") -> bool:
    """Alias for is_processed — backwards compatibility."""
    return is_processed(doc_id, entity_slug)


def mark_analyzed(doc_id: int, entity_slug: str = "default"):
    """Alias for mark_processed — backwards compatibility."""
    mark_processed(doc_id, entity_slug)
