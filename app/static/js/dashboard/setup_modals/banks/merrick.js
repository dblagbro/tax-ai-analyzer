/* setup_modals/banks/merrick.js — Merrick Bank importer modal
 * Phase 11E refactor.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[merrick] __bankFactory not loaded'); return; }

  var _mrkPoller = f.makePoller('mrk', 'mrk-mfa-box');
  var _mrkHelper = f.makeBankHelpers('merrick', 'mrk', 'mrk-status-badge', 'mrk-cookie-status', 'mrk-cookie-result');

  window.saveMrkCreds   = function() { _mrkHelper.saveCreds(); };
  window.loadMrkStatus  = function() { _mrkHelper.loadStatus(); };
  window.saveMrkCookies = function() { _mrkHelper.saveCookies('mrk-cookies-input'); };
  window.clearMrkCookies= function() { _mrkHelper.clearCookies(); };

  window.startMrkImport = async function() {
    var yrs = (document.getElementById('mrk-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('mrk-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/merrick/start', {entity_id:eid||null, years:yrs});
    if (r.error) { alert(r.error); return; }
    _mrkPoller.start(r.job_id);
    toast('Merrick Bank import started. Job #'+r.job_id, 'success');
  };

  window.submitMrkMfa = async function() {
    var code = document.getElementById('mrk-mfa-code').value.trim();
    var jid  = _mrkPoller.getJobId();
    if (!code || !jid) return;
    var r = await post('/api/import/merrick/mfa', {job_id:jid, code:code});
    if (r.status === 'ok') {
      document.getElementById('mrk-mfa-code').value = '';
      document.getElementById('mrk-mfa-box').style.display = 'none';
    } else { alert(r.error || 'Failed to submit MFA.'); }
  };
})();
