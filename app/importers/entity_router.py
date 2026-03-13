"""Map email sender/subject/amount to entity slug."""
import re
import json
from app import db


# Default domain → entity rules
DEFAULT_RULES = {
    "spectrum.net": "personal",
    "charter.com": "personal",
    "centralhudsononline.com": "personal",
    "verizon.com": "personal",
    "paracogas.com": "personal",
    "amazon.com": "personal",
    "ebay.com": "personal",
    "wellsfargo.com": "personal",
    "usalliance.org": "personal",
    "irs.gov": "personal",
    "turbotax.com": "personal",
    "microsoft.com": "voipguru",
    "cloudflare.com": "voipguru",
    "aws.amazon.com": "voipguru",
    "twilio.com": "voipguru",
    "woodridgeny.org": "personal",
    "tractorsupply.com": "martinfeld_ranch",
    "agway.com": "martinfeld_ranch",
}

FARM_KEYWORDS = ["farm", "ranch", "martinfeld", "feed", "seed", "tractor",
                 "equipment", "fuel", "livestock", "agway", "hay", "fertilizer"]

BIZ_KEYWORDS = ["voipguru", "voip", "sip", "pbx", "telecom", "hosting",
                "cloudflare", "twilio", "aws", "azure", "microsoft"]


def get_entity_slug(sender: str = "", subject: str = "", description: str = "") -> str:
    """Return best-match entity slug for a transaction."""
    # Load custom rules from DB settings
    try:
        settings = db.get_settings()
        custom_rules_json = settings.get("entity_routing_rules", "{}")
        custom_rules = json.loads(custom_rules_json)
    except Exception:
        custom_rules = {}

    rules = {**DEFAULT_RULES, **custom_rules}
    text = f"{sender} {subject} {description}".lower()

    # Exact domain match
    for domain, slug in rules.items():
        if domain.lower() in text:
            return slug

    # Keyword matching
    if any(kw in text for kw in FARM_KEYWORDS):
        return "martinfeld_ranch"
    if any(kw in text for kw in BIZ_KEYWORDS):
        return "voipguru"

    return "personal"
