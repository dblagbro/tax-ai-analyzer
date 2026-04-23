"""Shared in-memory MFA code registry for all bank importers.

Each bank's Playwright importer enters a wait state when it hits an MFA
prompt, polling this registry until the user submits a code via the
/api/import/<bank>/mfa endpoint.

job_id → {"code": str | None, "expires": float}
"""
import time
from typing import Optional, Callable

_registry: dict[int, dict] = {}


def set_code(job_id: int, code: str) -> None:
    _registry[job_id] = {"code": code.strip(), "expires": time.time() + 300}


def clear(job_id: int) -> None:
    _registry.pop(job_id, None)


def wait_for_code(job_id: int, log: Callable, timeout: int = 300) -> Optional[str]:
    """Block until a code is submitted or timeout (seconds). Returns code or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _registry.get(job_id)
        if entry and entry.get("code") and time.time() < entry["expires"]:
            code = entry["code"]
            _registry.pop(job_id, None)
            return code
        time.sleep(2)
    log("MFA timeout — no code submitted within the time limit.")
    return None
