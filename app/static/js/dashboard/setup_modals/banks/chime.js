/* setup_modals/banks/chime.js — Chime Playwright importer modal
 * Phase 11E refactor. Chime predates the makeBankHelpers factory so it
 * uses email/password instead of username/password and has its own
 * load/save logic — kept inline.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[chime] __bankFactory not loaded'); return; }

  var _chmPoller = f.makePoller('chm', 'chm-mfa-box');
  var _chmJobId = null;

  window.chmSaveCreds = async function() {
    var em = (document.getElementById('chm-email')||{}).value || '';
    var pw = (document.getElementById('chm-password')||{}).value || '';
    em = em.trim(); pw = pw.trim();
    if (!em || !pw) { alert('Enter both email and password.'); return; }
    var r = await post('/api/import/chime/credentials', {email:em, password:pw});
    if (r.status === 'saved') {
      document.getElementById('chm-status').innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved</span>';
      document.getElementById('chm-password').value = '';
    } else { alert(r.error || 'Save failed.'); }
  };
  window.chmSaveCookies = async function() {
    var raw = (document.getElementById('chm-cookies')||{}).value || '';
    raw = raw.trim();
    if (!raw) { alert('Paste cookies JSON first.'); return; }
    var r = await post('/api/import/chime/cookies', {cookies:raw});
    if (r.status === 'saved') {
      toast(r.message, 'success');
      document.getElementById('chm-cookies').value = '';
      loadChimeStatus();
    } else { alert(r.error || 'Failed.'); }
  };
  window.chmClearCookies = async function() {
    if (!confirm('Clear saved Chime cookies?')) return;
    await fetch(P+'/api/import/chime/cookies', {method:'DELETE'}).then(function(r){return r.json();}).catch(function(){});
    loadChimeStatus();
  };

  window.loadChimeStatus = async function() {
    var r = await fetch(P+'/api/import/chime/status').then(function(r){return r.json();}).catch(function(){return {};});
    var el = document.getElementById('chm-status');
    if (!el) return;
    if (r.configured) {
      el.innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved ('+esc(r.email_preview)+')'
        + (r.cookies_saved ? ' + '+r.cookies_count+' cookies' : '') + '</span>';
    } else {
      el.innerHTML = '<span style="color:#f57c00">&#9888; Enter Chime credentials below</span>';
    }
  };

  window.chmSubmitMfa = async function() {
    var code = document.getElementById('chm-mfa-code').value.trim();
    if (!code || !_chmJobId) return;
    await post('/api/import/chime/mfa', {job_id: _chmJobId, code: code});
    document.getElementById('chm-mfa-code').value = '';
    toast('MFA code submitted', 'success');
  };

  window.startChime = async function() {
    var yrs = (document.getElementById('chm-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('chm-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    document.getElementById('chm-start-btn').disabled = true;
    var r = await post('/api/import/chime/start', {entity_id:eid||null, years:yrs});
    document.getElementById('chm-start-btn').disabled = false;
    if (r.error) { alert(r.error); return; }
    _chmJobId = r.job_id;
    _chmPoller.start(r.job_id);
    toast('Chime import started. Job #'+r.job_id, 'success');
  };
})();
