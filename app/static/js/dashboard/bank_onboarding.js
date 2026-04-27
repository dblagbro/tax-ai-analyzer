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
    <div style="background:#f0f7ff;border-left:3px solid #2196f3;padding:10px 14px;font-size:.85rem;margin-bottom:10px">
      <strong>How to capture a HAR:</strong>
      <ol style="margin:6px 0 0 18px">
        <li>Open the bank's site in a real Chrome window (no automation)</li>
        <li>Open DevTools (F12) → Network tab → enable "Preserve log"</li>
        <li>Log in + navigate to statements + click one statement</li>
        <li>Right-click in the Network tab → "Save all as HAR with content"</li>
        <li>Upload the .har file below + describe what you did in the narration box</li>
      </ol>
    </div>
    <div style="display:grid;grid-template-columns:1fr;gap:8px;margin-bottom:10px;max-width:760px">
      <input type="file" id="bo-har-${bank.id}" accept=".har,.json" />
      <textarea id="bo-narration-${bank.id}" rows="4"
                placeholder="Narration: what did you do? e.g. 'Logged in via username + password, got SMS code, entered code, clicked Statements tab, clicked October 2025 row, PDF opened in new tab.'"></textarea>
      <div>
        <button class="btn btn-primary" onclick="uploadRecording(${bank.id})">Upload recording</button>
        <span id="bo-upload-result-${bank.id}" style="margin-left:10px;font-size:.86rem"></span>
      </div>
    </div>
    ${recordings.length === 0
      ? '<div class="empty" style="font-size:.86rem">No recordings uploaded yet.</div>'
      : `<table><thead><tr><th>#</th><th>Captured</th><th>HAR</th><th>Bytes</th><th>Narration</th><th></th></tr></thead><tbody>
        ${recordings.map(r => `<tr>
          <td>${r.id}</td>
          <td style="font-size:.78rem">${(r.captured_at || '').slice(0, 16).replace('T', ' ')}</td>
          <td style="font-size:.74rem">${r.har_path ? esc(r.har_path.split('/').pop()) : '—'}</td>
          <td>${r.byte_size?.toLocaleString() || 0}</td>
          <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.narration_text || '')}">${esc((r.narration_text || '').slice(0, 80))}</td>
          <td>${r.har_path ? `<a class="btn btn-sm btn-outline" href="${P}/api/admin/banks/${bank.id}/recordings/${r.id}?download=1" download>Download</a>` : ''}</td>
        </tr>`).join('')}
      </tbody></table>
      <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
        <button class="btn btn-primary" onclick="generateImporter(${bank.id})">&#9889; Generate importer (Claude)</button>
        <span id="bo-gen-result-${bank.id}" style="font-size:.86rem;color:var(--muted)">Uses the most recent recording. ~30-60s.</span>
      </div>`}

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

async function uploadRecording(bankId) {
  const harInput = document.getElementById(`bo-har-${bankId}`);
  const narration = document.getElementById(`bo-narration-${bankId}`)?.value.trim() || '';
  const result = document.getElementById(`bo-upload-result-${bankId}`);
  const harFile = harInput?.files?.[0];
  if (!harFile && !narration) {
    result.innerHTML = '<span style="color:var(--red)">Need a HAR file OR narration text</span>';
    return;
  }
  result.innerHTML = '<span style="color:var(--muted)">Uploading…</span>';
  const fd = new FormData();
  if (harFile) fd.append('har', harFile);
  if (narration) fd.append('narration', narration);
  try {
    const r = await fetch(P + `/api/admin/banks/${bankId}/recordings`, {
      method: 'POST',
      body: fd,
    });
    const j = await r.json();
    if (j && j.id) {
      result.innerHTML = `<span style="color:var(--income)">&#10003; Uploaded recording #${j.id} (${(j.byte_size || 0).toLocaleString()} bytes)</span>`;
      if (harInput) harInput.value = '';
      document.getElementById(`bo-narration-${bankId}`).value = '';
      // Refresh detail panel
      openBankDetail(bankId);
    } else {
      result.innerHTML = `<span style="color:var(--red)">&#10007; ${esc(j?.error || 'Upload failed')}</span>`;
    }
  } catch (e) {
    result.innerHTML = `<span style="color:var(--red)">&#10007; ${esc(e.message || 'Network error')}</span>`;
  }
}

async function generateImporter(bankId) {
  const result = document.getElementById(`bo-gen-result-${bankId}`);
  if (!result) return;
  if (!confirm('Run the AI codegen agent on the most recent recording?\n\nThis calls Claude (Opus) and may take 30-60 seconds. The draft lands in "Generated importers" for review.')) return;
  result.innerHTML = '<span style="color:var(--muted)"><span class="spinner" style="display:inline-block;width:12px;height:12px;vertical-align:middle"></span> Generating… this may take 30-60s</span>';
  try {
    const r = await fetch(P + `/api/admin/banks/${bankId}/generate`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    const j = await r.json();
    if (r.status === 201 && j.generated_id) {
      result.innerHTML = `<span style="color:var(--income)">&#10003; Draft #${j.generated_id} ready (model=${esc(j.model)}, tokens=${j.tokens_in}/${j.tokens_out}). Refreshing…</span>`;
      setTimeout(() => openBankDetail(bankId), 1200);
    } else {
      result.innerHTML = `<span style="color:var(--red)">&#10007; ${esc(j?.error || 'Codegen failed')}</span>`;
    }
  } catch (e) {
    result.innerHTML = `<span style="color:var(--red)">&#10007; ${esc(e.message || 'Network error')}</span>`;
  }
}

registerTabLoader("bank_onboarding", loadBankQueue);
