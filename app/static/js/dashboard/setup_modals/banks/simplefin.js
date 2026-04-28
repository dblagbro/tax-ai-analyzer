/* setup_modals/banks/simplefin.js — SimpleFIN Bridge connector modal
 * Phase 11E refactor. SimpleFIN is API-only (no scraping); uses
 * makePoller for the import job log only — no makeBankHelpers needed.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[simplefin] __bankFactory not loaded'); return; }

  var _sfinPoller = f.makePoller('sfin', null);

  window.loadSfinStatus = async function() {
    var r = await fetch(P+'/api/import/simplefin/status').then(function(r){return r.json();}).catch(function(){return {};});
    var badge = document.getElementById('sfin-status-badge');
    if (!badge) return;
    badge.innerHTML = r.connected
      ? '<span style="color:#4caf50">&#10003; Connected — '+esc(r.preview||'SimpleFIN Bridge')+'</span>'
      : '<span style="color:#f57c00">&#9888; Not connected — claim a token below</span>';
    document.getElementById('sfin-start-btn').disabled = !r.connected;
  };

  window.claimSimpleFin = async function() {
    var token = document.getElementById('sfin-token').value.trim();
    if (!token) { alert('Paste your SimpleFIN setup URL or token first.'); return; }
    var res = document.getElementById('sfin-claim-result');
    res.innerHTML = '<span style="color:var(--muted)">Claiming token…</span>';
    var r = await post('/api/import/simplefin/claim', {setup_url: token});
    if (r.status === 'connected') {
      res.innerHTML = '<span style="color:#4caf50">&#10003; '+esc(r.message)+'</span>';
      document.getElementById('sfin-token').value = '';
      loadSfinStatus();
    } else {
      res.innerHTML = '<span style="color:#d32f2f">&#10007; '+esc(r.error||'Claim failed')+'</span>';
    }
  };

  window.startSfinImport = async function() {
    var yrs = (document.getElementById('sfin-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('sfin-entity').value;
    var filterRaw = (document.getElementById('sfin-account-filter').value||'').trim();
    var acctFilter = filterRaw ? filterRaw.split(/[\s,]+/).filter(Boolean) : null;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/simplefin/start', {entity_id:eid||null, years:yrs, account_filter:acctFilter});
    if (r.error) { alert(r.error); return; }
    _sfinPoller.start(r.job_id);
    toast('SimpleFIN pull started. Job #'+r.job_id, 'success');
  };
})();
