"""Pure-utility helpers used across the US Alliance importer.

Extracted from the original 1,132-line ``app/importers/usalliance_importer.py``
during Phase 11G refactor. The module-level public API ``run_import`` +
``set_mfa_code`` is preserved via the package ``__init__`` so existing
callers (``app/routes/importers/import_usalliance.py``) keep working.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)


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
