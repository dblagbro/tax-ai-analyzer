"""US Alliance Federal Credit Union — Playwright-based statement downloader.

Logs into the US Alliance online banking portal, navigates to eStatements,
and downloads monthly PDF statements for the requested years.

Each statement is saved to:
  <consume_path>/<entity_slug>/<year>/YYYY_MM_01_usalliance_statement.pdf

MFA handling: if an OTP prompt is detected the job enters `mfa_pending` state
and polls for a code delivered via the in-memory MFA registry (fed by the
/api/import/usalliance/mfa endpoint in web_ui.py).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── in-memory MFA exchange ────────────────────────────────────────────────────
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_filename(year: str, month: str, account_suffix: str = "") -> str:
    suffix = f"_{account_suffix}" if account_suffix else ""
    return f"{year}_{month:>02}_01_usalliance_statement{suffix}.pdf"


def _months_for_year(year: str) -> list[str]:
    """Return two-digit month strings 01..12, capped at current month if current year."""
    now = datetime.now()
    current_year = str(now.year)
    if year == current_year:
        return [f"{m:02d}" for m in range(1, now.month + 1)]
    return [f"{m:02d}" for m in range(1, 13)]


# ── main importer ─────────────────────────────────────────────────────────────

def run_import(
    username: str,
    password: str,
    years: list[str],
    consume_path: str,
    entity_slug: str,
    job_id: int,
    log: Callable[[str], None] = logger.info,
    cookies: Optional[list] = None,
) -> dict:
    """
    Drive the US Alliance portal with Playwright.

    If `cookies` is provided (list of cookie dicts exported from a real browser),
    they are injected and the login form is skipped entirely — bypassing bot detection.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    try:
        from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Add it to requirements.txt and rebuild."
        )

    imported = skipped = errors = 0

    # patchright replaces playwright + playwright-stealth: CDP Runtime.Enable
    # leak and driver-level fingerprint issues are patched at build time, so no
    # explicit stealth hook is needed here.
    # Reuse the shared Chrome launch args from base_bank_importer to keep
    # us in sync if the stealth config evolves (was duplicated pre-Phase-9).
    from app.importers.base_bank_importer import _STEALTH_ARGS

    with sync_playwright() as pw:
        log("Launching real Chrome via patchright (visible under Xvfb)…")
        browser = pw.chromium.launch(
            headless=False,
            channel="chrome",  # real Chrome binary, not bundled Chromium
            args=_STEALTH_ARGS,
        )
        context = browser.new_context(
            accept_downloads=True,
            no_viewport=True,  # let Xvfb framebuffer drive size
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )

        if cookies:
            log(f"Injecting {len(cookies)} browser cookies…")
            context.add_cookies(cookies)

        # MED-PASS2-2 fingerprint hardening — override navigator.webdriver to
        # undefined (patchright leaves it as boolean false by default).
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {"
            "get: () => undefined, configurable: true});"
            "Object.defineProperty(navigator, 'plugins', {"
            "get: () => [1,2,3,4,5], configurable: true});"
            "Object.defineProperty(navigator, 'languages', {"
            "get: () => ['en-US', 'en'], configurable: true});"
        )

        page = context.new_page()

        try:
            # ── 1. Login ──────────────────────────────────────────────────────
            base_url = "https://account.usalliance.org"
            if cookies:
                # Navigate to home first (not /dashboard) to let stealth settle
                log("Navigating to portal with injected cookies…")
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                # Now go to dashboard
                page.goto(f"{base_url}/dashboard", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                current_url = page.url
                log(f"Current URL: {current_url}")
                if "/login" in current_url:
                    log("Cookies expired — falling back to credential login…")
                    _fill_login(page, username, password, log)
                elif _is_404_page(page):
                    # Bot detection: portal is blocking the automated browser even with valid cookies.
                    # Save a debug screenshot and fall back to credential login.
                    _save_debug_screenshot(page, "bot_block_with_cookies")
                    log("⚠️  Portal blocked this browser session (bot detection). Trying credential login as fallback…")
                    page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    _fill_login(page, username, password, log)
                else:
                    log(f"Authenticated via cookies — at {current_url}")
            else:
                login_url = f"{base_url}/login"
                log(f"Navigating to {login_url}")
                page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=20000)
                page.wait_for_timeout(2000)
                log("Filling login credentials…")
                _fill_login(page, username, password, log)

            # ── 2. Handle MFA if triggered ────────────────────────────────────
            if _is_mfa_page(page):
                if _is_push_mfa_page(page):
                    mfa_url = page.url
                    log("📱 Push MFA detected — check your US Alliance app and tap Approve.")
                    log(f"⏳ Waiting up to 10 minutes (MFA page: {mfa_url})…")
                    _save_debug_screenshot(page, "mfa_push")
                    # Wait for URL to change away from the MFA/login page — happens
                    # automatically the moment the user approves in the app.
                    try:
                        page.wait_for_url(
                            lambda url: url != mfa_url and "/login" not in url,
                            timeout=600000,
                        )
                    except Exception:
                        _save_debug_screenshot(page, "mfa_push_timeout")
                        raise RuntimeError(
                            "MFA timeout — push notification not approved within 10 minutes. "
                            "Check your US Alliance app and try again."
                        )
                    # URL changed — user approved. Wait for page to settle (best-effort;
                    # SPA may never reach networkidle due to background API calls).
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    log(f"✅ Push MFA approved — now at {page.url}")
                else:
                    log("🔢 MFA code prompt detected — waiting for code (up to 5 minutes)…")
                    log("⚠️  ENTER YOUR OTP CODE in the MFA field on the import page.")
                    code = _wait_for_mfa(job_id, log, timeout=300)
                    if not code:
                        raise RuntimeError("MFA timeout — no code submitted within 5 minutes.")
                    log(f"MFA code received: {code[:2]}****")
                    _submit_mfa(page, code, log)

            # ── 3. Verify we're logged in ─────────────────────────────────────
            _verify_logged_in(page, log)

            # ── 3b. Start global response interceptor ─────────────────────────
            # Capture PDF responses from anywhere — page load, XHR, service worker.
            # US Alliance may pre-fetch PDFs before any click.
            session_pdf_cache: list[bytes] = []
            session_requests: list[str] = []

            def _global_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    url_l = resp.url.lower()
                    if ("pdf" in ct or "octet-stream" in ct or url_l.endswith(".pdf")
                            or "/pdf" in url_l or "download" in url_l):
                        data = resp.body()
                        if data and len(data) > 500:
                            log(f"  [intercept] {resp.status} {ct[:25]} {resp.url[:80]} ({len(data):,}B)")
                            session_pdf_cache.append(data)
                except Exception:
                    pass

            def _global_request(req):
                session_requests.append(f"[{req.method}] {req.url}")

            page.on("response", _global_response)
            page.on("request", _global_request)

            # ── 4. Navigate to eStatements ────────────────────────────────────
            log("Navigating to eStatements…")
            _navigate_to_estatements(page, log)

            # Log what the page fetched during initial load
            log(f"Page load requests: {len(session_requests)} total")
            for r in session_requests[-15:]:
                log(f"  {r[:120]}")
            session_requests.clear()

            # ── 5. Iterate years and download statements ──────────────────────
            for year in years:
                log(f"── Year {year} ──")
                try:
                    year_imported, year_skipped, year_errors = _download_year(
                        page, context, year, consume_path, entity_slug, log,
                        session_pdf_cache=session_pdf_cache,
                        session_requests=session_requests,
                    )
                    imported += year_imported
                    skipped += year_skipped
                    errors += year_errors
                except Exception as e:
                    log(f"Error processing year {year}: {e}")
                    errors += 1
                    try:
                        _navigate_to_estatements(page, log)
                        session_requests.clear()
                    except Exception:
                        pass

        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    log(f"Done — imported: {imported}, skipped: {skipped}, errors: {errors}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── login helpers ─────────────────────────────────────────────────────────────

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


# ── eStatements navigation ────────────────────────────────────────────────────

def _is_404_page(page) -> bool:
    """Return True if the page is an error/404 page.

    Uses specific multi-word phrases only — generic terms like 'not found',
    'return home', or '404' alone appear on real authenticated pages (footer
    nav, document IDs, search results, etc.) and cause false positives.
    """
    try:
        content = page.content().lower()
        specific_404_phrases = [
            "404 page not found",
            "404 - page not found",
            "404 | page not found",
            "error 404",
            "page not found",           # only as standalone phrase checked below
            "we couldn't find that page",
            "the page you requested could not be found",
            "this page doesn't exist",
        ]
        # Check URL too — some portals redirect to /404 or /error
        url = page.url.lower()
        if any(u in url for u in ["/404", "/not-found", "/error/404"]):
            return True
        # Only fire on very specific 404 phrases, not single words
        for phrase in specific_404_phrases:
            if phrase in content:
                # Extra guard: "page not found" appears on real pages in help text —
                # only count it if the page has very little unique content
                if phrase == "page not found":
                    # A real 404 page has little content; an authenticated page has lots
                    stripped = re.sub(r"<[^>]+>", " ", content)
                    words = [w for w in stripped.split() if len(w) > 3]
                    if len(words) < 150:
                        return True
                else:
                    return True
        return False
    except Exception:
        return False


def _is_documents_page(page) -> bool:
    """Return True if the page looks like the eStatements/documents listing."""
    try:
        content = page.content().lower()
        return any(p in content for p in [
            "regular statement", "documents & statements",
            "documents and statements", "estatements",
            "tax forms & notices", "send authorization request",
        ])
    except Exception:
        return False


def _navigate_to_estatements(page, log: Callable):
    """Navigate to the eStatements / Documents section.

    Confirmed URL: https://account.usalliance.org/documents/docs/cash-accounts
    This is a React SPA — content loads async after the shell, so we must
    wait for statement rows to appear, not just domcontentloaded.
    """
    base = _get_base_url(page)

    # ── Step 1: try known URLs in order ───────────────────────────────────────
    for path in [
        f"{base}/documents/docs/cash-accounts",   # confirmed URL
        f"{base}/documents",
        f"{base}/estatements",
        f"{base}/accounts/statements",
        f"{base}/banking/documents",
        f"{base}/statements",
    ]:
        try:
            log(f"Trying: {path}")
            page.goto(path, wait_until="domcontentloaded", timeout=20000)
            # SPA: wait for React to render statement rows (up to 15 s)
            _wait_for_documents_content(page, log, timeout=15000)
            if _is_documents_page(page):
                log(f"✓ Documents page at: {page.url}")
                _save_debug_screenshot(page, "documents_page")
                return
            log(f"  → not documents page (at {page.url})")
        except Exception as e:
            log(f"  → failed: {e}")

    # ── Step 2: nav link fallback ──────────────────────────────────────────────
    try:
        page.goto(f"{base}/dashboard", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(3000)
        links = page.query_selector_all("a[href]")
        log(f"Nav fallback: scanning {len(links)} links…")
        for lnk in links:
            href = lnk.get_attribute("href") or ""
            txt = (lnk.inner_text() or "").strip().lower()
            if any(k in href.lower() for k in ("document", "statement", "estatement")):
                if href.startswith("/"):
                    href = base + href
                log(f"Clicking nav link '{txt}' → {href}")
                page.goto(href, wait_until="domcontentloaded", timeout=15000)
                _wait_for_documents_content(page, log, timeout=12000)
                if _is_documents_page(page):
                    log(f"✓ Documents page via nav: {page.url}")
                    return
    except Exception as e:
        log(f"Nav link fallback failed: {e}")

    _save_debug_screenshot(page, "estatements_not_found")
    raise RuntimeError(
        "Could not find eStatements/documents page. "
        "Debug screenshot saved in container /tmp/usalliance_debug_*.png"
    )


def _wait_for_documents_content(page, log: Callable, timeout: int = 12000):
    """Wait for the SPA to render statement content after navigation."""
    # Try waiting for a known content indicator
    indicators = [
        'text="Regular Statement"',
        'text="Documents & Statements"',
        'text="Tax Forms & Notices"',
        '[class*="document" i]',
        '[class*="statement" i]',
    ]
    for sel in indicators:
        try:
            page.wait_for_selector(sel, timeout=timeout)
            return
        except Exception:
            pass
    # Fallback: just wait a fixed time for JS to run
    page.wait_for_timeout(3000)


# ── per-year statement download ───────────────────────────────────────────────

def _download_year(
    page, context, year: str, consume_path: str, entity_slug: str, log: Callable,
    session_pdf_cache: list = None, session_requests: list = None,
) -> tuple[int, int, int]:
    """Select the given year in the eStatements portal and download each statement."""
    from patchright.sync_api import TimeoutError as PWTimeout

    imported = skipped = errors = 0
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    log(f"Looking for year selector for {year}…")
    _select_statement_year(page, year, log)

    # Find all statement rows/links on the page
    statements = _find_statement_links(page, year, log)
    if not statements:
        log(f"No statements found for {year} — saving debug screenshot.")
        _save_debug_screenshot(page, f"no_statements_{year}")
        return 0, 0, 0

    log(f"Found {len(statements)} statement(s) for {year}.")

    for stmt in statements:
        month = stmt.get("month", "01")
        account = stmt.get("account", "")
        filename = _safe_filename(year, month, account)
        dest_path = dest_dir / filename

        if dest_path.exists():
            log(f"  SKIP (exists): {filename}")
            skipped += 1
            continue

        log(f"  Downloading: {filename}")
        try:
            if session_pdf_cache is not None:
                session_pdf_cache.clear()
            if session_requests is not None:
                session_requests.clear()
            pdf_bytes = _download_statement(page, context, stmt, log,
                                             session_pdf_cache=session_pdf_cache,
                                             session_requests=session_requests)
            if pdf_bytes:
                dest_path.write_bytes(pdf_bytes)
                log(f"  ✓ Saved {filename} ({len(pdf_bytes):,} bytes)")
                imported += 1
            else:
                log(f"  ✗ Empty PDF for {filename}")
                errors += 1
        except Exception as e:
            log(f"  ✗ Failed {filename}: {e}")
            errors += 1

    return imported, skipped, errors


def _select_statement_year(page, year: str, log: Callable):
    """Try to select the given year in the year dropdown or filter."""
    # Year dropdown selectors
    year_select_selectors = [
        'select[id*="year" i]',
        'select[name*="year" i]',
        'select:has(option[value*="{}"])'.format(year),
    ]
    for sel in year_select_selectors:
        try:
            el = page.query_selector(sel)
            if el:
                log(f"Selecting year {year} in dropdown…")
                el.select_option(value=year)
                page.wait_for_load_state("networkidle", timeout=10000)
                return
        except Exception:
            pass

    # Year link/tab
    year_link_selectors = [
        f'a:has-text("{year}")',
        f'button:has-text("{year}")',
        f'[data-year="{year}"]',
        f'li:has-text("{year}") a',
    ]
    for sel in year_link_selectors:
        try:
            el = page.query_selector(sel)
            if el:
                log(f"Clicking year link {year}…")
                el.click()
                page.wait_for_load_state("networkidle", timeout=10000)
                return
        except Exception:
            pass

    log(f"No year selector found — using currently visible statements for {year}.")


def _find_statement_links(page, year: str, log: Callable) -> list[dict]:
    """
    Scan the eStatements page for US Alliance statement rows.

    US Alliance shows all years on one page. Each row contains text like:
    "January 2025 Regular Statement" with a Material UI chevron_right icon.
    Rows are clickable list items (li, div[role=button], etc.).
    """
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    def _extract_month(text: str) -> str:
        for mi, mn in enumerate(month_names, 1):
            if mn.lower() in text.lower():
                return f"{mi:02d}"
        return "01"

    # ── Strategy 1: Material UI leaf-level buttons (no nested role=button) ────
    # The year-group container is ALSO a div[role="button"] and contains all the
    # individual rows. We must target the innermost buttons — i.e. those that do
    # NOT have role="button" children (leaf nodes).
    for sel in [
        # Leaf-level: has the text but does NOT contain nested role=button
        f'div[role="button"]:has-text("Regular Statement"):not(:has(div[role="button"]))',
        f'li:has-text("Regular Statement"):not(:has(li))',
        f'[role="button"]:has-text("Regular Statement"):not(:has([role="button"]))',
        # Fallbacks without :not (broader)
        f'div[role="button"]:has-text("{year}"):has-text("Regular Statement")',
        f'li:has-text("{year}"):has-text("Regular Statement")',
    ]:
        try:
            rows = page.query_selector_all(sel)
            if rows:
                log(f"Strategy 1: {len(rows)} candidates via '{sel}'")
                statements = []
                seen_months = set()
                for row in rows:
                    text = (row.text_content() or "").strip()
                    # Skip if this element's text contains many months (year-group container)
                    month_count = sum(1 for mn in month_names if mn.lower() in text.lower())
                    if month_count > 1:
                        continue
                    # Skip if wrong year
                    if year not in text:
                        continue
                    month = _extract_month(text)
                    if month not in seen_months:
                        seen_months.add(month)
                        statements.append({"month": month, "account": "", "element": row, "text": text})
                        log(f"  Found: {text[:80]}")
                if statements:
                    return statements
        except Exception as e:
            log(f"Strategy 1 selector '{sel}' error: {e}")

    # ── Strategy 2: JS tree walk — find the smallest clickable ancestor of the text ─
    log("Strategy 1 found nothing — trying JS tree walk…")
    try:
        js_result = page.evaluate(f"""
            () => {{
                const year = "{year}";
                const results = [];
                const seen = new Set();

                // Walk every element looking for text matching "Month YEAR Regular Statement"
                const all = document.querySelectorAll('*');
                for (const el of all) {{
                    // Only look at leaf-ish elements (< 300 chars own text)
                    const ownText = (el.textContent || '').trim();
                    if (!ownText.includes(year) || !ownText.toLowerCase().includes('regular statement')) continue;
                    if (ownText.length > 300) continue;

                    // Walk up to find the nearest clickable ancestor
                    let target = el;
                    let found = el;
                    while (target && target !== document.body) {{
                        const tag = target.tagName;
                        const role = target.getAttribute('role') || '';
                        const tab = target.getAttribute('tabindex') || '';
                        if (tag === 'LI' || tag === 'A' || tag === 'BUTTON' ||
                            role === 'button' || role === 'listitem' || role === 'menuitem' || tab === '0') {{
                            found = target;
                        }}
                        target = target.parentElement;
                    }}

                    const key = found.className + '_' + ownText;
                    if (!seen.has(key)) {{
                        seen.add(key);
                        results.push({{
                            text: ownText,
                            tag: found.tagName,
                            role: found.getAttribute('role'),
                            cls: (found.className || '').substring(0, 80),
                        }});
                    }}
                }}
                return results;
            }}
        """)
        if js_result:
            log(f"JS walk found {len(js_result)} candidate(s):")
            for r in js_result[:5]:
                log(f"  [{r['tag']} role={r['role']}] cls={r['cls']} text={r['text'][:80]}")
        else:
            log("JS walk found 0 candidates — dumping page structure for debug…")
            _dump_page_structure(page, year, log)
    except Exception as e:
        log(f"JS walk failed: {e}")

    # ── Strategy 3: broad :has-text("Regular Statement") scan ─────────────────
    log("Strategy 3: broad 'Regular Statement' scan…")
    try:
        # Get all elements containing the phrase, pick those also containing year
        candidates = page.query_selector_all(':has-text("Regular Statement")')
        log(f"  Broad scan found {len(candidates)} elements with 'Regular Statement'")
        statements = []
        seen_months = set()
        for el in candidates:
            text = (el.text_content() or "").strip()
            if year not in text:
                continue
            if len(text) > 300:
                continue  # skip container elements
            month = _extract_month(text)
            if month in seen_months:
                continue
            seen_months.add(month)
            log(f"  Candidate [{el.tag_name()}]: {text[:80]}")
            statements.append({"month": month, "account": "", "element": el, "text": text})
        if statements:
            return statements
    except Exception as e:
        log(f"Strategy 3 failed: {e}")

    return []


def _dump_page_structure(page, year: str, log: Callable):
    """Log a summary of the page DOM structure to help debug statement detection."""
    try:
        summary = page.evaluate(f"""
            () => {{
                const year = "{year}";
                const out = [];
                // Find any text node mentioning the year
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while (node = walker.nextNode()) {{
                    const t = node.textContent.trim();
                    if (t.includes(year) && t.length < 200) {{
                        const p = node.parentElement;
                        out.push(p ? p.tagName + '.' + (p.className||'').split(' ')[0] + ': ' + t : t);
                    }}
                }}
                return out.slice(0, 20);
            }}
        """)
        log(f"DOM text nodes containing '{year}': {summary}")
    except Exception as e:
        log(f"DOM dump failed: {e}")


def _download_statement(page, context, stmt: dict, log: Callable,
                         session_pdf_cache: list = None,
                         session_requests: list = None) -> Optional[bytes]:
    """Click a statement row and capture the resulting PDF.

    US Alliance opens a PDF viewer a few seconds after clicking a row.
    Uses a global response interceptor (started at login time) plus
    per-click interception as backup.
    """
    from patchright.sync_api import TimeoutError as PWTimeout

    el = stmt.get("element")
    if not el:
        return None

    pre_click_url = page.url
    new_pages: list = []

    def _on_page(p):
        new_pages.append(p)

    context.on("page", _on_page)

    try:
        # ── Scroll element into view and take pre-click screenshot ───────────
        try:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
        except Exception:
            pass

        box = el.bounding_box()
        log(f"  Element box: {box}  text: {stmt.get('text','')[:60]}")
        _save_debug_screenshot(page, f"pre_click_{stmt.get('month','xx')}")

        # ── Click via mouse coordinates (most reliable for React apps) ───────
        try:
            if box and box["width"] > 0 and box["height"] > 0:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.click(cx, cy)
                log(f"  Mouse click at ({cx:.0f},{cy:.0f})")
            else:
                log("  No bounding box — using el.click()")
                el.click()
        except Exception as e:
            log(f"  Click error: {e} — trying dispatch_event")
            try:
                el.dispatch_event("click")
            except Exception:
                pass

        # Wait for response
        time.sleep(10)
        _save_debug_screenshot(page, f"post_click_{stmt.get('month','xx')}")
        log(f"  URL after click: {page.url}")

        # Log all requests that happened during this window
        if session_requests is not None:
            log(f"  Requests during click ({len(session_requests)}): {session_requests[-10:]}")

        # ── Check global session PDF cache (catches pre-fetched / click PDFs) ─
        if session_pdf_cache:
            log(f"  ✓ PDF from session cache ({len(session_pdf_cache[0]):,} bytes)")
            return session_pdf_cache.pop(0)

        # Check for blob/data URI or iframe PDF in page
        pdf_src = page.evaluate("""
            () => {
                const sels = ['iframe','embed','object','canvas'];
                const out = [];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        const src = el.src || el.data || '';
                        if (src) out.push(el.tagName + ':' + src.substring(0, 150));
                    }
                }
                // Also look for any anchor with pdf href
                for (const a of document.querySelectorAll('a[href]')) {
                    if (a.href.toLowerCase().includes('pdf') || a.href.startsWith('blob:'))
                        out.push('A:' + a.href.substring(0,150));
                }
                return out;
            }
        """)
        log(f"  DOM PDF sources: {pdf_src}")

        if pdf_src:
            for src_entry in pdf_src:
                src = src_entry.split(":", 1)[1] if ":" in src_entry else src_entry
                if src.startswith("blob:") or "pdf" in src.lower():
                    try:
                        resp = page.request.get(src)
                        data = resp.body()
                        if data and len(data) > 500:
                            log(f"  ✓ Fetched from DOM source ({len(data):,} bytes)")
                            _nav_back_to_statements(page, log)
                            return data
                    except Exception as e:
                        log(f"  DOM source fetch failed: {e}")

        # ── Direct download event check (second click attempt) ───────────────
        try:
            with page.expect_download(timeout=3000) as dl_info:
                el.click()  # second click attempt
            download = dl_info.value
            buf = io.BytesIO()
            stream = download.create_read_stream()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf.write(chunk)
            if buf.tell() > 0:
                log("  ✓ Direct download event")
                return buf.getvalue()
        except PWTimeout:
            pass  # no download event — SPA viewer
        except Exception as e:
            log(f"  Download event error: {e}")

        # ── New tab? ──────────────────────────────────────────────────────────
        if new_pages:
            new_page = new_pages[0]
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                new_url = new_page.url
                log(f"  New tab: {new_url}")
                time.sleep(3)
                if session_pdf_cache:
                    data = session_pdf_cache.pop(0)
                    log(f"  ✓ PDF from session cache (new tab context) ({len(data):,}B)")
                    new_page.close()
                    return data
                if new_url.lower().endswith(".pdf") or "pdf" in new_url.lower():
                    resp = new_page.request.get(new_url)
                    data = resp.body()
                    new_page.close()
                    if data:
                        log("  ✓ Fetched PDF from new tab URL")
                        return data
                pdf_bytes = new_page.pdf()
                new_page.close()
                if pdf_bytes:
                    log("  ✓ Rendered new tab as PDF")
                    return pdf_bytes
            except Exception as e:
                log(f"  New tab failed: {e}")
                try:
                    new_pages[0].close()
                except Exception:
                    pass

        # ── Final session cache check (delayed fetch) ─────────────────────────
        if session_pdf_cache:
            data = session_pdf_cache.pop(0)
            log(f"  ✓ PDF from session cache (delayed) ({len(data):,}B)")
            _nav_back_to_statements(page, log)
            return data

        current_url = page.url
        if current_url != pre_click_url:
            _nav_back_to_statements(page, log)

    finally:
        try:
            context.remove_listener("page", _on_page)
        except Exception:
            pass

    log("  ✗ All download attempts failed")
    return None


def _nav_back_to_statements(page, log: Callable):
    """Navigate back to the statements page after opening a PDF viewer."""
    try:
        page.go_back(wait_until="domcontentloaded", timeout=10000)
        _wait_for_documents_content(page, log, timeout=8000)
    except Exception:
        try:
            base = _get_base_url(page)
            page.goto(f"{base}/documents/docs/cash-accounts",
                      wait_until="domcontentloaded", timeout=15000)
            _wait_for_documents_content(page, log, timeout=10000)
        except Exception:
            pass


# ── utility helpers ───────────────────────────────────────────────────────────

def _find_element(page, selectors: list[str]):
    """Return the first matching element or None."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            pass
    return None


def _get_base_url(page) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(page.url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _save_debug_screenshot(page, label: str):
    """Save a PNG screenshot to /tmp/ for debugging."""
    try:
        ts = datetime.now().strftime("%H%M%S")
        path = f"/tmp/usalliance_debug_{label}_{ts}.png"
        page.screenshot(path=path)
        logger.info(f"Debug screenshot saved: {path}")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")
