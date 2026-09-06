"""Gmail AI review must (a) not NameError on _AI_TIMEOUT, (b) gate on
has_llm_capability rather than the direct key, (c) never call the direct SDK
with an empty key when the proxy pool is exhausted, and (d) fall back to the
rules triage — not "import everything" — in both no-LLM and LLM-error cases.
Also covers the GMAIL_SCOPES import that broke get_credentials(). (2026-09-06)"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.importers.gmail import ai_review  # noqa: E402
from app.importers.gmail.rules_review import rules_review_email, extract_amount  # noqa: E402


def test_ai_timeout_is_imported():
    assert isinstance(ai_review._AI_TIMEOUT, (int, float)) and ai_review._AI_TIMEOUT > 0


def test_gmail_scopes_imported_in_auth():
    from app.importers.gmail import auth
    assert isinstance(auth.GMAIL_SCOPES, list) and auth.GMAIL_SCOPES


def test_no_capability_uses_rules_without_calling_proxy():
    from app.llm_client import proxy_call
    with patch("app.llm_client.has_llm_capability", return_value=False), \
         patch.object(proxy_call, "call_anthropic_messages",
                      side_effect=AssertionError("must not be called")):
        keep = ai_review._ai_review_email("Your Google Play Order Receipt", "Google <noreply@google.com>",
                                          "Total: $2.15", "2023-01-28", log_fn=lambda m: None)
        drop = ai_review._ai_review_email("Your Amazon.com order has shipped", "Amazon <ship@amazon.com>",
                                          "Track your package", "2023-01-28", log_fn=lambda m: None)
    assert keep["relevant"] is True and keep["amount"] == 2.15
    assert keep["reason"].startswith("no LLM capability")
    assert drop["relevant"] is False


def test_proxy_exhausted_and_no_direct_key_falls_back_to_rules():
    from app.llm_client import proxy_call
    import anthropic
    with patch("app.llm_client.has_llm_capability", return_value=True), \
         patch.object(proxy_call, "call_anthropic_messages",
                      side_effect=proxy_call.NoProxyAvailable("pool exhausted")), \
         patch.object(anthropic, "Anthropic", side_effect=AssertionError("direct SDK must not be used")), \
         patch("app.db.get_setting", return_value=""), \
         patch("app.config.LLM_API_KEY", ""):
        r = ai_review._ai_review_email("Your Corporate Card Statement is Ready", "Amex <x@aexp.com>",
                                       "View your statement", "2023-01-28", log_fn=lambda m: None)
    assert r["relevant"] is True and r["doc_type"] == "statement"
    assert r["reason"].startswith("AI error")


# ── rules triage against the real January-2023 log examples ────────────────

def _rr(subject, snippet="", sender="x <x@y.com>"):
    return rules_review_email(subject, sender, snippet, "2023-01-28")


def test_rules_keep_cases():
    assert _rr("Your Corporate Card Statement is Ready")["relevant"]
    assert _rr("Your payment has posted")["relevant"]
    assert _rr("Your Google Play Order Receipt from Jan 27", "Total: $2.15")["amount"] == 2.15
    assert _rr("Your PayPal Extras World Mastercard monthly statement is ready")["relevant"]
    assert _rr("Your Payment Reminder", "Minimum due $4,375.09")["amount"] == 4375.09
    assert _rr("Payment confirmation")["relevant"]
    assert _rr("Receipt for your payment to PayPal Credit")["relevant"]
    assert _rr("Your 1099-NEC is available")["doc_type"] == "tax_notice"
    # weak subject only counts with an amount
    assert _rr("Your Amazon.com order #113-1675470-3862613", "Order Total: $45.67")["relevant"]
    assert not _rr("Your Amazon.com order #113-1675470-3862613", "Order confirmation")["relevant"]


def test_rules_drop_cases():
    assert not _rr("Devin's Benefits: could you be earning more cash back?")["relevant"]
    assert not _rr("How was your ship to home experience?")["relevant"]
    assert not _rr("Your Amazon.com order #111-1751462-3399413 has shipped")["relevant"]
    assert not _rr("Use your card to get a $5 Reward!", "Earn $5")["relevant"]  # amount but drop wins
    assert not _rr("The Evolution of our social club")["relevant"]
    assert not _rr("You're Invited to the Amcrest Product Launch")["relevant"]
    assert not _rr("Don't miss the opportunity to access funds from your credit line")["relevant"]


def test_rules_unmistakable_beats_drop_vocabulary():
    # "Statement" is unmistakable even when the subject also says "offer"
    assert _rr("Your statement is ready — plus a special offer inside")["relevant"]


def test_extract_amount_forms():
    assert extract_amount("USD 12.34") == 12.34
    assert extract_amount("$1,234.56 charged") == 1234.56
    assert extract_amount("no money here") is None
    assert extract_amount("", "Total $9.99") == 9.99
