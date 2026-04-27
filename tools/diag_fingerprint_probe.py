#!/usr/bin/env python3
"""Fingerprint probe — does the webdriver mask actually work?

Phase 10B (`5f0c2a3`) added context.add_init_script() in
base_bank_importer.launch_browser() to redefine navigator.webdriver,
plugins, and languages. This script validates that the mask is
operational by:

1. Reading every interesting navigator.* property directly via page.evaluate
2. Visiting bot.sannysoft.com (test page that scores ~25 fingerprint
   signals as "passed" or "failed")
3. Capturing creepjs (https://abrahamjuliot.github.io/creepjs/) FP score
   if reachable

Usage (inside container, with Xvfb running):
    python3 /tmp/diag_fingerprint_probe.py

Output saved to /tmp/diag_fingerprint_*.png + JSON report.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DISPLAY", ":99")

from app.importers.base_bank_importer import launch_browser

PROBES = {
    "navigator.webdriver": "navigator.webdriver",
    "navigator.webdriver_typeof": "typeof navigator.webdriver",
    "navigator.webdriver_undefined": "navigator.webdriver === undefined",
    "navigator.plugins.length": "navigator.plugins.length",
    "navigator.languages": "JSON.stringify(navigator.languages)",
    "navigator.platform": "navigator.platform",
    "navigator.vendor": "navigator.vendor",
    "navigator.userAgent": "navigator.userAgent",
    "navigator.hardwareConcurrency": "navigator.hardwareConcurrency",
    "window.chrome": "typeof window.chrome",
    "window.chrome.runtime": "typeof window.chrome?.runtime",
    "navigator.permissions": "typeof navigator.permissions?.query",
    "navigator.deviceMemory": "navigator.deviceMemory",
    "screen.width": "screen.width",
    "screen.height": "screen.height",
}


def probe(page, label, dump_to):
    print(f"\n[{label}] navigator probes:")
    results = {}
    for name, expr in PROBES.items():
        try:
            val = page.evaluate(f"() => {expr}")
        except Exception as e:
            val = f"ERR: {e!r}"
        results[name] = val
        print(f"  {name}: {val!r}")
    with open(dump_to, "w") as f:
        json.dump(results, f, indent=2)


def visit_sannysoft(page, log_prefix):
    """bot.sannysoft.com runs ~25 fingerprint tests and visually marks each
    as Pass/Fail. We screenshot + scrape the page to count results."""
    print(f"\n[{log_prefix}] visiting bot.sannysoft.com...")
    try:
        page.goto("https://bot.sannysoft.com/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(5)
        screenshot_path = f"/tmp/diag_fp_sannysoft_{log_prefix}.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"  screenshot: {screenshot_path}")

        # Sannysoft writes "passed" / "failed" classes on each row
        rows = page.evaluate("""() => {
            const out = [];
            for (const row of document.querySelectorAll('tr')) {
                const tds = row.querySelectorAll('td');
                if (tds.length < 2) continue;
                const label = tds[0].innerText.trim();
                const result = tds[1].innerText.trim();
                if (label) out.push({label, result});
            }
            return out.slice(0, 40);
        }""")
        # Count outcomes
        passed = sum(1 for r in rows if "passed" in r["result"].lower() or "ok" in r["result"].lower())
        failed = sum(1 for r in rows if "failed" in r["result"].lower() or "missing" in r["result"].lower())
        print(f"  ~{passed} passed, ~{failed} failed (heuristic from {len(rows)} rows)")
        return rows
    except Exception as e:
        print(f"  ERROR: {e!r}")
        return []


def main():
    print(f"DISPLAY={os.environ.get('DISPLAY')}")

    # Test 1: WITHOUT mask (raw patchright launch — bypasses our init-script)
    print("=" * 70)
    print("PHASE 1: BASELINE (no init-script mask)")
    print("=" * 70)
    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False, channel="chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(no_viewport=True)
        # NO add_init_script — this is the baseline
        page = ctx.new_page()
        page.goto("about:blank", timeout=10000)
        probe(page, "BASELINE", "/tmp/diag_fp_baseline.json")
        sannysoft_baseline = visit_sannysoft(page, "baseline")
        browser.close()

    # Test 2: WITH our launch_browser (which applies the mask)
    print("\n" + "=" * 70)
    print("PHASE 2: WITH PHASE-10B MASK (via launch_browser)")
    print("=" * 70)
    pw, ctx, page = launch_browser("fingerprint_probe", headless=False, log=print)
    try:
        page.goto("about:blank", timeout=10000)
        probe(page, "MASKED", "/tmp/diag_fp_masked.json")
        sannysoft_masked = visit_sannysoft(page, "masked")
    finally:
        ctx.close()
        pw.stop()

    # Save sannysoft comparison
    with open("/tmp/diag_fp_sannysoft.json", "w") as f:
        json.dump({"baseline": sannysoft_baseline, "masked": sannysoft_masked}, f, indent=2)

    # Diff summary
    print("\n" + "=" * 70)
    print("DIFF SUMMARY")
    print("=" * 70)
    with open("/tmp/diag_fp_baseline.json") as f:
        base = json.load(f)
    with open("/tmp/diag_fp_masked.json") as f:
        mask = json.load(f)
    for k in PROBES:
        b = base.get(k)
        m = mask.get(k)
        marker = "✓ same" if b == m else "✗ DIFF"
        print(f"  {marker}  {k}:")
        print(f"      baseline: {b!r}")
        print(f"      masked:   {m!r}")


if __name__ == "__main__":
    main()
