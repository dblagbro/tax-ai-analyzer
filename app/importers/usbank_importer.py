"""US Bank — Playwright-based transaction downloader.

Technique adapted from jbms/finance-dl (MIT licensed open-source project).
Key findings from source code analysis:
  - Login URL: https://onlinebanking.usbank.com/Auth/Login
  - US Bank embeds form fields inside iframes — use find_in_frames() for all lookups
  - Transaction download date inputs: id="FromDateInput", id="ToDateInput"
  - Download button: id="DTLLink"  OR link text "Download Transactions"
  - Output format: QFX (OFX) preferred; also supports CSV

MFA: US Bank sends OTP via SMS/email. Job enters mfa_pending state and polls
the shared MFA registry until the user submits a code.

Persistent profile (saved in /app/data/chrome_profiles/usbank/) means MFA
only fires on the first run after cookies expire.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

from app.importers.base_bank_importer import (
    find_element, find_in_frames, find_all_in_frames,
    handle_captcha_if_present,
    human_click, human_move, human_type,
    launch_browser, save_debug_screenshot,
    wait_for_element, wait_for_mfa_code,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://onlinebanking.usbank.com/Auth/Login"
DASHBOARD_URL = "https://onlinebanking.usbank.com/digitalbank/appmanager/userAccountSummary"

SOURCE = "usbank"


def set_mfa_code(job_id: int, code: str) -> None:
    from app.importers.mfa_registry import set_code
    set_code(job_id, code)


def run_import(
    username: str,
    password: str,
    years: list[str],
    consume_path: str,
    entity_slug: str,
    job_id: int,
    log: Callable[[str], None] = logger.info,
    cookies: Optional[list] = None,
    entity_id: Optional[int] = None,
) -> dict:
    """
    Download US Bank transaction files (QFX) for the requested years.

    Files are saved to <consume_path>/<entity_slug>/<year>/ and transactions
    are also parsed and upserted into the DB.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    imported = skipped = errors = 0
    pw = context = page = None

    try:
        pw, context, page = launch_browser("usbank", headless=True, log=log)

        if cookies:
            log(f"Injecting {len(cookies)} saved cookies…")
            context.add_cookies(cookies)

        logged_in = _login(page, username, password, log, cookies, job_id)
        if not logged_in:
            raise RuntimeError("US Bank login failed — check credentials or MFA.")

        accounts = _discover_accounts(page, log)
        if not accounts:
            log("No accounts discovered — trying generic download URL.")
            accounts = [{"name": "account", "id": "", "url": None}]

        for acct in accounts:
            log(f"── Account: {acct['name']} ──")
            for year in years:
                try:
                    yi, ys, ye = _download_year(
                        page, context, acct, year,
                        consume_path, entity_slug, log, entity_id,
                    )
                    imported += yi
                    skipped += ys
                    errors += ye
                except Exception as e:
                    import traceback
                    log(f"Error downloading {acct['name']} / {year}: {e}")
                    log(traceback.format_exc()[:400])
                    errors += 1

    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass

    log(f"US Bank done — imported: {imported}, skipped: {skipped}, errors: {errors}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── login ─────────────────────────────────────────────────────────────────────

def _warmup_navigation(page, log: Callable) -> bool:
    """Visit usbank.com homepage and click through to login organically — a cold
    direct goto to /Auth/Login is itself a bot signal for Akamai/Shape."""
    import random
    try:
        log("Warm-up: visiting usbank.com homepage…")
        page.goto("https://www.usbank.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000 + random.randint(500, 2500))
        vp = page.viewport_size or {"width": 1280, "height": 900}
        for _ in range(3):
            human_move(page, random.uniform(200, vp["width"] - 200),
                      random.uniform(150, vp["height"] - 150))
            page.wait_for_timeout(random.randint(400, 900))
        for sel in ['a:has-text("Personal")', 'a:has-text("Checking")', 'a:has-text("Credit cards")']:
            el = page.query_selector(sel)
            if el and el.is_visible():
                log(f"Warm-up click: {sel}")
                human_click(page, el)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000 + random.randint(500, 2000))
                break
        for sel in ['a:has-text("Log in")', 'a[href*="Auth/Login"]', 'button:has-text("Log in")']:
            el = page.query_selector(sel)
            if el and el.is_visible():
                log(f"Warm-up: clicking login link {sel}")
                human_click(page, el)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                return True
        log("Warm-up: no visible login link — falling back to direct goto")
        return False
    except Exception as e:
        log(f"Warm-up navigation failed: {e!r} — falling back to direct goto")
        return False


def _login(page, username: str, password: str, log: Callable,
           cookies: Optional[list], job_id: int) -> bool:
    if cookies:
        log("Navigating with saved cookies…")
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        if "login" not in page.url.lower() and "auth" not in page.url.lower():
            log(f"Authenticated via cookies at {page.url}")
            return True
        log("Cookies expired — falling back to credential login.")

    if not _warmup_navigation(page, log):
        log(f"Navigating to {LOGIN_URL}")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    save_debug_screenshot(page, "usb_login")

    # US Bank form fields may be inside iframes
    user_field = wait_for_element(page, [
        '#personal-id', 'input[id="personal-id"]',
        'input[name="Personal_ID"]', 'input[name="username"]',
        'input[type="text"]:visible', '#userid',
    ], timeout_ms=15000)

    if not user_field:
        save_debug_screenshot(page, "usb_no_user_field")
        raise RuntimeError("Could not find US Bank username field")

    log("Entering username…")
    human_click(page, user_field)
    human_type(user_field, username)
    page.wait_for_timeout(500)

    pw_field = find_element(page, [
        '#password', 'input[type="password"]',
        'input[name="password"]', 'input[name="Password"]',
    ])

    if pw_field:
        log("Entering password…")
        human_click(page, pw_field)
        human_type(pw_field, password)
    else:
        # Two-step: submit username first, then password page
        submit = find_element(page, [
            'button:has-text("Continue")', 'button[type="submit"]',
            'input[type="submit"]', '#btnContinue',
        ])
        if submit:
            log("Clicking Continue (username step)…")
            human_click(page, submit)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)

        pw_field = wait_for_element(page, [
            '#password', 'input[type="password"]',
        ], timeout_ms=10000)
        if pw_field:
            log("Entering password…")
            human_click(page, pw_field)
            human_type(pw_field, password)
        else:
            save_debug_screenshot(page, "usb_no_pw_field")
            raise RuntimeError("Could not find US Bank password field")

    page.wait_for_timeout(500)
    submit = find_element(page, [
        'button:has-text("Log In")', 'button:has-text("Sign In")',
        'button:has-text("Continue")', 'button[type="submit"]',
        'input[type="submit"]', '#btnLogin',
    ])
    if submit:
        log("Submitting login…")
        human_click(page, submit)
    else:
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    save_debug_screenshot(page, "usb_post_login")

    # Handle "Confirm you're a person" CAPTCHA if it appears
    for attempt in range(2):
        if handle_captcha_if_present(page, log):
            page.wait_for_timeout(1500)
            save_debug_screenshot(page, f"usb_post_captcha_{attempt}")
        else:
            break

    if _is_mfa_page(page):
        if not _handle_mfa(page, log, job_id):
            return False

    if "login" in page.url.lower() or "auth" in page.url.lower():
        content = page.content().lower()
        # Only treat as rejected credentials when the language is unambiguous.
        # A CAPTCHA/verification page can contain words like "invalid request"
        # without meaning the password was wrong.
        rejected_markers = [
            "password is incorrect", "username is incorrect",
            "we don't recognize that", "incorrect username or password",
            "invalid username or password", "credentials do not match",
            "please re-enter your", "try again with",
            "something you entered is incorrect",  # US Bank's generic reject (often = locked account)
            "account has been locked", "your account is locked",
            "too many failed", "login attempts exceeded",
        ]
        if any(m in content for m in rejected_markers):
            raise RuntimeError("US Bank credentials rejected.")
        # Unknown login-page state — save screenshots + safely-scrubbed HTML
        save_debug_screenshot(page, "usb_unknown_login_state")
        try:
            import re as _re
            raw_html = page.content()
            # SECURITY: scrub all form field values before writing to disk.
            # Form input values (including the password!) show up as value="..."
            # attributes in the serialized DOM — never dump raw.
            scrubbed = _re.sub(
                r'(value\s*=\s*)"[^"]*"',
                r'\1"[REDACTED]"',
                raw_html,
            )
            scrubbed = _re.sub(
                r"(value\s*=\s*)'[^']*'",
                r"\1'[REDACTED]'",
                scrubbed,
            )
            with open("/tmp/bank_debug_usb_unknown_login_state.html", "w") as f:
                f.write(scrubbed)
            log(f"  HTML dumped (input values scrubbed) to /tmp/bank_debug_usb_unknown_login_state.html")
            log(f"  URL: {page.url}")
            try:
                body_text = page.evaluate("() => document.body.innerText.slice(0, 800)")
                log(f"  body text: {body_text!r}")
            except Exception:
                pass
        except Exception as e:
            log(f"  dump failed: {e}")
        raise RuntimeError(
            "Could not complete US Bank login — still on an auth page but no "
            "clear rejection signal. Check /tmp/bank_debug_usb_unknown_login_state.html"
        )

    log(f"Logged in — at {page.url}")
    return True


