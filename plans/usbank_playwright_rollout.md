# US Bank Playwright Anti-Detection Rollout Plan

**Scope:** Steps 1-5 of the 7-step escalation ladder. Steps 6 (residential proxy) and 7 (Camoufox) are explicitly out of scope.

**Ground truth:** `app/importers/usalliance_importer.py` lines 82-180. This is the working config that successfully downloaded US Alliance statements. We port it into `base_bank_importer.launch_browser()` so all 5 bank importers that use it (usbank, merrick, chime, capitalone, verizon) inherit the hardening.

**Non-obvious constraint:** US Alliance itself does NOT call `launch_browser()` — it has its own inline `sync_playwright()` block. Changes to `base_bank_importer.py` cannot regress US Alliance because US Alliance doesn't use it. Regression risk is for the other 4 importers (merrick/chime/capitalone/verizon), which are not currently passing US Bank's level of bot detection anyway.

## Sequencing decision: one commit per step

Five separate commits, five separate `docker compose build && up -d --force-recreate --no-deps tax-ai-analyzer` cycles. Rationale: Steps 2 (`patchright`), 3 (real Chrome), and 4 (Xvfb) are each large enough to independently break the image. A single megacommit would force a full rollback on any single failure. Per-step commits give a known-good state after each green validation. Steps 1 and 5 are pure Python and cheap to roll back; Steps 2-4 touch the Docker image.

**Commit order = execution order = 1 → 2 → 3 → 4 → 5.** Do not reorder. Step 2 depends on Step 1's ephemeral context (patchright's stealth requires `new_context`, not `launch_persistent_context`). Steps 4 and 5 only make sense once Steps 1-3 are in place.

---

## Step 1 — Port US Alliance config to `base_bank_importer.launch_browser()`

**Change summary:** Replace `launch_persistent_context` with an ephemeral `browser + new_context` pair, hook `Stealth` at the `pw` instance level before launch, add `--headless=new` plus the full 16-arg hardening list.

**Files touched:**
- `app/importers/base_bank_importer.py` — rewrite `launch_browser()` (lines 170-234) and `_STEALTH_ARGS` constant (lines 149-167).

**Concrete code (new `launch_browser`):**

```python
_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--headless=new",
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
    "--accept-lang=en-US",
]

def launch_browser(bank_slug: str, headless: bool = True, log=logger.info):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()

    # Hook stealth at the pw level BEFORE launch — matches usalliance.py:122-123
    stealth = None
    try:
        from playwright_stealth import Stealth
        stealth = Stealth(
            navigator_webdriver=True, navigator_plugins=True,
            navigator_languages=True, navigator_platform=True,
            navigator_user_agent=True, navigator_vendor=True,
            chrome_app=True, chrome_csi=True, chrome_load_times=True,
            webgl_vendor=True, hairline=True, media_codecs=True,
            navigator_hardware_concurrency=True, navigator_permissions=True,
            error_prototype=True, sec_ch_ua=True, iframe_content_window=True,
            navigator_platform_override="Win32",
            navigator_languages_override=("en-US", "en"),
        )
        stealth.hook_playwright_context(pw)
        log("Stealth hooked at pw instance level")
    except ImportError:
        log("Warning: playwright-stealth unavailable")

    try:
        browser = pw.chromium.launch(headless=headless, args=_STEALTH_ARGS)
        context = browser.new_context(
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

    page = context.new_page()
    if stealth:
        try:
            stealth.apply_stealth_sync(page)
        except Exception:
            pass
    # Return signature unchanged: (pw, context, page). Callers already handle
    # context.close() + pw.stop(); browser gets garbage-collected on close.
    return pw, context, page
```

**Persistent-profile loss:** The `_CHROME_PROFILES_ROOT` directory logic at lines 25-27 becomes dead code. Leave the constant (do not delete) — Step 3/4 may reuse it for `--user-data-dir` as an arg. MFA sessions no longer survive between runs; this is an accepted trade-off — persistent contexts are a known fingerprint (the profile itself leaks anomalies back to detection).

