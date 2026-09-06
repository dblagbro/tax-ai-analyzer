"""
LLM package — Anthropic / OpenAI client with fallback chains and token tracking.

Re-exports the full public API so existing ``from app import llm_client`` callers
are unaffected after the package swap.
"""

from app.llm_client.vocab import (
    VALID_DOC_TYPES,
    VALID_CATEGORIES,
    VALID_ENTITIES,
    ANTHROPIC_FALLBACK_CHAIN,
    OPENAI_FALLBACK_CHAIN,
)

from app.llm_client.prompts import (
    ANALYSIS_SYSTEM,
    EXTRACTION_SYSTEM,
    CHAT_SYSTEM_TEMPLATE,
    SUMMARY_SYSTEM,
)

from app.llm_client.client import (
    LLMClient,
    get_client,
    analyze_document,
    extract_financial_data,
    chat,
    generate_summary,
)


def has_llm_capability(api_key: str = "") -> bool:
    """True if the app can make LLM calls right now.

    Fix (2026-09-05, per llm-proxy2 ops feedback): the analysis daemon
    was gating on `if not LLM_API_KEY` — meaning we'd skip cycles even
    when the proxy chain was fully configured and healthy. That was the
    wrong test because the proxy pool is the primary path since Phase 12
    and direct-SDK is only the fallback. We're LLM-capable if EITHER
    exists.

    Callers should replace bare `if not llm_api_key` gates with
    `if not has_llm_capability(llm_api_key)`.
    """
    if (api_key or "").strip():
        return True
    try:
        from app.llm_client import proxy_manager
        return bool(proxy_manager.get_endpoints())
    except Exception:
        return False
