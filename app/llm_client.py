"""
LLM client for Financial AI Analyzer.

Supports Anthropic (Claude) and OpenAI with:
  - Runtime model override via database settings
  - Fallback model chain on failure
  - Token tracking via llm_usage_tracker
  - All synchronous methods
"""
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Document type / category vocabularies ─────────────────────────────────────
VALID_DOC_TYPES = {
    "W-2", "1099-NEC", "1099-K", "1099-INT", "1099-DIV", "1099-MISC",
    "invoice", "receipt", "utility_bill", "bank_statement", "mortgage_statement",
    "property_tax", "vehicle", "equipment", "subscription", "charitable_donation",
    "medical", "farm_expense", "paypal_transaction", "venmo_transaction",
    "credit_card_statement", "insurance", "capital_improvement", "other",
}

VALID_CATEGORIES = {"income", "expense", "deduction", "asset", "other"}

VALID_ENTITIES = {"personal", "voipguru", "martinfeld_ranch"}

# ── Model pricing ($/1M tokens) ───────────────────────────────────────────────
# kept here for reference; authoritative copy is in llm_usage_tracker.py
_ANTHROPIC_FALLBACK_CHAIN = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-3-haiku-20240307",
]
_OPENAI_FALLBACK_CHAIN = [
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]

# ── System prompts ────────────────────────────────────────────────────────────

_ANALYSIS_SYSTEM = """You are a financial document analysis AI for a US tax management platform.
Your job is to extract structured data from financial documents for tax categorization.

You MUST respond with valid JSON only — no markdown, no prose, no code blocks.
The response must be a single JSON object matching this schema exactly:

{
  "title": "<short descriptive title, e.g. '2023 W-2 Wages — Acme Corp' or 'Amazon Receipt — Office Supplies'>",
  "doc_type": "<one of the valid types>",
  "category": "<income|expense|deduction|asset|other>",
  "entity": "<personal|voipguru|martinfeld_ranch>",
  "tax_year": "<4-digit year or null>",
  "vendor": "<company/payer name or null>",
  "amount": <number or null>,
  "date": "<YYYY-MM-DD or null>",
  "confidence": <0.0-1.0>,
  "description": "<one-sentence summary>",
  "tags": ["tag1", "tag2"],
  "extracted_fields": {
    "payer_name": null,
    "payer_ein": null,
    "recipient_name": null,
    "account_number_last4": null,
    "box_amounts": {}
  }
}

Valid doc_type values: W-2, 1099-NEC, 1099-K, 1099-INT, 1099-DIV, 1099-MISC,
invoice, receipt, utility_bill, bank_statement, mortgage_statement, property_tax,
vehicle, equipment, subscription, charitable_donation, medical, farm_expense,
paypal_transaction, venmo_transaction, credit_card_statement, insurance,
capital_improvement, other

Entity assignment rules:
- personal: personal income/expenses, W-2 wages, personal medical, personal mortgage
- voipguru: telecom/VoIP business expenses, business invoices, business subscriptions
- martinfeld_ranch: farm/ranch expenses, agricultural supplies, livestock, land

If entity cannot be determined from context, use the provided entity_hint.
Set confidence based on how certain you are (1.0 = tax form with clear data, 0.5 = ambiguous).

CRITICAL CLASSIFICATION RULES — apply before returning:

1. PROPOSALS / QUOTES / ESTIMATES / BIDS: If the document is a proposal, bid,
   estimate, scope-of-work document, or quote — meaning it describes work
   proposed or priced but NOT yet invoiced or paid — set doc_type="other",
   category="other", amount=null. Signals: "proposal", "quote", "estimate",
   "bid", "scope of work", "we are pleased to submit", "work to be performed",
   addressed to a third party not the account owner.

2. CAPITAL IMPROVEMENTS (IRS §263 — not immediately deductible): Construction,
   renovation, remodeling, demolition, asbestos/lead/mold abatement, structural
   work, roofing, HVAC replacement, major electrical/plumbing, and any single
   project > $2,500 must use doc_type="capital_improvement", category="asset".
   These are NOT current-year expenses.

3. BANK / CREDIT CARD / MORTGAGE STATEMENTS: Use category="other" (never
   "expense" or "income"). The statement balance or total-due is NOT an expense;
   individual charges captured elsewhere are. Extract minimum payment due as
   amount if available, otherwise null.

4. INVOICES for ordinary services (repairs < $2,500, professional fees, software,
   utilities, supplies): doc_type="invoice" or appropriate type, category="expense".

5. AMOUNT = actual charged/billed/paid amount only. Do NOT extract account
   balances, remaining loan principal, or proposal totals as the amount.
"""

