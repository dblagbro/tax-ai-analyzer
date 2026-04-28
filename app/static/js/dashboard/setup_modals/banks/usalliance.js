/* setup_modals/banks/usalliance.js — US Alliance FCU importer modal
 *
 * Phase 11E refactor: extracted verbatim from setup_modals.js. Reuses
 * makePoller + makeBankHelpers from factory.js. Loads after factory.js
 * but uses the lazy-init pattern from Phase 10B as a safety net.
 *
 * US-Alliance-specific UI: cookie-snippet clipboard copy, test-login
 * endpoint, bot-notice toggle, impTab auto-load hook.
 */
(function() {
  function _init() {
    if (!window.__bankFactory) { setTimeout(_init, 50); return; }
    const {makePoller, makeBankHelpers} = window.__bankFactory;

    const usaPoller = makePoller('usa', 'usa-mfa-box');
    const usaHelper = makeBankHelpers('usalliance', 'usa', 'usa-status-badge',
                                      'usa-cookie-status', 'usa-cookie-result');

    window.saveUsaCreds = () => usaHelper.saveCreds();
    window.saveUsaCookies = () => {
      usaHelper.saveCookies('usa-cookies-input').then(() => {
        // Hide bot notice once cookies exist
        const notice = document.getElementById('usa-bot-notice');
        if (notice) notice.style.display = 'none';
      });
    };
    window.clearUsaCookies = () => usaHelper.clearCookies();

    window.loadUsaStatus = async function() {
      await usaHelper.loadStatus();
      // Bot-notice integration: hide when cookies are saved.
      const r = await fetch(P + '/api/import/usalliance/status').then(r=>r.json()).catch(()=>({}));
      const notice = document.getElementById('usa-bot-notice');
      if (notice) notice.style.display = r.cookies_saved ? 'none' : '';
    };

    window.startUsaImport = async function() {
      const years = (document.getElementById('usa-years-input').value||'')
                      .split(/[\s,]+/).filter(Boolean);
      const eid = document.getElementById('usa-entity').value;
      if (!years.length) { alert('Enter at least one year (e.g. 2021,2022).'); return; }
      const r = await post('/api/import/usalliance/start', {entity_id: eid||null, years});
      if (r.error) { alert(r.error); return; }
      usaPoller.start(r.job_id);
      toast('US Alliance import started. Job #'+r.job_id, 'success');
    };

    window.submitUsaMfa = async function() {
      const code = document.getElementById('usa-mfa-code').value.trim();
      const jid = usaPoller.getJobId();
      if (!code || !jid) return;
      const r = await post('/api/import/usalliance/mfa', {job_id: jid, code});
      if (r.status === 'ok') {
        document.getElementById('usa-mfa-code').value = '';
        document.getElementById('usa-mfa-box').style.display = 'none';
      } else { alert(r.error || 'Failed to submit MFA code.'); }
    };

    // impTab auto-load hook — runs loadUsaStatus when the US Alliance tab activates.
    const _origImpTab = window.impTab;
    window.impTab = function(name, btn) {
      if (_origImpTab) _origImpTab(name, btn);
      if (name === 'usalliance') loadUsaStatus();
    };
  }
  _init();

  // ── US-Alliance-specific: cookie-snippet clipboard copy ───────────────
  window.usaCopySnippet = function() {
    const el = document.getElementById('usa-cookie-snippet');
    const btn = document.getElementById('usa-snippet-copy-btn');
    if (!el) return;
    const text = el.textContent;
    const confirm = () => { if (btn) { btn.textContent = '✓ Copied!';
      setTimeout(()=>{ btn.innerHTML = '&#128203; Copy'; }, 2000); } };
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta); ta.focus(); ta.select();
      try { document.execCommand('copy'); confirm(); }
      catch(e) { alert('Copy failed — select the snippet manually.'); }
      document.body.removeChild(ta);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(confirm).catch(fallback);
    } else { fallback(); }
  };

  // ── US-Alliance-specific: test-login button (no other bank has this) ──
  window.testUsaCreds = async function() {
    const u = document.getElementById('usa-username').value.trim();
    const p = document.getElementById('usa-password').value.trim();
    const btn = document.getElementById('usa-test-btn');
    const res = document.getElementById('usa-test-result');
    if (u && p) await post('/api/import/usalliance/credentials', {username:u, password:p});
    btn.disabled = true; btn.textContent = 'Testing…';
    res.innerHTML = '<span style="color:var(--muted)">Attempting login…</span>';
    const r = await post('/api/import/usalliance/test', {}).catch(() => ({error: 'Request failed'}));
    btn.disabled = false; btn.innerHTML = '&#9654; Test Login';
    if (r?.status === 'ok') {
      res.innerHTML = '<span style="color:var(--green)">&#10003; ' + (r.message || 'Login successful') + '</span>';
    } else {
      res.innerHTML = '<span style="color:var(--red)">&#10007; ' + esc(r?.error || 'Login failed — check credentials') + '</span>';
    }
  };
})();
