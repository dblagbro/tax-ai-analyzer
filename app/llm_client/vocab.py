"""Document type vocabularies and model fallback chains."""

VALID_DOC_TYPES = {
    "W-2", "1099-NEC", "1099-K", "1099-INT", "1099-DIV", "1099-MISC",
    "invoice", "receipt", "utility_bill", "bank_statement", "mortgage_statement",
    "property_tax", "vehicle", "equipment", "subscription", "charitable_donation",
    "medical", "farm_expense", "paypal_transaction", "venmo_transaction",
    "credit_card_statement", "insurance", "capital_improvement", "other",
}

VALID_CATEGORIES = {"income", "expense", "deduction", "asset", "other"}

VALID_ENTITIES = {"personal", "voipguru", "martinfeld_ranch"}

# Direct-SDK fallback chain (only reached when the proxy pool is exhausted).
# 2026-09-06: Claude 5 family. Ordered best-to-cheapest so a rate-limited
# flagship degrades gracefully rather than failing the call.
ANTHROPIC_FALLBACK_CHAIN = [
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
]

OPENAI_FALLBACK_CHAIN = [
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]
