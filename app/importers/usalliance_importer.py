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
) -> dict:
    """
    Drive the US Alliance portal with Playwright.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Add it to requirements.txt and rebuild."
        )

    imported = skipped = errors = 0

    with sync_playwright() as pw:
        log("Launching headless Chromium…")
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            # ── 1. Navigate to online banking login ───────────────────────────
            login_url = "https://onlinebanking.usalliance.org/"
            log(f"Navigating to {login_url}")
            page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)

            log("Filling login credentials…")
            _fill_login(page, username, password, log)

            # ── 2. Handle MFA if triggered ────────────────────────────────────
            if _is_mfa_page(page):
                log("MFA prompt detected — waiting for code (up to 5 minutes)…")
                log("⚠️  ENTER YOUR OTP CODE in the MFA field on the import page.")
                code = _wait_for_mfa(job_id, log, timeout=300)
                if not code:
                    raise RuntimeError("MFA timeout — no code submitted within 5 minutes.")
                log(f"MFA code received: {code[:2]}****")
                _submit_mfa(page, code, log)

            # ── 3. Verify we're logged in ─────────────────────────────────────
            _verify_logged_in(page, log)

            # ── 4. Navigate to eStatements ────────────────────────────────────
            log("Navigating to eStatements…")
            _navigate_to_estatements(page, log)

            # ── 5. Iterate years and download statements ──────────────────────
            for year in years:
                log(f"── Year {year} ──")
                try:
                    year_imported, year_skipped, year_errors = _download_year(
                        page, context, year, consume_path, entity_slug, log
                    )
                    imported += year_imported
                    skipped += year_skipped
                    errors += year_errors
                except Exception as e:
                    log(f"Error processing year {year}: {e}")
                    errors += 1
                    # Try to recover by re-navigating to eStatements
                    try:
                        _navigate_to_estatements(page, log)
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
    """Fill the US Alliance login form. Handles single-page and two-step flows."""
    from playwright.sync_api import TimeoutError as PWTimeout

    # Common selectors for username field
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
        # Save screenshot for debugging
        _save_debug_screenshot(page, "login_page")
        raise RuntimeError("Could not find username field on login page")

    log("Typing username…")
    user_field.fill(username)

    # Check if password field is visible now (single page) or need to click Next
    pw_field = _find_element(page, pw_selectors)
    if not pw_field:
        # Two-step: click Next/Continue
        next_selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Sign In")',
        ]
        next_btn = _find_element(page, next_selectors)
        if next_btn:
            log("Clicking Next (two-step login)…")
            next_btn.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            pw_field = _find_element(page, pw_selectors)

    if not pw_field:
        _save_debug_screenshot(page, "after_username")
        raise RuntimeError("Could not find password field")

    log("Typing password…")
    pw_field.fill(password)

    # Submit
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log In")',
        'button:has-text("Sign In")',
        'button:has-text("Login")',
    ]
    submit_btn = _find_element(page, submit_selectors)
    if submit_btn:
        log("Submitting login form…")
        submit_btn.click()
    else:
        log("Pressing Enter to submit…")
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)


def _is_mfa_page(page) -> bool:
    """Return True if the current page looks like an MFA/OTP challenge."""
    indicators = [
        'input[name*="otp" i]',
        'input[id*="otp" i]',
        'input[placeholder*="verification" i]',
        'input[placeholder*="one-time" i]',
        'input[placeholder*="security code" i]',
        '[class*="mfa" i]',
        '[class*="two-factor" i]',
        'text=verification code',
        'text=one-time password',
        'text=security code',
    ]
    try:
        for sel in indicators:
            if page.query_selector(sel):
                return True
    except Exception:
        pass
    return False


def _submit_mfa(page, code: str, log: Callable):
    """Type the OTP code and submit."""
    from playwright.sync_api import TimeoutError as PWTimeout

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
    # Check for error messages
    error_texts = ["invalid", "incorrect", "failed", "error", "locked"]
    for txt in error_texts:
        el = page.query_selector(f'text="{txt}"')
        if el:
            _save_debug_screenshot(page, "login_error")
            raise RuntimeError(f"Login may have failed — page contains '{txt}'")
    log("Login successful.")


# ── eStatements navigation ────────────────────────────────────────────────────

def _navigate_to_estatements(page, log: Callable):
    """Navigate to the eStatements / Documents section."""
    from playwright.sync_api import TimeoutError as PWTimeout

    # Try direct URL patterns first
    base = _get_base_url(page)
    direct_paths = [
        f"{base}/estatements",
        f"{base}/documents",
        f"{base}/accounts/statements",
        f"{base}/statements",
    ]

    # Try nav links
    nav_selectors = [
        'a:has-text("eStatements")',
        'a:has-text("Statements")',
        'a:has-text("Documents")',
        '[class*="nav"] a:has-text("Statement")',
        'nav a:has-text("Statement")',
        'li a:has-text("Statements")',
    ]

    for sel in nav_selectors:
        try:
            el = page.query_selector(sel)
            if el:
                log(f"Clicking nav link: {sel}")
                el.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                log(f"Now at: {page.url}")
                return
        except Exception as e:
            log(f"Nav click failed ({sel}): {e}")

    # Try direct URL as fallback
    for path in direct_paths:
        try:
            log(f"Trying direct URL: {path}")
            page.goto(path, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=10000)
            if "statement" in page.url.lower() or "document" in page.url.lower():
                log(f"Reached statements at: {page.url}")
                return
        except Exception:
            pass

    _save_debug_screenshot(page, "estatements_not_found")
    log("⚠️  Could not auto-navigate to eStatements. Check debug screenshot in container /tmp/")
    raise RuntimeError(
        "Could not find eStatements page. The portal layout may have changed — "
        "check /tmp/usalliance_debug_*.png inside the container for screenshots."
    )


# ── per-year statement download ───────────────────────────────────────────────

def _download_year(
    page, context, year: str, consume_path: str, entity_slug: str, log: Callable
) -> tuple[int, int, int]:
    """Select the given year in the eStatements portal and download each statement."""
    from playwright.sync_api import TimeoutError as PWTimeout

    imported = skipped = errors = 0
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    log(f"Looking for year selector for {year}…")
    _select_statement_year(page, year, log)

    # Find all statement rows/links on the page
    statements = _find_statement_links(page, year, log)
    if not statements:
        log(f"No statements found for {year}.")
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
            pdf_bytes = _download_statement(page, context, stmt, log)
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
    Scan the eStatements page and return a list of statement descriptors.
    Each dict: {"element_index": int, "month": str, "account": str, "selector": str}
    """
    statements = []

    # Strategy 1: rows with month-year text + a download/view link
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    for idx, month_name in enumerate(month_names, 1):
        month_str = f"{idx:02d}"
        # Look for a row containing this month name and year
        row_selectors = [
            f'tr:has-text("{month_name}"):has-text("{year}")',
            f'li:has-text("{month_name}"):has-text("{year}")',
            f'div:has-text("{month_name} {year}")',
        ]
        for rsel in row_selectors:
            try:
                rows = page.query_selector_all(rsel)
                if rows:
                    for row in rows:
                        # Find the clickable link/button in this row
                        link = (
                            row.query_selector("a") or
                            row.query_selector('button:has-text("View")') or
                            row.query_selector('button:has-text("Download")') or
                            row.query_selector('button:has-text("PDF")') or
                            row.query_selector("button")
                        )
                        if link:
                            statements.append({
                                "month": month_str,
                                "account": "",
                                "element": link,
                            })
            except Exception:
                pass

    # Strategy 2: any link/button with text hinting at a statement
    if not statements:
        try:
            stmt_links = page.query_selector_all(
                'a[href*="statement" i], a[href*="pdf" i], '
                'button:has-text("View"), button:has-text("Download")'
            )
            for i, el in enumerate(stmt_links):
                text = (el.text_content() or "").strip()
                # Try to extract month from text
                month_str = "01"
                for mi, mn in enumerate(month_names, 1):
                    if mn.lower() in text.lower():
                        month_str = f"{mi:02d}"
                        break
                statements.append({"month": month_str, "account": f"{i}", "element": el})
        except Exception as e:
            log(f"Strategy 2 failed: {e}")

    return statements