**Validation:** Run US Bank import from the web UI. Expected log sequence: `Stealth hooked at pw instance level` → `Chrome profile:` line is GONE → login page screenshot lands on `/tmp/bank_debug_usb_login_*.png`. Run a Merrick import too — it should not throw a new import error.

**Rollback:** `git checkout app/importers/base_bank_importer.py` + `docker compose up -d --force-recreate --no-deps tax-ai-analyzer`.

**Risk:** (a) `browser` object is no longer in the return tuple — if any caller references it, they'll break. Confirmed none do (grep `launch_browser` in app/importers — all use 3-tuple unpack). (b) No persistent cookies means US Bank may trigger MFA every run. Accepted — the bot-detect bypass matters more than MFA frequency.

---

## Step 2 — Swap `playwright` → `patchright`

**Change summary:** Replace the Playwright Python distribution with `patchright`, a drop-in fork that pre-patches CDP fingerprints at the driver level. Remove `playwright-stealth` usage (patchright supersedes it).

**Files touched:**
- `requirements.txt` — remove `playwright==1.44.0` and `playwright-stealth>=2.0.0`, add `patchright>=1.44`.
- `Dockerfile` — replace `python -m playwright install chromium` with `python -m patchright install chromium`.
- `app/importers/base_bank_importer.py` — change `from playwright.sync_api import sync_playwright` to `from patchright.sync_api import sync_playwright`. Drop the `Stealth` hook block entirely (lines we added in Step 1).
- `app/importers/usalliance_importer.py` — change import to `from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout`. Drop the `from playwright_stealth import Stealth` block (lines 92-118) and the `_stealth.hook_playwright_context(pw)` call (lines 122-123) and the `_stealth.apply_stealth_sync(page)` call (lines 179-180).
- Every other importer that does `from playwright.sync_api import ...` — there are 6 files. Grep for `from playwright` and change each.

**Concrete diff (requirements.txt):**
```
-playwright==1.44.0
-playwright-stealth>=2.0.0
+patchright>=1.44
```

**Dockerfile:** change line 32 `&& python -m playwright install chromium` → `&& python -m patchright install chromium`.

**Validation:** `docker compose exec tax-ai-analyzer python -c "from patchright.sync_api import sync_playwright; print('ok')"`. Then run US Alliance import — it must still succeed (canary for the working baseline). Then run US Bank.

**Rollback:** `git checkout requirements.txt Dockerfile app/importers/` then rebuild image.

**Risk:** (HIGH) patchright's API is 99% drop-in but the `Stealth` helper from `playwright_stealth` is gone — any code still calling `Stealth()` raises `ImportError`. Grep `playwright_stealth` across the whole repo after the change and confirm zero hits. Also: patchright may have a version floor incompatible with `playwright==1.44.0` behaviour — if a test suite fails, pin `patchright==1.48.*` or whatever the latest stable is.

---

## Step 3 — Install real Chrome (not Chromium)

**Change summary:** Switch from bundled Chromium to the official Google Chrome `.deb`. Real Chrome has the `Google Inc.` WebGL vendor, correct `chrome.loadTimes`, proper codec fingerprint. Sets `channel="chrome"` on `pw.chromium.launch`.

**Files touched:**
- `Dockerfile` — add `python -m patchright install chrome` after the `install chromium` line (keep chromium as fallback).
- `app/importers/base_bank_importer.py` — `pw.chromium.launch(headless=headless, args=_STEALTH_ARGS, channel="chrome")`.

**Concrete Dockerfile append (after line 32):**
```
RUN python -m patchright install chrome
```

**Validation:** `docker compose exec tax-ai-analyzer which google-chrome` should return a path. `docker compose exec tax-ai-analyzer google-chrome --version` prints `Google Chrome 131.x`. Run US Alliance import — critical canary: if it regresses, real-Chrome channel is incompatible with something usalliance-specific and we back out. Then US Bank.

