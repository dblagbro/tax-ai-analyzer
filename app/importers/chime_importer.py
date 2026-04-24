"""Chime Playwright importer.

Uses persistent Chrome profile + bot-deflection (Bézier curves, human typing)
to log in to Chime's web app, download transaction exports (CSV), and write
transactions to the DB.

MFA: Chime sends a one-time code via SMS. The job enters mfa_pending state
and polls the MFA registry until the user submits the code.

Persistent profile at /app/data/chrome_profiles/chime/ means MFA only fires
once per session lifetime (until cookies expire).

Login URL: https://member.chime.com/login/identifier
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

from app.importers.base_bank_importer import (
    find_element, find_in_frames, find_all_in_frames,
    human_click, human_move, human_type,
    launch_browser, save_auth_cookies, save_debug_screenshot,
    wait_for_element, wait_for_mfa_code,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://member.chime.com/login/identifier"
APP_URL = "https://app.chime.com"
SOURCE = "chime"


def set_mfa_code(job_id: int, code: str) -> None:
    from app.importers.mfa_registry import set_code
    set_code(job_id, code)


def run_import(
    email: str,
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
    Log into Chime, download transaction export(s), write to DB.

    Returns {"imported": int, "skipped": int, "errors": int}.
    """
    imported = skipped = errors = 0
    pw = context = page = None

    try:
        pw, context, page = launch_browser("chime", headless=True, log=log)

        if cookies:
            log(f"Injecting {len(cookies)} saved cookies…")
            context.add_cookies(cookies)

        logged_in = _login(page, email, password, log, cookies, job_id)
        if not logged_in:
            raise RuntimeError("Chime login failed — check credentials or MFA.")

        for year in years:
            try:
                yi, ys, ye = _download_year(
                    page, context, year,
                    consume_path, entity_slug, entity_id, log,
                )
                imported += yi
                skipped += ys
                errors += ye
            except Exception as e:
                import traceback
                log(f"Error downloading year {year}: {e}")
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

    log(f"Chime done — imported: {imported}, skipped: {skipped}, errors: {errors}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ── login ─────────────────────────────────────────────────────────────────────

def _login(page, email: str, password: str, log: Callable,
           cookies: Optional[list], job_id: int) -> bool:
    # Try cookies first
    if cookies:
        log("Navigating with saved cookies…")
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        if "login" not in page.url.lower() and "identifier" not in page.url.lower():
            log(f"Authenticated via cookies at {page.url}")
            return True
        log("Cookies expired — falling back to credential login.")

    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    save_debug_screenshot(page, "chime_login")

    # Step 1: enter email / identifier
    email_field = wait_for_element(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[id*="email" i]',
        'input[placeholder*="email" i]',
        'input[type="text"]:visible',
    ], timeout_ms=15000)

    if not email_field:
        save_debug_screenshot(page, "chime_no_email_field")
        raise RuntimeError("Could not find Chime email field")

    log("Entering email…")
    human_click(page, email_field)
    human_type(email_field, email)
    page.wait_for_timeout(500)

    # Submit email
    continue_btn = find_element(page, [
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button[type="submit"]',
        'input[type="submit"]',
    ])
    if continue_btn:
        human_click(page, continue_btn)
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)

    save_debug_screenshot(page, "chime_post_email")

    # Step 2: enter password (may be on same page or next page)
    pw_field = wait_for_element(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[id*="password" i]',
    ], timeout_ms=10000)

    if not pw_field:
        save_debug_screenshot(page, "chime_no_pw_field")
        raise RuntimeError("Could not find Chime password field")

    log("Entering password…")
    human_click(page, pw_field)
    human_type(pw_field, password)
    page.wait_for_timeout(500)

    submit = find_element(page, [
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("Continue")',
        'button[type="submit"]',
        'input[type="submit"]',
    ])
    if submit:
        human_click(page, submit)
    else:
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    save_debug_screenshot(page, "chime_post_login")

    if _is_mfa_page(page):
        if not _handle_mfa(page, log, job_id):
            return False

    if "login" in page.url.lower() or "identifier" in page.url.lower():
        content = page.content().lower()
        if "incorrect" in content or "invalid" in content or "wrong" in content:
            raise RuntimeError("Chime credentials rejected.")
        return False

    log(f"Logged in — at {page.url}")
    save_auth_cookies(page.context, "chime", log)
    return True


def _is_mfa_page(page) -> bool:
    mfa_texts = [
        "verification code", "one-time", "security code",
        "we sent", "enter the code", "confirm your identity",
        "two-step", "text message", "check your",
    ]
    try:
        content = page.content().lower()
        return any(t in content for t in mfa_texts)
    except Exception:
        return False


