/* setup_modals/banks/plaid.js — Plaid Link / aggregated bank importer
 * Phase 11E refactor. Plaid has its own client-side SDK (Plaid Link) and
 * per-item connection management; layout is wider than the others.
 */
(function() {
  var f = window.__bankFactory;
  if (!f) { console.error('[plaid] __bankFactory not loaded'); return; }

  var _plaidPoller = f.makePoller('plaid', null);

  window.loadPlaidStatus = async function() {
    var r = await fetch(P+'/api/import/plaid/status').then(function(r){return r.json();}).catch(function(){return {};});
    var badge = document.getElementById('plaid-status-badge');
    var list = document.getElementById('plaid-items-list');
    if (!badge) return;
    if (r.configured) {
      badge.innerHTML = '<span style="color:#4caf50">&#10003; Configured ('+esc(r.env||'sandbox')+') · '+(r.item_count||0)+' bank'+(r.item_count===1?'':'s')+' connected</span>';
      var envSel = document.getElementById('plaid-env'); if (envSel && r.env) envSel.value = r.env;
    } else {
      badge.innerHTML = '<span style="color:#f57c00">&#9888; Not configured — enter Plaid client_id + secret below</span>';
    }
    document.getElementById('plaid-connect-btn').disabled = !r.configured;
    document.getElementById('plaid-sync-btn').disabled = !r.configured || !(r.items && r.items.length);
    if (list) {
      if (r.items && r.items.length) {
        list.innerHTML = r.items.map(function(it){
          var last = it.last_sync ? new Date(it.last_sync).toLocaleString() : 'never';
          return '<div style="border:1px solid #e0e4ea;border-radius:6px;padding:8px 12px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">'
            + '<div><strong>'+esc(it.institution_name||it.item_id)+'</strong>'
            + '<div style="font-size:.74rem;color:var(--muted)">last sync: '+esc(last)+' · status: '+esc(it.status)+'</div></div>'
            + '<div style="display:flex;gap:6px">'
            + '<button class="btn btn-sm btn-primary" onclick="syncOnePlaid(\''+esc(it.item_id)+'\')">Sync</button>'
            + '<button class="btn btn-sm btn-outline" onclick="removePlaidItem(\''+esc(it.item_id)+'\')">&#128465;</button>'
            + '</div></div>';
        }).join('');
      } else {
        list.innerHTML = '<span style="color:var(--muted)">No banks connected yet.</span>';
      }
    }
  };

  window.savePlaidSettings = async function() {
    var ci = (document.getElementById('plaid-client-id')||{}).value || '';
    var sec = (document.getElementById('plaid-secret')||{}).value || '';
    var env = (document.getElementById('plaid-env')||{}).value || 'sandbox';
    if (!ci.trim() || !sec.trim()) { alert('Enter both client_id and secret.'); return; }
    var r = await post('/api/import/plaid/settings', {client_id:ci.trim(), secret:sec.trim(), env:env});
    if (r.status === 'saved') {
      document.getElementById('plaid-secret').value = '';
      toast('Plaid credentials saved ('+esc(env)+')', 'success');
      loadPlaidStatus();
    } else {
      alert(r.error || 'Save failed');
    }
  };

  window.openPlaidLink = async function() {
    if (typeof Plaid === 'undefined') { alert('Plaid Link SDK not loaded. Reload the page.'); return; }
    var msg = document.getElementById('plaid-connect-msg');
    msg.textContent = 'Creating link token…';
    var tok = await post('/api/import/plaid/link-token', {});
    if (!tok.link_token) { msg.textContent = 'Error: '+(tok.error||'failed to create link token'); return; }
    var entityId = document.getElementById('plaid-entity').value || null;
    msg.textContent = 'Opening Plaid Link…';
    var handler = Plaid.create({
      token: tok.link_token,
      onSuccess: async function(public_token, metadata) {
        msg.textContent = 'Exchanging token…';
        var r = await post('/api/import/plaid/exchange', {
          public_token: public_token,
          institution_id: (metadata.institution||{}).institution_id || null,
          institution_name: (metadata.institution||{}).name || null,
          entity_id: entityId ? parseInt(entityId) : null,
        });
        if (r.status === 'ok') {
          msg.innerHTML = '<span style="color:#28a745">&#10003; Connected '+esc((r.item||{}).institution_name||'bank')+'</span>';
          toast('Bank connected', 'success');
          loadPlaidStatus();
        } else {
          msg.innerHTML = '<span style="color:#d32f2f">Error: '+esc(r.error||'exchange failed')+'</span>';
        }
      },
      onExit: function(err, metadata) {
        msg.textContent = err ? 'Plaid Link cancelled: '+(err.error_message||err.error_code||'') : '';
      },
    });
    handler.open();
  };

  window.syncAllPlaid = async function() {
    var entityId = document.getElementById('plaid-entity').value || null;
    var r = await post('/api/import/plaid/start', {entity_id: entityId ? parseInt(entityId) : null});
    if (r.status === 'started') {
      _plaidPoller.start(r.job_id);
      toast('Plaid sync started (job #'+r.job_id+')', 'success');
    } else {
      alert(r.error || 'Failed to start sync');
    }
  };

  window.syncOnePlaid = async function(itemId) {
    var r = await post('/api/import/plaid/start', {item_id: itemId});
    if (r.status === 'started') {
      _plaidPoller.start(r.job_id);
      toast('Syncing item (job #'+r.job_id+')', 'success');
    } else {
      alert(r.error || 'Sync failed');
    }
  };

  window.removePlaidItem = async function(itemId) {
    if (!confirm('Disconnect this bank? Transactions already imported will be kept.')) return;
    var r = await fetch(P+'/api/import/plaid/items/'+encodeURIComponent(itemId), {method:'DELETE'})
              .then(function(r){return r.json();}).catch(function(){return {error:'request failed'};});
    if (r.status === 'removed') {
      toast('Disconnected', 'success');
      loadPlaidStatus();
    } else {
      alert(r.error || 'Failed to disconnect');
    }
  };
})();
