/* setup_modals/banks/usbank.js — US Bank importer modal
 * Phase 11E refactor.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[usbank] __bankFactory not loaded'); return; }

  var _usbPoller = f.makePoller('usb', 'usb-mfa-box');
  var _usbHelper = f.makeBankHelpers('usbank', 'usb', 'usb-status-badge', 'usb-cookie-status', 'usb-cookie-result');

  window.saveUsbCreds   = function() { _usbHelper.saveCreds(); };
  window.loadUsbStatus  = function() { _usbHelper.loadStatus(); };
  window.saveUsbCookies = function() { _usbHelper.saveCookies('usb-cookies-input'); };
  window.clearUsbCookies= function() { _usbHelper.clearCookies(); };

  window.startUsbImport = async function() {
    var yrs = (document.getElementById('usb-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('usb-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/usbank/start', {entity_id:eid||null, years:yrs});
    if (r.error) { alert(r.error); return; }
    _usbPoller.start(r.job_id);
    toast('US Bank import started. Job #'+r.job_id, 'success');
  };

  window.submitUsbMfa = async function() {
    var code = document.getElementById('usb-mfa-code').value.trim();
    var jid  = _usbPoller.getJobId();
    if (!code || !jid) return;
    var r = await post('/api/import/usbank/mfa', {job_id:jid, code:code});
    if (r.status === 'ok') {
      document.getElementById('usb-mfa-code').value = '';
      document.getElementById('usb-mfa-box').style.display = 'none';
    } else { alert(r.error || 'Failed to submit MFA.'); }
  };
})();
