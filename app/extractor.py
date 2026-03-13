"""Extract structured financial data from document content."""
import json
import logging
import re
from datetime import datetime
from app.llm_client import analyze_document

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """Extract financial data from this tax document. Return a JSON object:

{
  "date": "YYYY-MM-DD or null",
  "amount_total": <number or null>,
  "amount_tax": <number or null>,
  "amount_federal_withheld": <number or null>,
  "amount_state_withheld": <number or null>,
  "payer_name": "string or null",
  "payer_ein": "XX-XXXXXXX or null",
  "recipient_name": "string or null",
  "recipient_ssn_last4": "XXXX or null",
  "account_number": "string or null",
  "memo": "string or null",
  "line_items": [{"description": "...", "amount": 0.00}]
}

Return ONLY the JSON object."""


def extract_amounts(text: str) -> list[float]:
    """Regex fallback to find dollar amounts."""
    pattern = r'\$\s*([\d,]+(?:\.\d{2})?)'
    matches = re.findall(pattern, text)
    return [float(m.replace(",", "")) for m in matches]


def extract_dates(text: str) -> list[str]:
    patterns = [
        r'\b(\d{4}[-/]\d{2}[-/]\d{2})\b',
        r'\b(\d{1,2}[-/]\d{1,2}[-/]\d{4})\b',
        r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b',
    ]
    results = []
    for p in patterns:
        results.extend(re.findall(p, text, re.IGNORECASE))
    return results


def extract(content: str) -> dict:
    """Use AI + regex to extract structured data."""
    prompt = f"Document content:\n{content[:8000]}"
    try:
        response = analyze_document(prompt, EXTRACT_PROMPT, max_tokens=800)
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return data
    except Exception as e:
        logger.error(f"AI extraction failed: {e}, using regex fallback")

    # Regex fallback
    amounts = extract_amounts(content)
    dates = extract_dates(content)
    return {
        "date": dates[0] if dates else None,
        "amount_total": max(amounts) if amounts else None,
        "amount_tax": None,
        "payer_name": None,
        "memo": None,
        "line_items": [],
    }
