"""Bank-importer codegen agent (Phase 11D).

Takes a recorded HAR + free-text narration uploaded against a pending bank,
and produces a starter Playwright importer (Python) plus a smoke test.

The output is NOT auto-deployed — it lands in the `generated_importers` table,
where an admin reviews it on the dashboard, then copy/pastes the source into
`app/importers/<slug>_importer.py` and registers a route. The agent is
deliberately scoped to *draft* code that captures the observed flow; it is
expected that humans will refine.

Why a single Anthropic call (not a multi-step agent loop):
  - We have one big context (HAR summary + narration + reference template)
    and one well-defined output (Python source). The task is mostly pattern
    transfer from a working importer — agentic exploration buys little.
  - Prompt caching makes the static system + reference template ~free on the
    second call, so re-generation iterations are cheap.

If anyone later wants to extend this into a full agent loop with tool use
(e.g. let the model open a real Playwright session against the bank to verify
the selectors it generated), do that as a separate file — keep this one as the
deterministic "draft from a recording" baseline.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default model. Codegen is high-stakes (security-sensitive auth flows + the
# importer ships into our app), so we use the most capable available. Override
# via the `model` kwarg or the LLM_CODEGEN_MODEL env var for testing.
DEFAULT_MODEL = "claude-opus-4-7"

# Hard cap on output tokens. Importer source is typically 400-700 LOC; budget
# generously to avoid mid-file truncation.
MAX_OUTPUT_TOKENS = 8000

# Where the reference template lives. Read once at module-init.
_REF_PATH = Path(__file__).resolve().parents[1] / "importers" / "usbank_importer.py"
_BASE_PATH = Path(__file__).resolve().parents[1] / "importers" / "base_bank_importer.py"


def _load_reference_template() -> str:
    """Concatenate the base + a known-good importer as the "this is what good
    looks like" reference for the model. Cached via prompt caching, so the
    cost is paid once per ~5 minute window across all calls."""
    parts = []
    try:
        parts.append("# === base_bank_importer.py (shared helpers) ===\n")
        parts.append(_BASE_PATH.read_text())
    except Exception as e:
        logger.warning(f"Could not read base_bank_importer.py: {e}")
    try:
        parts.append("\n\n# === usbank_importer.py (reference implementation) ===\n")
        parts.append(_REF_PATH.read_text())
    except Exception as e:
        logger.warning(f"Could not read usbank_importer.py: {e}")
    return "".join(parts)


SYSTEM_PROMPT = """You are an expert Python + Playwright engineer who writes \
bank statement / transaction importers for a tax-management web app.

You will be given:
  1. Metadata about a pending bank (display name, slug, login URL, optional notes)
  2. A compact summary of a real HAR recording captured from a successful login + \
download session against that bank
  3. Free-text narration the user wrote describing what they did
  4. A reference implementation (`usbank_importer.py`) and the shared base \
helpers (`base_bank_importer.py`) — your output MUST follow the same patterns

Your job is to produce a starter `<slug>_importer.py` file that an admin will \
review, refine, and deploy. The code will live at \
`app/importers/<slug>_importer.py` and will be invoked via a Flask route.

═══ Output format ═══
Respond with valid JSON ONLY. No markdown fences. No prose before or after. \
The JSON must match this exact schema:

{
  "source_code":   "<full Python source for the importer>",
  "test_code":     "<a pytest smoke test that imports the module and checks the public surface>",
  "generation_notes": "<3-8 short bullet points covering: confidence level, gaps the human reviewer must fill (e.g. selectors I had to guess), and any anti-detection considerations specific to this bank>"
}

