# Bug: US Alliance Statement PDF Download Failure

**Filed:** 2026-04-24 EDT
**Severity:** HIGH (importer reaches authenticated session but cannot retrieve statements)
**Component:** `app/importers/usalliance_importer.py` — statement-row click + PDF extraction logic
**Discovered during:** US Alliance canary validation of Phase 9 infrastructure (job #68)
**Status:** UNFIXED — scoped for a dedicated session

---

## Reproduction

1. Start US Alliance import via `/api/import/usalliance/start` with `{"years":["2025"]}`
2. Complete login flow (credentials + MFA push)
3. Importer navigates to `https://account.usalliance.org/documents/docs/cash-accounts` successfully
4. Importer enumerates statement rows for 2025 (Oct, Sep, Aug, Jul observed)
5. For each row:
   - Importer clicks the row (mouse click at row coordinates)
   - URL after click: always `https://account.usalliance.org/documents/docs/cash-accounts` (unchanged)
   - DOM PDF sources always show: `IFRAME:https://account.usalliance.org/document-viewer/ConsolidatedStatement/cathedral%7C3E28AA1394AA344A9E958D1A2243E4AF?download=false&filename=December%202`
   - Importer logs: `✗ All download attempts failed` + `✗ Empty PDF for 2025_XX_01_usalliance_statement.pdf`

## Expected behavior

Each statement row click should:
- Either navigate to a statement-specific viewer URL
- OR update the iframe with the selected statement's PDF
- OR trigger a file download

Currently NONE of these happen — the iframe always shows the **same** statement (`cathedral|3E28AA...` which appears to be a fixed/default document named "December 2").

## Observed evidence

From job #68 log excerpts (all captures show identical iframe src regardless of which row was clicked):

```
[16:20:45] Downloading: 2025_10_01_usalliance_statement.pdf
[16:20:45]   Element box: {'x': 58, 'y': 127.546875, 'width': 885.703125, 'height': 48}
             text: 2025 - October 2025 Regular Statement
[16:20:45]   Mouse click at (501,151)
[16:21:06]   URL after click: https://account.usalliance.org/documents/docs/cash-accounts
[16:21:06]   DOM PDF sources: ['IFRAME:about:blank', 'IFRAME:...lumindigital.../HP?...',
              'IFRAME:https://account.usalliance.org/document-viewer/ConsolidatedStatement/cathedral%7C3E28AA1394AA344A9E958D1A2243E4AF?download=false&filename=December%202']
[16:21:36]   ✗ All download attempts failed
[16:21:36]   ✗ Empty PDF for 2025_10_01_usalliance_statement.pdf

[16:21:36] Downloading: 2025_09_01_usalliance_statement.pdf     (next row)
[16:21:36]   text: 2025 - September 2025 Regular Statement
[16:21:36]   Mouse click at (501,174)
[16:21:46]   URL after click: https://account.usalliance.org/documents/docs/cash-accounts  ← same
[16:21:46]   DOM PDF sources: [...'cathedral%7C3E28AA...?filename=December%202']         ← SAME iframe
[16:22:16]   ✗ All download attempts failed

# ... pattern continues for August, July, etc.
```

## Key observations

1. **The click is reaching SOMETHING** — 2-6 network requests fire after each click (Lumin Digital chaperone + pendo analytics).
2. **The iframe never changes** — document-viewer src is identical across all row clicks.
3. **The "filename=December%202" parameter is suspicious** — looks like a default-document fallback that renders when no statement is actually selected.
4. **URL doesn't change** — indicates SPA routing isn't activated by the click. The click targets might be:
   - A row-level `onclick` that expects a different event pattern (e.g., keyboard navigation, tap vs click)
   - A React/Vue component that listens for events Playwright's mouse click doesn't dispatch correctly (e.g., `onMouseDown` + `onMouseUp` instead of `onClick`)
   - An element that's visually at those coordinates but semantically NOT a statement-selector (e.g., an info icon or label)

## Prior working state (pre-Phase 9)

The US Alliance importer was successfully downloading statements in earlier sessions — commit history shows job #46 on 2026-03-13 with status=completed (though imported=0; unclear if that's because there were no 2025 statements at that point OR the same bug existed).

This means the regression is likely between:
- 2026-03-13 (last apparently-working run) — old playwright stack
- Now — new patchright + real Chrome + Xvfb stack, OR US Alliance site changes

**Unknown:** did US Alliance change their statement viewer in this window, OR did the patchright click-handling differ from playwright's?

## Suggested investigation

Priority-ordered:

### 1. Capture a visible-browser screenshot during a click
Add `save_debug_screenshot(page, f"before_click_{year}_{month}")` and `save_debug_screenshot(page, f"after_click_{year}_{month}")` around the click. Compare: does the UI visually change? Does a modal open? Does the iframe update but patchright doesn't see it?

### 2. Inspect what `page.click` actually dispatches
Replace `human_click()` with a direct `row_element.click()` + use DevTools to observe the event:
```python
await page.evaluate("""
  (el) => {
    const orig = el.addEventListener.bind(el);
    el.addEventListener = function(type, fn, opts) {
      console.log('Listener:', type);
      return orig(type, fn, opts);
    };
  }
""", row_element)
```

### 3. Try `row.locator('a').click()` if rows are anchor-wrapped
The click coords are falling on the row's text area, but the actual clickable element might be a child `<a>` or `<button>` with a specific handler.

### 4. Check if the site uses Tealeaf/Lumin's session recording to block automation
The logged requests show `chaperone.lumindigitalhosting.com` and `data.pendo.lumindigitalhosting.com`. Lumin Digital is a fintech-platform provider whose products often include bot-detection at the interaction level — not just login. They may be detecting Playwright mouse events as non-human and silently blocking the click from propagating to the app router.

If (4) is true, the fix path is similar to Phase 9: use CDP-level dispatching, or Camoufox (Firefox), or a residential proxy.

### 5. Try a different click approach
Quick A/B:
```python
row_element.click()              # current
row_element.click(force=True)    # skip actionability checks
row_element.dispatch_event("click")  # synthetic DOM event
row_element.evaluate("(el) => el.click()")  # JS-level click
```
One of these may propagate where others don't.

### 6. Inspect the network tab for a statement-specific fetch
When a row is clicked in a real browser, SOMETHING fires that identifies the statement — an XHR to `/api/documents/<id>` or a postMessage to the iframe. Compare the request log between a real browser and the automated session.

## Workarounds (not fixes, but keep data flowing)

- **Direct API call**: if we can find the XHR that a real browser makes on row click, bypass the UI entirely and fetch statement PDFs via the statement-ID → URL mapping.
- **Use the "consolidated statement" URL directly**: the iframe URL contains a statement-ID (`cathedral|3E28AA...`). Enumerate those IDs from the row DOM, build download URLs programmatically, skip the click.

## Out of scope for this bug

- Does NOT block Phase 9 validation — the Phase 9 infrastructure (login + MFA + bot detection bypass) works correctly.
- Does NOT require another image rebuild.
- Does NOT require residential proxy or Camoufox fallback (those are login-stage escalations, not download-stage).

## Verification after fix

1. Run US Alliance import for a month where a statement exists (any 2025 month).
2. Confirm `imported > 0` in the final job log.
3. Inspect `/consume/personal/2025/` for the downloaded PDF.
4. Confirm Paperless consumes and OCRs the PDF.