_EXTRACTION_SYSTEM = """You are a financial data extraction AI.
Extract all financial data from the provided document text.
Respond with valid JSON only — no markdown, no prose.

Schema:
{
  "amounts": [{"value": 0.00, "label": "description", "currency": "USD"}],
  "dates": ["YYYY-MM-DD"],
  "payer": "<name or null>",
  "payee": "<name or null>",
  "account_numbers": ["last 4 digits only"],
  "tax_ids": ["EIN/SSN patterns found, last 4 only"],
  "addresses": ["address strings found"],
  "totals": {"gross": null, "net": null, "tax_withheld": null, "fees": null}
}
"""

_CHAT_SYSTEM_TEMPLATE = """You are a financial AI assistant for a US tax management platform.
You help with bookkeeping, tax categorization, and financial analysis across multiple entities:
- Personal: personal income and expenses
- VoIPGuru: telecom/VoIP business
- Martinfeld Ranch: farm/ranch operations

Current context:
  Entity: {entity_name}
  Tax Year: {tax_year}

You have access to the following recent documents from this entity/year:
{doc_context}

Answer questions about finances, taxes, categorization, and deductions.
Be concise and specific. For tax advice, note that you are providing information only,
not professional tax advice.
"""

_SUMMARY_SYSTEM = """You are a financial reporting AI. Write clear, professional narrative
summaries of financial data for tax preparation purposes. Be specific with numbers.
Keep the summary to 3-5 paragraphs."""


