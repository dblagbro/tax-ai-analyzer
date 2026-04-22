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
