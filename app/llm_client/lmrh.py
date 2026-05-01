"""LLM Model Routing Hint (LMRH) header builder.

Spec: ``/home/dblagbro/llm-proxy-v2/v1-reference/LMRH-PROTOCOL.md``
Format: RFC 8941 structured dictionary, semicolon-separated key=value pairs.

Emit via ``extra_headers={'LLM-Hint': build_lmrh_header(...)}`` on OpenAI SDK
calls. llm-proxy2 parses this header and uses it to score provider candidates.
Unknown dimensions are ignored (soft preference) unless marked ``;require``.

Phase 12 — task taxonomy adapted from paperless-ai-analyzer to fit tax-ai's
call sites: document analysis, financial extraction, AI Chat, tax review,
entity classification, summarization.
"""

from __future__ import annotations

from typing import Optional


# Task presets per call-site. The proxy uses these to pick a model when the
# caller doesn't specify one explicitly. Keep this table in sync with the
# call-site labels in ``app/llm_client/client.py``.
TASK_PRESETS: dict[str, dict] = {
    # Document analysis pipeline (categorizer / extractor / analyze_document)
    "analysis":         {"model_pref": "claude-sonnet-4-6", "fallback_chain": "anthropic,openai"},
    "extraction":       {"model_pref": "gpt-4o", "fallback_chain": "openai,anthropic"},
    "classification":   {"model_pref": "gpt-4o-mini"},

    # AI Chat tab
    "chat":             {"model_pref": "claude-sonnet-4-6"},

    # Q&A — short factual answers
    "qa":               {},

    # Reasoning-heavy: tax review, multi-document synthesis
    "reasoning":        {"model_pref": "claude-opus-4-7", "quality": "high"},
    "tax-review":       {"model_pref": "claude-opus-4-7", "quality": "high"},

    # Free-text summary generation
    "summarize":        {"model_pref": "claude-sonnet-4-6"},

    # Bank-importer codegen agent (Phase 11D-E)
    "codegen":          {"model_pref": "claude-sonnet-4-6", "fallback_chain": "anthropic,openai"},
}


def build_lmrh_header(
    task: str,
    *,
    model_pref: Optional[str] = None,
    fallback_chain: Optional[str] = None,
    quality: Optional[str] = None,
    has_images: bool = False,
    extras: Optional[dict] = None,
) -> str:
    """Build the LLM-Hint header value.

    Arguments:
        task: semantic label (e.g. ``"chat"``, ``"analysis"``, ``"reasoning"``).
              Looked up in TASK_PRESETS to fill model_pref / quality defaults.
              Explicit kwargs override presets.
        model_pref: soft preference for a specific provider model name
        fallback_chain: comma-separated provider preference order
        quality: ``"low" | "standard" | "high"``
        has_images: if True, adds ``modality=vision``
        extras: dict of extra key=value pairs appended verbatim

    Returns:
        A string like ``task=chat; model-pref=claude-sonnet-4-6`` suitable for
        the ``LLM-Hint`` HTTP header. Callers attach via
        ``extra_headers={'LLM-Hint': ...}``.
    """
    preset = TASK_PRESETS.get(task, {})
    pref = model_pref or preset.get("model_pref")
    fb = fallback_chain or preset.get("fallback_chain")
    q = quality or preset.get("quality")

    parts: list[str] = [f"task={task}"]
    if pref:
        parts.append(f"model-pref={pref}")
    if fb:
        parts.append(f"fallback-chain={fb}")
    if q:
        parts.append(f"quality={q}")
    if has_images:
        parts.append("modality=vision")
    if extras:
        for k, v in extras.items():
            key = str(k).lower().replace("_", "-")
            parts.append(f"{key}={v}")

    return "; ".join(parts)