**Rollback:** Remove the new RUN line, remove `channel="chrome"` arg, rebuild.

**Risk:** (a) Image size grows ~200MB. Acceptable. (b) `playwright install chrome` requires internet inside `docker build`, which is normally fine, but behind a corporate proxy it fails silently and the `channel="chrome"` call at runtime will `Error: Executable doesn't exist`. (c) This is a full image rebuild: `docker compose build tax-ai-analyzer && docker compose up -d --force-recreate --no-deps tax-ai-analyzer`. The `tax_ai_data` volume is external in docker-compose.yml, so volume contents survive.

---

## Step 4 — Xvfb + visible (headful) browser

**Change summary:** Install `xvfb`, wrap the container entrypoint with `xvfb-run -a`, flip to `headless=False` and drop the fixed `viewport` in favor of `no_viewport=True`. A visible browser with a real framebuffer is harder to fingerprint than `headless=new`.

**Files touched:**
- `Dockerfile` — add `xvfb` to the apt-get list (near line 26). Change `CMD` to `CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x900x24", "python", "-m", "app.main"]`.
- `app/importers/base_bank_importer.py` — default `headless=False` in `launch_browser`, remove `viewport={"width": 1280, "height": 900}` from `new_context` call, add `no_viewport=True`. Remove `--window-size=1280,900` from `_STEALTH_ARGS` (Xvfb controls screen size).

**Concrete Dockerfile additions:**
```
# line ~26, inside the second apt-get:
xvfb \
```
`CMD` line:
```
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x900x24", "python", "-m", "app.main"]
```

**Validation:** `docker compose logs tax-ai-analyzer | grep -i xvfb` shows Xvfb startup. `docker compose exec tax-ai-analyzer pgrep Xvfb` returns a PID. Run US Alliance import — ground-truth canary. Run US Bank and inspect `/tmp/bank_debug_usb_login_*.png` — it should look visually identical to a desktop Chrome screenshot, not the headless "box".

**Rollback:** Revert Dockerfile CMD, revert `launch_browser` viewport args, rebuild.

**Risk:** (HIGH) Xvfb under `xvfb-run -a` adds a second init-like process. If Xvfb crashes, every subsequent browser launch fails with `DISPLAY not set`. Mitigation: add a health check that Xvfb PID is live before any importer run. (b) `no_viewport=True` means the page uses the OS window size; if any selector depends on viewport dimensions (e.g. `page.viewport_size` in `human_move`), it must handle `None`. Check line 50 of `base_bank_importer.py` — the fallback `{"width": 1280, "height": 900}` already exists, so we're fine.

---

## Step 5 — Warm-up navigation before `/Auth/Login`

**Change summary:** Before US Bank's `_login()` navigates to `https://onlinebanking.usbank.com/Auth/Login`, first visit `https://www.usbank.com/`, idle 3s + random jitter, wiggle the mouse, click a marketing nav link, idle again, THEN click the visible "Log in" button to arrive at `/Auth/Login` organically. Cold-loading `/Auth/Login` is itself a bot signal.

**Files touched:**
- `app/importers/usbank_importer.py` — add `_warmup_navigation(page, log)` helper, call it before `page.goto(LOGIN_URL, ...)` at line 133.

**Concrete code:**

