/* bank_onboarding.js — admin queue for new-bank-importer onboarding (Phase 11A) */

async function loadBankQueue() {
  const filter = document.getElementById('bo-status-filter')?.value || '';
  const url = P + '/api/admin/banks/queue' + (filter ? '?status=' + encodeURIComponent(filter) : '');
  const list = document.getElementById('bo-queue-list');
  list.innerHTML = '<div class="empty"><div class="spinner"></div></div>';
  const data = await fetch(url).then(r => r.json()).catch(() => ({banks: []}));
  const banks = data.banks || [];
  if (!banks.length) {
    list.innerHTML = '<div class="empty">No banks in the queue. Submit one above.</div>';
    return;
  }
  list.innerHTML = `<table>
    <thead><tr>
      <th>#</th><th>Bank</th><th>URL</th><th>Status</th><th>Submitted</th><th></th>
    </tr></thead>
    <tbody>${banks.map(b => `<tr>
      <td>${b.id}</td>
      <td><strong>${esc(b.display_name)}</strong>
          <div style="font-size:.74rem;color:var(--muted)">slug: ${esc(b.slug)}</div></td>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(b.login_url)}">${esc(b.login_url)}</td>
      <td><span class="badge badge-${b.status === 'live' ? 'income' : (b.status === 'rejected' ? 'expense' : 'other')}">${esc(b.status)}</span></td>
      <td style="font-size:.78rem;color:var(--muted)">${(b.created_at || '').slice(0, 16).replace('T', ' ')}</td>
      <td>
        <button class="btn btn-sm btn-outline" onclick="openBankDetail(${b.id})">Details</button>
      </td>
    </tr>`).join('')}</tbody>
  </table>`;
}

async function submitBankCandidate() {
  const result = document.getElementById('bo-submit-result');
  const body = {
    display_name: document.getElementById('bo-name').value.trim(),
    login_url: document.getElementById('bo-login-url').value.trim(),
    statements_url: document.getElementById('bo-statements-url').value.trim(),
    platform_hint: document.getElementById('bo-platform-hint').value.trim(),
    notes: document.getElementById('bo-notes').value.trim(),
  };
  if (!body.display_name) { result.innerHTML = '<span style="color:var(--red)">Display name required</span>'; return; }
  if (!body.login_url) { result.innerHTML = '<span style="color:var(--red)">Login URL required</span>'; return; }
  result.innerHTML = '<span style="color:var(--muted)">Submitting…</span>';
  const r = await post('/api/admin/banks', body);
  if (r && r.id) {
    result.innerHTML = `<span style="color:var(--income)">&#10003; Submitted (id ${r.id})</span>`;
    document.getElementById('bo-name').value = '';
    document.getElementById('bo-login-url').value = '';
    document.getElementById('bo-statements-url').value = '';
    document.getElementById('bo-platform-hint').value = '';
    document.getElementById('bo-notes').value = '';
    loadBankQueue();
  } else {
    result.innerHTML = `<span style="color:var(--red)">&#10007; ${esc(r?.error || 'Failed')}</span>`;
  }
}

