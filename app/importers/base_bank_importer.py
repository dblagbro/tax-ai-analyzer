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
    # Do NOT pass --headless= — patchright + channel=chrome + headless=False
    # launches real visible Chrome under Xvfb, which has a much lower
    # fingerprint than --headless=new in bundled Chromium.
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
    # Window sizing is handled by Xvfb's --screen arg, not a Chrome flag.
    "--lang=en-US",
    "--accept-lang=en-US",
]


def launch_browser(bank_slug: str, headless: bool = False, log: Callable = logger.info):
    """
    Launch a hardened Chromium with the same anti-detection config that works
    for US Alliance FCU (see app/importers/usalliance_importer.py for provenance).

    Stealth is hooked at the playwright-instance level BEFORE launch, then also
    applied to the page — both lifecycle points matter for different signals
    (the pw-level hook rewires the driver, the page-level apply covers
    late-bound init scripts).

    Uses an ephemeral browser+context rather than launch_persistent_context:
    persistent profiles leak fingerprint anomalies back to detectors when
    they're not a real, accumulated user-driven profile.

    Returns (pw, context, page). Caller closes context + calls pw.stop().
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("patchright not installed")

    # patchright is a hardened Playwright drop-in that pre-patches CDP
    # Runtime.Enable and driver-level fingerprints at build time — no need for
    # a separate playwright_stealth hook. Use real Chrome channel + visible
    # (headful) browser via Xvfb for the lowest fingerprint.
    pw = sync_playwright().start()

    try:
        browser = pw.chromium.launch(
            headless=headless,
            channel="chrome",  # Step 3: use real Google Chrome, not bundled Chromium
            args=_STEALTH_ARGS,
        )
        context = browser.new_context(
            accept_downloads=True,
            no_viewport=True,  # Step 4: let Xvfb framebuffer drive size
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

    # NOTE: an earlier Phase-10B commit (5f0c2a3) called
    # context.add_init_script() here to redefine navigator.webdriver to
    # `undefined`. A 2026-04-24 fingerprint probe (tools/diag_fingerprint_probe.py)
    # proved that patchright deliberately suppresses runtime JS injection
    # — both `add_init_script` AND direct CDP
    # `Page.addScriptToEvaluateOnNewDocument` are silently no-op'd. (This
    # is a patchright design choice: runtime injection is itself a
    # detectable fingerprint, so they refuse to do it.)
    #
    # Result: patchright leaves `navigator.webdriver` as boolean `false`
    # (better than the default `true`, but not `undefined` like a real
    # human browser). Strict detectors that check `=== undefined` will
    # still flag us. To override webdriver further would require either
    # (a) abandon patchright for a tool that allows runtime injection
    # (most also less stealthy), or (b) use a Chrome --user-data-dir
    # with a real-human profile (contradicts Phase-9's ephemeral-context
    # decision).
    #
    # Filed as MED-PASS2-2 in qa/bug-log-post-phase9-pass2.md (deferred).
    # No-op kept here to make the limitation discoverable from code.

    page = context.new_page()
    return pw, context, page


# ── Cookie persistence helpers ────────────────────────────────────────────────

def save_auth_cookies(context, bank_slug: str, log: Callable = logger.info) -> int:
    """Capture the context's current cookies and store them under
    `<bank_slug>_cookies` in the settings table. Returns cookie count.

    Call this AFTER the importer has proven an authenticated session
    (e.g. after MFA approval + navigation to a post-login URL). Saving
    pre-auth cookies would let a bad cookie jar poison future runs.
    """
    try:
        import json as _json
        from app import db as _db
        cookies = context.cookies()
        if not cookies:
            return 0
        _db.set_setting(f"{bank_slug}_cookies", _json.dumps(cookies))
        log(f"💾 Saved {len(cookies)} auth cookies for {bank_slug}")
        return len(cookies)
    except Exception as e:
        log(f"  cookie save failed ({bank_slug}): {e!r}")
        return 0


def load_auth_cookies(bank_slug: str) -> Optional[list]:
    """Return the saved cookie list for `<bank_slug>_cookies`, or None.

    Complement to save_auth_cookies. Callers should pass the result to
    `context.add_cookies(...)` before the first navigation. If the cookies
    are stale (server rejected the session), the importer MUST fall back
    to the full credential login flow — caller's responsibility to check.
    """
    try:
        import json as _json
        from app import db as _db
        raw = _db.get_setting(f"{bank_slug}_cookies") or ""
        if not raw:
            return None
        cookies = _json.loads(raw)
        return cookies if isinstance(cookies, list) and cookies else None
    except Exception:
        return None


# ── CAPTCHA / human-verification handling ─────────────────────────────────────

def _normalize_apostrophes(text: str) -> str:
    """Replace typographic quote variants with ASCII so string matching works
    whether the page uses U+2019 (smart quote) or U+0027 (ASCII)."""
    return (text.replace("’", "'")
                .replace("‘", "'")
                .replace("“", '"')
                .replace("”", '"'))


def handle_captcha_if_present(page, log: Callable = logger.info, timeout_ms: int = 3000) -> bool:
    """Detect + click through common 'confirm you're a person' checkbox CAPTCHAs.

    Returns True if a CAPTCHA was handled (page may need reload to proceed),
    False if none was found. Does NOT solve hCaptcha/reCAPTCHA image puzzles —
    those require user intervention or external solvers.
    """
    try:
        raw = page.content()
        content = _normalize_apostrophes(raw).lower()
    except Exception:
        return False

    captcha_markers = [
        "confirm you're a person", "i'm a person", "i am a person",
        "verify you're human", "verify you are human",
        "prove you are not a robot", "press & hold", "press and hold",
    ]
    if not any(m in content for m in captcha_markers):
        return False

    log("CAPTCHA challenge detected — attempting to click through")

    # Dump the page HTML to /tmp for debugging selector selection if we fail
    try:
        import os
        dump_path = f"/tmp/bank_debug_captcha_dump.html"
        with open(dump_path, "w") as f:
            f.write(raw)
        log(f"  HTML dumped to {dump_path}")
    except Exception:
        pass

    # Try MANY click strategies — US Bank's bot-detect overlay uses custom widgets
    checkbox_selectors = [
        # Native checkbox variants
        "input[type='checkbox'][aria-label*='person' i]",
        "input[type='checkbox'][name*='person' i]",
        "input[type='checkbox'][id*='person' i]",
        # Label-by-text
        "label:has-text(\"I'm a person\")",
        "label:has-text(\"I am a person\")",
        "label:has-text(\"human\")",
        # ARIA roles (custom React widgets)
        "[role='checkbox'][aria-label*='person' i]",
        "[role='checkbox']:has-text('person')",
        # Parent label wrapping an input — click the label visible text
        "xpath=//label[contains(., \"I'm a person\")]",
        "xpath=//label[contains(., \"I am a person\")]",
        # Span or div near the checkbox
        "xpath=//*[contains(text(), \"I'm a person\")]/preceding::input[@type='checkbox'][1]",
        "xpath=//*[contains(text(), \"I'm a person\")]/ancestor::*[contains(@class, 'check')][1]",
        # Desperate: any checkbox inside the modal
        "div[role='dialog'] input[type='checkbox']",
        "[class*='modal'] input[type='checkbox']",
        "[class*='challenge'] input[type='checkbox']",
    ]
    clicked_checkbox = False
    for sel in checkbox_selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            try:
                el.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                box = el.bounding_box()
                if box:
                    human_move(page, box["x"] + box["width"] / 2,
                               box["y"] + box["height"] / 2)
            except Exception:
                pass
            # Try both .click() and .check() for checkbox semantics
            try:
                el.click(timeout=timeout_ms, force=True)
            except Exception:
                try:
                    el.check(timeout=timeout_ms, force=True)
                except Exception:
                    continue
            log(f"  clicked: {sel}")
            clicked_checkbox = True
            page.wait_for_timeout(800)
            break
        except Exception:
            continue

    # If no checkbox click worked, try clicking the Continue button directly
    # (some implementations auto-verify on button press)
    cont_selectors = [
        "button:has-text('Continue'):not([disabled])",
        "button:has-text('Continue')",
        "button:has-text('Verify')",
        "button:has-text('Submit')",
        "button[type='submit']",
        "[role='button']:has-text('Continue')",
    ]
    for sel in cont_selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            try:
                if el.is_disabled():
                    continue
            except Exception:
                pass
            el.click(timeout=timeout_ms)
            log(f"  clicked continue: {sel}")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            return True
        except Exception:
            continue

    if not clicked_checkbox:
        log("  CAPTCHA handler: no clickable widget found — selector set needs update")
    return clicked_checkbox


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
