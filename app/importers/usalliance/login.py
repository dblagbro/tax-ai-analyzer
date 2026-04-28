"""Login form fill + post-login verification.

Extracted from the original 1,132-line ``app/importers/usalliance_importer.py``
during Phase 11G refactor. The module-level public API ``run_import`` +
``set_mfa_code`` is preserved via the package ``__init__`` so existing
callers (``app/routes/importers/import_usalliance.py``) keep working.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable

logger = logging.getLogger(__name__)


def _fill_login(page, username: str, password: str, log: Callable):
    """Fill the US Alliance login form with human-like typing."""
    from patchright.sync_api import TimeoutError as PWTimeout
    import random

    # account.usalliance.org uses randomised input name/id — only type selectors work
    user_selectors = [
        'input[name="username"]',
        'input[id*="username" i]',
        'input[id*="user" i]',
        'input[placeholder*="username" i]',
        'input[placeholder*="user id" i]',
        'input[type="text"]:visible',
    ]
    pw_selectors = [
        'input[name="password"]',
        'input[id*="password" i]',
        'input[type="password"]:visible',
    ]

    user_field = _find_element(page, user_selectors)
    if not user_field:
        _save_debug_screenshot(page, "login_page_no_user_field")
        raise RuntimeError("Could not find username field on login page")

    log("Typing username…")
    user_field.click()
    page.wait_for_timeout(300)
    # Type character-by-character with random delays (more human-like)
    for char in username:
        user_field.press(char)
        page.wait_for_timeout(random.randint(50, 150))

    pw_field = _find_element(page, pw_selectors)
    if not pw_field:
        # Two-step: click Next/Continue
        for sel in ['button[type="submit"]', 'button:has-text("Next")', 'button:has-text("Continue")']:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                log("Clicking Next (two-step login)…")
                btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1000)
                break
        pw_field = _find_element(page, pw_selectors)

    if not pw_field:
        _save_debug_screenshot(page, "after_username_no_pw")
        raise RuntimeError("Could not find password field")

    log("Typing password…")
    pw_field.click()
    page.wait_for_timeout(300)
    for char in password:
        pw_field.press(char)
        page.wait_for_timeout(random.randint(50, 150))

    page.wait_for_timeout(500)

    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log In")',
        'button:has-text("Log in")',
        'button:has-text("Sign In")',
        'button:has-text("Login")',
    ]
    submit_btn = _find_element(page, submit_selectors)
    if submit_btn:
        log("Submitting login form…")
        # Move mouse to button then click (more human-like)
        submit_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        submit_btn.click()
    else:
        log("Pressing Enter to submit…")
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)



def _verify_logged_in(page, log: Callable):
    """Raise if we don't appear to be on an authenticated page."""
    url = page.url
    log(f"Post-login URL: {url}")

    # If still on login page, the login failed
    if "/login" in url:
        content = page.content()
        if "unable to log" in content.lower():
            _save_debug_screenshot(page, "bot_detection")
            raise RuntimeError(
                "Login blocked by bot detection. The credit union is rejecting automated logins "
                "from this server's IP address. Use cookie-based authentication instead: "
                "log in from your real browser, export cookies as JSON, and paste them in "
                "the 'Browser Cookies' field on the import page."
            )
        _save_debug_screenshot(page, "login_failed")
        raise RuntimeError(
            f"Login failed — still on login page ({url}). "
            "Check credentials or use cookie authentication."
        )

    # Check for 404/error page — can mean bot detection even after stealth
    if _is_404_page(page):
        _save_debug_screenshot(page, "dashboard_404")
        raise RuntimeError(
            "The portal is still blocking this browser session (bot detection). "
            "The stealth mode may not be sufficient for this portal. "
            "Try again — each run uses slightly different fingerprints. "
            "If it keeps failing, the portal may require a CAPTCHA or device trust check."
        )

    log("Login successful.")

    # Auto-save cookies so the next import can skip MFA entirely. Uses the
    # shared base_bank_importer.save_auth_cookies helper so other bank
    # importers can adopt the same pattern with a one-line change.
    # NOTE: per bug-statement-download-usalliance.md Finding A, US Alliance's
    # session model is not pure cookie-based — cookies persist but the
    # server-side session also expires quickly. Save them anyway for cases
    # where the gap between runs is short enough.
    from app.importers.base_bank_importer import save_auth_cookies
    save_auth_cookies(page.context, "usalliance", log)