def _is_mfa_page(page) -> bool:
    mfa_texts = [
        "verification code", "one-time", "security code",
        "we sent", "enter the code", "two-step", "2-step", "text message",
    ]
    try:
        content = page.content().lower()
        return any(t in content for t in mfa_texts)
    except Exception:
        return False


def _handle_mfa(page, log: Callable, job_id: int) -> bool:
    log("MFA prompt detected — waiting for code (up to 5 minutes)…")
    log("Submit the code via the MFA field on the Import page.")
    save_debug_screenshot(page, "usb_mfa")

    from app import db
    db.update_import_job(job_id, status="mfa_pending")

    code = wait_for_mfa_code(job_id, log, timeout=300)
    if not code:
        log("MFA timeout.")
        return False

    log(f"MFA code received: {code[:2]}****")
    otp_field = wait_for_element(page, [
        'input[placeholder*="code" i]', 'input[name*="otp" i]',
        'input[id*="otp" i]', 'input[type="tel"]',
        'input[maxlength="6"]', 'input[autocomplete="one-time-code"]',
    ], timeout_ms=5000)

    if not otp_field:
        save_debug_screenshot(page, "usb_no_otp_field")
        return False

    human_click(page, otp_field)
    human_type(otp_field, code, clear_first=True)
    page.wait_for_timeout(400)

    submit = find_element(page, [
        'button:has-text("Continue")', 'button:has-text("Verify")',
        'button:has-text("Submit")', 'button[type="submit"]',
    ])
    if submit:
        human_click(page, submit)
    else:
        otp_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(1500)

    from app import db
    db.update_import_job(job_id, status="running")
    log("MFA submitted.")
    return True