```python
def _warmup_navigation(page, log: Callable):
    """Visit usbank.com homepage and click through to login organically."""
    import random
    log("Warm-up: visiting usbank.com homepage…")
    page.goto("https://www.usbank.com/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000 + random.randint(500, 2500))
    # Jitter mouse
    vp = page.viewport_size or {"width": 1280, "height": 900}
    for _ in range(3):
        human_move(page, random.uniform(200, vp["width"]-200),
                          random.uniform(150, vp["height"]-150))
        page.wait_for_timeout(random.randint(400, 900))
    # Click a marketing nav link (any visible top-nav anchor except login)
    for sel in ['a:has-text("Personal")', 'a:has-text("Checking")', 'a:has-text("Credit cards")']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            log(f"Warm-up click: {sel}")
            human_click(page, el)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000 + random.randint(500, 2000))
            break
    # Now find and click the real "Log in" link
    for sel in ['a:has-text("Log in")', 'a[href*="Auth/Login"]', 'button:has-text("Log in")']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            log(f"Warm-up: clicking login link {sel}")
            human_click(page, el)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            return True
    log("Warm-up: fallback to direct goto")
    return False
```

In `_login` (line ~132), replace the direct `page.goto(LOGIN_URL, ...)` with:
```python
if not cookies:
    if not _warmup_navigation(page, log):
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
```

**Validation:** Check log for `Warm-up: visiting usbank.com homepage` → `Warm-up click: a:has-text("Personal")` → `Warm-up: clicking login link`. Then the existing `Entering username…` line. Compare `/tmp/bank_debug_usb_login_*.png` before/after — should now show the session has existing cookies.

**Rollback:** `git checkout app/importers/usbank_importer.py`; no image rebuild needed.

**Risk:** (LOW) If usbank.com homepage layout changes, the marketing nav selectors miss and we fall back to direct goto — which is the old behavior. No regression possible.

---

## Preserving US Alliance success

**Canary test after every step:** Run a US Alliance import for a single year (e.g. 2024). It must still return `imported > 0`. Run it BEFORE committing each step. US Alliance is the ground truth — if it breaks, the change is wrong even if US Bank appears to work. Step 2 has the highest US-Alliance regression risk because it changes the playwright import at the top of `usalliance_importer.py`.

## Test harness

After each of steps 1-5:
1. `docker compose build tax-ai-analyzer && docker compose up -d --force-recreate --no-deps tax-ai-analyzer`
2. `docker compose logs -f tax-ai-analyzer` in a separate terminal.
3. Trigger US Alliance import via web UI. Confirm `imported > 0` in final log line.
4. Trigger US Bank import. Grep logs for `CAPTCHA challenge detected` and for `Logged in — at`. Success = `Logged in` without `CAPTCHA`. Partial = `CAPTCHA detected` then `clicked continue` then `Logged in`. Failure = `US Bank credentials rejected` or `still on an auth page`.
5. Inspect `/tmp/bank_debug_usb_*.png` in the container (`docker cp tax-ai-analyzer:/tmp ./debug-stepN/`).

## Abort conditions

- US Alliance import fails after any step → revert that step immediately, do not proceed.
- Container fails to start after Step 2, 3, or 4 → revert, investigate image layer.
- US Bank still shows "Access Denied" or Akamai block page after all 5 steps → escalate to user; Step 6 (residential proxy) becomes the only remaining lever.

## Docker concerns

Step 2: requires full `docker compose build tax-ai-analyzer` because requirements.txt changed. The `app/` bind mount means Step 1 and Step 5 python-only changes do NOT require a rebuild — just restart the container.

Step 3 + 4: full image rebuild, `--force-recreate --no-deps tax-ai-analyzer` is sufficient. The `tax_ai_data` named volume and all bind-mounted paths (`/mnt/s/documents/tax-organizer/...`) survive unchanged. Image grows from ~1.8GB to ~2.2GB; pruning old images recommended between steps.

---

## Critical files for implementation

- `/home/dblagbro/docker/tax-ai-analyzer/app/importers/base_bank_importer.py`
- `/home/dblagbro/docker/tax-ai-analyzer/app/importers/usbank_importer.py`
- `/home/dblagbro/docker/tax-ai-analyzer/app/importers/usalliance_importer.py`
- `/home/dblagbro/docker/tax-ai-analyzer/Dockerfile`
- `/home/dblagbro/docker/tax-ai-analyzer/requirements.txt`
