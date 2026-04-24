"""Capital One — Playwright-based transaction downloader.

Uses Capital One's native "Download Transactions" UI to export CSV files
(not PDF statement scraping, which is blocked by bot detection).

Flow per account per year:
  1. Login → accounts dashboard (myaccounts.capitalone.com)
  2. Navigate into each account
  3. Click "I Want To…" dropdown → "Download Transactions"
  4. Set 90-day date range chunks to cover the full year
  5. Download CSV → parse → upsert transactions into DB
  6. Save CSV files to consume_path for archival

Persistent Chrome profile (/app/data/chrome_profiles/capitalone/) means
MFA only fires once per session lifetime.

MFA: Capital One sends SMS OTP. Job enters mfa_pending state and polls the
shared MFA registry until the user submits a code via the import page.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from app.importers.base_bank_importer import (
    find_element, find_in_frames, find_all_in_frames,
    human_click, human_type,
    launch_browser, save_auth_cookies, save_debug_screenshot,
    wait_for_element, wait_for_mfa_code,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://verified.capitalone.com/auth/signin"
ACCOUNTS_URL = "https://myaccounts.capitalone.com"
SOURCE = "capitalone"


# Keep wrapper for backward-compatibility with route
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
    Download Capital One transaction CSVs and import to DB.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    imported = skipped = errors = 0
    pw = context = page = None

    try:
        pw, context, page = launch_browser("capitalone", headless=True, log=log)

        if cookies:
            log(f"Injecting {len(cookies)} saved cookies…")
            context.add_cookies(cookies)

        _login(page, username, password, log, cookies, job_id)

        accounts = _discover_accounts(page, log)
        if not accounts:
            log("No accounts found — attempting generic download from dashboard.")
            accounts = [{"name": "account", "url": ACCOUNTS_URL, "id": ""}]

        for acct in accounts:
            log(f"── Account: {acct['name']} ──")
            for year in years:
                try:
                    yi, ys, ye = _download_year(
                        page, context, acct, year,
                        consume_path, entity_slug, log, job_id, entity_id,
                    )
                    imported += yi
                    skipped += ys
                    errors += ye
                except Exception as e:
                    import traceback
                    log(f"Error on {acct['name']} / {year}: {e}")
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

    log(f"Capital One done — imported: {imported}, skipped: {skipped}, errors: {errors}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── login ─────────────────────────────────────────────────────────────────────

def _login(page, username: str, password: str, log: Callable,
           cookies: Optional[list], job_id: int) -> None:
    if cookies:
        log("Navigating with saved cookies…")
        page.goto(ACCOUNTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        if "signin" not in page.url and "login" not in page.url:
            log(f"Authenticated via cookies at {page.url}")
            return
        log("Cookies expired — using credential login.")

    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    save_debug_screenshot(page, "co_login")

    # Capital One two-step login: username page → password page
    user_field = wait_for_element(page, [
        '#ods-input-0', 'input[name="username"]',
        'input[id*="username" i]', 'input[type="text"]:visible',
    ], timeout_ms=15000)
    if not user_field:
        save_debug_screenshot(page, "co_no_user")
        raise RuntimeError("Could not find Capital One username field")

    log("Entering username…")
    human_click(page, user_field)
    human_type(user_field, username)
    page.wait_for_timeout(400)

    btn = find_element(page, [
        'button:has-text("Continue")', 'button:has-text("Next")',
        'button[type="submit"]', 'input[type="submit"]',
    ])
    if btn:
        log("Clicking Continue…")
        human_click(page, btn)
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)

    pw_field = wait_for_element(page, [
        '#ods-input-1', 'input[name="password"]',
        'input[type="password"]:visible',
    ], timeout_ms=10000)
    if not pw_field:
        save_debug_screenshot(page, "co_no_pw")
        raise RuntimeError("Could not find Capital One password field")

    log("Entering password…")
    human_click(page, pw_field)
    human_type(pw_field, password)
    page.wait_for_timeout(400)

    submit = find_element(page, [
        'button:has-text("Sign In")', 'button:has-text("Log In")',
        'button:has-text("Continue")', 'button[type="submit"]',
        'input[type="submit"]',
    ])
    if submit:
        human_click(page, submit)
    else:
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    save_debug_screenshot(page, "co_post_login")

    if _is_mfa_page(page):
        _handle_mfa(page, log, job_id)

    url = page.url
    if "signin" in url or "login" in url:
        content = page.content().lower()
        if "incorrect" in content or "invalid" in content:
            raise RuntimeError("Capital One credentials rejected.")
        raise RuntimeError(f"Still on sign-in page ({url}). Try cookie-based auth.")

    log(f"Logged in — at {page.url}")
    save_auth_cookies(page.context, "capitalone", log)


def _is_mfa_page(page) -> bool:
    try:
        content = page.content().lower()
        return any(t in content for t in [
            "verification code", "one-time", "security code",
            "we sent a code", "sent a text", "check your phone",
            "enter the code", "two-step", "2-step",
        ])
    except Exception:
        return False


def _handle_mfa(page, log: Callable, job_id: int) -> None:
    log("MFA prompt detected — waiting for OTP code (up to 5 minutes)…")
    log("Enter the code from your phone via the MFA field on the Import page.")
    save_debug_screenshot(page, "co_mfa")

    from app import db
    db.update_import_job(job_id, status="mfa_pending")

    code = wait_for_mfa_code(job_id, log, timeout=300)
    if not code:
        raise RuntimeError("MFA timeout — no code submitted within 5 minutes.")

    log(f"MFA code received: {code[:2]}****")
    otp_field = wait_for_element(page, [
        'input[placeholder*="code" i]', 'input[name*="otp" i]',
        'input[id*="otp" i]', '#ods-input-0',
        'input[autocomplete="one-time-code"]', 'input[maxlength="6"]',
    ], timeout_ms=5000)

    if not otp_field:
        save_debug_screenshot(page, "co_no_otp")
        raise RuntimeError("OTP input field not found")

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


# ── account discovery ─────────────────────────────────────────────────────────

def _discover_accounts(page, log: Callable) -> list[dict]:
    log("Navigating to Capital One accounts dashboard…")
    try:
        page.goto(ACCOUNTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        save_debug_screenshot(page, "co_dashboard")
    except Exception as e:
        log(f"Dashboard nav failed: {e}")
        return []

    accounts = []
    seen: set[str] = set()

    for sel in [
        'a[href*="/account/"]', 'a[href*="/accounts/"]',
        '[data-testid*="account"]', '.account-tile a',
        'a.account-card', '[class*="accountCard"] a',
    ]:
        for el in find_all_in_frames(page, sel):
            try:
                text = (el.text_content() or "").strip()
                href = el.get_attribute("href") or ""
                # Skip non-account links
                if not text or text in seen or len(text) < 3 or len(text) > 80:
                    continue
                if any(skip in text.lower() for skip in ["skip", "nav", "menu", "help"]):
                    continue
                seen.add(text)
                if href and not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(ACCOUNTS_URL, href)
                accounts.append({"name": text, "url": href or None, "id": ""})
                log(f"  Account: {text[:60]}")
            except Exception:
                pass
        if accounts:
            break

    if not accounts:
        # Fallback: JS evaluation
        try:
            results = page.evaluate("""
                () => {
                    const out = [];
                    const seen = new Set();
                    for (const el of document.querySelectorAll('[class*="account"]')) {
                        const t = (el.textContent||'').trim().split('\\n')[0].trim();
                        const a = el.tagName === 'A' ? el : el.querySelector('a');
                        if (t.length > 3 && t.length < 80 && !seen.has(t)) {
                            seen.add(t);
                            out.push({name: t, href: a ? a.href : ''});
                        }
                    }
                    return out.slice(0, 10);
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
    consume_path: str, entity_slug: str, log: Callable, job_id: int,
    entity_id: Optional[int] = None,
) -> tuple[int, int, int]:
    """Download CSV in 90-day chunks for one account/year. Returns (imported, skipped, errors)."""
    imported = skipped = errors = 0

    acct_slug = re.sub(r"[^a-z0-9]", "_", acct["name"].lower()).strip("_") or "account"
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    y = int(year)
    today = date.today()
    start = date(y, 1, 1)
    end = min(date(y, 12, 31), today)

    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=89), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)

    log(f"  {len(chunks)} chunk(s) for {year}")

    for start_date, end_date in chunks:
        tag = f"{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}"
        filename = f"{year}_{acct_slug}_{tag}_capitalone.csv"
        dest_path = dest_dir / filename

        if dest_path.exists():
            log(f"  SKIP (exists): {filename}")
            skipped += 1
            continue

        log(f"  Downloading {filename}…")
        try:
            csv_bytes = _download_chunk(
                page, context, acct, start_date, end_date, log,
            )
            if csv_bytes and len(csv_bytes) > 50:
                dest_path.write_bytes(csv_bytes)
                log(f"  ✓ Saved {filename} ({len(csv_bytes):,}B)")
                txn_count = _parse_and_import_csv(
                    csv_bytes, acct["name"], entity_id=entity_id,
                    year=year, log=log,
                )
                imported += txn_count
            else:
                log(f"  No data for {tag}")
                errors += 1
        except Exception as e:
            log(f"  ✗ Chunk {tag} failed: {e}")
            errors += 1

    return imported, skipped, errors


