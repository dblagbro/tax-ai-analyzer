/* llm_routing.js — Phase 13 admin tab for proxy endpoints + LMRH hint overrides */

async function loadLLMRouting() {
  await Promise.all([loadProxyList(), loadHintList()]);
}

// ── Proxy endpoints ────────────────────────────────────────────────────────

async function loadProxyList() {
  const wrap = document.getElementById('lr-proxy-list');
  wrap.innerHTML = '<div class="empty"><div class="spinner"></div></div>';
  const data = await fetch(P + '/api/admin/llm-proxies').then(r => r.json()).catch(() => ({}));
  const eps = data.endpoints || [];
  if (!eps.length) {
    wrap.innerHTML = '<div class="empty">No endpoints configured. Add one above.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Priority</th><th>Label</th><th>URL</th><th>v</th><th>Key</th>
      <th>Status</th><th>Breaker</th><th>Actions</th>
    </tr></thead>
    <tbody>${eps.map(e => {
      const breaker = e.breaker || {};
      const breakerHtml = breaker.tripped
        ? `<span class="badge badge-expense" title="cooldown ${breaker.cooldown_remaining_sec}s">tripped (${breaker.cooldown_remaining_sec}s)</span>`
        : (breaker.failures
            ? `<span class="badge badge-other">${breaker.failures} fail${breaker.failures>1?'s':''}</span>`
            : '<span style="color:var(--income)">&#10003; ok</span>');
      const enabledHtml = e.enabled
        ? '<span style="color:var(--income)">enabled</span>'
        : '<span style="color:var(--muted)">disabled</span>';
      return `<tr>
        <td><input type="number" value="${e.priority}" style="width:60px;font-size:.82rem"
                   onchange="updateProxy('${esc(e.id)}', {priority: parseInt(this.value)})" /></td>
        <td><strong>${esc(e.label)}</strong></td>
        <td style="font-size:.78rem;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${esc(e.url)}">${esc(e.url)}</td>
        <td>${e.version}</td>
        <td style="font-size:.74rem;color:var(--muted)">…${esc(e.api_key_tail || '')}</td>
        <td>${enabledHtml}</td>
        <td>${breakerHtml}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-sm btn-outline" onclick="testProxy('${esc(e.id)}', this)">Test</button>
          <button class="btn btn-sm btn-outline" onclick="updateProxy('${esc(e.id)}', {enabled: ${e.enabled?'false':'true'}})">${e.enabled ? 'Disable' : 'Enable'}</button>
          ${breaker.tripped || breaker.failures ? `<button class="btn btn-sm btn-outline" onclick="resetProxyBreaker('${esc(e.id)}')">Reset</button>` : ''}
          <button class="btn btn-sm btn-danger" onclick="deleteProxy('${esc(e.id)}', '${esc(e.label)}')">&times;</button>
        </td>
      </tr>`;
    }).join('')}</tbody>
  </table>`;
}

async function updateProxy(eid, patch) {
  const r = await fetch(P + '/api/admin/llm-proxies/' + eid, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(patch),
  });
  const j = await r.json().catch(() => ({}));
  if (j.status === 'updated') {
    toast('Updated', 'success');
    loadProxyList();
  } else {
    toast('Update failed: ' + (j.error || ''), 'error');
  }
}

async function deleteProxy(eid, label) {
  if (!confirm(`Delete proxy endpoint "${label}"?\n\nThis removes it from the chain. The DB row is destroyed.`)) return;
  const r = await fetch(P + '/api/admin/llm-proxies/' + eid, {method: 'DELETE'});
  const j = await r.json().catch(() => ({}));
  if (j.status === 'deleted') {
    toast('Deleted', 'success');
    loadProxyList();
  } else {
    toast('Delete failed: ' + (j.error || ''), 'error');
  }
}

async function testProxy(eid, btn) {
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
  try {
    const r = await fetch(P + '/api/admin/llm-proxies/' + eid + '/test', {method: 'POST'});
    const j = await r.json().catch(() => ({}));
    if (j.status === 'ok') {
      toast(`✓ ${j.model || 'ok'} (${j.latency_ms}ms)`, 'success');
    } else {
      toast(`✗ ${j.error || 'failed'}`, 'error');
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
    loadProxyList();
  }
}

async function resetProxyBreaker(eid) {
  await fetch(P + '/api/admin/llm-proxies/' + eid + '/reset-breaker', {method: 'POST'});
  toast('Breaker reset', 'success');
  loadProxyList();
}

function openAddProxyModal()  { document.getElementById('lr-add-modal').style.display = 'block'; }
function closeAddProxyModal() { document.getElementById('lr-add-modal').style.display = 'none'; }

async function submitAddProxy() {
  const body = {
    label:    document.getElementById('lr-add-label').value.trim(),
    url:      document.getElementById('lr-add-url').value.trim(),
    api_key:  document.getElementById('lr-add-key').value,
    version:  parseInt(document.getElementById('lr-add-version').value),
    priority: parseInt(document.getElementById('lr-add-priority').value),
    enabled:  document.getElementById('lr-add-enabled').checked,
  };
  if (!body.label || !body.url || !body.api_key) {
    toast('label, url, and api_key are required', 'error');
    return;
  }
  const r = await fetch(P + '/api/admin/llm-proxies', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (j.id) {
    toast('Added — id ' + j.id.slice(0, 8), 'success');
    closeAddProxyModal();
    document.getElementById('lr-add-label').value = '';
    document.getElementById('lr-add-url').value = '';
    document.getElementById('lr-add-key').value = '';
    loadProxyList();
  } else {
    toast('Add failed: ' + (j.error || ''), 'error');
  }
}

// ── LMRH hint overrides ────────────────────────────────────────────────────

async function loadHintList() {
  const wrap = document.getElementById('lr-hint-list');
  wrap.innerHTML = '<div class="empty"><div class="spinner"></div></div>';
  const data = await fetch(P + '/api/admin/llm-hints').then(r => r.json()).catch(() => ({}));
  const hints = data.hints || [];
  if (!hints.length) {
    wrap.innerHTML = '<div class="empty">No tasks registered.</div>';
    return;
  }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th style="width:140px">Task</th>
      <th>Default</th>
      <th>Override (optional)</th>
      <th style="width:80px"></th>
    </tr></thead>
    <tbody>${hints.map(h => `<tr>
      <td><strong>${esc(h.task)}</strong></td>
      <td style="font-family:monospace;font-size:.78rem;color:var(--muted)">${esc(h.default || '(none)')}</td>
      <td><input id="lr-hint-${esc(h.task)}" type="text"
                 style="width:100%;font-family:monospace;font-size:.78rem"
                 placeholder="${esc(h.default)}"
                 value="${esc(h.override || '')}" /></td>
      <td><button class="btn btn-sm btn-primary" onclick="saveHint('${esc(h.task)}')">Save</button></td>
    </tr>`).join('')}</tbody>
  </table>
  <div style="font-size:.78rem;color:var(--muted);margin-top:10px">
    <strong>Tip:</strong> the proxy already does cross-vendor failover internally.
    Override these dims to nudge the proxy's choice — e.g. <code>task=analysis, cost=premium</code>
    forces a top-tier model for analysis. Don't hardcode model names here; let
    the proxy pick from <code>task=</code> + <code>cost=</code> + <code>safety-min=</code>.
  </div>`;
}

async function saveHint(task) {
  const value = document.getElementById('lr-hint-' + task).value.trim();
  const r = await fetch(P + '/api/admin/llm-hints/' + task, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({override: value}),
  });
  const j = await r.json().catch(() => ({}));
  if (j.task) {
    toast(`Saved — effective: ${j.effective}`, 'success');
    loadHintList();
  } else {
    toast('Save failed: ' + (j.error || ''), 'error');
  }
}

registerTabLoader("llm_routing", loadLLMRouting);
