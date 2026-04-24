"""Merrick Bank — Playwright-based transaction downloader.

Merrick Bank uses a traditional ASP.NET MVC form (no SPA), which is
significantly easier to automate than Capital One / US Bank.

Login: https://logon.merrickbank.com
MFA: SMS OTP
Download: CSV/DAT export (90-day window per request)

Persistent Chrome profile means MFA only fires when session expires.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from app.importers.base_bank_importer import (
    find_element, find_in_frames,
    human_click, human_type,
    launch_browser, save_debug_screenshot,
    wait_for_element, wait_for_mfa_code,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://logon.merrickbank.com"
SOURCE = "merrick"


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
) -> dict:
    """
    Download Merrick Bank CSV transactions for the requested years.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    imported = skipped = errors = 0
    pw = context = page = None

    try:
        pw, context, page = launch_browser("merrick", headless=True, log=log)

        if cookies:
            log(f"Injecting {len(cookies)} saved cookies…")
            context.add_cookies(cookies)

        _login(page, username, password, log, cookies, job_id)

        for year in years:
            try:
                yi, ys, ye = _download_year(
                    page, context, year, consume_path, entity_slug, log,
                )
                imported += yi
                skipped += ys
                errors += ye
            except Exception as e:
                import traceback
                log(f"Error downloading {year}: {e}")
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

    log(f"Merrick done — imported: {imported}, skipped: {skipped}, errors: {errors}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── login ─────────────────────────────────────────────────────────────────────

def _login(page, username: str, password: str, log: Callable,
           cookies: Optional[list], job_id: int) -> None:
    if cookies:
        log("Navigating with saved cookies…")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        if "logon" not in page.url.lower() or "login" not in page.url.lower():
            log(f"Authenticated via cookies at {page.url}")
            return
        log("Cookies expired — using credential login.")

    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    save_debug_screenshot(page, "merrick_login")

    user_field = wait_for_element(page, [
        '#UserName', 'input[name="UserName"]',
        'input[id*="user" i]', 'input[name*="user" i]',
        'input[type="text"]:visible',
    ], timeout_ms=15000)
    if not user_field:
        raise RuntimeError("Merrick Bank username field not found")

    log("Entering username…")
    human_click(page, user_field)
    human_type(user_field, username)
    page.wait_for_timeout(300)

    pw_field = find_element(page, [
        '#Password', 'input[name="Password"]', 'input[type="password"]',
    ])
    if not pw_field:
        raise RuntimeError("Merrick Bank password field not found")

    log("Entering password…")
    human_click(page, pw_field)
    human_type(pw_field, password)
    page.wait_for_timeout(400)

    submit = find_element(page, [
        'button:has-text("Log In")', 'button:has-text("Sign In")',
        'input[type="submit"]', 'button[type="submit"]',
    ])
    if submit:
        human_click(page, submit)
    else:
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    save_debug_screenshot(page, "merrick_post_login")

    if _is_mfa_page(page):
        _handle_mfa(page, log, job_id)

    log(f"Logged in — at {page.url}")


def _is_mfa_page(page) -> bool:
    try:
        content = page.content().lower()
        return any(t in content for t in [
            "verification code", "security code", "one-time",
            "enter the code", "we sent", "text message", "otp",
        ])
    except Exception:
        return False


def _handle_mfa(page, log: Callable, job_id: int) -> None:
    log("MFA prompt detected — waiting for code (up to 5 minutes)…")
    save_debug_screenshot(page, "merrick_mfa")

    from app import db
    db.update_import_job(job_id, status="mfa_pending")

    code = wait_for_mfa_code(job_id, log, timeout=300)
    if not code:
        raise RuntimeError("MFA timeout — no code submitted within 5 minutes.")

    log(f"MFA code received: {code[:2]}****")
    otp_field = wait_for_element(page, [
        'input[name*="code" i]', 'input[id*="code" i]',
        'input[placeholder*="code" i]', 'input[type="tel"]',
        'input[maxlength="6"]',
    ], timeout_ms=5000)

    if otp_field:
        human_click(page, otp_field)
        human_type(otp_field, code, clear_first=True)
        page.wait_for_timeout(300)

        submit = find_element(page, [
            'button:has-text("Continue")', 'button:has-text("Verify")',
            'button:has-text("Submit")', 'button[type="submit"]',
            'input[type="submit"]',
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


# ── download ──────────────────────────────────────────────────────────────────

def _download_year(
    page, context, year: str,
    consume_path: str, entity_slug: str, log: Callable,
) -> tuple[int, int, int]:
    """Download CSV in 90-day chunks for the year. Returns (imported, skipped, errors)."""
    imported = skipped = errors = 0
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
        filename = f"{year}_{tag}_merrick.csv"
        dest_path = dest_dir / filename

        if dest_path.exists():
            log(f"  SKIP (exists): {filename}")
            skipped += 1
            continue

        log(f"  Downloading {filename}…")
        try:
            csv_bytes = _download_chunk(page, context, start_date, end_date, log)
            if csv_bytes and len(csv_bytes) > 50:
                dest_path.write_bytes(csv_bytes)
                log(f"  ✓ Saved {filename} ({len(csv_bytes):,}B)")
                # Parse and count transactions
                row_count = max(0, csv_bytes.decode("utf-8", errors="replace").count("\n") - 1)
                imported += max(1, row_count)
            else:
                log(f"  No data for {tag}")
                errors += 1
        except Exception as e:
            log(f"  ✗ Chunk {tag} failed: {e}")
            errors += 1

    return imported, skipped, errors


def _download_chunk(page, context, start_date: date, end_date: date,
                    log: Callable) -> Optional[bytes]:
    """Navigate to download UI, set dates, download CSV."""
    from patchright.sync_api import TimeoutError as PWTimeout

    # Try to navigate to the download/export section
    export_reached = False
    for sel in [
        'a:has-text("Download")', 'a:has-text("Export")',
        'a:has-text("Transaction History")',
        '[href*="download" i]', '[href*="export" i]',
        'a:has-text("Activity")',
    ]:
        el = find_element(page, [sel])
        if el:
            try:
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                page.wait_for_timeout(1500)
                export_reached = True
                break
            except Exception:
                pass

    save_debug_screenshot(page, f"merrick_export_{start_date}")

    # Set start date
    start_input = find_element(page, [
        '#startDate', '#BeginDate', '#FromDate',
        'input[name*="start" i]', 'input[name*="from" i]',
        'input[placeholder*="from" i]', 'input[placeholder*="start" i]',
    ])
    end_input = find_element(page, [
        '#endDate', '#EndDate', '#ToDate',
        'input[name*="end" i]', 'input[name*="to" i]',
        'input[placeholder*="to" i]', 'input[placeholder*="end" i]',
    ])

    if start_input:
        human_click(page, start_input)
        human_type(start_input, start_date.strftime("%m/%d/%Y"), clear_first=True)
        page.wait_for_timeout(300)
    if end_input:
        human_click(page, end_input)
        human_type(end_input, end_date.strftime("%m/%d/%Y"), clear_first=True)
        page.wait_for_timeout(300)

    # Select CSV format
    fmt = find_element(page, ['select[name*="format" i]', 'select[id*="format" i]'])
    if fmt:
        try:
            fmt.select_option(label="CSV")
        except Exception:
            try:
                fmt.select_option(value="CSV")
            except Exception:
                pass

    # Click download
    dl_btn = find_element(page, [
        'button:has-text("Download")', 'input[type="submit"][value*="Download" i]',
        'button:has-text("Export")', 'a:has-text("Download")',
    ])

    if not dl_btn:
        log("  Download button not found on Merrick Bank page")
        save_debug_screenshot(page, "merrick_no_dl_btn")
        return None

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