═══ Hard rules for source_code ═══
- Module docstring at the top, mirroring the style of usbank_importer.py.
- `from __future__ import annotations` at the top, then standard-lib imports, then \
`from app.importers.base_bank_importer import (...)` — reuse the base helpers, \
never reimplement them.
- Public function: `run_import(username, password, years, consume_path, entity_slug, job_id, log=logger.info, cookies=None, entity_id=None) -> dict` \
returning `{"imported": int, "skipped": int, "errors": int}`.
- Public function: `set_mfa_code(job_id: int, code: str) -> None` that delegates to `mfa_registry.set_code`.
- Constant `SOURCE = "<slug>"` at module scope.
- Use `launch_browser(slug, headless=True, log=log)` to start the browser.
- Always call `save_auth_cookies(page.context, slug, log)` immediately after a successful login — this is how persistent sessions work in our codebase.
- If the HAR shows MFA, implement MFA the same way usbank_importer does: detect the page, call `db.update_import_job(job_id, status="mfa_pending")`, then `wait_for_mfa_code(job_id, log)`, then submit the code.
- Use `human_click`, `human_type`, `human_move` for ALL user interactions — never raw page.click()/page.fill() (Akamai/Shape will flag linear input).
- Use `find_element` / `wait_for_element` / `find_in_frames` for selector lookups — banks frequently embed forms in iframes.
- Use multiple selector candidates per field — list all selectors you see in the HAR, in order of confidence.
- Handle CAPTCHA by calling `handle_captcha_if_present(page, log)` after login submit.
- Save debug screenshots at every meaningful step using `save_debug_screenshot(page, "<slug>_<step>")`.
- Wrap the whole flow in `try/finally` that closes context + stops playwright cleanly, just like usbank_importer.
- Date-range download: parse the user's `years` list and download per-year files into `<consume_path>/<entity_slug>/<year>/`. Filename should include the year and the source slug.
- Never log secrets. Truncate any partial-credential output to `[2 chars]****` like the reference does.
- If the HAR shows a downloadable file (PDF/CSV/QFX/OFX), fetch it the same way our reference does — by clicking the link inside Playwright (cookies travel automatically), NOT by reconstructing an authenticated requests.get call.

═══ Hard rules for test_code ═══
A short pytest module that verifies the importer's *shape* without actually \
hitting a bank (no Playwright, no network):
- import the module
- assert it exposes `run_import`, `set_mfa_code`, `SOURCE`
- assert `SOURCE` matches the slug
- assert `run_import` accepts the expected kwargs by inspecting `inspect.signature`

═══ When in doubt ═══
- Prefer over-broad selector lists ("here are 4 things that might match the username field") over picking one and being wrong.
- Mark unclear sections with a comment `# TODO: confirm in real session — HAR was ambiguous`.
- If you cannot determine the post-login dashboard URL from the HAR, leave it as the login URL and note this in `generation_notes`.

