#!/usr/bin/env python3
"""
Diagnostic: log into US Alliance, dump DOM structure of the statements page,
click the first statement row, and capture ALL network activity for 15 seconds.
Saves screenshots to /tmp/diag_*.png
"""
import sys, os, time, json
sys.path.insert(0, '/app')
os.chdir('/app')

import app.db as db

username = db.get_setting('usalliance_username')
password = db.get_setting('usalliance_password')
print(f"[*] Username: {username}")
print(f"[*] Password set: {bool(password)}")

from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import Stealth
    stealth = Stealth(navigator_webdriver=True, navigator_plugins=True,
                      navigator_languages=True, navigator_platform=True,
                      navigator_user_agent=True, chrome_app=True,
                      navigator_platform_override="Win32")
    print("[*] Stealth loaded")
except ImportError:
    stealth = None
    print("[!] Stealth not available")

with sync_playwright() as pw:
    if stealth:
        stealth.hook_playwright_context(pw)

    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
              "--headless=new","--disable-blink-features=AutomationControlled",
              "--window-size=1280,900"],
    )
    context = browser.new_context(
        accept_downloads=True,
        viewport={"width":1280,"height":900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="America/New_York",
    )
    page = context.new_page()
    if stealth:
        stealth.apply_stealth_sync(page)

    # ── Login ──────────────────────────────────────────────────────────────────
    print("[*] Navigating to login...")
    page.goto("https://account.usalliance.org/login", wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    # Fill login
    import random
    for sel in ['input[name="username"]','input[id*="username" i]','input[type="text"]:visible']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            for ch in username:
                el.press(ch)
                page.wait_for_timeout(random.randint(50,120))
            break

    for sel in ['input[name="password"]','input[id*="password" i]','input[type="password"]:visible']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.click()
            for ch in password:
                el.press(ch)
                page.wait_for_timeout(random.randint(50,120))
            break

    for sel in ['button[type="submit"]','button:has-text("Log In")','button:has-text("Login")']:
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            btn.click()
            break

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    page.screenshot(path="/tmp/diag_after_login.png")
    print(f"[*] After login URL: {page.url}")

    # ── Check for MFA ──────────────────────────────────────────────────────────
    content = page.content().lower()
    if "authorization request" in content or "verify your identity" in content:
        print("[!] MFA required — this script cannot handle MFA interactively.")
        print("[!] Screenshots saved to /tmp/diag_after_login.png")
        browser.close()
        sys.exit(1)

    # ── Navigate to documents page ─────────────────────────────────────────────
    print("[*] Navigating to /documents/docs/cash-accounts ...")
    # Intercept ALL responses from here on
    all_responses = []
    all_requests = []
    def on_resp(r):
        ct = r.headers.get("content-type","")
        all_responses.append(f"[{r.status}] {ct[:30]:30s} {r.url[:100]}")
    def on_req(r):
        all_requests.append(f"[{r.method}] {r.url[:100]}")
    page.on("response", on_resp)
    page.on("request", on_req)

    page.goto("https://account.usalliance.org/documents/docs/cash-accounts",
              wait_until="domcontentloaded", timeout=20000)
    time.sleep(8)  # wait for SPA content to load

    page.screenshot(path="/tmp/diag_documents_page.png")
    print(f"[*] Documents page URL: {page.url}")
    print(f"[*] Requests during page load ({len(all_requests)}):")
    for r in all_requests[-20:]:
        print(f"    {r}")
    print(f"[*] Responses during page load ({len(all_responses)}):")
    for r in all_responses[-20:]:
        print(f"    {r}")

    # ── Dump DOM structure ─────────────────────────────────────────────────────
    print("\n[*] DOM structure — div[role=button] elements:")
    btns = page.query_selector_all('div[role="button"]')
    print(f"    Total div[role=button]: {len(btns)}")
    for btn in btns[:30]:
        text = (btn.text_content() or "").strip().replace("\n"," ")[:100]
        box = btn.bounding_box()
        print(f"    box={box} text={text!r}")

    print("\n[*] All text nodes containing '2025':")
    nodes = page.evaluate("""
        () => {
            const out = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let n;
            while (n = walker.nextNode()) {
                const t = n.textContent.trim();
                if (t.includes('2025') && t.length < 150) {
                    const p = n.parentElement;
                    const pp = p ? p.parentElement : null;
                    out.push({
                        text: t,
                        tag: p ? p.tagName : '?',
                        role: p ? p.getAttribute('role') : null,
                        cls: p ? (p.className||'').substring(0,60) : '',
                        ptag: pp ? pp.tagName : '?',
                        prole: pp ? pp.getAttribute('role') : null,
                        pcls: pp ? (pp.className||'').substring(0,60) : '',
                    });
                }
            }
            return out;
        }
    """)
    for n in nodes[:30]:
        print(f"    [{n['tag']} role={n['role']} cls={n['cls'][:40]}]"
              f" parent=[{n['ptag']} role={n['prole']}]"
              f" text={n['text']!r}")

    # ── Find a 2025 Regular Statement element and click it ────────────────────
    print("\n[*] Attempting to find and click a 2025 Regular Statement...")
    all_requests.clear()
    all_responses.clear()

    target = None
    for sel in [
        'div[role="button"]:has-text("Regular Statement"):not(:has(div[role="button"]))',
        'div[role="button"]:has-text("2025"):has-text("Regular Statement")',
        ':has-text("January 2025 Regular Statement")',
    ]:
        els = page.query_selector_all(sel)
        if els:
            # Find one with "2025" in its text and fewest month names (leaf)
            month_names = ["January","February","March","April","May","June",
                           "July","August","September","October","November","December"]
            for el in els:
                text = (el.text_content() or "").strip()
                if "2025" not in text:
                    continue
                mc = sum(1 for m in month_names if m.lower() in text.lower())
                print(f"    Candidate: sel={sel!r} mc={mc} box={el.bounding_box()} text={text[:80]!r}")
                if mc == 1:
                    target = el
                    break
            if target:
                break

    if not target:
        print("[!] No suitable target found!")
        browser.close()
        sys.exit(1)

    print(f"[*] Clicking: {(target.text_content() or '').strip()[:80]!r}")
    box = target.bounding_box()
    print(f"[*] Bounding box: {box}")
    if box:
        page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
    else:
        target.scroll_into_view_if_needed()
        target.click()

    print("[*] Waiting 15s for response...")
    time.sleep(15)

    page.screenshot(path="/tmp/diag_after_click.png")
    print(f"[*] URL after click: {page.url}")
    print(f"[*] Requests after click ({len(all_requests)}):")
    for r in all_requests:
        print(f"    {r}")
    print(f"[*] Responses after click ({len(all_responses)}):")
    for r in all_responses:
        print(f"    {r}")

    # Check for any PDF in DOM
    pdf_src = page.evaluate("""
        () => {
            const sels = ['iframe','embed','object','a[href]'];
            const out = [];
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const src = el.src || el.data || el.href || '';
                    if (src && (src.includes('pdf') || src.startsWith('blob:')))
                        out.push(el.tagName + ': ' + src.substring(0,150));
                }
            }
            return out;
        }
    """)
    print(f"[*] PDF sources in DOM: {pdf_src}")

    browser.close()
    print("\n[*] Done. Screenshots: /tmp/diag_after_login.png, /tmp/diag_documents_page.png, /tmp/diag_after_click.png")