# ── account discovery ─────────────────────────────────────────────────────────

def _discover_accounts(page, log: Callable) -> list[dict]:
    log("Discovering US Bank accounts…")
    try:
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        save_debug_screenshot(page, "usb_dashboard")
    except Exception as e:
        log(f"Dashboard nav error: {e}")
        return []

    accounts = []
    seen: set[str] = set()

    # Try multiple selectors for account links
    for sel in [
        'a[href*="account"]', '[data-testid*="account"]',
        '.account-name a', '.account-title', 'a.account',
    ]:
        for el in find_all_in_frames(page, sel):
            try:
                text = (el.text_content() or "").strip()
                href = el.get_attribute("href") or ""
                if not text or text in seen or len(text) < 3:
                    continue
                seen.add(text)
                if href and not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(DASHBOARD_URL, href)
                accounts.append({"name": text[:60], "url": href or None, "id": ""})
                log(f"  Account: {text[:60]}")
            except Exception:
                pass
        if accounts:
            break

    if not accounts:
        # Fallback: grab any text that looks like an account name
        try:
            results = page.evaluate("""
                () => {
                    const accts = [];
                    const seen = new Set();
                    document.querySelectorAll('[class*="account"], [id*="account"]').forEach(el => {
                        const t = (el.textContent||'').trim().split('\\n')[0].trim();
                        if (t.length > 3 && t.length < 60 && !seen.has(t)) {
                            seen.add(t);
                            const a = el.closest('a') || el.querySelector('a');
                            accts.push({name: t, href: a ? a.href : ''});
                        }
                    });
                    return accts.slice(0, 10);
                }
            """)
            for r in results:
                accounts.append({"name": r["name"], "url": r.get("href") or None, "id": ""})
                log(f"  Account (JS): {r['name']}")
        except Exception as e:
            log(f"JS account scan failed: {e}")

    log(f"Found {len(accounts)} account(s).")
    return accounts