def _handle_mfa(page, log: Callable, job_id: int) -> bool:
    log("MFA prompt detected — waiting for code (up to 5 minutes)…")
    log("Submit the code via the MFA field on the Import page.")
    save_debug_screenshot(page, "chime_mfa")

    from app import db
    db.update_import_job(job_id, status="mfa_pending")

    code = wait_for_mfa_code(job_id, log, timeout=300)
    if not code:
        log("MFA timeout.")
        return False

    log(f"MFA code received: {code[:2]}****")
    otp_field = wait_for_element(page, [
        'input[placeholder*="code" i]',
        'input[name*="otp" i]',
        'input[id*="otp" i]',
        'input[type="tel"]',
        'input[maxlength="6"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
    ], timeout_ms=5000)

    if not otp_field:
        save_debug_screenshot(page, "chime_no_otp_field")
        return False

    human_click(page, otp_field)
    human_type(otp_field, code, clear_first=True)
    page.wait_for_timeout(400)

    submit = find_element(page, [
        'button:has-text("Continue")',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button[type="submit"]',
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


# ── per-year download ─────────────────────────────────────────────────────────

def _download_year(
    page, context, year: str,
    consume_path: str, entity_slug: str,
    entity_id: Optional[int], log: Callable,
) -> tuple[int, int, int]:
    """Download transactions for a full year. Returns (imported, skipped, errors)."""
    imported = skipped = errors = 0
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Try CSV export first
    csv_bytes = _try_csv_export(page, context, year, log)
    if csv_bytes:
        filename = f"{year}_chime_transactions.csv"
        dest_path = dest_dir / filename
        dest_path.write_bytes(csv_bytes)
        log(f"Saved {filename} ({len(csv_bytes):,}B)")
        count = _import_csv_bytes(csv_bytes, year, entity_id, log)
        imported += count
        return imported, skipped, errors

    # Fall back to scraping the transaction list from the DOM
    log(f"CSV export not available — scraping transaction list for {year}…")
    transactions = _scrape_transactions(page, year, log)
    if transactions:
        for txn in transactions:
            try:
                from app import db
                source_id = _make_source_id(txn)
                db.upsert_transaction(
                    source=SOURCE,
                    source_id=source_id,
                    entity_id=entity_id,
                    tax_year=year,
                    date=txn["date"],
                    amount=txn["amount"],
                    vendor=txn.get("vendor", txn["description"][:60]),
                    description=txn["description"],
                    category=txn.get("category", ""),
                    doc_type="bank_statement",
                )
                imported += 1
            except Exception as e:
                log(f"DB insert failed: {e}")
                errors += 1
    else:
        log(f"No transactions found for {year}")

    return imported, skipped, errors


def _try_csv_export(page, context, year: str, log: Callable) -> Optional[bytes]:
    """Navigate to Chime transactions and attempt CSV export. Returns bytes or None."""
    # Navigate to transactions / spending
    for url in [
        "https://app.chime.com/spending",
        "https://app.chime.com/transactions",
        "https://member.chime.com/spending",
    ]:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            if "login" not in page.url.lower() and "identifier" not in page.url.lower():
                log(f"Transactions page: {page.url}")
                break
        except Exception:
            continue

    save_debug_screenshot(page, f"chime_transactions_{year}")

    # Look for Export / Download button
    export_btn = find_element(page, [
        'button:has-text("Export")',
        'button:has-text("Download")',
        'a:has-text("Export")',
        'a:has-text("Download")',
        '[aria-label*="export" i]',
        '[aria-label*="download" i]',
        '[data-testid*="export" i]',
    ])

    if not export_btn:
        log("No export button found on Chime transactions page")
        return None

    log("Found export button — clicking…")
    try:
        with page.expect_download(timeout=15000) as dl_info:
            human_click(page, export_btn)

        # There may be a date range dialog
        page.wait_for_timeout(1000)
        _set_date_range_if_dialog(page, year, log)

        dl = dl_info.value
        buf = io.BytesIO()
        stream = dl.create_read_stream()
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buf.write(chunk)
        data = buf.getvalue()
        if data and len(data) > 50:
            log(f"Downloaded CSV: {len(data):,} bytes")
            return data
    except Exception as e:
        log(f"CSV export attempt failed: {e}")

    return None


def _set_date_range_if_dialog(page, year: str, log: Callable) -> None:
    """If an export dialog with date range appears, fill it for the full year."""
    try:
        date_inputs = page.query_selector_all('input[type="date"], input[placeholder*="date" i]')
        if len(date_inputs) >= 2:
            log(f"Setting date range for {year}…")
            date_inputs[0].fill(f"{year}-01-01")
            page.wait_for_timeout(200)
            date_inputs[1].fill(f"{year}-12-31")
            page.wait_for_timeout(200)

        confirm = find_element(page, [
            'button:has-text("Export")',
            'button:has-text("Download")',
            'button:has-text("Confirm")',
            'button:has-text("Submit")',
        ])
        if confirm:
            human_click(page, confirm)
            page.wait_for_timeout(500)
    except Exception as e:
        log(f"Date range dialog handling failed: {e}")


def _scrape_transactions(page, year: str, log: Callable) -> list[dict]:
    """Scrape transactions from the Chime web app DOM as a fallback."""
    transactions = []
    seen: set[str] = set()

    # Scroll down to load more transactions
    for _ in range(20):
        page.evaluate("window.scrollBy(0, 800)")
        page.wait_for_timeout(400)

    save_debug_screenshot(page, f"chime_scrape_{year}")

    # Try known Chime DOM patterns
    try:
        items = page.evaluate(f"""
            () => {{
                const results = [];
                const seen = new Set();
                // Try multiple selectors for transaction rows
                const selectors = [
                    '[data-testid*="transaction"]',
                    '[class*="transaction"]',
                    '[class*="TransactionItem"]',
                    'li[class*="item"]',
                ];
                for (const sel of selectors) {{
                    document.querySelectorAll(sel).forEach(el => {{
                        const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (!text || seen.has(text)) return;
                        seen.add(text);
                        results.push(text);
                    }});
                    if (results.length > 0) break;
                }}
                return results.slice(0, 500);
            }}
        """)

        for text in items:
            txn = _parse_dom_text(text, year)
            if txn:
                key = f"{txn['date']}|{txn['amount']}|{txn['description']}"
                if key not in seen:
                    seen.add(key)
                    transactions.append(txn)

    except Exception as e:
        log(f"DOM scrape failed: {e}")

    log(f"Scraped {len(transactions)} transactions for {year}")
    return transactions


def _parse_dom_text(text: str, year: str) -> Optional[dict]:
    """Best-effort parse of a transaction element's text content."""
    date_re = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4}|\w{3}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})\b")
    amount_re = re.compile(r"[−\-]?\$?[\d,]+\.[\d]{2}")

    m_date = date_re.search(text)
    m_amounts = amount_re.findall(text)
    if not m_date or not m_amounts:
        return None

    txn_date = _parse_date(m_date.group(0))
    if not txn_date or not txn_date.startswith(year):
        return None

    amount = _parse_amount(m_amounts[0])
    if amount is None:
        return None

    desc_text = text[m_date.end():].strip()
    desc_text = amount_re.sub("", desc_text).strip()
    desc = re.sub(r"\s+", " ", desc_text)[:200] or text[:100]

    return {
        "date": txn_date,
        "amount": amount,
        "description": desc,
        "vendor": desc[:60],
    }


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _import_csv_bytes(csv_bytes: bytes, year: str, entity_id: Optional[int],
                      log: Callable) -> int:
    """Parse a Chime CSV export and upsert all transactions. Returns count inserted."""
    from app import db

    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = csv_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        log("CSV has no header row")
        return 0

    # Normalize headers
    headers = {h.lower().strip().strip("﻿"): h for h in reader.fieldnames}
    col_date = _find_col_name(headers, ["date", "transaction date", "posted date"])
    col_desc = _find_col_name(headers, ["description", "name", "memo", "merchant"])
    col_amt = _find_col_name(headers, ["amount", "debit", "credit"])
    col_cat = _find_col_name(headers, ["category", "type"])

    if not col_date or not col_amt:
        log(f"CSV columns not recognized — headers: {list(headers.keys())}")
        return 0

    count = 0
    for row in reader:
        try:
            raw_date = row.get(col_date, "").strip()
            raw_amt = row.get(col_amt, "").strip()
            desc = row.get(col_desc, "").strip() if col_desc else ""
            category = row.get(col_cat, "").strip() if col_cat else ""

            txn_date = _parse_date(raw_date)
            if not txn_date:
                continue
            if not txn_date.startswith(year):
                continue

            amount = _parse_amount(raw_amt)
            if amount is None:
                continue

            txn = {"date": txn_date, "amount": amount, "description": desc}
            source_id = _make_source_id(txn)
            db.upsert_transaction(
                source=SOURCE,
                source_id=source_id,
                entity_id=entity_id,
                tax_year=year,
                date=txn_date,
                amount=amount,
                vendor=desc[:60],
                description=desc,
                category=category,
                doc_type="bank_statement",
            )
            count += 1
        except Exception as e:
            log(f"CSV row error: {e}")

    log(f"Chime CSV: {count} transactions imported for {year}")
    return count


def _find_col_name(normalized_map: dict, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in normalized_map:
            return normalized_map[c]
    return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> Optional[str]:
    raw = raw.strip().replace("−", "-")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%b %d %Y",
                "%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_amount(raw: str) -> Optional[float]:
    if not raw:
        return None
    cleaned = raw.replace("−", "-").replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _make_source_id(txn: dict) -> str:
    import hashlib
    raw = f"{txn['date']}|{txn['description']}|{txn['amount']}"
    return f"chime:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"
