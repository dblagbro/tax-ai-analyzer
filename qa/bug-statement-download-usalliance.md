# Bug: US Alliance Statement PDF Download Failure

**Filed:** 2026-04-24 EDT
**Last updated:** 2026-04-24 EDT (after additional live diagnostic)
**Severity:** HIGH (importer reaches authenticated session but cannot retrieve statements)
**Component:** `app/importers/usalliance_importer.py` — statement-row click + PDF extraction logic
**Status:** UNFIXED — investigation ongoing; two new findings added below

---

## Symptoms (confirmed across multiple live runs)

1. Login + MFA + navigation to `/documents/docs/cash-accounts` works correctly.
2. Statement rows are rendered; importer enumerates them.
3. Clicking a row:
   - Either leaves the page visually unchanged (no URL change, iframe stays on default "December 2" statement)
   - OR intercepts a **536-byte response** at `/document-viewer/ConsolidatedStatement/cathedral%7C<ID>` which is treated as "the PDF" but is clearly too small to be a real statement.
4. Only ONE statement per run gets a 536B intercept; subsequent rows all return "empty PDF".

## Additional findings (added this session)

### Finding A — Cookie auto-save doesn't survive to a new session

After commit `5564ec6` added automatic cookie saving post-login, 7 cookies were captured and persisted (including `sessionId`, `thx_guid`, `tmx_guid`). However, a subsequent diagnostic run that re-injected these cookies into a fresh browser context and navigated directly to `/documents/docs/cash-accounts` landed on a page titled **"Login"** — the saved cookies did NOT restore the authenticated session.

**Implication:** US Alliance's session model is NOT pure cookie-based. Possibilities:
- Server-side session store keyed on fingerprint + IP + cookie triple (any change invalidates)
- Short server-side session TTL (invalidates in minutes)
- localStorage / sessionStorage state also required (cookies() doesn't capture these)
- ThreatMetrix / Lumin Digital tmx_guid binds to the specific browser fingerprint, different on each patchright launch

### Finding B — The 536-byte "PDF" is a stub

Every successful-looking intercept captures exactly 536 bytes from `document-viewer/ConsolidatedStatement/cathedral%7C<ID>`. This is NOT a real statement (typical US Alliance statements are 50-200KB). Likely a JSON error response misclassified as `application/pdf`, OR a viewer bootstrap shell that expects subsequent fetches.

The actual PDF content is presumably loaded by the viewer via one or more follow-up fetches (possibly POST to Lumin Digital's `/data-viewer` or similar with the statement ID in the body). We're not capturing these.

### Finding C — Row selector coverage gap

The diagnostic harness used `tr, [role="row"], [class*="statement"], [class*="document"], [class*="row"]` to find statement rows and found **zero matches** on the statements page. This means the current importer's row-detection is specific (probably CSS-class-matched) and the diagnostic needs those specifics to reproduce. The importer DOES find rows correctly at runtime (we see "Element box: ..." logs), so the runtime selector is different from what I guessed.

---

## Suggested investigation — updated priority

### 1. Visible-browser capture with screenshot + HAR file (highest value)

Run the current importer but with `headless=False` AND save a full HAR of all requests for one successful-looking click. Analyze the HAR for:
- Which specific endpoint the viewer POSTs to when loading statement bytes
- What parameters it sends (likely statement ID + auth token)
- Whether the response is chunked / streamed

### 2. Instrument the viewer iframe (DOM+XHR)

Inject a content script into the `document-viewer` iframe that logs every `fetch()` and `XMLHttpRequest.open()` call. Identifies the exact URL pattern we need to replay.

### 3. Check localStorage / sessionStorage

After login, dump `localStorage` and `sessionStorage` for both `account.usalliance.org` and the Lumin Digital subdomain. If critical state lives there, update the save/restore logic to cover it.

### 4. Replay the viewer URL directly with curl

Take one captured iframe URL like:
```
https://account.usalliance.org/document-viewer/ConsolidatedStatement/cathedral%7CC0D1E7BFCF596E429C07711981038149?download=false&filename=November%202
```
And try:
```
GET with saved cookies + `&download=true`  (force download variant?)
GET with saved cookies + Referer header
GET + no trailing params
POST to the same URL with empty body
```
One of these may yield the real PDF.

### 5. Consider a visible-browser-only fallback

If (1)-(4) don't yield a programmatic fix, use `page.pdf()` (Chrome's "Save as PDF" capability) on the viewer iframe itself — render what the user would see.

---

## What's been tried (committed, didn't fix)

| Commit | Attempt | Result |
|---|---|---|
| 5564ec6 | auto-save cookies | Cookies save, but re-injection doesn't restore session |
| 5f0c2a3 | MED-PASS2-2 navigator.webdriver mask | Reduces fingerprint surface but doesn't fix download |
| Job 69 | headed session with new cookies | Still only 536B stubs; subsequent rows fail entirely |

## Workarounds (not fixes, but preserve data flow)

- **Manual download**: Log in to US Alliance via the mobile app, download statements to your local machine, place them in `/consume/personal/<year>/`. Paperless will OCR and ingest them.
- **PDF monthly statement export** via US Alliance email notifications — if the credit union offers "email me my statement" notifications, those can be consumed via the existing Gmail importer.

## Out of scope

- Does NOT block the Phase 9 + remediation session's sign-off. Login + MFA + bot-detection-bypass all proven.
- Does NOT require residential proxy / Camoufox.

## Next-session prerequisites

Before re-investigating:
- Record what US Alliance's mobile app shows when opening a statement (their own pipeline for display) — may hint at the correct API
- Capture a full HAR from a real-browser session by user manually opening a statement with DevTools open
- Save that HAR to `/tmp/real_session_har.json` for analysis
