"""Top-level ``run_import`` orchestrator — coordinates login → MFA → download.

Extracted from the original 1,132-line ``app/importers/usalliance_importer.py``
during Phase 11G refactor. The module-level public API ``run_import`` +
``set_mfa_code`` is preserved via the package ``__init__`` so existing
callers (``app/routes/importers/import_usalliance.py``) keep working.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from app.importers.usalliance.login import _fill_login, _verify_logged_in
from app.importers.usalliance.mfa import (
    _wait_for_mfa,
    _is_mfa_page,
    _is_push_mfa_page,
    _submit_mfa,
)
from app.importers.usalliance.estatements import (
    _is_404_page,
    _navigate_to_estatements,
)
from app.importers.usalliance.download import _download_year
from app.importers.usalliance.helpers import _save_debug_screenshot

logger = logging.getLogger(__name__)


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

        # NOTE: prior commit added context.add_init_script() to override
        # navigator.webdriver. 2026-04-24 fingerprint probe proved patchright
        # silently suppresses runtime JS injection (by design — injection
        # itself is a detectable fingerprint). The override didn't take
        # effect; webdriver stays at boolean `false`. See base_bank_importer.py
        # for full rationale + the kept-for-discoverability comment.

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
