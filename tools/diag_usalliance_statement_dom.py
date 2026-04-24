#!/usr/bin/env python3
"""Diagnostic: capture the US Alliance statement-list page DOM + attributes +
full row HTML, so we can reverse-engineer the proper download URL pattern
without blowing through MFA cycles.

Uses saved usalliance_cookies from the DB so it runs MFA-free.

Usage (inside tax-ai-analyzer container):
    python3 /app/tools/diag_usalliance_statement_dom.py

Output:
    /tmp/diag_usa_statements.json  — structured dump (rows + attributes)
    /tmp/diag_usa_before_click.png  — page screenshot
    /tmp/diag_usa_after_click.png   — after clicking one row
    /tmp/diag_usa_post_click_requests.json — network requests fired by click
"""
import json
import os
import sys
import time

sys.path.insert(0, "/app")
os.chdir("/app")

from app import db as _db

username = _db.get_setting("usalliance_username")
password = _db.get_setting("usalliance_password")
cookies_raw = _db.get_setting("usalliance_cookies") or ""
cookies = json.loads(cookies_raw) if cookies_raw else None

if not cookies:
    print("[!] No saved cookies — run a full import first to seed the cookie jar")
    sys.exit(1)

print(f"[*] Using {len(cookies)} saved cookies")

os.environ["DISPLAY"] = ":99"
from patchright.sync_api import sync_playwright

BASE = "https://account.usalliance.org"
STATEMENTS_URL = f"{BASE}/documents/docs/cash-accounts"

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=False, channel="chrome",
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        no_viewport=True, locale="en-US", timezone_id="America/New_York",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    )
    ctx.add_cookies(cookies)
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});"
    )

    all_requests = []
    ctx.on("request", lambda r: all_requests.append({
        "method": r.method, "url": r.url[:300], "ts": time.time(),
    }))
    all_responses = []
    ctx.on("response", lambda r: all_responses.append({
        "status": r.status, "url": r.url[:300],
        "content_type": r.headers.get("content-type", ""),
        "ts": time.time(),
    }))

    page = ctx.new_page()

    print(f"[*] Navigating to {STATEMENTS_URL}")
    page.goto(STATEMENTS_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    print(f"[*] URL after nav: {page.url}")

    page.screenshot(path="/tmp/diag_usa_before_click.png", full_page=True)

    # Dump ALL interesting rows + attributes
    dom = page.evaluate("""
        () => {
            // Find anything that looks like a statement row
            const candidates = Array.from(document.querySelectorAll(
                'tr, [role="row"], [class*="statement"], [class*="document"], [class*="row"]'
            ));
            const rows = [];
            for (const el of candidates) {
                const text = (el.innerText || '').trim();
                if (text.match(/\\d{4}.*statement/i) || text.match(/statement.*\\d{4}/i)) {
                    const attrs = {};
                    for (const a of el.attributes) attrs[a.name] = a.value;
                    // Also capture children with [data-*] or [href] or [onclick]
                    const children = Array.from(el.querySelectorAll('[href], [data-*], [onclick]'))
                        .slice(0, 10)
                        .map(c => {
                            const cattrs = {};
                            for (const a of c.attributes) cattrs[a.name] = a.value;
                            return {tag: c.tagName, text: (c.innerText||'').trim().slice(0,80), attrs: cattrs};
                        });
                    rows.push({
                        tag: el.tagName,
                        text: text.slice(0, 200),
                        attrs: attrs,
                        html: el.outerHTML.slice(0, 2000),
                        children: children,
                    });
                    if (rows.length >= 5) break;
                }
            }
            // Also dump all iframes + anchors with PDF-ish URLs
            const iframes = Array.from(document.querySelectorAll('iframe')).map(i => ({
                src: (i.src || '').slice(0, 300),
                name: i.name,
                id: i.id,
            }));
            const pdfAnchors = Array.from(document.querySelectorAll('a[href]'))
                .filter(a => /pdf|download|statement|document/i.test(a.href))
                .slice(0, 20)
                .map(a => ({href: a.href.slice(0,300), text: (a.innerText||'').trim().slice(0,100)}));
            return {rows, iframes, pdfAnchors, title: document.title};
        }
    """)
    with open("/tmp/diag_usa_statements.json", "w") as f:
        json.dump(dom, f, indent=2)
    print(f"[*] Saved DOM dump: {len(dom.get('rows', []))} rows, "
          f"{len(dom.get('iframes', []))} iframes, "
          f"{len(dom.get('pdfAnchors', []))} pdf-ish anchors")

    # Click the first statement row + capture what happens
    if dom.get("rows"):
        all_requests.clear()
        all_responses.clear()

        first_row_text = dom["rows"][0]["text"][:80]
        print(f"[*] Clicking first row: {first_row_text!r}")

        # Locate + click by text match
        row = page.locator(f'text=/{first_row_text.split()[0]}/')
        try:
            row.first.click(timeout=5000)
        except Exception as e:
            print(f"[*] Text-based click failed: {e}; trying by bounding box")
            els = page.query_selector_all("tr, [role='row']")
            for el in els:
                if first_row_text[:20] in (el.inner_text() or "")[:50]:
                    box = el.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"]/2,
                                        box["y"] + box["height"]/2)
                        break

        time.sleep(8)
        print(f"[*] URL after click: {page.url}")

        page.screenshot(path="/tmp/diag_usa_after_click.png", full_page=True)

        # Capture requests + responses during the click window
        pdf_responses = [r for r in all_responses
                         if "pdf" in r.get("content_type", "").lower()
                         or r["url"].endswith(".pdf")
                         or "document" in r["url"].lower()
                         or "statement" in r["url"].lower()]
        with open("/tmp/diag_usa_post_click_requests.json", "w") as f:
            json.dump({
                "post_click_url": page.url,
                "requests": all_requests[-50:],
                "responses": all_responses[-50:],
                "pdf_responses": pdf_responses,
            }, f, indent=2)
        print(f"[*] {len(all_requests)} requests, {len(all_responses)} responses; "
              f"{len(pdf_responses)} PDF-relevant")

        # Dump iframe contents after click
        after_iframes = page.evaluate("""
            () => Array.from(document.querySelectorAll('iframe')).map(i => ({
                src: (i.src || '').slice(0, 300),
                name: i.name || '', id: i.id || '',
            }))
        """)
        print(f"[*] Iframes after click: {json.dumps(after_iframes, indent=2)[:800]}")

    browser.close()

print("\n[*] Artifacts saved:")
print("  /tmp/diag_usa_before_click.png")
print("  /tmp/diag_usa_after_click.png")
print("  /tmp/diag_usa_statements.json")
print("  /tmp/diag_usa_post_click_requests.json")
