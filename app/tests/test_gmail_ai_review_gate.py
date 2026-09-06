"""Gmail AI review must (a) not NameError on _AI_TIMEOUT, (b) gate on
has_llm_capability rather than the direct key, (c) never call the direct SDK
with an empty key when the proxy pool is exhausted. (2026-09-06)"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.importers.gmail import ai_review  # noqa: E402


def test_ai_timeout_is_imported():
    assert isinstance(ai_review._AI_TIMEOUT, (int, float)) and ai_review._AI_TIMEOUT > 0


def test_no_capability_returns_default_without_calling_proxy():
    from app.llm_client import proxy_call
    with patch("app.llm_client.has_llm_capability", return_value=False), \
         patch.object(proxy_call, "call_anthropic_messages",
                      side_effect=AssertionError("must not be called")):
        r = ai_review._ai_review_email("Subj", "a@b.c", "body", "2023-03-01", log_fn=lambda m: None)
    assert r["relevant"] is True and r["reason"] == "no LLM capability"


def test_proxy_exhausted_and_no_direct_key_defaults_to_import():
    from app.llm_client import proxy_call
    import anthropic
    with patch("app.llm_client.has_llm_capability", return_value=True), \
         patch.object(proxy_call, "call_anthropic_messages",
                      side_effect=proxy_call.NoProxyAvailable("pool exhausted")), \
         patch.object(anthropic, "Anthropic", side_effect=AssertionError("direct SDK must not be used")), \
         patch("app.db.get_setting", return_value=""), \
         patch("app.config.LLM_API_KEY", ""):
        r = ai_review._ai_review_email("Subj", "a@b.c", "body", "2023-03-01", log_fn=lambda m: None)
    assert r["relevant"] is True and r["reason"] == "error"
