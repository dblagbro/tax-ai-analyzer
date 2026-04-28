"""eStatements page navigation + readiness checks.

Extracted from the original 1,132-line ``app/importers/usalliance_importer.py``
during Phase 11G refactor. The module-level public API ``run_import`` +
``set_mfa_code`` is preserved via the package ``__init__`` so existing
callers (``app/routes/importers/import_usalliance.py``) keep working.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

from app.importers.usalliance.helpers import _save_debug_screenshot

logger = logging.getLogger(__name__)


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

