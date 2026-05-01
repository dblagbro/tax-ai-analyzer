"""LLM client — Anthropic and OpenAI with fallback chains and token tracking."""
import json
import logging
import re
from typing import Optional

from app.llm_client.vocab import (
    VALID_DOC_TYPES, VALID_CATEGORIES, VALID_ENTITIES,
    ANTHROPIC_FALLBACK_CHAIN, OPENAI_FALLBACK_CHAIN,
)
from app.llm_client.prompts import (
    ANALYSIS_SYSTEM, EXTRACTION_SYSTEM, CHAT_SYSTEM_TEMPLATE, SUMMARY_SYSTEM,
)

logger = logging.getLogger(__name__)


class LLMClient:
    """
    Unified LLM client for Anthropic and OpenAI.

    Config is resolved at call time from (in priority order):
      1. Constructor arguments
      2. db.get_setting() runtime overrides (changed in UI without restart)
      3. Environment variables via config.py
    """

    def __init__(self, provider: str = None, api_key: str = None, model: str = None):
        self._anthropic_client = None
        self._openai_client = None
        self._provider_override = provider
        self._api_key_override = api_key
        self._model_override = model

    # ── Client factories ──────────────────────────────────────────────────────

    def _get_anthropic(self, api_key: str):
        try:
            import anthropic as _anthropic
        except ImportError:
            raise RuntimeError("anthropic package is not installed")
        if self._anthropic_client is None:
            self._anthropic_client = _anthropic.Anthropic(api_key=api_key)
        return self._anthropic_client

    def _get_openai(self, api_key: str, base_url: str = None):
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError("openai package is not installed")
        if base_url:
            return _openai.OpenAI(api_key=api_key, base_url=base_url)
        if self._openai_client is None:
            self._openai_client = _openai.OpenAI(api_key=api_key)
        return self._openai_client

    # NOTE: _get_proxy_client() (single-URL llm-proxy-manager fallback) was
    # removed 2026-04-30 along with the Tier 2 legacy path in _call(). The
    # proxy pool (db.llm_proxy_endpoints + proxy_manager) replaces it.

    # ── Runtime config resolution ─────────────────────────────────────────────

    def _resolve_config(self) -> dict:
        from app import db
        from app import config

        provider = self._provider_override or db.get_setting("llm_provider") or config.LLM_PROVIDER
        api_key = self._api_key_override or db.get_setting("llm_api_key") or config.LLM_API_KEY
        model = self._model_override or db.get_setting("llm_model") or config.LLM_MODEL
        openai_key = db.get_setting("openai_api_key") or config.OPENAI_API_KEY
        openai_model = db.get_setting("openai_model") or config.OPENAI_MODEL

        return {
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "openai_key": openai_key,
            "openai_model": openai_model,
        }

    # ── Core call methods ─────────────────────────────────────────────────────

    def _call_anthropic(
        self, api_key, model, system, messages,
        max_tokens=2048, operation="unknown", doc_id=None,
    ) -> tuple:
        from app import llm_usage_tracker as tracker

        chain = [model] + [m for m in ANTHROPIC_FALLBACK_CHAIN if m != model]
        last_error = None

        for attempt_model in chain:
            try:
                client = self._get_anthropic(api_key)
                resp = client.messages.create(
                    model=attempt_model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
                text = resp.content[0].text
                in_tok = resp.usage.input_tokens
                out_tok = resp.usage.output_tokens
                cost = tracker.compute_cost("anthropic", attempt_model, in_tok, out_tok)
                tracker.log_usage(
                    provider="anthropic", model=attempt_model, operation=operation,
                    input_tokens=in_tok, output_tokens=out_tok, cost=cost,
                    success=True, doc_id=doc_id,
                )
                if attempt_model != model:
                    logger.info(f"Fell back from {model} to {attempt_model}")
                return text, in_tok, out_tok
            except Exception as e:
                last_error = e
                logger.warning(f"Anthropic model {attempt_model} failed: {e}")
                tracker.log_usage(
                    provider="anthropic", model=attempt_model, operation=operation,
                    input_tokens=0, output_tokens=0, cost=0.0,
                    success=False, doc_id=doc_id,
                )

        raise RuntimeError(f"All Anthropic models failed. Last error: {last_error}")

    def _call_openai(
        self, api_key, model, system, messages,
        max_tokens=2048, operation="unknown", doc_id=None,
    ) -> tuple:
        from app import llm_usage_tracker as tracker

        chain = [model] + [m for m in OPENAI_FALLBACK_CHAIN if m != model]
        last_error = None
        oai_messages = [{"role": "system", "content": system}] + messages

        for attempt_model in chain:
            try:
                client = self._get_openai(api_key)
                resp = client.chat.completions.create(
                    model=attempt_model,
                    messages=oai_messages,
                    max_tokens=max_tokens,
                    temperature=0.1,
                )
                text = resp.choices[0].message.content or ""
                in_tok = resp.usage.prompt_tokens
                out_tok = resp.usage.completion_tokens
                cost = tracker.compute_cost("openai", attempt_model, in_tok, out_tok)
                tracker.log_usage(
                    provider="openai", model=attempt_model, operation=operation,
                    input_tokens=in_tok, output_tokens=out_tok, cost=cost,
                    success=True, doc_id=doc_id,
                )
                if attempt_model != model:
                    logger.info(f"Fell back from {model} to {attempt_model}")
                return text, in_tok, out_tok
            except Exception as e:
                last_error = e
                logger.warning(f"OpenAI model {attempt_model} failed: {e}")
                tracker.log_usage(
                    provider="openai", model=attempt_model, operation=operation,
                    input_tokens=0, output_tokens=0, cost=0.0,
                    success=False, doc_id=doc_id,
                )

        raise RuntimeError(f"All OpenAI models failed. Last error: {last_error}")

    def _call(
        self, system, user_content, max_tokens=2048,
        operation="unknown", doc_id=None, history=None,
        task: str = "analysis",
    ) -> tuple:
        """Send a chat completion request.

        Phase 12: routes through the llm_proxy_endpoints pool first
        (priority order, per-endpoint circuit breaker), with the
        single-URL ``LLM_PROXY_URL`` env var as a second-tier fallback,
        and direct Anthropic/OpenAI as the absolute last resort.

        ``task`` selects the LMRH preset (``analysis`` | ``chat`` |
        ``extraction`` | ``classification`` | ``reasoning`` |
        ``tax-review`` | ``summarize`` | ``codegen`` | ``qa``).
        """
        cfg = self._resolve_config()
        provider = cfg["provider"].lower()
        messages = list(history or [])
        messages.append({"role": "user", "content": user_content})

        oai_messages = [{"role": "system", "content": system}] + messages

        # ── Tier 1: pool of proxy endpoints (Phase 12) ──────────────────
        from app.llm_client import proxy_manager
        from app.llm_client.lmrh import build_lmrh_header
        from app import llm_usage_tracker as tracker

        lmrh = build_lmrh_header(task)
        send_model = cfg["model"] if provider != "openai" else cfg["openai_model"]

        for client, eid in proxy_manager.get_all_clients():
            try:
                resp = client.chat.completions.create(
                    model=send_model,
                    messages=oai_messages,
                    max_tokens=max_tokens,
                    temperature=0.1,
                    extra_headers={"LLM-Hint": lmrh},
                )
                text = resp.choices[0].message.content or ""
                in_tok = resp.usage.prompt_tokens if resp.usage else 0
                out_tok = resp.usage.completion_tokens if resp.usage else 0
                model_used = getattr(resp, "model", send_model) or send_model
                proxy_manager.mark_success(eid)
                try:
                    cost = tracker.compute_cost(provider, model_used, in_tok, out_tok)
                    tracker.log_usage(
                        provider=f"llm-proxy:{eid[:8]}", model=model_used,
                        operation=operation, input_tokens=in_tok,
                        output_tokens=out_tok, cost=cost,
                        success=True, doc_id=doc_id,
                    )
                except Exception:
                    pass
                logger.info(
                    "[llm-proxy] %s model=%s task=%s in=%d out=%d",
                    eid[:8], model_used, task, in_tok, out_tok
                )
                return text, in_tok, out_tok
            except Exception as proxy_err:
                logger.warning(
                    "[llm-proxy] %s call failed (%s) — trying next endpoint",
                    eid[:8], str(proxy_err)[:120]
                )
                proxy_manager.mark_failure(eid)
                continue

        # ── Tier 2: direct provider SDK (last resort) ──────────────────
        # NOTE: a previous "Tier 2" path called self._get_proxy_client() to hit
        # a legacy single-URL proxy via LLM_PROXY_URL. That code was removed
        # 2026-04-30 — llm-proxy-manager (v1) is permanently decommissioned per
        # ops directive. The chain is now Tier 1 (proxy pool with breaker) →
        # direct vendor SDK. Re-introducing v1 would re-create a dead-host
        # dependency.
        if provider == "openai":
            return self._call_openai(
                api_key=cfg["openai_key"], model=cfg["openai_model"],
                system=system, messages=messages, max_tokens=max_tokens,
                operation=operation, doc_id=doc_id,
            )
        return self._call_anthropic(
            api_key=cfg["api_key"], model=cfg["model"],
            system=system, messages=messages, max_tokens=max_tokens,
            operation=operation, doc_id=doc_id,
        )

    # ── JSON extraction helper ────────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> dict:
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        logger.error(f"Could not parse JSON from LLM response: {text[:200]}")
        return {}

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze_document(
        self,
        content: str,
        title: str = "",
        entity_hint: str = "personal",
        year_hint: str = "",
        doc_id: int = None,
    ) -> dict:
        if not content and not title:
            return _empty_analysis()

        content_trunc = content[:12000] if len(content) > 12000 else content
        user_msg = (
            f"Document title: {title}\n"
            f"Entity hint: {entity_hint}\n"
            f"Tax year hint: {year_hint or 'unknown'}\n\n"
            f"Document content:\n{content_trunc}"
        )

        try:
            text, _, _ = self._call(
                system=ANALYSIS_SYSTEM, user_content=user_msg,
                max_tokens=1024, operation="analyze_document", doc_id=doc_id,
            )
            return _normalize_analysis(self._extract_json(text), entity_hint, year_hint)
        except Exception as e:
            logger.error(f"analyze_document failed for doc {doc_id}: {e}")
            return _empty_analysis(error=str(e))

    def stream_text(self, prompt: str):
        cfg = self._resolve_config()
        provider = cfg["provider"].lower()
        messages = [{"role": "user", "content": prompt}]
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=cfg["api_key"])
            with client.messages.stream(
                model=cfg["model"], max_tokens=4096, messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield text
        elif provider == "openai":
            import openai
            client = openai.OpenAI(api_key=cfg.get("openai_key") or cfg["api_key"])
            response = client.chat.completions.create(
                model=cfg.get("openai_model") or cfg["model"],
                messages=messages, stream=True, max_tokens=4096,
            )
            for chunk in response:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    yield delta
        else:
            try:
                yield self.chat(messages)
            except Exception as e:
                yield f"(streaming not available: {e})"

    def extract_financial_data(self, content: str, doc_id: int = None) -> dict:
        if not content:
            return _empty_extraction()
        content_trunc = content[:10000] if len(content) > 10000 else content
        try:
            text, _, _ = self._call(
                system=EXTRACTION_SYSTEM, user_content=content_trunc,
                max_tokens=512, operation="extract_financial_data", doc_id=doc_id,
            )
            return _normalize_extraction(self._extract_json(text))
        except Exception as e:
            logger.error(f"extract_financial_data failed: {e}")
            return _empty_extraction(error=str(e))

    def chat(
        self,
        messages: list,
        entity_name: str = "Personal",
        tax_year: str = "",
        context_docs: list = None,
    ) -> str:
        doc_context = _format_doc_context(context_docs or [])
        system = CHAT_SYSTEM_TEMPLATE.format(
            entity_name=entity_name or "All Entities",
            tax_year=tax_year or "All Years",
            doc_context=doc_context or "(no documents loaded)",
        )
        history = [{"role": m["role"], "content": m["content"]} for m in messages[:-1]]
        last_user = messages[-1]["content"] if messages else ""
        try:
            text, _, _ = self._call(
                system=system, user_content=last_user,
                max_tokens=2048, operation="chat", history=history,
            )
            return text
        except Exception as e:
            logger.error(f"chat failed: {e}")
            return f"I'm sorry, I encountered an error: {e}"

    def generate_summary(
        self,
        entity_name: str,
        year: str,
        documents: list,
        summary_data: dict = None,
    ) -> str:
        doc_lines = []
        for doc in documents[:50]:
            try:
                doc_lines.append(
                    f"- {doc.get('doc_type','?')} | {doc.get('category','?')} | "
                    f"${doc.get('amount') or 0:.2f} | {doc.get('vendor','') or ''} | "
                    f"{doc.get('date','') or ''}"
                )
            except Exception:
                continue

        totals_block = ""
        if summary_data:
            totals_block = (
                f"\nFinancial totals:\n"
                f"  Income:    ${summary_data.get('income', 0):.2f}\n"
                f"  Expenses:  ${summary_data.get('expense', 0):.2f}\n"
                f"  Deductions:${summary_data.get('deduction', 0):.2f}\n"
                f"  Net:       ${summary_data.get('net', 0):.2f}\n"
            )

        user_msg = (
            f"Generate a financial summary for {entity_name} for tax year {year}.\n"
            f"{totals_block}\n"
            f"Documents ({len(documents)} total, showing up to 50):\n"
            + "\n".join(doc_lines)
        )
        try:
            text, _, _ = self._call(
                system=SUMMARY_SYSTEM, user_content=user_msg,
                max_tokens=1024, operation="generate_summary",
            )
            return text
        except Exception as e:
            logger.error(f"generate_summary failed: {e}")
            return f"Unable to generate summary: {e}"

    def classify_entity(self, content: str, title: str = "") -> str:
        prompt = (
            f"Given this financial document (title: '{title}'), "
            f"which entity does it belong to? "
            f"Respond with ONLY one word: personal, voipguru, or martinfeld_ranch.\n\n"
            f"Document excerpt:\n{content[:3000]}"
        )
        try:
            text, _, _ = self._call(
                system="You classify financial documents to entities. Respond with one word only.",
                user_content=prompt, max_tokens=10, operation="classify_entity",
            )
            slug = text.strip().lower()
            return slug if slug in VALID_ENTITIES else "personal"
        except Exception as e:
            logger.warning(f"classify_entity failed: {e}")
            return "personal"


# ── Normalization helpers ─────────────────────────────────────────────────────

def _normalize_analysis(raw: dict, entity_hint: str, year_hint: str) -> dict:
    doc_type = raw.get("doc_type", "other")
    if doc_type not in VALID_DOC_TYPES:
        doc_type = "other"

    category = raw.get("category", "other")
    if category not in VALID_CATEGORIES:
        category = "other"

    entity = raw.get("entity") or entity_hint or "personal"
    if entity not in VALID_ENTITIES:
        entity = entity_hint or "personal"

    tax_year = raw.get("tax_year") or year_hint or ""
    if tax_year:
        tax_year = str(tax_year).strip()[:4]

    try:
        amount = float(raw["amount"]) if raw.get("amount") is not None else None
    except (ValueError, TypeError):
        amount = None

    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
    except (ValueError, TypeError):
        confidence = 0.5

    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    return {
        "doc_type": doc_type,
        "category": category,
        "entity": entity,
        "tax_year": tax_year,
        "vendor": (raw.get("vendor") or "").strip() or None,
        "amount": amount,
        "date": raw.get("date") or None,
        "confidence": confidence,
        "description": (raw.get("description") or "").strip(),
        "tags": tags,
        "extracted_fields": raw.get("extracted_fields") or {},
    }


def _normalize_extraction(raw: dict) -> dict:
    return {
        "amounts": raw.get("amounts") or [],
        "dates": raw.get("dates") or [],
        "payer": raw.get("payer") or None,
        "payee": raw.get("payee") or None,
        "account_numbers": raw.get("account_numbers") or [],
        "tax_ids": raw.get("tax_ids") or [],
        "addresses": raw.get("addresses") or [],
        "totals": raw.get("totals") or {},
    }


def _empty_analysis(error: str = "") -> dict:
    return {
        "doc_type": "other",
        "category": "other",
        "entity": "personal",
        "tax_year": None,
        "vendor": None,
        "amount": None,
        "date": None,
        "confidence": 0.0,
        "description": f"Analysis failed: {error}" if error else "No content to analyze",
        "tags": [],
        "extracted_fields": {},
    }


def _empty_extraction(error: str = "") -> dict:
    return {
        "amounts": [], "dates": [], "payer": None, "payee": None,
        "account_numbers": [], "tax_ids": [], "addresses": [], "totals": {},
    }


def _format_doc_context(docs: list, max_docs: int = 20) -> str:
    lines = []
    for doc in docs[:max_docs]:
        try:
            lines.append(
                f"[{doc.get('doc_type','?')}] {doc.get('vendor') or 'Unknown'} | "
                f"{doc.get('category','?')} | "
                f"${doc.get('amount') or 0:.2f} | {doc.get('date','?')}"
            )
        except Exception:
            continue
    return "\n".join(lines) if lines else "(no documents)"


# ── Singleton ─────────────────────────────────────────────────────────────────

_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


# ── Convenience functions (backwards-compatible) ───────────────────────────────

def analyze_document(
    content: str, title: str = "", entity_hint: str = "personal",
    year_hint: str = "", doc_id: int = None,
) -> dict:
    return get_client().analyze_document(content, title, entity_hint, year_hint, doc_id)


def extract_financial_data(content: str, doc_id: int = None) -> dict:
    return get_client().extract_financial_data(content, doc_id)


def chat(
    messages: list, entity_name: str = "Personal",
    tax_year: str = "", context_docs: list = None,
) -> str:
    return get_client().chat(messages, entity_name, tax_year, context_docs)


def generate_summary(
    entity_name: str, year: str, documents: list, summary_data: dict = None,
) -> str:
    return get_client().generate_summary(entity_name, year, documents, summary_data)
