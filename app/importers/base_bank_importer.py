"""Base Playwright bank importer with anti-detection hardening.

Provides:
  - Persistent Chrome profile stored in /app/data/chrome_profiles/<bank>/
    so MFA only fires once per profile lifetime.
  - Human-like typing with random inter-keystroke delays.
  - Bézier-curve mouse movement to avoid linear motion signatures.
  - Headless → visible browser fallback when bot detection is suspected.
  - Recursive iframe traversal utility.
  - Shared MFA registry integration.

All bank-specific importers should call launch_browser() and use the helpers.
"""
from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_CHROME_PROFILES_ROOT = Path(
    os.environ.get("DATA_DIR", "/app/data")
) / "chrome_profiles"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ── mouse movement ─────────────────────────────────────────────────────────────

def _bezier(p0, p1, p2, p3, t):
    x = ((1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0]
         + 3*(1-t)*t**2*p2[0] + t**3*p3[0])
    y = ((1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1]
         + 3*(1-t)*t**2*p2[1] + t**3*p3[1])
    return x, y


def human_move(page, x: float, y: float, from_xy: Optional[tuple] = None):
    """Move mouse from from_xy (or a random point) to (x,y) along a Bézier path."""
    if from_xy:
        fx, fy = from_xy
    else:
        vp = page.viewport_size or {"width": 1280, "height": 900}
        fx = random.uniform(vp["width"] * 0.2, vp["width"] * 0.8)
        fy = random.uniform(vp["height"] * 0.2, vp["height"] * 0.8)

    jitter = lambda: random.uniform(-60, 60)
    cp1 = (fx + jitter(), fy + jitter())
    cp2 = (x + jitter(), y + jitter())

    steps = random.randint(20, 35)
    for i in range(steps + 1):
        t = i / steps
        px, py = _bezier((fx, fy), cp1, cp2, (x, y), t)
        page.mouse.move(px, py)
        time.sleep(random.uniform(0.008, 0.025))


def human_click(page, element, *, double=False):
    """Move to element with Bézier curve, then click."""
    try:
        box = element.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            human_move(page, cx, cy)
            time.sleep(random.uniform(0.05, 0.15))
    except Exception:
        pass
    if double:
        element.dblclick()
    else:
        element.click()


# ── typing ─────────────────────────────────────────────────────────────────────

def human_type(element, text: str, *, clear_first: bool = True,
               wpm_range=(55, 80)):
    """Type text with randomised inter-keystroke delays mimicking human WPM."""
    if clear_first:
        try:
            element.triple_click()
            time.sleep(random.uniform(0.05, 0.12))
        except Exception:
            pass
    avg_char_delay = 60 / (((wpm_range[0] + wpm_range[1]) / 2) * 5)
    for ch in text:
        element.press(ch)
        jitter = random.gauss(0, avg_char_delay * 0.3)
        delay = max(0.03, avg_char_delay + jitter)
        time.sleep(delay)


# ── iframe traversal ──────────────────────────────────────────────────────────

def find_in_frames(page, selector: str):
    """Recursively search all frames for the first visible element matching selector."""
    def _search(frame):
        try:
            el = frame.query_selector(selector)
            if el:
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    return el
        except Exception:
            pass
        for child in frame.child_frames:
            result = _search(child)
            if result:
                return result
        return None

    return _search(page.main_frame)


def find_all_in_frames(page, selector: str) -> list:
    """Return all visible elements matching selector across all frames."""
    results = []

    def _search(frame):
        try:
            for el in frame.query_selector_all(selector):
                try:
                    if el.is_visible():
                        results.append(el)
                except Exception:
                    results.append(el)
        except Exception:
            pass
        for child in frame.child_frames:
            _search(child)

    _search(page.main_frame)
    return results


# ── browser launch ────────────────────────────────────────────────────────────

_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
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
    "--disable-features=IsolateOrigins,site-per-process",
]


def launch_browser(bank_slug: str, headless: bool = True, log: Callable = logger.info):
    """
    Launch a Chromium browser with a persistent profile.

    Returns (playwright_context_manager, browser, context, page).
    Caller is responsible for closing browser and pw context.

    Persistent profile is stored at /app/data/chrome_profiles/<bank_slug>/
    so MFA sessions survive between runs.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed")

    profile_dir = _CHROME_PROFILES_ROOT / bank_slug
    profile_dir.mkdir(parents=True, exist_ok=True)
    log(f"Chrome profile: {profile_dir}")

    pw = sync_playwright().start()

    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            args=_STEALTH_ARGS,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            user_agent=_DEFAULT_UA,
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
    except Exception:
        pw.stop()
        raise

    page = context.pages[0] if context.pages else context.new_page()

    # Remove webdriver flag via CDP
    try:
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        """)
    except Exception:
        pass

    return pw, context, page


def launch_browser_visible_fallback(bank_slug: str, log: Callable = logger.info):
    """Same as launch_browser but forces headful mode for manual MFA entry."""
    return launch_browser(bank_slug, headless=False, log=log)


# ── element helpers ───────────────────────────────────────────────────────────

def find_element(page, selectors: list[str], timeout_ms: int = 2000):
    """Return first visible element from selector list, searching all frames."""
    for sel in selectors:
        el = find_in_frames(page, sel)
        if el:
            return el
    return None


def wait_for_element(page, selectors: list[str], timeout_ms: int = 15000):
    """Wait until any selector in the list matches, return the element."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        el = find_element(page, selectors)
        if el:
            return el
        time.sleep(0.5)
    return None


# ── MFA helpers ───────────────────────────────────────────────────────────────

def wait_for_mfa_code(job_id: int, log: Callable, timeout: int = 300) -> Optional[str]:
    """Block until a code is submitted via the MFA registry or timeout."""
    from app.importers.mfa_registry import wait_for_code
    return wait_for_code(job_id, log, timeout)


def set_mfa_code(job_id: int, code: str) -> None:
    from app.importers.mfa_registry import set_code
    set_code(job_id, code)


# ── screenshot helper ─────────────────────────────────────────────────────────

def save_debug_screenshot(page, label: str):
    try:
        ts = datetime.now().strftime("%H%M%S")
        path = f"/tmp/bank_debug_{label}_{ts}.png"
        page.screenshot(path=path)
        logger.debug(f"Screenshot: {path}")
    except Exception:
        pass


try:
    from datetime import datetime
except ImportError:
    pass