class LLMClient:
    """
    Unified LLM client for Anthropic and OpenAI.

    Model and API key are resolved at call time from:
      1. Constructor arguments (if provided — e.g. from analysis daemon)
      2. db.get_setting() (runtime override via UI)
      3. environment variables (config.py)

    This allows the model to be changed without restarting the container.
    """

    def __init__(self, provider: str = None, api_key: str = None, model: str = None):
        self._anthropic_client = None
        self._openai_client = None
        # Optional constructor-level overrides (take precedence over DB/env)
        self._provider_override = provider
        self._api_key_override = api_key
        self._model_override = model

    # ── Client factories ──────────────────────────────────────────────────────

    def _get_anthropic(self, api_key: str):
        """Return a (possibly cached) Anthropic client."""
        try:
            import anthropic as _anthropic
        except ImportError:
            raise RuntimeError("anthropic package is not installed")
        if self._anthropic_client is None:
            self._anthropic_client = _anthropic.Anthropic(api_key=api_key)
        return self._anthropic_client

    def _get_openai(self, api_key: str):
        """Return a (possibly cached) OpenAI client."""
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError("openai package is not installed")
        if self._openai_client is None:
            self._openai_client = _openai.OpenAI(api_key=api_key)
        return self._openai_client

    # ── Runtime config resolution ─────────────────────────────────────────────

    def _resolve_config(self) -> dict:
        """Return current effective config: constructor args > DB overrides > env."""
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
        self,
        api_key: str,
        model: str,
        system: str,
        messages: list,
        max_tokens: int = 2048,
        operation: str = "unknown",
        doc_id: int = None,
    ) -> tuple[str, int, int]:
        """
        Call Anthropic API with fallback chain.
        Returns (response_text, input_tokens, output_tokens).
        """
        from app import llm_usage_tracker as tracker

        chain = [model] + [m for m in _ANTHROPIC_FALLBACK_CHAIN if m != model]
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
                    provider="anthropic",
                    model=attempt_model,
                    operation=operation,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost=cost,
                    success=True,
                    doc_id=doc_id,
                )
                if attempt_model != model:
                    logger.info(f"Fell back from {model} to {attempt_model}")
                return text, in_tok, out_tok
            except Exception as e:
                last_error = e
                logger.warning(f"Anthropic model {attempt_model} failed: {e}")
                tracker.log_usage(
                    provider="anthropic",
                    model=attempt_model,
                    operation=operation,
                    input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                    success=False,
                    doc_id=doc_id,
                )

        raise RuntimeError(f"All Anthropic models failed. Last error: {last_error}")

    def _call_openai(
        self,
        api_key: str,
        model: str,
        system: str,
        messages: list,
        max_tokens: int = 2048,
        operation: str = "unknown",
        doc_id: int = None,
    ) -> tuple[str, int, int]:
        """
        Call OpenAI API with fallback chain.
        Returns (response_text, input_tokens, output_tokens).
        """
        from app import llm_usage_tracker as tracker

        chain = [model] + [m for m in _OPENAI_FALLBACK_CHAIN if m != model]
        last_error = None

        # Build OpenAI message format
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
                    provider="openai",
                    model=attempt_model,
                    operation=operation,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost=cost,
                    success=True,
                    doc_id=doc_id,
                )
                if attempt_model != model:
                    logger.info(f"Fell back from {model} to {attempt_model}")
                return text, in_tok, out_tok
            except Exception as e:
                last_error = e
                logger.warning(f"OpenAI model {attempt_model} failed: {e}")
                tracker.log_usage(
                    provider="openai",
                    model=attempt_model,
                    operation=operation,
                    input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                    success=False,
                    doc_id=doc_id,
                )

        raise RuntimeError(f"All OpenAI models failed. Last error: {last_error}")

    def _call(
        self,
        system: str,
        user_content: str,
        max_tokens: int = 2048,
        operation: str = "unknown",
        doc_id: int = None,
        history: list = None,
    ) -> tuple[str, int, int]:
        """Route to the correct provider based on runtime config."""
        cfg = self._resolve_config()
        provider = cfg["provider"].lower()

        messages = list(history or [])
        messages.append({"role": "user", "content": user_content})

        if provider == "openai":
            return self._call_openai(
                api_key=cfg["openai_key"],
                model=cfg["openai_model"],
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                operation=operation,
                doc_id=doc_id,
            )
        else:
            return self._call_anthropic(
                api_key=cfg["api_key"],
                model=cfg["model"],
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                operation=operation,
                doc_id=doc_id,
            )

    # ── JSON extraction helper ────────────────────────────────────────────────

    @staticmethod
    def _extract_json(text: str) -> dict:
        """
        Robustly extract a JSON object from LLM response text.
        Handles markdown code fences and leading/trailing prose.
        """
        # Strip markdown code fences
        text = re.sub(r"```(?:json)?\s*", "", text).strip()
        text = text.rstrip("`").strip()

        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Find first { ... } block
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
        """
        Analyze a financial document and return structured classification.

        Returns dict with keys:
          doc_type, category, entity, tax_year, vendor, amount, date,
          confidence, description, tags, extracted_fields
        """
        if not content and not title:
            return _empty_analysis()

        # Truncate very long content (keep first 12k chars — enough for most docs)
        content_trunc = content[:12000] if len(content) > 12000 else content

        user_msg = (
            f"Document title: {title}\n"
            f"Entity hint: {entity_hint}\n"
            f"Tax year hint: {year_hint or 'unknown'}\n\n"
            f"Document content:\n{content_trunc}"
        )

        try:
            text, in_tok, out_tok = self._call(
                system=_ANALYSIS_SYSTEM,
                user_content=user_msg,
                max_tokens=1024,
                operation="analyze_document",
                doc_id=doc_id,
            )
            result = self._extract_json(text)
            return _normalize_analysis(result, entity_hint, year_hint)
        except Exception as e:
            logger.error(f"analyze_document failed for doc {doc_id}: {e}")
            return _empty_analysis(error=str(e))

    def extract_financial_data(
        self,
        content: str,
        doc_id: int = None,
    ) -> dict:
        """
        Extract raw financial data (amounts, dates, payer info) from document text.

        Returns dict with keys:
          amounts, dates, payer, payee, account_numbers, tax_ids, addresses, totals
        """
        if not content:
            return _empty_extraction()

        content_trunc = content[:10000] if len(content) > 10000 else content

        try:
            text, _, _ = self._call(
                system=_EXTRACTION_SYSTEM,
                user_content=content_trunc,
                max_tokens=512,
                operation="extract_financial_data",
                doc_id=doc_id,
            )
            result = self._extract_json(text)
            return _normalize_extraction(result)
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
        """
        Chat with the financial AI assistant.

        Args:
            messages: List of {"role": "user"|"assistant", "content": str}
            entity_name: Current entity display name
            tax_year: Current tax year filter
            context_docs: List of analyzed_document dicts to include as context

        Returns response string (may include markdown).
        """
        # Build document context snippet
        doc_context = _format_doc_context(context_docs or [])

        system = _CHAT_SYSTEM_TEMPLATE.format(
            entity_name=entity_name or "All Entities",
            tax_year=tax_year or "All Years",
            doc_context=doc_context or "(no documents loaded)",
        )

        # Convert message list to API format — use all but last as history
        history = []
        for msg in messages[:-1]:
            history.append({"role": msg["role"], "content": msg["content"]})

        last_user = messages[-1]["content"] if messages else ""

        try:
            text, _, _ = self._call(
                system=system,
                user_content=last_user,
                max_tokens=2048,
                operation="chat",
                history=history,
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
        """
        Generate a narrative financial summary for an entity/year.

        Args:
            entity_name: Display name of the entity
            year: Tax year
            documents: List of analyzed_document row dicts
            summary_data: Optional pre-computed totals dict (from db.get_financial_summary)

        Returns narrative summary string (plain text or light markdown).
        """
        # Build a compact data representation
        doc_lines = []
        for doc in documents[:50]:  # cap at 50 for context size
            try:
                line = (
                    f"- {doc.get('doc_type','?')} | {doc.get('category','?')} | "
                    f"${doc.get('amount') or 0:.2f} | {doc.get('vendor','') or ''} | "
                    f"{doc.get('date','') or ''}"
                )
                doc_lines.append(line)
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
                system=_SUMMARY_SYSTEM,
                user_content=user_msg,
                max_tokens=1024,
                operation="generate_summary",
            )
            return text
        except Exception as e:
            logger.error(f"generate_summary failed: {e}")
            return f"Unable to generate summary: {e}"

    def classify_entity(self, content: str, title: str = "") -> str:
        """
        Quick entity classification. Returns slug: personal/voipguru/martinfeld_ranch.
        """
        prompt = (
            f"Given this financial document (title: '{title}'), "
            f"which entity does it belong to? "
            f"Respond with ONLY one word: personal, voipguru, or martinfeld_ranch.\n\n"
            f"Document excerpt:\n{content[:3000]}"
        )
        try:
            text, _, _ = self._call(
                system="You classify financial documents to entities. Respond with one word only.",
                user_content=prompt,
                max_tokens=10,
                operation="classify_entity",
            )
            slug = text.strip().lower()
            return slug if slug in VALID_ENTITIES else "personal"
        except Exception as e:
            logger.warning(f"classify_entity failed: {e}")
            return "personal"


