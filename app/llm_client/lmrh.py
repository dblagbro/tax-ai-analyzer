"""LMRH (LLM Model Routing Hint) header builder.

Spec: https://www.voipguru.org/llm-proxy2/lmrh.md (v1.0 RFC draft)
Format: RFC 8941 structured field list, comma-separated `dim=value` pairs.

Emit via `extra_headers={'LLM-Hint': build_lmrh_header(...)}` on the OpenAI SDK,
or via `default_headers={'LLM-Hint': ...}` when constructing the Anthropic client.
llm-proxy2 parses this header and uses it to score provider candidates.
Unknown dimensions are ignored (soft preference) unless marked `;require`.

Ops directive (2026-04-30):
  - Do NOT hardcode model names per operation. Let the proxy pick the best
    model+provider based on `task=` + `cost=` + `safety-min=` + `context-length=`.
  - If Anthropic ships a cheaper Haiku tomorrow, the proxy auto-picks it.
    Tax-ai code does not change.

Recognized dims (from the LMRH 1.0 spec):
  task            chat | reasoning | analysis | code | creative | audio | vision | summarize | classify | extract
  cost            economy | standard | premium
  latency         low | medium | high
  safety-min      1..5         (with optional `;require` for hard constraint)
  safety-max      1..5
  context-length  positive int (token count needed)
  modality        text | vision | audio
  region          us | eu | asia | <ISO-3166-1 alpha-2>
  refusal-rate    permissive | standard | strict | maximum
"""

from __future__ import annotations

from typing import Optional


# Per-task default `cost` tier. Callers can override by passing cost= explicitly.
# Picked to match the ops-recommended pattern:
#   - Cheap classification / extraction               → economy
#   - Doc analysis / summaries / chat (mid-volume)    → standard
#   - Reasoning-heavy / codegen                       → premium
TASK_PRESETS: dict[str, dict] = {
    # Document analysis pipeline (categorizer / extractor / analyze_document)
    "analysis":         {"cost": "standard", "safety-min": 3},
    "extraction":       {"cost": "economy"},
    "classification":   {"cost": "economy"},

    # AI Chat tab — keep premium for quality
    "chat":             {"cost": "premium"},

    # Q&A — short factual answers
    "qa":               {"cost": "standard"},

    # Reasoning-heavy: tax review, multi-document synthesis. cascade=auto
    # lets the proxy chain a cheap reasoning model with a quality model
    # behind a single answer — better quality-per-dollar on reasoning.
    #
    # provider-hint=anthropic (SOFT — no ;require): prefer anthropic when
    # available, but allow the proxy to score-pick a substitute. The
    # post-call CrossFamilySubstitution exception in proxy_call.py is the
    # hard backstop — if the proxy actually substitutes, we refuse the
    # response. We don't use ;require here because the proxy's exact
    # provider id ("anthropic" vs "anthropic-direct" etc.) isn't a stable
    # public contract; over-constraining trips 503s on coherent calls.
    "reasoning":        {"cost": "premium", "cascade": "auto"},
    "tax-review":       {"cost": "premium", "cascade": "auto",
                         "provider-hint": "anthropic"},

    # Free-text summary generation
    "summarize":        {"cost": "standard"},

    # Bank-importer codegen agent (Phase 11D-E) — premium + long context
    # provider-hint=anthropic (soft) + post-call CrossFamilySubstitution
    # exception: bank importer code is security-sensitive (user banking
    # creds + MFA flows). We prefer anthropic but accept the proxy's
    # judgment IF it scores anthropic as best. If it cross-family-falls-
    # back to a non-anthropic upstream, the post-call check refuses the
    # response so we never ship substituted code.
    "codegen":          {"cost": "premium", "context-length": 60000,
                         "provider-hint": "anthropic"},

    # Vision / image-modality calls
    "vision":           {"cost": "standard"},

    # Embeddings
    "embed":            {"cost": "economy"},
}


