/* setup_modals/banks/imap.js — generic IMAP email importer modal
 * Phase 11E refactor. Not really a bank but lives in the same Import Hub
 * tab so it shares the layout/poller infrastructure.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[imap] __bankFactory not loaded'); return; }

  var _imapPoller = f.makePoller('imap', null);
  var _imapProviders = {};

  window.loadImapStatus = async function() {
    var r = await fetch(P+'/api/import/imap/status').then(function(r){return r.json();}).catch(function(){return {};});
    var badge = document.getElementById('imap-status-badge');
    if (!badge) return;
    if (r.configured) {
      badge.innerHTML = '<span style="color:#4caf50">&#10003; Configured — '+esc(r.username||'')+' @ '+esc(r.host||'')+'</span>';
    } else if (r.host || r.username) {
      badge.innerHTML = '<span style="color:#f57c00">&#9888; Partial — save password to enable imports</span>';
    } else {
      badge.innerHTML = '<span style="color:var(--muted)">Not configured</span>';
    }
    // Pre-populate form
    if (r.provider) document.getElementById('imap-provider').value = r.provider;
    if (r.host)     document.getElementById('imap-host').value = r.host;
    if (r.port)     document.getElementById('imap-port').value = r.port;
    if (r.username) document.getElementById('imap-username').value = r.username;
    if (r.folder)   document.getElementById('imap-folder').value = r.folder;
    document.getElementById('imap-use-ssl').checked = (r.use_ssl !== false);
    // Load providers for preset dropdown
    if (!Object.keys(_imapProviders).length) {
      try {
        var pr = await fetch(P+'/api/import/imap/providers').then(function(r){return r.json();});
        _imapProviders = pr.providers || {};
      } catch(e) {}
    }
  };

  window.imapApplyPreset = function() {
    var prov = document.getElementById('imap-provider').value;
    var preset = _imapProviders[prov];
    if (preset && preset.host) {
      document.getElementById('imap-host').value = preset.host;
      document.getElementById('imap-port').value = preset.port;
    }
  };

  window.imapSaveSettings = async function() {
    var payload = {
      provider: document.getElementById('imap-provider').value,
      host:     document.getElementById('imap-host').value.trim(),
      port:     parseInt(document.getElementById('imap-port').value) || 993,
      username: document.getElementById('imap-username').value.trim(),
      password: document.getElementById('imap-password').value,  // may be blank → keep existing
      folder:   document.getElementById('imap-folder').value.trim() || 'INBOX',
      use_ssl:  document.getElementById('imap-use-ssl').checked,
    };
    if (!payload.host || !payload.username) { alert('Host and username required.'); return; }
    var r = await post('/api/import/imap/settings', payload);
    if (r.status === 'saved') {
      toast('IMAP settings saved' + (r.password_updated ? ' (password updated)' : ''), 'success');
      document.getElementById('imap-password').value = '';
      loadImapStatus();
    } else {
      alert(r.error || 'Save failed');
    }
  };

  window.imapTestConnection = async function() {
    var el = document.getElementById('imap-test-result');
    el.innerHTML = '<span style="color:var(--muted)">Testing…</span>';
    var r = await post('/api/import/imap/test', {});
    if (r.ok) {
      var folders = (r.folders || []).slice(0, 8).join(', ');
      el.innerHTML = '<span style="color:#4caf50">&#10003; Connected</span>' +
        (folders ? '<br><span style="font-size:.74rem;color:var(--muted)">folders: '+esc(folders)+(r.folders.length>8?'…':'')+'</span>' : '');
    } else {
      el.innerHTML = '<span style="color:#d32f2f">&#10007; ' + esc(r.error || 'failed') + '</span>';
    }
  };

  window.startImapImport = async function() {
    var yrs = (document.getElementById('imap-years').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('imap-entity').value;
    var terms = document.getElementById('imap-search-terms').value.trim();
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    document.getElementById('imap-start-btn').disabled = true;
    var r = await post('/api/import/imap/start', {
      entity_id: eid || null,
      years: yrs,
      search_terms: terms || null,
    });
    document.getElementById('imap-start-btn').disabled = false;
    if (r.error) { alert(r.error); return; }
    _imapPoller.start(r.job_id);
    toast('IMAP import started. Job #'+r.job_id, 'success');
  };
})();