async function openBankDetail(bankId) {
  const panel = document.getElementById('bo-detail-panel');
  const body = document.getElementById('bo-detail-body');
  const title = document.getElementById('bo-detail-title');
  panel.style.display = '';
  body.innerHTML = '<div class="empty"><div class="spinner"></div></div>';
  const bank = await fetch(P + '/api/admin/banks/' + bankId).then(r => r.json()).catch(() => null);
  if (!bank || bank.error) {
    body.innerHTML = `<div class="empty" style="color:var(--red)">${esc(bank?.error || 'Failed to load')}</div>`;
    return;
  }
  title.textContent = `${bank.display_name} — bank #${bank.id} (${bank.status})`;
  const recordings = bank.recordings || [];
  const generated = bank.generated || [];
  body.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;font-size:.86rem;margin:10px 0">
      <div><strong>Slug:</strong> ${esc(bank.slug)}</div>
      <div><strong>Submitted by:</strong> user #${bank.submitted_by ?? '—'}</div>
      <div><strong>Login URL:</strong> <a href="${esc(bank.login_url)}" target="_blank" rel="noopener">${esc(bank.login_url)}</a></div>
      <div><strong>Statements URL:</strong> ${bank.statements_url ? `<a href="${esc(bank.statements_url)}" target="_blank" rel="noopener">${esc(bank.statements_url)}</a>` : '<em>not set</em>'}</div>
      <div><strong>Platform hint:</strong> ${esc(bank.platform_hint || '<em>none</em>')}</div>
      <div><strong>Updated:</strong> ${(bank.updated_at || '').slice(0, 16).replace('T', ' ')}</div>
    </div>
    ${bank.notes ? `<div style="background:#f8f9fb;padding:10px;border-radius:6px;font-size:.85rem;margin-bottom:14px"><strong>Notes:</strong><br>${esc(bank.notes).replace(/\n/g, '<br>')}</div>` : ''}

    <div style="margin-top:14px">
      <strong>Status:</strong>
      <select id="bo-detail-status">
        ${['pending','recording','recorded','processing','generated','approved','rejected','live']
          .map(s => `<option value="${s}"${s === bank.status ? ' selected' : ''}>${s}</option>`).join('')}
      </select>
      <button class="btn btn-sm btn-primary" onclick="saveBankStatus(${bank.id})">Save status</button>
      <button class="btn btn-sm btn-danger" onclick="deleteBank(${bank.id})" style="margin-left:14px">Delete</button>
    </div>

    <h4 style="margin-top:18px">Recordings (${recordings.length})</h4>
    ${recordings.length === 0
      ? '<div class="empty" style="font-size:.86rem">No recordings uploaded yet.</div>'
      : `<table><thead><tr><th>#</th><th>Captured</th><th>HAR</th><th>Bytes</th><th>Narration</th></tr></thead><tbody>
        ${recordings.map(r => `<tr>
          <td>${r.id}</td>
          <td style="font-size:.78rem">${(r.captured_at || '').slice(0, 16).replace('T', ' ')}</td>
          <td style="font-size:.74rem">${esc(r.har_path || '—')}</td>
          <td>${r.byte_size?.toLocaleString() || 0}</td>
          <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.narration_text || '')}">${esc((r.narration_text || '').slice(0, 80))}</td>
        </tr>`).join('')}
      </tbody></table>`}

    <h4 style="margin-top:18px">Generated importers (${generated.length})</h4>
    ${generated.length === 0
      ? '<div class="empty" style="font-size:.86rem">No importer drafts yet.</div>'
      : `<table><thead><tr><th>#</th><th>Generated</th><th>LLM</th><th>Tokens</th><th>Approved</th><th></th></tr></thead><tbody>
        ${generated.map(g => `<tr>
          <td>${g.id}</td>
          <td style="font-size:.78rem">${(g.generated_at || '').slice(0, 16).replace('T', ' ')}</td>
          <td style="font-size:.78rem">${esc(g.llm_model || '—')}</td>
          <td style="font-size:.78rem">in:${g.llm_tokens_in || 0} out:${g.llm_tokens_out || 0}</td>
          <td>${g.approved_at ? '✓' : '—'}</td>
          <td>
            <button class="btn btn-sm btn-outline" onclick="viewGenerated(${bank.id}, ${g.id})">View</button>
            ${g.approved_at ? '' : `<button class="btn btn-sm btn-primary" onclick="approveGenerated(${bank.id}, ${g.id})">Approve</button>`}
          </td>
        </tr>`).join('')}
      </tbody></table>`}
  `;
}

function closeBankDetail() {
  document.getElementById('bo-detail-panel').style.display = 'none';
}

async function saveBankStatus(bankId) {
  const status = document.getElementById('bo-detail-status').value;
  const r = await post('/api/admin/banks/' + bankId, {status});
  if (r && r.id) {
    toast('Status updated', 'success');
    loadBankQueue();
    openBankDetail(bankId);
  } else {
    toast('Update failed: ' + (r?.error || ''), 'error');
  }
}

async function deleteBank(bankId) {
  if (!confirm('Delete this bank from the queue? Recordings + generated importers will also be removed.')) return;
  const r = await fetch(P + '/api/admin/banks/' + bankId, {method: 'DELETE'}).then(r => r.json()).catch(() => ({}));
  if (r.status === 'deleted') {
    toast('Deleted', 'success');
    closeBankDetail();
    loadBankQueue();
  } else {
    toast('Delete failed: ' + (r?.error || ''), 'error');
  }
}

async function viewGenerated(bankId, genId) {
  const g = await fetch(P + `/api/admin/banks/${bankId}/generated/${genId}`).then(r => r.json()).catch(() => null);
  if (!g || g.error) { alert('Failed to load: ' + (g?.error || 'unknown')); return; }
  const w = window.open('', '_blank');
  if (!w) { alert('Pop-up blocked. Allow pop-ups to view generated source.'); return; }
  w.document.write(`<html><head><title>Generated importer #${g.id}</title>
    <style>body{font-family:monospace;white-space:pre-wrap;padding:20px;font-size:.82rem;background:#0d1117;color:#c9d1d9}h2{color:#58a6ff}</style>
    </head><body>
    <h2>Generated importer #${g.id}</h2>
    <div style="color:#8b949e;margin-bottom:10px">model: ${g.llm_model || 'unknown'} | tokens in/out: ${g.llm_tokens_in}/${g.llm_tokens_out}</div>
    ${g.generation_notes ? `<div style="background:#161b22;padding:10px;border-radius:6px;color:#a8e6cf">${(g.generation_notes || '').replace(/[<&]/g, c => ({'<':'&lt;','&':'&amp;'}[c]))}</div>` : ''}
    <h3 style="color:#58a6ff;margin-top:20px">source_code</h3>
    <pre style="background:#161b22;padding:14px;border-radius:6px;overflow:auto">${(g.source_code || '').replace(/[<&]/g, c => ({'<':'&lt;','&':'&amp;'}[c]))}</pre>
    ${g.test_code ? `<h3 style="color:#58a6ff;margin-top:20px">test_code</h3><pre style="background:#161b22;padding:14px;border-radius:6px;overflow:auto">${g.test_code.replace(/[<&]/g, c => ({'<':'&lt;','&':'&amp;'}[c]))}</pre>` : ''}
    </body></html>`);
}

async function approveGenerated(bankId, genId) {
  if (!confirm(`Approve generated importer #${genId}?\n\nNote: this records approval but does NOT auto-deploy. You still need to copy the source into app/importers/ + register a route.`)) return;
  const r = await post(`/api/admin/banks/${bankId}/generated/${genId}/approve`, {});
  if (r && r.status === 'approved') {
    toast('Approved', 'success');
    openBankDetail(bankId);
    loadBankQueue();
  } else {
    toast('Approve failed: ' + (r?.error || ''), 'error');
  }
}

registerTabLoader("bank_onboarding", loadBankQueue);