def _download_statement(page, context, stmt: dict, log: Callable) -> Optional[bytes]:
    """Click a statement link and capture the resulting PDF."""
    from playwright.sync_api import TimeoutError as PWTimeout

    el = stmt.get("element")
    if not el:
        return None

    # Try download event first
    try:
        with page.expect_download(timeout=20000) as dl_info:
            el.click()
        download = dl_info.value
        with io.BytesIO() as buf:
            stream = download.create_read_stream()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf.write(chunk)
            return buf.getvalue() if buf.tell() > 0 else None
    except PWTimeout:
        pass
    except Exception as e:
        log(f"Download event failed: {e}")

    # Fallback: new tab / page.pdf()
    try:
        new_pages = []
        context.on("page", lambda p: new_pages.append(p))
        el.click()
        time.sleep(3)
        if new_pages:
            new_page = new_pages[0]
            new_page.wait_for_load_state("networkidle", timeout=15000)
            url = new_page.url
            if url.endswith(".pdf") or "pdf" in url.lower():
                # Fetch raw bytes
                response = new_page.request.get(url)
                return response.body()
            else:
                pdf_bytes = new_page.pdf()
                new_page.close()
                return pdf_bytes
        else:
            # Render current page as PDF
            page.wait_for_load_state("networkidle", timeout=15000)
            return page.pdf()
    except Exception as e:
        log(f"PDF capture fallback failed: {e}")
        return None


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
