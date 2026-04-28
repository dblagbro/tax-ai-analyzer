"""Per-year statement discovery + PDF download orchestration.

Extracted from the original 1,132-line ``app/importers/usalliance_importer.py``
during Phase 11G refactor. The module-level public API ``run_import`` +
``set_mfa_code`` is preserved via the package ``__init__`` so existing
callers (``app/routes/importers/import_usalliance.py``) keep working.
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.importers.usalliance.helpers import (
    _safe_filename,
    _months_for_year,
    _save_debug_screenshot,
)
from app.importers.usalliance.estatements import (
    _navigate_to_estatements,
    _wait_for_documents_content,
)

logger = logging.getLogger(__name__)


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

