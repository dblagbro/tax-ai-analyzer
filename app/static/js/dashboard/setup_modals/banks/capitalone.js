/* setup_modals/banks/capitalone.js — Capital One importer modal
 *
 * Phase 11E refactor: extracted from setup_modals.js. Uses
 * window.__bankFactory.makePoller / makeBankHelpers (loaded by factory.js).
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[capitalone] __bankFactory not loaded'); return; }

  var _coPoller = f.makePoller('co', 'co-mfa-box');
  var _coHelper = f.makeBankHelpers('capitalone', 'co', 'co-status-badge', 'co-cookie-status', 'co-cookie-result');

  window.saveCoOneCreds = function() { _coHelper.saveCreds(); };
  window.loadCoStatus   = function() { _coHelper.loadStatus(); };
  window.saveCoCookies  = function() { _coHelper.saveCookies('co-cookies-input'); };
  window.clearCoCookies = function() { _coHelper.clearCookies(); };

  window.startCoOneImport = async function() {
    var yrs = (document.getElementById('co-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('co-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/capitalone/start', {entity_id:eid||null, years:yrs});
    if (r.error) { alert(r.error); return; }
    _coPoller.start(r.job_id);
    toast('Capital One import started. Job #'+r.job_id, 'success');
  };

  window.submitCoMfa = async function() {
    var code = document.getElementById('co-mfa-code').value.trim();
    var jid  = _coPoller.getJobId();
    if (!code || !jid) return;
    var r = await post('/api/import/capitalone/mfa', {job_id:jid, code:code});
    if (r.status === 'ok') {
      document.getElementById('co-mfa-code').value = '';
      document.getElementById('co-mfa-box').style.display = 'none';
    } else { alert(r.error || 'Failed to submit MFA.'); }
  };
})();
