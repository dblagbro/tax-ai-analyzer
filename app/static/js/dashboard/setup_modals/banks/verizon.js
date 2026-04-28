/* setup_modals/banks/verizon.js — Verizon Playwright importer modal
 * Phase 11E refactor.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[verizon] __bankFactory not loaded'); return; }

  var _vznPoller = f.makePoller('vzn', 'vzn-mfa-box');
  var _vznJobId = null;

  window.vznSaveCreds = async function() {
    var u = (document.getElementById('vzn-username')||{}).value || '';
    var pw = (document.getElementById('vzn-password')||{}).value || '';
    u = u.trim(); pw = pw.trim();
    if (!u || !pw) { alert('Enter both username and password.'); return; }
    var r = await post('/api/import/verizon/credentials', {username:u, password:pw});
    if (r.status === 'saved') {
      document.getElementById('vzn-status').innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved</span>';
      document.getElementById('vzn-password').value = '';
    } else { alert(r.error || 'Save failed.'); }
  };
  window.vznSaveCookies = async function() {
    var raw = (document.getElementById('vzn-cookies')||{}).value || '';
    raw = raw.trim();
    if (!raw) { alert('Paste cookies JSON first.'); return; }
    var r = await post('/api/import/verizon/cookies', {cookies:raw});
    if (r.status === 'saved') {
      toast(r.message, 'success');
      document.getElementById('vzn-cookies').value = '';
      loadVznStatus();
    } else { alert(r.error || 'Failed.'); }
  };
  window.vznClearCookies = async function() {
    if (!confirm('Clear saved Verizon cookies?')) return;
    await fetch(P+'/api/import/verizon/cookies', {method:'DELETE'}).then(function(r){return r.json();}).catch(function(){});
    loadVznStatus();
  };

  window.loadVznStatus = async function() {
    var r = await fetch(P+'/api/import/verizon/status').then(function(r){return r.json();}).catch(function(){return {};});
    var el = document.getElementById('vzn-status');
    if (!el) return;
    if (r.configured) {
      el.innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved ('+esc(r.username_preview)+')'
        + (r.cookies_saved ? ' + '+r.cookies_count+' cookies' : '') + '</span>';
    } else {
      el.innerHTML = '<span style="color:#f57c00">&#9888; Enter My Verizon credentials below</span>';
    }
  };

  window.vznSubmitMfa = async function() {
    var code = document.getElementById('vzn-mfa-code').value.trim();
    if (!code || !_vznJobId) return;
    await post('/api/import/verizon/mfa', {job_id: _vznJobId, code: code});
    document.getElementById('vzn-mfa-code').value = '';
    toast('MFA submitted', 'success');
  };

  window.startVerizon = async function() {
    var yrs = (document.getElementById('vzn-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('vzn-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    document.getElementById('vzn-start-btn').disabled = true;
    var r = await post('/api/import/verizon/start', {entity_id:eid||null, years:yrs});
    document.getElementById('vzn-start-btn').disabled = false;
    if (r.error) { alert(r.error); return; }
    _vznJobId = r.job_id;
    _vznPoller.start(r.job_id);
    toast('Verizon import started. Job #'+r.job_id, 'success');
  };
})();
