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

ANTHROPIC_FALLBACK_CHAIN = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-3-haiku-20240307",
]

OPENAI_FALLBACK_CHAIN = [
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]