Generate code that is realistic, runnable Python — not pseudo-code, not a sketch."""


def _build_user_message(
    bank: dict,
    recording: dict,
    har_summary_text: str,
) -> str:
    """Render the bank + recording metadata + HAR summary as a single user
    message. We deliberately keep this short — heavy lifting (instructions,
    reference template) lives in the cached system prompt."""
    notes = bank.get("notes") or "(none)"
    narration = recording.get("narration_text") or "(no narration provided)"
    return (
        f"=== Pending bank ===\n"
        f"  display_name : {bank.get('display_name')}\n"
        f"  slug         : {bank.get('slug')}\n"
        f"  login_url    : {bank.get('login_url')}\n"
        f"  statements_url: {bank.get('statements_url') or '(unknown)'}\n"
        f"  platform_hint: {bank.get('platform_hint') or '(none)'}\n"
        f"  user notes   : {notes}\n\n"
        f"=== Narration (what the user did during recording) ===\n"
        f"{narration}\n\n"
        f"=== HAR summary ===\n"
        f"{har_summary_text}\n\n"
        f"Now produce the JSON-only output described in the system prompt."
    )


def generate_importer(
    bank_id: int,
    *,
    recording_id: Optional[int] = None,
    model: Optional[str] = None,
) -> dict:
    """Run the codegen pipeline for a single pending bank.

    Reads the bank + its most recent (or specified) recording from the DB,
    parses the HAR, calls Anthropic with prompt caching, parses the JSON
    output, and persists the result to the `generated_importers` table.

    Returns:
      {
        "id":            <new generated_importers row id>,
        "tokens_in":     <int>,
        "tokens_out":    <int>,
        "model":         <model used>,
        "notes":         <generation_notes from the LLM>,
      }

    Raises RuntimeError on any unrecoverable step (bank not found, no
    recording, HAR parse error, LLM call failed, malformed JSON).
    """
    from app import db
    from app.ai_agents.har_analyzer import parse_har, render_summary_for_prompt

    bank = db.get_pending_bank(bank_id)
    if not bank:
        raise RuntimeError(f"pending bank id={bank_id} not found")

    # Pick the recording: explicit id, else most recent for this bank
    if recording_id is not None:
        recording = db.get_recording(recording_id)
        if not recording or recording.get("pending_bank_id") != bank_id:
            raise RuntimeError(
                f"recording id={recording_id} not found for bank {bank_id}"
            )
    else:
        recordings = db.list_recordings(bank_id) or []
        if not recordings:
            raise RuntimeError(
                f"bank {bank_id} has no recordings — upload a HAR first"
            )
        recording = recordings[0]

    # Parse the HAR if one was uploaded
    har_summary_text = ""
    if recording.get("har_path") and os.path.exists(recording["har_path"]):
        summary = parse_har(recording["har_path"])
        har_summary_text = render_summary_for_prompt(summary)
    else:
        har_summary_text = "(no HAR file — narration only)"

    if not (recording.get("narration_text") or har_summary_text.strip()):
        raise RuntimeError("recording has neither HAR data nor narration text")

    # Mark bank as processing so the UI shows the in-flight state
    db.update_pending_bank(bank_id, status="processing")

    try:
        result = _call_codegen_llm(
            bank=bank,
            recording=recording,
            har_summary_text=har_summary_text,
            model=model,
        )
    except Exception as e:
        # Roll status back so the admin can retry
        db.update_pending_bank(bank_id, status="recorded")
        raise RuntimeError(f"codegen LLM call failed: {e}") from e

    # Static + shape validation BEFORE we accept the output. This catches the
    # most common LLM failure modes (syntax errors, missing public surface,
    # hallucinated helper names) so the admin reviewer doesn't have to.
    from app.ai_agents.importer_validator import validate as _validate
    validation_status, validation_notes = _validate(result["source_code"])
    if validation_status != "pass":
        logger.warning(
            f"codegen output failed validation ({validation_status}): "
            f"{validation_notes[:200]}"
        )

    gen_id = db.add_generated_importer(
        pending_bank_id=bank_id,
        recording_id=recording["id"],
        source_code=result["source_code"],
        test_code=result.get("test_code", ""),
        llm_model=result["model"],
        llm_tokens_in=result["tokens_in"],
        llm_tokens_out=result["tokens_out"],
        generation_notes=result.get("generation_notes", ""),
        validation_status=validation_status,
        validation_notes=validation_notes,
    )
    db.update_pending_bank(bank_id, status="generated")
    db.log_activity(
        "bank_importer_generated",
        f"bank={bank_id} gen_id={gen_id} model={result['model']} "
        f"tokens={result['tokens_in']}/{result['tokens_out']} "
        f"validation={validation_status}",
    )

    return {
        "id": gen_id,
        "tokens_in": result["tokens_in"],
        "tokens_out": result["tokens_out"],
        "model": result["model"],
        "notes": result.get("generation_notes", ""),
        "validation_status": validation_status,
        "validation_notes": validation_notes,
    }


def _call_codegen_llm(
    *,
    bank: dict,
    recording: dict,
    har_summary_text: str,
    model: Optional[str],
) -> dict:
    """Issue the actual Anthropic API call and parse the JSON response.

    Uses prompt caching on the (large, static) system prompt + reference
    template so subsequent calls in the same 5-minute window are much cheaper.
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic package is not installed — `pip install anthropic`"
        ) from e

    from app import config, db as _db
    from app import llm_usage_tracker as tracker

    api_key = (
        os.environ.get("LLM_CODEGEN_API_KEY")
        or _db.get_setting("llm_api_key")
        or config.LLM_API_KEY
    )
    if not api_key:
        raise RuntimeError(
            "no Anthropic API key configured (set LLM_API_KEY or llm_api_key)"
        )

    chosen_model = (
        model
        or os.environ.get("LLM_CODEGEN_MODEL")
        or DEFAULT_MODEL
    )
    reference = _load_reference_template()

    # The system prompt is split into two blocks so we can pin the cache
    # breakpoint just before the per-call user message. The reference template
    # is the heavy bit (~30k tokens), and it doesn't change between calls.
    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
        },
        {
            "type": "text",
            "text": (
                "═══ REFERENCE TEMPLATE ═══\n\n"
                "These two files are the canon for what a good importer looks like in "
                "this codebase. Mirror their style and reuse their helpers.\n\n"
                f"{reference}"
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user_msg = _build_user_message(bank, recording, har_summary_text)
    user_messages = [{"role": "user", "content": user_msg}]

    # Phase 12: try the proxy chain first (LMRH-aware, multi-endpoint with
    # circuit breaker). If the whole pool is exhausted, fall back to a direct
    # Anthropic SDK call so codegen stays operational when the proxies are down.
    resp = None
    proxy_endpoint_id = None
    try:
        from app.llm_client import proxy_call
        result = proxy_call.call_anthropic_messages(
            "codegen",
            model=chosen_model,
            system=system_blocks,
            messages=user_messages,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        resp = result["response"]
        proxy_endpoint_id = result["endpoint_id"]
        chosen_model = result["model"]  # whatever the proxy actually picked
        logger.info(f"bank_codegen routed via proxy ep={proxy_endpoint_id}")
    except Exception as proxy_err:
        logger.warning(
            f"bank_codegen proxy chain failed ({proxy_err}); "
            f"falling back to direct Anthropic SDK"
        )

    if resp is None:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=chosen_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_blocks,
            messages=user_messages,
        )

    # Token + cost tracking
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    cache_create = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    # Bill cache_create at full price, cache_read at the discounted rate
    # (Anthropic charges 10% of input price for cache reads). The tracker's
    # compute_cost() uses standard input price, so we approximate by adding
    # cached read cost separately. Good enough for our cost UI.
    cost = tracker.compute_cost("anthropic", chosen_model, in_tok + cache_create, out_tok)
    provider_label = (
        f"proxy:anthropic" if proxy_endpoint_id else "anthropic"
    )
    tracker.log_usage(
        provider=provider_label, model=chosen_model, operation="bank_codegen",
        input_tokens=in_tok + cache_create + cache_read,
        output_tokens=out_tok, cost=cost, success=True,
    )
    logger.info(
        f"codegen tokens — input={in_tok} cache_create={cache_create} "
        f"cache_read={cache_read} output={out_tok}"
    )

    raw_text = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    if not raw_text:
        raise RuntimeError("LLM response had no text content")

    parsed = _parse_json_response(raw_text)
    if not parsed.get("source_code"):
        raise RuntimeError(
            f"LLM response missing 'source_code'. First 300 chars: {raw_text[:300]!r}"
        )

    return {
        "source_code": parsed["source_code"],
        "test_code": parsed.get("test_code", ""),
        "generation_notes": parsed.get("generation_notes", ""),
        "tokens_in": in_tok + cache_create + cache_read,
        "tokens_out": out_tok,
        "model": chosen_model,
    }


def _parse_json_response(text: str) -> dict:
    """Parse a JSON-only response, with a couple of recovery tactics for
    when the model wraps it in fences or adds prose."""
    import re

    # 1) Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Strip ``` fences
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip())
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 3) Substring between first { and last }
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(text[first : last + 1])
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        f"could not parse JSON from LLM response. First 300 chars: {text[:300]!r}"
    )