# ── per-year download ─────────────────────────────────────────────────────────

def _download_year(
    page, context, acct: dict, year: str,
    consume_path: str, entity_slug: str, log: Callable,
    entity_id: Optional[int] = None,
) -> tuple[int, int, int]:
    """Download QFX for an entire year in 90-day chunks. Returns (imported, skipped, errors)."""
    from app.importers.ofx_importer import parse_ofx

    imported = skipped = errors = 0
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    acct_slug = re.sub(r"[^a-z0-9]", "_", acct["name"].lower()).strip("_") or "account"

    # Navigate to account if we have a URL
    if acct.get("url"):
        try:
            page.goto(acct["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
        except Exception as e:
            log(f"  Account nav failed: {e}")

    # Build 90-day windows for the year
    windows = _year_windows_for_download(year)
    log(f"  {len(windows)} chunk(s) for {year}")

    for i, (start_date, end_date) in enumerate(windows):
        chunk_tag = f"{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}"
        filename = f"{year}_{acct_slug}_{chunk_tag}_usbank.qfx"
        dest_path = dest_dir / filename

        if dest_path.exists():
            log(f"  SKIP (exists): {filename}")
            skipped += 1
            continue

        log(f"  Downloading {filename}…")
        try:
            qfx_bytes = _download_chunk(page, context, acct, start_date, end_date, log)
            if qfx_bytes and len(qfx_bytes) > 100:
                dest_path.write_bytes(qfx_bytes)
                log(f"  ✓ Saved {filename} ({len(qfx_bytes):,}B)")
                try:
                    from app import db
                    txns = parse_ofx(qfx_bytes, entity_id=entity_id, default_year=year)
                    for txn in txns:
                        source_id = f"usbank:{txn.get('external_id', '')}" or \
                                    f"usbank:{txn['date']}:{txn['amount']}:{txn['vendor']}"
                        db.upsert_transaction(
                            source=SOURCE,
                            source_id=source_id,
                            entity_id=entity_id,
                            tax_year=txn.get("tax_year", year),
                            date=txn["date"],
                            amount=txn["amount"],
                            vendor=txn.get("vendor", ""),
                            description=txn.get("description", ""),
                            category=txn.get("category", ""),
                            doc_type=txn.get("doc_type", "bank_statement"),
                        )
                    imported += len(txns)
                    log(f"  → {len(txns)} transactions written to DB")
                except Exception as e:
                    log(f"  OFX parse/import error: {e}")
                    errors += 1
            else:
                log(f"  ✗ Empty or no data for chunk {chunk_tag}")
                errors += 1
        except Exception as e:
            log(f"  ✗ Chunk {chunk_tag} failed: {e}")
            errors += 1

    return imported, skipped, errors


def _year_windows_for_download(year: str) -> list[tuple[date, date]]:
    from datetime import date, timedelta
    y = int(year)
    today = date.today()
    start = date(y, 1, 1)
    end = min(date(y, 12, 31), today)
    windows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + _timedelta_days(89), end)
        windows.append((cur, chunk_end))
        cur = chunk_end + _timedelta_days(1)
    return windows


def _timedelta_days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _download_chunk(
    page, context, acct: dict, start_date, end_date, log: Callable
) -> Optional[bytes]:
    """Navigate to download UI, set date range, trigger QFX download."""
    from patchright.sync_api import TimeoutError as PWTimeout

    # Navigate to "Download Transactions" page
    download_reached = _navigate_to_download(page, log)
    if not download_reached:
        log("  Could not reach download UI")
        return None

    save_debug_screenshot(page, f"usb_dl_{start_date}")

    # Set FromDateInput
    from_el = find_in_frames(page, "#FromDateInput") or find_in_frames(
        page, 'input[id*="FromDate" i]'
    ) or find_in_frames(page, 'input[placeholder*="from" i]')

    # Set ToDateInput
    to_el = find_in_frames(page, "#ToDateInput") or find_in_frames(
        page, 'input[id*="ToDate" i]'
    ) or find_in_frames(page, 'input[placeholder*="to" i]')

    if not from_el or not to_el:
        log("  Date inputs not found — trying alternate selectors")
        save_debug_screenshot(page, "usb_no_date_inputs")
        return None

    human_click(page, from_el)
    human_type(from_el, start_date.strftime("%m/%d/%Y"), clear_first=True)
    page.wait_for_timeout(300)

    human_click(page, to_el)
    human_type(to_el, end_date.strftime("%m/%d/%Y"), clear_first=True)
    page.wait_for_timeout(300)

    # Select QFX format if dropdown exists
    fmt_sel = find_in_frames(page, 'select[id*="format" i], select[id*="Format"]')
    if fmt_sel:
        try:
            fmt_sel.select_option(value="QFX")
        except Exception:
            try:
                fmt_sel.select_option(label="Quicken")
            except Exception:
                pass

    # Click download
    dl_btn = find_in_frames(page, "#DTLLink") or find_element(page, [
        'a:has-text("Download Transactions")',
        'button:has-text("Download")',
        '#download-btn', 'a[id*="download" i]',
        'button[id*="download" i]',
    ])

    if not dl_btn:
        save_debug_screenshot(page, "usb_no_dl_btn")
        log("  Download button not found")
        return None

    log(f"  Clicking download ({start_date} → {end_date})…")
    try:
        with page.expect_download(timeout=20000) as dl_info:
            human_click(page, dl_btn)
        dl = dl_info.value
        import io
        buf = io.BytesIO()
        stream = dl.create_read_stream()
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buf.write(chunk)
        return buf.getvalue() if buf.tell() > 0 else None
    except (PWTimeout, Exception) as e:
        log(f"  Download failed: {e}")
        return None


def _navigate_to_download(page, log: Callable) -> bool:
    """Try to reach the Download Transactions page."""
    # First try direct link
    dl_link = find_element(page, [
        'a:has-text("Download Transactions")',
        'a[href*="download" i][href*="transaction" i]',
        '#downloadTransactions',
    ])
    if dl_link:
        dl_link.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

    # Check if date inputs are now visible
    if find_in_frames(page, "#FromDateInput"):
        log("  Download UI reached.")
        return True

    # Try direct URL patterns
    for url_candidate in [
        "https://onlinebanking.usbank.com/digitalbank/appmanager/download",
        "https://onlinebanking.usbank.com/AccountHistory/Download",
    ]:
        try:
            page.goto(url_candidate, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            if find_in_frames(page, "#FromDateInput"):
                log(f"  Download UI at {url_candidate}")
                return True
        except Exception:
            pass

    return False
