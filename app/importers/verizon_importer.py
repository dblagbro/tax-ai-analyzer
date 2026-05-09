"""Verizon My Verizon Playwright importer.

Logs into my.verizon.com, navigates to bill history, downloads PDF bills,
and parses each bill's line items (base plan, device payment, taxes per line,
fees, etc.) as individual transactions — not just the monthly total.

MFA: Verizon sends an OTP via SMS or prompts push approval via the Verizon
app. The job enters mfa_pending state and polls the MFA registry.

Persistent profile at /app/data/chrome_profiles/verizon/ means MFA only
fires on first run after session expiry.

Login URL: https://secure.verizon.com/signin
"""
from __future__ import annotations

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
    launch_browser, run_bank_import, save_auth_cookies, save_debug_screenshot,
    wait_for_element, wait_for_mfa_code,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://secure.verizon.com/signin"
BILL_HISTORY_URL = "https://www.verizon.com/myverizon/vicr/billing/bill-history"
MY_VERIZON_HOME = "https://www.verizon.com/home/myaccount"

SOURCE = "verizon"


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
    Log into My Verizon, download bill PDFs, parse line items, write to DB.

    Returns {"imported": int, "skipped": int, "errors": int}.

    Phase 14 refactor: delegates to run_bank_import. _login here returns
    bool, so the closure translates False → RuntimeError (same pattern
    as chime_importer).
    """
    def _login_fn(page, context):
        if not _login(page, username, password, log, cookies, job_id):
            raise RuntimeError("Verizon login failed — check credentials or MFA.")

    def _download_fn(page, context, _account, year):
        return _download_year(
            page, context, year,
            consume_path, entity_slug, entity_id, log,
        )

    return run_bank_import(
        slug="verizon",
        login_fn=_login_fn, download_fn=_download_fn,
        years=years, cookies=cookies, headless=True, log=log,
    )


# ── login ─────────────────────────────────────────────────────────────────────

def _login(page, username: str, password: str, log: Callable,
           cookies: Optional[list], job_id: int) -> bool:
    if cookies:
        log("Navigating with saved cookies…")
        page.goto(MY_VERIZON_HOME, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        if "signin" not in page.url.lower() and "login" not in page.url.lower():
            log(f"Authenticated via cookies at {page.url}")
            return True
        log("Cookies expired — falling back to credential login.")

    log(f"Navigating to {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    save_debug_screenshot(page, "vzn_login")

    # Verizon uses a two-step login (username → continue → password)
    user_field = wait_for_element(page, [
        '#IDField',
        'input[name="IDField"]',
        'input[id="IDToken1"]',
        'input[placeholder*="user" i]',
        'input[type="email"]',
        'input[type="text"]:visible',
    ], timeout_ms=15000)

    if not user_field:
        save_debug_screenshot(page, "vzn_no_user_field")
        raise RuntimeError("Could not find Verizon username field")

    log("Entering username…")
    human_click(page, user_field)
    human_type(user_field, username)
    page.wait_for_timeout(500)

    # May be single-page or two-step
    pw_field = find_element(page, [
        '#PWField',
        'input[name="PWField"]',
        'input[id="IDToken2"]',
        'input[type="password"]',
    ])

    if pw_field:
        log("Entering password (single-page flow)…")
        human_click(page, pw_field)
        human_type(pw_field, password)
    else:
        # Two-step: submit username first
        continue_btn = find_element(page, [
            'button:has-text("Continue")',
            'button:has-text("Next")',
            '#IDButton',
            'button[type="submit"]',
            'input[type="submit"]',
        ])
        if continue_btn:
            log("Clicking Continue (username step)…")
            human_click(page, continue_btn)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)

        pw_field = wait_for_element(page, [
            '#PWField',
            'input[type="password"]',
            'input[name="PWField"]',
        ], timeout_ms=10000)

        if pw_field:
            log("Entering password…")
            human_click(page, pw_field)
            human_type(pw_field, password)
        else:
            save_debug_screenshot(page, "vzn_no_pw_field")
            raise RuntimeError("Could not find Verizon password field")

    page.wait_for_timeout(500)
    submit = find_element(page, [
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        '#IDButton',
        'button[type="submit"]',
        'input[type="submit"]',
    ])
    if submit:
        log("Submitting login…")
        human_click(page, submit)
    else:
        pw_field.press("Enter")

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2500)
    save_debug_screenshot(page, "vzn_post_login")

    if _is_mfa_page(page):
        if not _handle_mfa(page, log, job_id):
            return False

    if "signin" in page.url.lower() or "login" in page.url.lower():
        content = page.content().lower()
        if "incorrect" in content or "invalid" in content or "error" in content:
            raise RuntimeError("Verizon credentials rejected.")
        return False

    log(f"Logged in — at {page.url}")
    save_auth_cookies(page.context, "verizon", log)
    return True


def _is_mfa_page(page) -> bool:
    mfa_texts = [
        "verification code", "one-time", "security code",
        "we sent", "enter the code", "verify your identity",
        "two-step", "text message", "check your",
        "push notification", "approve",
    ]
    try:
        content = page.content().lower()
        return any(t in content for t in mfa_texts)
    except Exception:
        return False


def _handle_mfa(page, log: Callable, job_id: int) -> bool:
    log("MFA prompt detected — waiting for code or app approval (up to 5 min)…")
    log("Submit the code via the MFA field, or approve in the Verizon app.")
    save_debug_screenshot(page, "vzn_mfa")

    from app import db
    db.update_import_job(job_id, status="mfa_pending")

    # Check if it's a push-approval flow (no code field needed)
    content = page.content().lower()
    if "approve" in content and "app" in content and "code" not in content:
        log("Push approval MFA detected — waiting for user to approve in Verizon app…")
        code = wait_for_mfa_code(job_id, log, timeout=300)
        # For push approval, any submitted value means the user has approved
        if not code:
            log("MFA timeout — no approval received.")
            return False
    else:
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
            save_debug_screenshot(page, "vzn_no_otp_field")
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
    log("MFA completed.")
    return True


# ── per-year download ─────────────────────────────────────────────────────────

def _download_year(
    page, context, year: str,
    consume_path: str, entity_slug: str,
    entity_id: Optional[int], log: Callable,
) -> tuple[int, int, int]:
    """Download and parse all Verizon bills for a year. Returns (imported, skipped, errors)."""
    imported = skipped = errors = 0
    dest_dir = Path(consume_path) / entity_slug / year
    dest_dir.mkdir(parents=True, exist_ok=True)

    bills = _get_bill_list(page, year, log)
    if not bills:
        log(f"No Verizon bills found for {year}")
        return imported, skipped, errors

    log(f"Found {len(bills)} bill(s) for {year}")

    for bill in bills:
        bill_date = bill.get("date", "unknown")
        filename = f"{year}_verizon_bill_{bill_date.replace('/', '-')}.pdf"
        dest_path = dest_dir / filename

        if dest_path.exists():
            log(f"  SKIP (exists): {filename}")
            skipped += 1
            # Still parse the cached PDF if not already in DB
            try:
                pdf_bytes = dest_path.read_bytes()
                count = _parse_and_import_pdf(pdf_bytes, filename, year, entity_id, log)
                imported += count
            except Exception:
                pass
            continue

        log(f"  Downloading {filename}…")
        try:
            pdf_bytes = _download_bill_pdf(page, context, bill, log)
            if pdf_bytes and len(pdf_bytes) > 500:
                dest_path.write_bytes(pdf_bytes)
                log(f"  Saved {filename} ({len(pdf_bytes):,}B)")
                count = _parse_and_import_pdf(pdf_bytes, filename, year, entity_id, log)
                imported += count
            else:
                log(f"  ✗ Empty PDF for {bill_date}")
                errors += 1
        except Exception as e:
            log(f"  ✗ Failed to download bill {bill_date}: {e}")
            errors += 1

    return imported, skipped, errors


def _get_bill_list(page, year: str, log: Callable) -> list[dict]:
    """Navigate to bill history and return list of bills for the given year."""
    log("Navigating to bill history…")

    for url in [BILL_HISTORY_URL, MY_VERIZON_HOME + "/billing/bill-history"]:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2500)
            if "signin" not in page.url.lower():
                log(f"Bill history page: {page.url}")
                break
        except Exception:
            continue

    save_debug_screenshot(page, f"vzn_bills_{year}")

    # Try to find bill list via JavaScript
    try:
        bills = page.evaluate(f"""
            () => {{
                const results = [];
                const seen = new Set();
                // Look for bill date elements and associated download links
                const datePattern = /(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d{{1,2}},?\\s+{year}/i;
                document.querySelectorAll('*').forEach(el => {{
                    const text = (el.textContent || '').trim();
                    if (datePattern.test(text) && text.length < 80) {{
                        const link = el.closest('[href]') ||
                                     el.querySelector('a[href*="pdf"], a[href*="bill"], a[href*="download"]') ||
                                     el.parentElement && el.parentElement.querySelector('a');
                        const href = link ? (link.getAttribute('href') || link.href) : '';
                        if (!seen.has(text)) {{
                            seen.add(text);
                            results.push({{date: text.slice(0, 20), href: href}});
                        }}
                    }}
                }});
                return results.slice(0, 24);
            }}
        """)

        valid = [b for b in bills if b.get("date")]
        log(f"Found {len(valid)} bill entries for {year}")
        return valid

    except Exception as e:
        log(f"Bill list JS evaluation failed: {e}")

    # Fallback: click through pagination to find bills
    return _scan_bill_list_dom(page, year, log)


def _scan_bill_list_dom(page, year: str, log: Callable) -> list[dict]:
    """Scan DOM selectors for bill download links."""
    bills = []
    seen: set[str] = set()

    for sel in [
        'a[href*="pdf"]',
        'a[href*="bill"]',
        'a[href*="invoice"]',
        'button[aria-label*="download" i]',
        '[data-testid*="bill-download"]',
        '[data-testid*="pdf"]',
    ]:
        for el in find_all_in_frames(page, sel):
            try:
                text = (el.text_content() or el.get_attribute("aria-label") or "").strip()
                href = el.get_attribute("href") or ""
                key = href or text
                if key and key not in seen and year in (text + href):
                    seen.add(key)
                    bills.append({"date": text[:20], "href": href, "element_handle": None})
            except Exception:
                pass

    return bills


def _download_bill_pdf(page, context, bill: dict, log: Callable) -> Optional[bytes]:
    """Download a single bill PDF. Returns bytes or None."""
    href = bill.get("href", "")

    # Direct URL download
    if href and href.startswith("http"):
        try:
            with page.expect_download(timeout=30000) as dl_info:
                page.goto(href, wait_until="domcontentloaded", timeout=20000)
            dl = dl_info.value
            buf = io.BytesIO()
            stream = dl.create_read_stream()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf.write(chunk)
            return buf.getvalue() if buf.tell() > 0 else None
        except Exception as e:
            log(f"  Direct PDF download failed: {e}")

    # Relative href
    if href:
        try:
            from urllib.parse import urljoin
            full_url = urljoin(page.url, href)
            with page.expect_download(timeout=30000) as dl_info:
                page.goto(full_url, wait_until="domcontentloaded", timeout=20000)
            dl = dl_info.value
            buf = io.BytesIO()
            stream = dl.create_read_stream()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buf.write(chunk)
            return buf.getvalue() if buf.tell() > 0 else None
        except Exception as e:
            log(f"  Relative URL PDF download failed: {e}")

    # Try clicking the bill element if we have its text
    bill_text = bill.get("date", "")
    if bill_text:
        try:
            link = find_element(page, [
                f'a:has-text("{bill_text}")',
                f'[aria-label*="{bill_text}"]',
            ])
            if link:
                with page.expect_download(timeout=30000) as dl_info:
                    human_click(page, link)
                dl = dl_info.value
                buf = io.BytesIO()
                stream = dl.create_read_stream()
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    buf.write(chunk)
                return buf.getvalue() if buf.tell() > 0 else None
        except Exception as e:
            log(f"  Element click download failed: {e}")

    return None


# ── PDF parsing ───────────────────────────────────────────────────────────────

def _parse_and_import_pdf(
    pdf_bytes: bytes,
    filename: str,
    year: str,
    entity_id: Optional[int],
    log: Callable,
) -> int:
    """Parse a Verizon bill PDF and upsert line items as individual transactions.

    Returns number of transactions written to DB.
    """
    try:
        import pdfplumber
    except ImportError:
        log("pdfplumber not installed — cannot parse PDF line items")
        return 0

    from app import db

    log(f"Parsing bill PDF: {filename}")

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            line_items = _extract_verizon_line_items(pdf, log)
    except Exception as e:
        log(f"PDF parse failed: {e}")
        return 0

    if not line_items:
        log("No line items extracted from PDF")
        return 0

    log(f"Extracted {len(line_items)} line items from {filename}")
    count = 0
    for item in line_items:
        try:
            import hashlib
            raw = f"{item['date']}|{item['description']}|{item['amount']}|{filename}"
            source_id = f"verizon:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"
            db.upsert_transaction(
                source=SOURCE,
                source_id=source_id,
                entity_id=entity_id,
                tax_year=year,
                date=item["date"],
                amount=item["amount"],
                vendor="Verizon",
                description=item["description"],
                category="expense",
                doc_type="bill",
                metadata_json=json.dumps({"file": filename, "line_type": item.get("line_type", "")}),
            )
            count += 1
        except Exception as e:
            log(f"  DB insert failed: {e}")

    log(f"Wrote {count} Verizon line items to DB from {filename}")
    return count


def _extract_verizon_line_items(pdf, log: Callable) -> list[dict]:
    """
    Extract individual charge line items from a Verizon bill PDF.

    Verizon bills have sections per phone line plus shared plan charges.
    Each section has line items like:
      Base plan              $XX.XX
      Device payment         $XX.XX
      Federal taxes          $X.XX
      State/local taxes      $X.XX

    We emit each non-zero charge as a separate transaction.
    """
    items = []
    bill_date = None

    # Pattern: charge description (left) + dollar amount (right)
    # Amounts may be negative (credits)
    amount_re = re.compile(r"\$\s*([\d,]+\.\d{2})")
    date_re = re.compile(r"\b(\w+ \d{1,2},? \d{4})\b")
    section_re = re.compile(
        r"(account charges|monthly charges|one-time charges|"
        r"taxes.*fees|surcharges|credits|adjustments)",
        re.IGNORECASE,
    )

    full_text = ""
    for page in pdf.pages:
        text = page.extract_text() or ""
        full_text += text + "\n"
        # Try table-based extraction for well-formatted pages
        tables = page.extract_tables()
        for table in tables:
            for row in table:
                if not row:
                    continue
                cells = [str(c or "").strip() for c in row]
                item = _parse_verizon_table_row(cells, bill_date)
                if item:
                    items.append(item)

    # Extract bill date from full text
    for m in date_re.finditer(full_text[:500]):
        try:
            bill_date = datetime.strptime(m.group(1).replace(",", ""), "%B %d %Y").strftime("%Y-%m-%d")
            break
        except ValueError:
            pass

    if not bill_date:
        bill_date = datetime.now().strftime("%Y-01-01")

    # If table extraction found nothing, parse text lines
    if not items:
        items = _parse_verizon_text_lines(full_text, bill_date, log)

    # Deduplicate
    seen: set[str] = set()
    unique = []
    for item in items:
        key = f"{item['description']}|{item['amount']}"
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


def _parse_verizon_table_row(cells: list[str], bill_date: Optional[str]) -> Optional[dict]:
    """Parse a table row from a Verizon bill page."""
    if len(cells) < 2:
        return None

    # Look for a dollar amount in any cell
    amount_re = re.compile(r"-?\$?\s*([\d,]+\.\d{2})")
    desc = cells[0].strip()
    amount_val = None

    for cell in cells[1:]:
        m = amount_re.search(cell)
        if m:
            try:
                neg = "-" in cell and not cell.strip().startswith("-")
                raw = m.group(1).replace(",", "")
                amount_val = -float(raw) if neg else float(raw)
                break
            except ValueError:
                pass

    if not desc or amount_val is None or amount_val == 0:
        return None

    # Skip obvious header/total rows
    lower = desc.lower()
    if any(skip in lower for skip in ["total", "subtotal", "balance", "amount due", "page"]):
        return None

    return {
        "date": bill_date or datetime.now().strftime("%Y-%m-%d"),
        "description": f"Verizon: {desc}"[:200],
        "amount": -abs(amount_val),  # bills are expenses (negative)
        "line_type": "charge",
    }


def _parse_verizon_text_lines(text: str, bill_date: str, log: Callable) -> list[dict]:
    """Parse plain text from Verizon bill pages when table extraction fails."""
    items = []
    lines = text.split("\n")

    # Pattern: description followed by amount on same line or next line
    amount_re = re.compile(r"(-?\$[\d,]+\.\d{2}|\$[\d,]+\.\d{2})")
    skip_patterns = [
        "total", "subtotal", "amount due", "balance forward",
        "account number", "billing period", "page ", "www.",
        "customer service", "thank you", "verizon wireless",
    ]

    for i, line in enumerate(lines):
        line = line.strip()
        if not line or len(line) < 3:
            continue
        lower = line.lower()
        if any(p in lower for p in skip_patterns):
            continue

        m_amounts = amount_re.findall(line)
        if not m_amounts:
            continue

        # Take the last dollar amount on the line as the charge
        raw_amt = m_amounts[-1].replace("$", "").replace(",", "")
        try:
            amount = float(raw_amt)
        except ValueError:
            continue

        if amount == 0:
            continue

        # Description: text before the amount
        amt_pos = line.rfind(m_amounts[-1])
        desc = line[:amt_pos].strip()
        desc = re.sub(r"\s{2,}", " ", desc)
        if not desc or len(desc) < 3:
            continue

        items.append({
            "date": bill_date,
            "description": f"Verizon: {desc}"[:200],
            "amount": -abs(amount),  # expenses
            "line_type": "text_parsed",
        })

    return items
