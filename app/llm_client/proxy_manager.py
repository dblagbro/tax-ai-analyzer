"""LLM proxy endpoint pool with in-memory circuit breaker.

Phase 12: ported from
``/home/dblagbro/docker/paperless-ai-analyzer/analyzer/llm/proxy_manager.py``
(canonical pattern from devingpt). Maintains an ordered list of llm-proxy
endpoints from the ``llm_proxy_endpoints`` DB table and applies a
3-failure / 60-second cooldown per endpoint.

v1 (Node.js llm-proxy-manager):  ``Authorization: Bearer <key>``
v2 (Python/FastAPI llm-proxy2):  ``x-api-key: <key>``
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from openai import OpenAI

from app.db import llm_proxy_list_endpoints

logger = logging.getLogger(__name__)

# Circuit breaker: 3 failures within the window → 60 s cooldown
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECS = 60

_lock = threading.Lock()
_state: dict[str, dict] = {}  # {endpoint_id: {"failures": int, "cooldown_until": float}}


def _ep_state(eid: str) -> dict:
    if eid not in _state:
        _state[eid] = {"failures": 0, "cooldown_until": 0.0}
    return _state[eid]


def mark_success(endpoint_id: str) -> None:
    """Reset circuit-breaker state after a successful call."""
    with _lock:
        st = _ep_state(endpoint_id)
        st["failures"] = 0
        st["cooldown_until"] = 0.0


def mark_failure(endpoint_id: str) -> None:
    """Record a failure. Trips breaker after _FAILURE_THRESHOLD failures."""
    with _lock:
        st = _ep_state(endpoint_id)
        st["failures"] += 1
        if st["failures"] >= _FAILURE_THRESHOLD:
            st["cooldown_until"] = time.time() + _COOLDOWN_SECS
            logger.warning(
                f"[llm-proxy] endpoint {endpoint_id} tripped — "
                f"cooling down {_COOLDOWN_SECS}s"
            )


def _is_healthy(endpoint_id: str) -> bool:
    with _lock:
        st = _ep_state(endpoint_id)
        if st["cooldown_until"] and time.time() < st["cooldown_until"]:
            return False
        if st["cooldown_until"] and time.time() >= st["cooldown_until"]:
            # Cooldown expired: reset and grant one retry
            st["failures"] = 0
            st["cooldown_until"] = 0.0
    return True


def get_breaker_status(endpoint_id: str) -> dict:
    """Return circuit-breaker state for admin UI display."""
    with _lock:
        st = _ep_state(endpoint_id).copy()
    now = time.time()
    cooldown_remaining = max(0, st["cooldown_until"] - now) if st["cooldown_until"] else 0
    return {
        "failures": st["failures"],
        "tripped": cooldown_remaining > 0,
        "cooldown_remaining_sec": int(cooldown_remaining),
    }


def get_endpoints() -> list[dict]:
    """Return all enabled endpoints ordered by priority ASC."""
    try:
        return llm_proxy_list_endpoints()
    except Exception as e:
        logger.error(f"[llm-proxy] failed to load endpoints: {e}")
        return []


def get_healthy_endpoints() -> list[dict]:
    """Return enabled endpoints NOT currently in breaker cooldown."""
    return [ep for ep in get_endpoints() if _is_healthy(ep["id"])]


def build_client(endpoint: dict) -> OpenAI:
    """Build an OpenAI-compatible client for a proxy endpoint.

    v1 uses Bearer; v2 uses x-api-key header.
    """
    url = endpoint["url"].rstrip("/")
    key = endpoint["api_key"]
    version = int(endpoint.get("version", 1))
    if version == 2:
        return OpenAI(
            base_url=url,
            api_key="not-used",
            default_headers={"x-api-key": key},
        )
    return OpenAI(base_url=url, api_key=key)


def get_chat_client() -> tuple[Optional[OpenAI], Optional[str]]:
    """Return (client, endpoint_id) for the highest-priority healthy endpoint,
    or (None, None) if no healthy endpoint exists."""
    endpoints = get_healthy_endpoints()
    if endpoints:
        ep = endpoints[0]
        return build_client(ep), ep["id"]
    return None, None


def get_all_clients() -> list[tuple[OpenAI, str]]:
    """Return (client, endpoint_id) for ALL healthy endpoints, priority ASC.

    Used by the retry loop in ``proxy_call.call_llm()``: iterate, try each,
    on connection failure ``mark_failure(eid)`` and try the next.
    """
    return [(build_client(ep), ep["id"]) for ep in get_healthy_endpoints()]