def build_lmrh_header(
    task: str,
    *,
    cost: Optional[str] = None,
    quality: Optional[str] = None,
    safety_min: Optional[int] = None,
    context_length: Optional[int] = None,
    has_images: bool = False,
    cascade: Optional[str] = None,
    provider_hint: Optional[str] = None,
    provider_hint_required: bool = False,
    exclude: Optional[str] = None,
    exclude_required: bool = False,
    extras: Optional[dict] = None,
) -> str:
    """Build the LLM-Hint header value.

    Arguments:
      task: cognitive type — one of the LMRH `task=` tokens above. Used as
            both the literal hint dim and the lookup key into TASK_PRESETS.
      cost: economy | standard | premium. Defaults to TASK_PRESETS[task]["cost"].
      quality: low | standard | high. Optional pass-through.
      safety_min: 1..5. Hard constraint via `;require`.
      context_length: int — minimum tokens of context the model must support.
      has_images: if True, adds modality=vision.
      cascade: pass-through dim (LMRH spec lists it as a provider-specific
            extension). When set to ``"auto"``, llm-proxy2 may chain a
            cheap reasoning model with a quality model behind a single
            answer — typically improves quality-per-dollar on reasoning
            tasks. Per-task default in TASK_PRESETS["cascade"].
      extras: dict of extra dim=value pairs appended verbatim.

    Returns:
      A string like `task=analysis, cost=standard, safety-min=3` suitable for
      the LLM-Hint header. Empty string if `task` is empty.

    Per the LMRH 1.0 spec, unknown dims are ignored by the proxy. We keep
    things conservative — emit only well-formed dims.
    """
    if not task:
        return ""

    preset = TASK_PRESETS.get(task, {})
    eff_cost = cost or preset.get("cost")
    eff_ctx = context_length if context_length is not None else preset.get("context-length")
    eff_safety = safety_min if safety_min is not None else preset.get("safety-min")
    eff_cascade = cascade if cascade is not None else preset.get("cascade")
    eff_provider = provider_hint if provider_hint is not None else preset.get("provider-hint")
    eff_provider_req = (provider_hint_required if provider_hint is not None
                        else preset.get("provider-hint-required", False))
    eff_exclude = exclude if exclude is not None else preset.get("exclude")
    eff_exclude_req = (exclude_required if exclude is not None
                       else preset.get("exclude-required", False))

    parts: list[str] = [f"task={task}"]
    if eff_cost:
        parts.append(f"cost={eff_cost}")
    if quality:
        parts.append(f"quality={quality}")
    if eff_safety is not None:
        parts.append(f"safety-min={int(eff_safety)}")
    if eff_ctx is not None:
        parts.append(f"context-length={int(eff_ctx)}")
    if has_images:
        parts.append("modality=vision")
    if eff_cascade:
        parts.append(f"cascade={eff_cascade}")
    # v3.0.46 paid-plan substitution defense — provider-hint=<vendor>;require
    # tells the proxy to fail-fast (HTTP 503) instead of cross-family substituting.
    if eff_provider:
        suffix = ";require" if eff_provider_req else ""
        parts.append(f"provider-hint={eff_provider}{suffix}")
    # v3.0.25 escape hatch — exclude=<vendor>;require steers around a flaky upstream.
    if eff_exclude:
        suffix = ";require" if eff_exclude_req else ""
        parts.append(f"exclude={eff_exclude}{suffix}")
    if extras:
        for k, v in extras.items():
            key = str(k).lower().replace("_", "-")
            parts.append(f"{key}={v}")

    # Spec calls for comma+space separator on the structured-field list.
    return ", ".join(parts)


def get_hint(operation_or_task: str, **overrides) -> str:
    """Convenience: look up an operation/task name and return the hint string.

    First checks for an operator override in db.get_setting(f"lmrh.hint.{key}"),
    then falls through to build_lmrh_header(task=...). Mirrors the
    coordinator-hub get_lmrh_hint() pattern.
    """
    try:
        from app import db as _db
        ovr = (_db.get_setting(f"lmrh.hint.{operation_or_task}") or "").strip()
        if ovr:
            return ovr
    except Exception:
        pass
    return build_lmrh_header(operation_or_task, **overrides)


def list_tasks() -> list[str]:
    """Task names with built-in presets (used by the admin UI)."""
    return sorted(TASK_PRESETS.keys())