def _download_chunk(
    page, context, acct: dict, start_date: date, end_date: date, log: Callable,
) -> Optional[bytes]:
    """
    Navigate to Capital One's 'Download Transactions' UI for one account,
    select a date range, and download the CSV.
    """
    from patchright.sync_api import TimeoutError as PWTimeout

    # Navigate to account page
    if acct.get("url") and acct["url"] != ACCOUNTS_URL:
        try:
            page.goto(acct["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
        except Exception as e:
            log(f"  Account page nav failed: {e}")

    save_debug_screenshot(page, f"co_acct_{start_date}")

    # Click "I Want To..." or find Download link
    download_dialog_open = _open_download_dialog(page, log)
    if not download_dialog_open:
        # Try alternative: look for a direct download/export link
        dl_link = find_element(page, [
            'a:has-text("Download")', 'button:has-text("Download")',
            'a:has-text("Export")', 'button:has-text("Export")',
            '[data-testid*="download"]',
        ])
        if dl_link:
            human_click(page, dl_link)
            page.wait_for_timeout(1500)
        else:
            log("  Could not open download dialog")
            save_debug_screenshot(page, f"co_no_dl_dialog_{start_date}")
            return None

    save_debug_screenshot(page, f"co_dl_dialog_{start_date}")

    # Set date range in the download dialog
    _set_date_range(page, start_date, end_date, log)

    # Select CSV format
    _select_format(page, "CSV", log)

    # Trigger download
    try:
        with page.expect_download(timeout=25000) as dl_info:
            dl_btn = find_element(page, [
                'button:has-text("Download")',
                'button:has-text("Export")',
                '[data-testid*="download"]:has-text("Download")',
                'a:has-text("Download")',
            ])
            if dl_btn:
                human_click(page, dl_btn)
            else:
                log("  Download submit button not found")
                return None

        dl = dl_info.value
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
        save_debug_screenshot(page, f"co_dl_fail_{start_date}")
        return None


def _open_download_dialog(page, log: Callable) -> bool:
    """Click 'I Want To...' or similar to open the download options dialog."""
    # Capital One uses various UI patterns across account types
    triggers = [
        'button:has-text("I Want To")',
        'button[aria-label*="more options" i]',
        '[class*="iwantto" i]',
        'button:has-text("Download Transactions")',
        'a:has-text("Download Transactions")',
        '[data-testid*="i-want-to"]',
        'button:has-text("More")',
        '[aria-label*="download" i]',
    ]
    for sel in triggers:
        el = find_element(page, [sel])
        if el:
            log(f"  Opening download dialog via: {sel}")
            human_click(page, el)
            page.wait_for_timeout(1000)
            # Check if a download option appeared
            if find_element(page, [
                'button:has-text("Download Transactions")',
                'a:has-text("Download Transactions")',
                '[data-testid*="download"]',
            ]):
                # Click the actual "Download Transactions" option
                dl = find_element(page, [
                    'button:has-text("Download Transactions")',
                    'a:has-text("Download Transactions")',
                ])
                if dl:
                    human_click(page, dl)
                    page.wait_for_timeout(1500)
            return True

    return False


def _set_date_range(page, start_date: date, end_date: date, log: Callable) -> None:
    """Fill date range inputs in the download dialog."""
    start_str = start_date.strftime("%m/%d/%Y")
    end_str = end_date.strftime("%m/%d/%Y")

    start_field = find_element(page, [
        'input[aria-label*="from" i]', 'input[aria-label*="start" i]',
        'input[placeholder*="from" i]', 'input[placeholder*="start" i]',
        'input[id*="from" i]', 'input[id*="start" i]',
        'input[name*="from" i]', 'input[name*="start" i]',
        'input[type="date"]:first-of-type',
    ])
    end_field = find_element(page, [
        'input[aria-label*="to" i]', 'input[aria-label*="end" i]',
        'input[placeholder*="to" i]', 'input[placeholder*="end" i]',
        'input[id*="to" i]', 'input[id*="end" i]',
        'input[name*="to" i]', 'input[name*="end" i]',
        'input[type="date"]:last-of-type',
    ])

    if start_field:
        human_click(page, start_field)
        human_type(start_field, start_str, clear_first=True)
        page.wait_for_timeout(200)
    else:
        log(f"  Warning: start date field not found")

    if end_field:
        human_click(page, end_field)
        human_type(end_field, end_str, clear_first=True)
        page.wait_for_timeout(200)
    else:
        log(f"  Warning: end date field not found")


def _select_format(page, format_name: str, log: Callable) -> None:
    """Select download format (CSV preferred for parsing)."""
    fmt_sel = find_element(page, [
        'select[aria-label*="format" i]', 'select[id*="format" i]',
        'select[name*="format" i]',
    ])
    if fmt_sel:
        try:
            fmt_sel.select_option(label=format_name)
            log(f"  Format set to {format_name}")
            return
        except Exception:
            pass

    # Try radio buttons
    for sel in [
        f'input[type="radio"][value="{format_name}"]',
        f'label:has-text("{format_name}")',
    ]:
        el = find_element(page, [sel])
        if el:
            human_click(page, el)
            log(f"  Format selected: {format_name} via radio")
            return


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _parse_and_import_csv(
    csv_bytes: bytes, account_name: str,
    entity_id: Optional[int], year: str, log: Callable,
) -> int:
    """Parse Capital One CSV and upsert transactions. Returns count inserted."""
    from app import db
    import hashlib

    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    # Capital One CSV columns (normalized to lowercase):
    # "Transaction Date","Posted Date","Card No.","Description","Category","Debit","Credit"
    # OR for checking: "Date","Description","Debit","Credit"
    count = 0
    for row in reader:
        keys = {k.lower().strip() for k in row.keys()}
        if not keys:
            continue

        # Date
        raw_date = (row.get("Transaction Date") or row.get("Date") or
                    row.get("transaction date") or "").strip()
        txn_date = _parse_date_str(raw_date)
        if not txn_date:
            continue

        txn_year = txn_date[:4]
        if txn_year != year:
            continue

        # Amount: debit is negative, credit is positive
        debit = _parse_csv_amount(
            row.get("Debit") or row.get("debit") or ""
        )
        credit = _parse_csv_amount(
            row.get("Credit") or row.get("credit") or ""
        )
        if debit is not None:
            amount = -abs(debit)
        elif credit is not None:
            amount = abs(credit)
        else:
            amount_raw = row.get("Amount") or row.get("amount") or ""
            amount = _parse_csv_amount(amount_raw)
            if amount is None:
                continue

        description = (row.get("Description") or row.get("description") or "").strip()
        category = (row.get("Category") or row.get("category") or "").strip()

        # Stable source_id from date + description + amount
        raw_id = f"{txn_date}|{description}|{amount}|{account_name}"
        source_id = f"capitalone:{hashlib.sha1(raw_id.encode()).hexdigest()[:16]}"

        try:
            db.upsert_transaction(
                source=SOURCE,
                source_id=source_id,
                entity_id=entity_id,
                tax_year=txn_year,
                date=txn_date,
                amount=amount,
                vendor=description[:60],
                description=description[:500],
                category=category,
                metadata_json=json.dumps({"account": account_name}),
            )
            count += 1
        except Exception as e:
            log(f"  DB insert error: {e}")

    return count


def _parse_date_str(raw: str) -> Optional[str]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_csv_amount(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None
