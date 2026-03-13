"""AI-powered document categorization."""
import json
import logging
import re
from app.llm_client import analyze_document

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a tax document classifier. Analyze the provided document text and return a JSON object with these exact fields:

{
  "doc_type": "<one of: W-2, 1099-NEC, 1099-K, 1099-INT, 1099-DIV, 1099-MISC, invoice, receipt, utility_bill, mortgage_statement, insurance, bank_statement, property_tax, vehicle, equipment, subscription, charitable_donation, medical, farm_expense, other>",
  "category": "<one of: income, deduction, expense, asset, other>",
  "entity": "<one of: personal, voipguru, martinfeld_ranch>",
  "tax_year": "<4-digit year or null>",
  "vendor": "<vendor/payer name or null>",
  "amount": <dollar amount as number or null>,
  "description": "<one-sentence description>",
  "confidence": <0.0 to 1.0>,
  "tags": ["list", "of", "relevant", "tags"]
}

For entity assignment:
- personal: personal expenses, W-2 income, personal bills (utilities, mortgage, medical)
- voipguru: consulting/telecom business income and expenses, tech equipment, hosting, software
- martinfeld_ranch: farm income and expenses, agricultural equipment, feed, fuel, livestock

Return ONLY the JSON object, no other text."""


def categorize(content: str, filename: str = "") -> dict:
    """Return categorization dict for a document."""
    prompt = f"Filename: {filename}\n\nDocument content:\n{content[:8000]}"
    try:
        response = analyze_document(prompt, SYSTEM_PROMPT, max_tokens=500)
        # Extract JSON from response
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        logger.error(f"Categorization failed: {e}")

    # Fallback
    return {
        "doc_type": "other",
        "category": "other",
        "entity": "personal",
        "tax_year": None,
        "vendor": None,
        "amount": None,
        "description": f"Document: {filename}",
        "confidence": 0.1,
        "tags": ["needs_review"],
    }
