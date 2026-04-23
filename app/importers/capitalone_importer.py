"""Capital One — Playwright-based statement downloader.

Logs into Capital One online banking, navigates to eStatements for each
account, and downloads monthly PDF statements for the requested years.

Each statement is saved to:
  <consume_path>/<entity_slug>/<year>/YYYY_MM_01_capitalone_<account>_statement.pdf

MFA handling: Capital One typically sends an SMS OTP.  The job enters
`mfa_pending` state and polls for a code via the in-memory MFA registry
(fed by the /api/import/capitalone/mfa endpoint).
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
_mfa_registry: dict[int, dict] = {}

def set_mfa_code(job_id: int, code: str):
    _mfa_registry[job_id] = {"code": code, "expires": time.time() + 300}

def _wait_for_mfa(job_id: int, log: Callable, timeout: int = 300) -> Optional[str]:
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

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

def _month_num(text: str) -> str:
    for i, name in enumerate(MONTH_NAMES, 1):
        if name.lower() in text.lower():
            return f"{i:02d}"
    return "01"

def _safe_filename(year: str, month: str, account_suffix: str = "") -> str:
    suffix = f"_{account_suffix}" if account_suffix else ""
    return f"{year}_{month:>02}_01_capitalone{suffix}_statement.pdf"

def _months_for_year(year: str) -> list[str]:
    now = datetime.now()
    if year == str(now.year):
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
    Drive the Capital One portal with Playwright.

    If `cookies` is provided they are injected and login is skipped,
    bypassing bot detection.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Add it to requirements.txt and rebuild."
        )

    imported = skipped = errors = 0

    try:
        from playwright_stealth import Stealth
        _stealth = Stealth(
            navigator_webdriver=True,
            navigator_plugins=True,
            navigator_languages=True,
            navigator_platform=True,
            navigator_user_agent=True,
            navigator_vendor=True,
            chrome_app=True,
            chrome_csi=True,
            chrome_load_times=True,
            webgl_vendor=True,
            hairline=True,
            media_codecs=True,
            navigator_hardware_concurrency=True,
            navigator_permissions=True,
            error_prototype=True,
            sec_ch_ua=True,
            iframe_content_window=True,
            navigator_platform_override="Win32",
            navigator_languages_override=("en-US", "en"),
        )
        log("Stealth mode enabled.")
    except ImportError:
        _stealth = None
        log("Warning: playwright-stealth not available — bot detection risk.")

    with sync_playwright() as pw:
        if _stealth is not None:
            _stealth.hook_playwright_context(pw)
        log("Launching headless Chromium…")
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-component-extensions-with-background-pages",
                "--disable-background-networking",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--password-store=basic",
                "--use-mock-keychain",
                "--window-size=1280,900",
                "--lang=en-US",
            ],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
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

        page = context.new_page()
        if _stealth is not None:
            _stealth.apply_stealth_sync(page)

        try:
            _login(page, username, password, log, cookies, job_id)

            # Global PDF interceptor
            session_pdf_cache: list[bytes] = []

            def _on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    url_l = resp.url.lower()
                    if ("pdf" in ct or "octet-stream" in ct
                            or url_l.endswith(".pdf") or "/pdf" in url_l):
                        data = resp.body()
                        if data and len(data) > 500:
                            log(f"  [intercept] {resp.status} {resp.url[:80]} ({len(data):,}B)")
                            session_pdf_cache.append(data)
                except Exception:
                    pass

            page.on("response", _on_response)

            # Discover accounts then download statements for each
            accounts = _discover_accounts(page, log)
            if not accounts:
                log("No accounts found — attempting generic statement navigation.")
                accounts = [{"name": "account", "url": None}]

            for acct in accounts:
                acct_slug = re.sub(r"[^a-z0-9]", "_", acct["name"].lower()).strip("_")
                log(f"── Account: {acct['name']} ──")
                for year in years:
                    try:
                        yi, ys, ye = _download_statements_for_account(
                            page, context, acct, acct_slug, year,
                            consume_path, entity_slug, log, session_pdf_cache,
                        )
                        imported += yi
                        skipped += ys
                        errors += ye
                    except Exception as e:
                        log(f"Error on {acct['name']} / {year}: {e}")
                        errors += 1

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


# ── login ─────────────────────────────────────────────────────────────────────

LOGIN_URL = "https://verified.capitalone.com/auth/signin"
ACCOUNTS_URL = "https://myaccounts.capitalone.com"

def _login(page, username: str, password: str, log: Callable,
           cookies: Optional[list], job_id: int):
    if cookies:
        log("Navigating to Capital One with injected cookies…")
        page.goto(ACCOUNTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        if "signin" not in page.url and "login" not in page.url:
            log(f"Authenticated via cookies — at {page.url}")
            return
        log("Cookies expired — falling back to credential login…")

    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    _fill_login(page, username, password, log)
    _handle_mfa(page, log, job_id)
    _verify_logged_in(page, log)


def _fill_login(page, username: str, password: str, log: Callable):
    import random

    # Capital One uses a two-step login: username first, then password page
    user_selectors = [
        '#ods-input-0',
        'input[name="username"]',
        'input[id*="username" i]',
        'input[placeholder*="username" i]',
        'input[type="text"]:visible',
    ]
    user_field = _find_element(page, user_selectors)
    if not user_field:
        _save_debug_screenshot(page, "co_login_no_user")
        raise RuntimeError("Could not find Capital One username field")

    log("Typing username…")
    user_field.click()
    page.wait_for_timeout(300)
    for ch in username:
        user_field.press(ch)
        page.wait_for_timeout(random.randint(50, 150))

    # Click Continue to advance to password step
    continue_selectors = [
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    btn = _find_element(page, continue_selectors)
    if btn:
        log("Clicking Continue…")
        btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)

    # Password field (may be on same page or new page after Continue)
    pw_selectors = [
        '#ods-input-1',
        'input[name="password"]',
        'input[id*="password" i]',
        'input[type="password"]:visible',
    ]
    pw_field = _find_element(page, pw_selectors)
    if not pw_field:
        _save_debug_screenshot(page, "co_login_no_pw")
        raise RuntimeError("Could not find Capital One password field")

    log("Typing password…")
    pw_field.click()
    page.wait_for_timeout(300)
    for ch in password:
        pw_field.press(ch)
        page.wait_for_timeout(random.randint(50, 150))
    page.wait_for_timeout(500)

    submit = _find_element(page, [
        'button:has-text("Sign In")',
        'button:has-text("Log In")',
        'button:has-text("Continue")',
        'button[type="submit"]',
        'input[type="submit"]',
    ])
    if submit:
        log("Submitting login…")
        submit.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        submit.click()
    else:
        log("Pressing Enter to submit…")
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)


def _handle_mfa(page, log: Callable, job_id: int):
    """Handle Capital One OTP / device trust prompts."""
    content = page.content().lower()
    url = page.url.lower()

    otp_indicators = [
        'input[placeholder*="code" i]',
        'input[placeholder*="verification" i]',
        'input[name*="otp" i]',
        'input[id*="otp" i]',
        '#ods-input-0',   # Capital One reuses this id on the OTP page too
    ]
    text_indicators = [
        "verification code", "one-time", "security code",
        "we sent a code", "sent a text", "check your phone",
        "enter the code", "two-step", "2-step",
    ]

    is_mfa = any(page.query_selector(s) for s in otp_indicators) or \
             any(t in content for t in text_indicators)

    if not is_mfa:
        return

    log("🔢 MFA prompt detected — waiting for OTP code (up to 5 minutes)…")
    log("⚠️  ENTER THE CODE from your phone/email in the MFA field on the import page.")
    _save_debug_screenshot(page, "co_mfa")
    code = _wait_for_mfa(job_id, log, timeout=300)
    if not code:
        raise RuntimeError("MFA timeout — no code submitted within 5 minutes.")
    log(f"MFA code received: {code[:2]}****")

    otp_field = _find_element(page, otp_indicators)
    if not otp_field:
        _save_debug_screenshot(page, "co_mfa_no_input")
        raise RuntimeError("Could not find OTP input field")

    otp_field.fill(code)

    submit = _find_element(page, [
        'button:has-text("Continue")',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ])
    if submit:
        submit.click()
    else:
        otp_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(1500)
    log("MFA submitted.")


def _verify_logged_in(page, log: Callable):
    url = page.url
    log(f"Post-login URL: {url}")
    if "signin" in url or "login" in url:
        content = page.content().lower()
        _save_debug_screenshot(page, "co_login_failed")
        if "incorrect" in content or "invalid" in content or "try again" in content:
            raise RuntimeError("Capital One login failed — check credentials.")
        raise RuntimeError(
            f"Still on login/signin page ({url}). "
            "Try cookie-based authentication or check credentials."
        )
    log("Login successful.")


# ── account discovery ─────────────────────────────────────────────────────────

def _discover_accounts(page, log: Callable) -> list[dict]:
    """Return list of {name, url} dicts for each account on the dashboard."""
    log("Discovering accounts…")
    try:
        page.goto(ACCOUNTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        _save_debug_screenshot(page, "co_dashboard")
    except Exception as e:
        log(f"Dashboard navigation error: {e}")
        return []

    accounts = []
    try:
        # Capital One account tiles are typically <a> elements with account names
        acct_selectors = [
            'a[href*="/account/"]',
            'a[href*="/accounts/"]',
            '[data-testid*="account"]',
            '.account-tile a',
            '.account-name',
        ]
        seen_hrefs = set()
        for sel in acct_selectors:
            els = page.query_selector_all(sel)
            for el in els:
                href = el.get_attribute("href") or ""
                text = (el.text_content() or "").strip()
                if not text or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                if href and not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(ACCOUNTS_URL, href)
                accounts.append({"name": text[:60], "url": href or None})
                log(f"  Account: {text[:60]} → {href[:80]}")

        if not accounts:
            # Fallback: JS evaluation of account card text
            results = page.evaluate("""
                () => {
                    const out = [];
                    const els = document.querySelectorAll('[class*="account"], [class*="card"]');
                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        const a = el.querySelector('a');
                        if (t.length > 3 && t.length < 100)
                            out.push({name: t.substring(0,60), href: a ? a.href : ''});
                    }
                    return out.slice(0, 10);
                }
            """)
            for r in results:
                if r["name"] not in [a["name"] for a in accounts]:
                    accounts.append({"name": r["name"], "url": r.get("href") or None})
                    log(f"  Account (JS): {r['name']}")

    except Exception as e:
        log(f"Account discovery error: {e}")

    log(f"Found {len(accounts)} account(s).")
    return accounts


# ── statement download per account per year ───────────────────────────────────

def _download_statements_for_account(
    page, context, acct: dict, acct_slug: str, year: str,
    consume_path: str, entity_slug: str, log: Callable,
    session_pdf_cache: list,
) -> tuple[int, int, int]:
    imported = skipped = errors = 0
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Navigate to statements for this account
    stmt_urls = _statement_urls_for_account(acct)
    reached = False
    for url in stmt_urls:
        try:
            log(f"Navigating to statements: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(3000)
            if _is_statements_page(page):
                reached = True
                log(f"✓ Statements page at {page.url}")
                _save_debug_screenshot(page, f"co_stmts_{acct_slug}")
                break
        except Exception as e:
            log(f"  failed: {e}")

    if not reached:
        # Try clicking "Statements" link from account detail page
        if acct.get("url"):
            try:
                page.goto(acct["url"], wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
                reached = _click_statements_link(page, log)
            except Exception as e:
                log(f"Account page navigation failed: {e}")

    if not reached:
        log(f"Could not reach statements page for {acct['name']} — skipping.")
        return 0, 0, 1

    # Find statement rows matching the requested year
    stmts = _find_statements(page, year, log)
    if not stmts:
        _save_debug_screenshot(page, f"co_no_stmts_{acct_slug}_{year}")
        log(f"No statements found for {acct['name']} / {year}")
        return 0, 0, 0

    log(f"Found {len(stmts)} statement(s) for {acct['name']} / {year}")

    for stmt in stmts:
        month = stmt["month"]
        filename = _safe_filename(year, month, acct_slug)
        dest_path = dest_dir / filename

        if dest_path.exists():
            log(f"  SKIP (exists): {filename}")
            skipped += 1
            continue

        log(f"  Downloading: {filename}")
        try:
            session_pdf_cache.clear()
            pdf_bytes = _download_statement(page, context, stmt, log, session_pdf_cache)
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


def _statement_urls_for_account(acct: dict) -> list[str]:
    """Generate candidate statement URLs for a Capital One account."""
    base = ACCOUNTS_URL
    urls = [
        f"{base}/accounts/",
        f"{base}/accounts/summary/statement",
    ]
    if acct.get("url"):
        acct_url = acct["url"].rstrip("/")
        urls = [
            f"{acct_url}/statements",
            f"{acct_url}/activity/statements",
            f"{acct_url}/statement",
        ] + urls
    return urls


def _is_statements_page(page) -> bool:
    try:
        content = page.content().lower()
        return any(p in content for p in [
            "statement date", "statement period", "view statement",
            "download statement", "e-statement", "estatement",
            "account statement", "monthly statement",
        ])
    except Exception:
        return False


def _click_statements_link(page, log: Callable) -> bool:
    """Click a Statements navigation link if found. Returns True on success."""
    selectors = [
        'a:has-text("Statements")',
        'a:has-text("eStatements")',
        'a:has-text("Account Statements")',
        'button:has-text("Statements")',
        '[href*="statement" i]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                log(f"Clicking statements link: {sel}")
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
                if _is_statements_page(page):
                    return True
        except Exception:
            pass
    return False


def _find_statements(page, year: str, log: Callable) -> list[dict]:
    """Return list of {month, element, text} dicts for the requested year."""
    statements = []
    seen_months: set[str] = set()

    # Strategy 1: element-based scan
    for sel in [
        f':has-text("{year}"):has-text("Statement")',
        f'[class*="statement" i]:has-text("{year}")',
        f'li:has-text("{year}")',
        f'tr:has-text("{year}")',
        f'div:has-text("{year} Statement")',
    ]:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                text = (el.text_content() or "").strip()
                if year not in text or len(text) > 300:
                    continue
                month = _month_num(text)
                if month in seen_months:
                    continue
                seen_months.add(month)
                statements.append({"month": month, "element": el, "text": text})
                log(f"  Stmt: {text[:80]}")
            if statements:
                return statements
        except Exception as e:
            log(f"Selector '{sel}' error: {e}")

    # Strategy 2: JS walk (same approach as US Alliance)
    log("Element scan found nothing — trying JS walk…")
    try:
        results = page.evaluate(f"""
            () => {{
                const year = "{year}";
                const out = [];
                const seen = new Set();
                for (const el of document.querySelectorAll('*')) {{
                    const t = (el.textContent || '').trim();
                    if (!t.includes(year)) continue;
                    if (t.length > 300) continue;
                    const lower = t.toLowerCase();
                    if (!lower.includes('statement') && !lower.includes('period')) continue;
                    const key = t.substring(0, 40);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({{text: t, tag: el.tagName, cls: (el.className||'').substring(0,60)}});
                }}
                return out.slice(0, 20);
            }}
        """)
        for r in results:
            log(f"  JS [{r['tag']}] {r['text'][:80]}")
    except Exception as e:
        log(f"JS walk failed: {e}")

    return []


def _download_statement(
    page, context, stmt: dict, log: Callable, session_pdf_cache: list,
) -> Optional[bytes]:
    from playwright.sync_api import TimeoutError as PWTimeout

    el = stmt.get("element")
    if not el:
        return None

    new_pages: list = []
    context.on("page", lambda p: new_pages.append(p))
    pre_url = page.url

    try:
        el.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        box = el.bounding_box()
        log(f"  Element box: {box}  text: {stmt.get('text','')[:60]}")
        _save_debug_screenshot(page, f"co_pre_click_{stmt['month']}")

        # First: look for a Download/PDF link within the row
        row_pdf = None
        for link_sel in [
            'a[href*=".pdf" i]', 'a:has-text("Download")', 'a:has-text("PDF")',
            'button:has-text("Download")', 'button:has-text("PDF")',
        ]:
            try:
                link = el.query_selector(link_sel)
                if link and link.is_visible():
                    row_pdf = link
                    break
            except Exception:
                pass

        if row_pdf:
            try:
                with page.expect_download(timeout=15000) as dl_info:
                    row_pdf.click()
                dl = dl_info.value
                buf = io.BytesIO()
                s = dl.create_read_stream()
                while True:
                    chunk = s.read(65536)
                    if not chunk:
                        break
                    buf.write(chunk)
                if buf.tell() > 0:
                    log(f"  ✓ Direct download from row link")
                    return buf.getvalue()
            except (PWTimeout, Exception) as e:
                log(f"  Row link download failed: {e}")

        # Click the row itself
        try:
            if box and box["width"] > 0 and box["height"] > 0:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                el.click()
        except Exception as e:
            log(f"  Click error: {e}")
            try:
                el.dispatch_event("click")
            except Exception:
                pass

        time.sleep(8)
        _save_debug_screenshot(page, f"co_post_click_{stmt['month']}")

        # Check session PDF cache
        if session_pdf_cache:
            data = session_pdf_cache.pop(0)
            log(f"  ✓ PDF from session cache ({len(data):,}B)")
            return data

        # Check for download link that appeared after click
        for dl_sel in [
            'a[href*=".pdf" i]', 'a:has-text("Download PDF")',
            '[download]:visible', 'button:has-text("Download PDF")',
        ]:
            try:
                lnk = page.query_selector(dl_sel)
                if lnk and lnk.is_visible():
                    with page.expect_download(timeout=10000) as dl_info:
                        lnk.click()
                    dl = dl_info.value
                    buf = io.BytesIO()
                    s = dl.create_read_stream()
                    while True:
                        chunk = s.read(65536)
                        if not chunk:
                            break
                        buf.write(chunk)
                    if buf.tell() > 0:
                        log(f"  ✓ Downloaded via appeared link ({dl_sel})")
                        _nav_back(page, log)
                        return buf.getvalue()
            except (PWTimeout, Exception):
                pass

        # Check new tab
        if new_pages:
            np = new_pages[0]
            try:
                np.wait_for_load_state("domcontentloaded", timeout=10000)
                new_url = np.url
                log(f"  New tab: {new_url}")
                time.sleep(3)
                if session_pdf_cache:
                    data = session_pdf_cache.pop(0)
                    np.close()
                    return data
                if ".pdf" in new_url.lower() or "pdf" in new_url.lower():
                    resp = np.request.get(new_url)
                    data = resp.body()
                    np.close()
                    if data:
                        return data
                pdf_bytes = np.pdf()
                np.close()
                if pdf_bytes:
                    return pdf_bytes
            except Exception as e:
                log(f"  New tab failed: {e}")
                try:
                    new_pages[0].close()
                except Exception:
                    pass

        if page.url != pre_url:
            _nav_back(page, log)

    finally:
        try:
            context.remove_listener("page", lambda p: new_pages.append(p))
        except Exception:
            pass

    log("  ✗ All download attempts failed")
    return None


def _nav_back(page, log: Callable):
    try:
        page.go_back(wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(2000)
    except Exception:
        try:
            page.goto(ACCOUNTS_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass


# ── utility helpers ───────────────────────────────────────────────────────────

def _find_element(page, selectors: list[str]):
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            pass
    return None


def _save_debug_screenshot(page, label: str):
    try:
        ts = datetime.now().strftime("%H%M%S")
        path = f"/tmp/capitalone_debug_{label}_{ts}.png"
        page.screenshot(path=path)
        logger.info(f"Debug screenshot: {path}")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")
