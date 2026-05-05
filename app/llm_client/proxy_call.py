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


class CrossFamilySubstitution(Exception):
    """Raised when the proxy reports `chosen-because=cross-family-fallback` on
    a call that opted into strict-provider mode (correctness-critical tasks).

    Per llm-proxy2 v3.0.46: gpt-4o-family requests may get silently substituted
    to gpt-5.5-via-Codex subscription. We don't currently route gpt-4o, but
    AI Analyzer's v3.9.19 hit a related case — defensive check recommended.
    Tax-review and bank-codegen default to strict because correctness >
    availability for those operations. Override with strict_provider=False
    or by passing provider-hint=...;require in the LMRH hint.
    """


# Tasks where correctness matters more than availability — never silently
# accept a cross-family upstream substitution. Caller can override via
# the strict_provider= kwarg on call_chat / call_anthropic_messages.
_STRICT_PROVIDER_TASKS = {"tax-review", "codegen"}


def _detect_substitution(operation: str, headers: dict) -> tuple[bool, str]:
    """Return (was_substituted, capability_string).

    Substitution is signaled by `chosen-because=cross-family-fallback` in the
    LLM-Capability response header. Caller decides whether to raise based on
    operation policy.
    """
    if not headers:
        return False, ""
    lc = {k.lower(): v for k, v in headers.items()}
    cap = lc.get("llm-capability", "") or ""
    return ("chosen-because=cross-family-fallback" in cap), cap


def _extract_cost_class(headers: dict) -> str:
    """Pull the cost_class dim from the LLM-Capability response header.

    llm-proxy2 v3.0.50+: subscription-tier providers (claude-oauth, codex-
    oauth) emit cost_class=subscription so callers can distinguish quota-
    based zero-cost calls from paid pay-per-call. Pay-per-call upstreams
    emit cost_class=paid. Returns "" if absent (pre-v3.0.50 proxy or
    direct-vendor SDK path).

    Capability header is a comma-separated list of `key=value` pairs per
    LMRH 1.0 §6. Be tolerant of arbitrary whitespace between items.
    """
    if not headers:
        return ""
    lc = {k.lower(): v for k, v in headers.items()}
    cap = lc.get("llm-capability", "") or ""
    if "cost_class=" not in cap and "cost-class=" not in cap:
        return ""
    # Walk comma-separated items; tolerate either underscore or hyphen
    for raw in cap.split(","):
        item = raw.strip()
        for prefix in ("cost_class=", "cost-class="):
            if item.startswith(prefix):
                value = item[len(prefix):].strip()
                # Stop at any structured-field parameter separator
                for sep in (";", " "):
                    if sep in value:
                        value = value.split(sep, 1)[0]
                return value.strip()
    return ""


