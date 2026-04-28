"""MFA code exchange + MFA page detection for US Alliance login.

Extracted from the original 1,132-line ``app/importers/usalliance_importer.py``
during Phase 11G refactor. The module-level public API ``run_import`` +
``set_mfa_code`` is preserved via the package ``__init__`` so existing
callers (``app/routes/importers/import_usalliance.py``) keep working.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# job_id → {"code": str | None, "expires": float}
_mfa_registry: dict[int, dict] = {}


def set_mfa_code(job_id: int, code: str):
    _mfa_registry[job_id] = {"code": code, "expires": time.time() + 300}


def _wait_for_mfa(job_id: int, log: Callable, timeout: int = 300) -> Optional[str]:
    """Block until a MFA code is submitted via set_mfa_code, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _mfa_registry.get(job_id)
        if entry and entry.get("code") and time.time() < entry["expires"]:
            code = entry["code"]
            _mfa_registry.pop(job_id, None)
            return code
        time.sleep(2)
    return None


def _is_mfa_page(page) -> bool:
    """Return True if the current page looks like any kind of MFA challenge."""
    try:
        content = page.content().lower()
        # OTP input field selectors
        otp_selectors = [
            'input[name*="otp" i]',
            'input[id*="otp" i]',
            'input[placeholder*="verification" i]',
            'input[placeholder*="one-time" i]',
            'input[placeholder*="security code" i]',
            '[class*="mfa" i]',
            '[class*="two-factor" i]',
        ]
        for sel in otp_selectors:
            if page.query_selector(sel):
                return True
        # Push / app-based MFA text indicators (no input field)
        push_phrases = [
            # US Alliance specific
            "authorization request has been sent",
            "authorize to proceed",
            "verify your identity",
            "send authorization request",
            # Generic
            "push notification", "approve on your", "open your app",
            "notification has been sent", "approve the request",
            "check your device", "sent a notification", "tap approve",
            "authenticate with your", "confirm on your", "verify on your",
            "one-touch", "sent to your registered",
        ]
        if any(p in content for p in push_phrases):
            return True
        # Generic MFA text (fallback)
        if any(p in content for p in ["verification code", "one-time password", "security code"]):
            return True
    except Exception:
        pass
    return False



def _is_push_mfa_page(page) -> bool:
    """Return True if this is a push/app-based MFA (no code to type)."""
    try:
        content = page.content().lower()
        push_phrases = [
            # US Alliance specific
            "authorization request has been sent",
            "authorize to proceed",
            "verify your identity",
            "send authorization request",
            # Generic push MFA phrases
            "push notification", "approve on your", "open your app",
            "notification has been sent", "approve the request",
            "check your device", "sent a notification", "tap approve",
            "sent to your device", "sent to your registered",
        ]
        if any(p in content for p in push_phrases):
            # Only push if there's no OTP input field on the page
            has_input = any(
                page.query_selector(s) for s in [
                    'input[name*="otp" i]', 'input[id*="otp" i]',
                    'input[placeholder*="code" i]', 'input[type="number"]',
                ]
            )
            return not has_input
    except Exception:
        pass
    return False



def _submit_mfa(page, code: str, log: Callable):
    """Type the OTP code and submit."""
    from patchright.sync_api import TimeoutError as PWTimeout

    otp_selectors = [
        'input[name*="otp" i]',
        'input[id*="otp" i]',
        'input[placeholder*="code" i]',
        'input[placeholder*="verification" i]',
        'input[type="text"]:visible',
        'input[type="number"]:visible',
    ]
    otp_field = _find_element(page, otp_selectors)
    if not otp_field:
        _save_debug_screenshot(page, "mfa_page")
        raise RuntimeError("Could not find OTP input field")

    otp_field.fill(code)

    submit = _find_element(page, [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button:has-text("Continue")',
    ])
    if submit:
        submit.click()
    else:
        otp_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=20000)
    log("MFA submitted.")

