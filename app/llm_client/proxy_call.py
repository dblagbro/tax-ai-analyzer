"""High-level send-through-proxy-chain helpers (Phase 12).

Ports the paperless-ai-analyzer pattern (`proxy_call.call_llm`) and adds an
Anthropic-native variant for callers that need prompt caching.

Two entry points:

  call_chat(operation, system, messages, ...)
      OpenAI-shaped chat completion. Walks the proxy pool with LLM-Hint,
      returns the assistant string + usage. Used by analyze_document /
      classify_entity / extract_financial_data / generate_summary / chat.

  call_anthropic_messages(operation, model, system, messages, ...)
      Anthropic-shaped /v1/messages call. Walks the same pool but uses
      the native Anthropic SDK so cache_control blocks and prompt caching
      work end-to-end. Used by bank_codegen.

Both raise NoProxyAvailable when the entire pool is exhausted; the
caller is expected to fall through to a direct vendor SDK call (the
existing _call_anthropic / _call_openai paths in client.py do this).

Per the 2026-04-30 ops directive:
  - We do NOT do cross-vendor failover here. The proxy does that internally.
  - We DO walk multiple proxy endpoints in priority order.
  - We DO fall back to direct vendor SDKs once all proxies are exhausted.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.llm_client import proxy_manager
from app.llm_client.lmrh import get_hint

logger = logging.getLogger(__name__)


class NoProxyAvailable(Exception):
    """Raised when every healthy proxy endpoint failed (or the pool is empty).
    Caller should fall back to a direct-vendor SDK call."""


# Connection-class errors that mean "this endpoint is not reachable" — try
# the next one in the chain rather than treating it as a hard failure.
_CONN_ERR_TYPES: tuple = ()
try:
    import httpx
    _CONN_ERR_TYPES = _CONN_ERR_TYPES + (
        httpx.ConnectError, httpx.ConnectTimeout,
        httpx.ReadTimeout, httpx.RemoteProtocolError,
    )
except ImportError:
    pass
try:
    import anthropic
    _CONN_ERR_TYPES = _CONN_ERR_TYPES + (
        anthropic.APIConnectionError, anthropic.APITimeoutError,
    )
except ImportError:
    pass


# ── OpenAI-shape chat (analyze, classify, extract, summarize, chat) ─────────

def call_chat(
    operation: str,
    *,
    system: str,
    messages: list,
    max_tokens: int = 2048,
    temperature: Optional[float] = 0.1,
    model: Optional[str] = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Send an OpenAI-shaped chat.completions call through the proxy chain.

    `operation` selects the LMRH hint via lmrh_hints.get_hint().
    `model` defaults to "auto" — llm-proxy2 uses LMRH hints to pick.

    Returns:
      {"content": <text>, "model": <picked>, "endpoint_id": <id>,
       "in_tokens": int, "out_tokens": int}

    Raises NoProxyAvailable on whole-pool failure.
    """
    hint = get_hint(operation)
    send_model = model or "auto"

    chain = proxy_manager.get_all_clients()
    if not chain:
        raise NoProxyAvailable("no healthy proxy endpoints available")

    # Build OpenAI-shape messages: prepend system as a role:'system' entry
    oai_messages = [{"role": "system", "content": system}] + messages

    last_err = None
    # Per llm-proxy2 ops 2026-04-30: cascade is read from LLM-Hint per the
    # LMRH spec; X-Cot-Cascade is a v1 DevinGPT artifact that v2 ignores.
    # Sending it was harmless but adds nothing — dropped.
    extra_headers = {"LLM-Hint": hint} if hint else {}

    for client, eid in chain:
        t0 = time.time()
        try:
            kwargs: dict[str, Any] = {
                "model": send_model,
                "messages": oai_messages,
                "max_tokens": max_tokens,
                "extra_headers": dict(extra_headers),
                "timeout": timeout,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            resp = client.chat.completions.create(**kwargs)
            choice = resp.choices[0] if resp.choices else None
            content = (choice.message.content or "") if choice and choice.message else ""
            usage = getattr(resp, "usage", None)
            in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
            out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
            model_used = getattr(resp, "model", send_model) or send_model

            proxy_manager.mark_success(eid)
            logger.info(
                f"[proxy] ✓ {operation} via ep={eid} model={model_used} "
                f"in={in_tok} out={out_tok} {time.time()-t0:.2f}s"
            )
            return {
                "content": content, "model": model_used, "endpoint_id": eid,
                "in_tokens": in_tok, "out_tokens": out_tok,
            }
        except _CONN_ERR_TYPES as e:
            logger.warning(f"[proxy] connection error on ep={eid}: {e!r}")
            proxy_manager.mark_failure(eid)
            last_err = e
            continue
        except Exception as e:
            msg = str(e)[:200]
            logger.warning(f"[proxy] call failed on ep={eid}: {msg}")
            proxy_manager.mark_failure(eid)
            last_err = e
            continue

    raise NoProxyAvailable(
        f"all {len(chain)} proxy endpoints failed for {operation}; "
        f"last error: {last_err!r}"
    )


# ── Anthropic-shape /v1/messages (bank codegen + future cache-aware calls) ──

def call_anthropic_messages(
    operation: str,
    *,
    model: str,
    system,                      # str OR list[dict] (with cache_control blocks)
    messages: list,
    max_tokens: int = 4096,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Send a native Anthropic Messages call through the proxy chain.

    Preserves Anthropic-only features (prompt caching with cache_control,
    cache_creation_input_tokens / cache_read_input_tokens in usage).

    Returns:
      {"response": <anthropic Message>, "endpoint_id": <id>, "model": str,
       "in_tokens": int, "out_tokens": int,
       "cache_creation": int, "cache_read": int}

    Raises NoProxyAvailable on whole-pool failure.
    """
    chain = proxy_manager.get_all_anthropic_clients(operation)
    if not chain:
        raise NoProxyAvailable("no healthy proxy endpoints available")

    last_err = None
    for client, eid in chain:
        t0 = time.time()
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
                "timeout": timeout,
            }
            if system is not None:
                kwargs["system"] = system
            resp = client.messages.create(**kwargs)

            usage = resp.usage
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            model_used = getattr(resp, "model", model) or model

            proxy_manager.mark_success(eid)
            logger.info(
                f"[proxy/anthropic] ✓ {operation} via ep={eid} model={model_used} "
                f"in={in_tok} out={out_tok} cache+={cache_create} cache↩={cache_read} "
                f"{time.time()-t0:.2f}s"
            )
            return {
                "response": resp, "endpoint_id": eid, "model": model_used,
                "in_tokens": in_tok, "out_tokens": out_tok,
                "cache_creation": cache_create, "cache_read": cache_read,
            }
        except _CONN_ERR_TYPES as e:
            logger.warning(f"[proxy/anthropic] connection error on ep={eid}: {e!r}")
            proxy_manager.mark_failure(eid)
            last_err = e
            continue
        except Exception as e:
            msg = str(e)[:200]
            logger.warning(f"[proxy/anthropic] call failed on ep={eid}: {msg}")
            proxy_manager.mark_failure(eid)
            last_err = e
            continue

    raise NoProxyAvailable(
        f"all {len(chain)} proxy endpoints failed for anthropic-shape "
        f"{operation}; last error: {last_err!r}"
    )


# ── Streaming Anthropic client (chat, tax_review, helpers) ───────────────────

def get_streaming_anthropic_client(operation: str = "chat"):
    """Return a tuple of (anthropic_client, endpoint_id) configured to stream
    through the highest-priority healthy proxy endpoint.

    Streaming responses can't be transparently failed-over mid-stream — by
    the time we know the connection died, half the answer has been delivered
    to the user. So we pick ONE endpoint up front. If it fails to connect,
    we mark it failed in the breaker and the caller can fall through to
    direct vendor SDK (or retry — but on a fresh stream, not mid-stream).

    The returned client carries the LMRH hint as a default header, so the
    caller's `client.messages.stream(...)` call automatically includes it.

    Raises NoProxyAvailable if the pool is empty.
    """
    chain = proxy_manager.get_all_anthropic_clients(operation)
    if not chain:
        raise NoProxyAvailable("no healthy proxy endpoints available")
    # First in priority order; the chain is already filtered by breaker state.
    return chain[0]


def mark_endpoint_failure(endpoint_id: str) -> None:
    """Mark an endpoint failed in the breaker. Use this from a streaming
    caller when the stream errored — we can't retry mid-stream, but we can
    take this endpoint out of rotation for the next call."""
    proxy_manager.mark_failure(endpoint_id)


def mark_endpoint_success(endpoint_id: str) -> None:
    """Reset breaker state after a successful streaming call."""
    proxy_manager.mark_success(endpoint_id)