def _log_lmrh_diagnostics(operation: str, headers: dict) -> None:
    """Surface llm-proxy2 response headers as log lines for ops visibility.

    Headers we care about (per the v3.0.25 spec):
      - LLM-Capability: which provider+model the proxy actually picked, plus
        why (chosen-because=score|hard-constraint|fallback|cheapest|p2c) and
        any unmet dims.
      - X-LMRH-Warnings: the proxy received unknown/non-canonical dim names.
        Surfaced as a WARNING so we catch dim-name drift before it silently
        becomes a no-op routing hint.
      - LLM-Hint-Set: echo of the parsed input — diagnostic.

    Header lookup is case-insensitive (httpx returns mixed case).
    """
    if not headers:
        return
    lc = {k.lower(): v for k, v in headers.items()}
    cap = lc.get("llm-capability", "")
    warn = lc.get("x-lmrh-warnings", "")
    if cap:
        if "chosen-because=cross-family-fallback" in cap:
            # v3.0.46 paid-plan substitution. Loud at WARNING so it's visible
            # in any log review even if the operation didn't opt into strict.
            logger.warning(
                f"[proxy/{operation}] CROSS-FAMILY MODEL SUBSTITUTION by proxy: {cap} — "
                f"if this disrespects correctness needs, send "
                f"`provider-hint=<vendor>;require` in the LMRH hint to fail-fast (503)"
            )
        else:
            logger.info(f"[proxy/{operation}] capability: {cap}")
    if warn:
        logger.warning(
            f"[proxy/{operation}] LMRH warnings from proxy: {warn} — "
            f"check that our dim names match the canonical set "
            f"(see /lmrh.md or /lmrh/register)"
        )


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
    strict_provider: Optional[bool] = None,
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
            # Use with_raw_response so we can read X-LMRH-Warnings /
            # LLM-Capability headers for diagnostics. Falls back to plain
            # create() for older SDKs / mocked test clients.
            resp_headers = {}
            try:
                raw = client.chat.completions.with_raw_response.create(**kwargs)
                resp = raw.parse()
                resp_headers = dict(raw.headers.items()) if hasattr(raw, "headers") else {}
            except AttributeError:
                resp = client.chat.completions.create(**kwargs)
            choice = resp.choices[0] if resp.choices else None
            content = (choice.message.content or "") if choice and choice.message else ""
            usage = getattr(resp, "usage", None)
            in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
            out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
            model_used = getattr(resp, "model", send_model) or send_model

            _log_lmrh_diagnostics(operation, resp_headers)
            # Strict-provider check: refuse cross-family-fallback responses
            # for correctness-critical tasks (or any caller that opts in).
            if strict_provider is None:
                strict_provider = operation in _STRICT_PROVIDER_TASKS
            if strict_provider:
                substituted, cap = _detect_substitution(operation, resp_headers)
                if substituted:
                    proxy_manager.mark_success(eid)  # endpoint itself is fine
                    raise CrossFamilySubstitution(
                        f"{operation}: proxy substituted across model families "
                        f"({cap}); refusing for correctness. To accept, pass "
                        f"strict_provider=False or omit the operation from "
                        f"_STRICT_PROVIDER_TASKS."
                    )
            proxy_manager.mark_success(eid)
            cost_class = _extract_cost_class(resp_headers)
            logger.info(
                f"[proxy] ✓ {operation} via ep={eid} model={model_used} "
                f"in={in_tok} out={out_tok} cost_class={cost_class or '?'} "
                f"{time.time()-t0:.2f}s"
            )
            return {
                "content": content, "model": model_used, "endpoint_id": eid,
                "in_tokens": in_tok, "out_tokens": out_tok,
                "cost_class": cost_class,
            }
        except CrossFamilySubstitution:
            # Policy failure, not transport — bubble up immediately. Trying
            # the next endpoint won't help; the proxy made the same decision.
            raise
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
    strict_provider: Optional[bool] = None,
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
            # Capture response headers for LMRH diagnostics. Anthropic SDK's
            # with_raw_response wrapper exposes them.
            resp_headers = {}
            try:
                raw = client.messages.with_raw_response.create(**kwargs)
                resp = raw.parse()
                resp_headers = dict(raw.headers.items()) if hasattr(raw, "headers") else {}
            except AttributeError:
                resp = client.messages.create(**kwargs)

            usage = resp.usage
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            # Cache token reporting note: claude-oauth (Pro Max OAuth path)
            # returns 0 for cache_creation_input_tokens and cache_read_input_tokens
            # by design — savings are recorded server-side in the proxy's
            # event_meta but not surfaced to API callers (per llm-proxy2 ops
            # 2026-05-01 + paperless-ai-analyzer's parallel finding). With our
            # ;require comma-list pinning to the Anthropic family (which is
            # currently all claude-oauth in the fleet), expect zeros here.
            # Don't treat 0 as "cache miss"; it's "claude-oauth doesn't expose
            # the field." anthropic-direct (held in reserve) does expose it,
            # so non-zero values mean we routed to that path.
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            model_used = getattr(resp, "model", model) or model
            _log_lmrh_diagnostics(operation, resp_headers)

            # Strict-provider check (matches OpenAI-shape path)
            sp = strict_provider if strict_provider is not None else (
                operation in _STRICT_PROVIDER_TASKS
            )
            if sp:
                substituted, cap = _detect_substitution(operation, resp_headers)
                if substituted:
                    proxy_manager.mark_success(eid)
                    raise CrossFamilySubstitution(
                        f"{operation}: proxy substituted across model families "
                        f"({cap}); refusing for correctness."
                    )

            proxy_manager.mark_success(eid)
            cost_class = _extract_cost_class(resp_headers)
            logger.info(
                f"[proxy/anthropic] ✓ {operation} via ep={eid} model={model_used} "
                f"in={in_tok} out={out_tok} cache+={cache_create} cache↩={cache_read} "
                f"cost_class={cost_class or '?'} {time.time()-t0:.2f}s"
            )
            return {
                "response": resp, "endpoint_id": eid, "model": model_used,
                "in_tokens": in_tok, "out_tokens": out_tok,
                "cache_creation": cache_create, "cache_read": cache_read,
                "cost_class": cost_class,
            }
        except CrossFamilySubstitution:
            raise
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