# ── Normalization helpers ─────────────────────────────────────────────────────

def _normalize_analysis(raw: dict, entity_hint: str, year_hint: str) -> dict:
    """Clamp and validate analysis result fields."""
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
        # Normalize to 4-digit string
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
    """Validate extraction result fields."""
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
        "amounts": [],
        "dates": [],
        "payer": None,
        "payee": None,
        "account_numbers": [],
        "tax_ids": [],
        "addresses": [],
        "totals": {},
    }


def _format_doc_context(docs: list, max_docs: int = 20) -> str:
    """Format document list into a compact context string for chat."""
    lines = []
    for doc in docs[:max_docs]:
        try:
            line = (
                f"[{doc.get('doc_type','?')}] {doc.get('vendor') or 'Unknown'} | "
                f"{doc.get('category','?')} | "
                f"${doc.get('amount') or 0:.2f} | {doc.get('date','?')}"
            )
            lines.append(line)
        except Exception:
            continue
    return "\n".join(lines) if lines else "(no documents)"


# ── Module-level singleton ────────────────────────────────────────────────────

_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    """Return the module-level LLMClient singleton."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


# ── Convenience top-level functions (backwards-compatible API) ────────────────

def analyze_document(
    content: str,
    title: str = "",
    entity_hint: str = "personal",
    year_hint: str = "",
    doc_id: int = None,
) -> dict:
    return get_client().analyze_document(content, title, entity_hint, year_hint, doc_id)


def extract_financial_data(content: str, doc_id: int = None) -> dict:
    return get_client().extract_financial_data(content, doc_id)


def chat(
    messages: list,
    entity_name: str = "Personal",
    tax_year: str = "",
    context_docs: list = None,
) -> str:
    return get_client().chat(messages, entity_name, tax_year, context_docs)


def generate_summary(
    entity_name: str,
    year: str,
    documents: list,
    summary_data: dict = None,
) -> str:
    return get_client().generate_summary(entity_name, year, documents, summary_data)
