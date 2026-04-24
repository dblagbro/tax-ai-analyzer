/* tax_review.js — Tax Review tab: filed returns, SSE review stream, Q&A followups */

/* Filed Tax Returns */
async function loadFiledReturns(entityId) {
  const eid = entityId || document.getElementById('g-entity')?.value || '';
  const p = eid ? '?entity_id='+eid : '';
  const data = await fetch(P+'/api/filed-returns'+p).then(r=>r.json()).catch(()=>({returns:[]}));
  const returns = data.returns || [];
  // Render in dashboard tab
  const el = document.getElementById('filed-returns-table');
  const el2 = document.getElementById('tr-filed-returns-list');
  const html = returns.length ? `<div class="tbl-wrap"><table>
    <thead><tr><th>Year</th><th>Entity</th><th>Filing Status</th><th>Total Income</th><th>AGI</th><th>Deductions</th><th>Total Tax</th><th>Refund</th><th>Filed</th><th></th></tr></thead>
    <tbody>${returns.map(r=>`<tr>
      <td><strong>${r.tax_year}</strong></td>
      <td>${esc(r.entity_name||'')}</td>
      <td style="font-size:.8rem">${(r.filing_status||'').replace('_',' ')}</td>
      <td class="pos">${r.total_income ? '$'+fmt(r.total_income) : '—'}</td>
      <td>${r.agi ? '$'+fmt(r.agi) : '—'}</td>
      <td class="neg">${r.total_deductions ? '$'+fmt(r.total_deductions) : '—'}</td>
      <td>${r.total_tax ? '$'+fmt(r.total_tax) : '—'}</td>
      <td class="${r.refund_amount ? 'pos' : ''}">${r.refund_amount ? '$'+fmt(r.refund_amount) : (r.amount_owed ? '<span class="neg">owed $'+fmt(r.amount_owed)+'</span>' : '—')}</td>
      <td style="font-size:.78rem">${r.filed_date ? r.filed_date.slice(0,10) : (r.preparer_name ? esc(r.preparer_name) : '—')}</td>
      <td><button class="btn btn-sm btn-outline" onclick='editFiledReturn(${JSON.stringify(r)})' style="padding:2px 8px;font-size:.72rem">&#9998;</button></td>
    </tr>`).join('')}</tbody>
  </table></div>` : '<div class="empty" style="padding:16px;font-size:.85rem">No filed return data. <a href="#" onclick="openFiledReturnModal();return false">Add filed return numbers</a> to compare against analyzed data.</div>';
  if (el) el.innerHTML = html;
  if (el2) el2.innerHTML = html;
}

function openFiledReturnModal(prefillYear) {
  // Clear fields
  ['fr-wages','fr-biz-income','fr-total-income','fr-agi','fr-deductions','fr-taxable','fr-tax','fr-refund','fr-owed','fr-preparer','fr-notes'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  document.getElementById('fr-year').value = prefillYear || document.getElementById('g-year')?.value || document.getElementById('tr-year')?.value || '';
  document.getElementById('fr-filing-status').value = 'single';
  document.getElementById('fr-filed-date').value = '';
  // Pre-select entity
  const gEnt = document.getElementById('g-entity')?.value || document.getElementById('tr-entity')?.value || '';
  if (gEnt) document.getElementById('fr-entity').value = gEnt;
  openM('mFiledReturn');
}

function editFiledReturn(r) {
  document.getElementById('fr-entity').value = r.entity_id || '';
  document.getElementById('fr-year').value = r.tax_year || '';
  document.getElementById('fr-filing-status').value = r.filing_status || 'single';
  document.getElementById('fr-wages').value = r.wages_income || '';
  document.getElementById('fr-biz-income').value = r.business_income || '';
  document.getElementById('fr-total-income').value = r.total_income || '';
  document.getElementById('fr-agi').value = r.agi || '';
  document.getElementById('fr-deductions').value = r.total_deductions || '';
  document.getElementById('fr-taxable').value = r.taxable_income || '';
  document.getElementById('fr-tax').value = r.total_tax || '';
  document.getElementById('fr-refund').value = r.refund_amount || '';
  document.getElementById('fr-owed').value = r.amount_owed || '';
  document.getElementById('fr-preparer').value = r.preparer_name || '';
  document.getElementById('fr-filed-date').value = r.filed_date ? r.filed_date.slice(0,10) : '';
  document.getElementById('fr-notes').value = r.notes || '';
  openM('mFiledReturn');
}

async function saveFiledReturn() {
  const payload = {
    entity_id: parseInt(document.getElementById('fr-entity').value)||null,
    tax_year: document.getElementById('fr-year').value.trim(),
    filing_status: document.getElementById('fr-filing-status').value,
    wages_income: parseFloat(document.getElementById('fr-wages').value)||null,
    business_income: parseFloat(document.getElementById('fr-biz-income').value)||null,
    total_income: parseFloat(document.getElementById('fr-total-income').value)||null,
    agi: parseFloat(document.getElementById('fr-agi').value)||null,
    total_deductions: parseFloat(document.getElementById('fr-deductions').value)||null,
    taxable_income: parseFloat(document.getElementById('fr-taxable').value)||null,
    total_tax: parseFloat(document.getElementById('fr-tax').value)||null,
    refund_amount: parseFloat(document.getElementById('fr-refund').value)||null,
    amount_owed: parseFloat(document.getElementById('fr-owed').value)||null,
    preparer_name: document.getElementById('fr-preparer').value.trim()||null,
    filed_date: document.getElementById('fr-filed-date').value||null,
    notes: document.getElementById('fr-notes').value.trim()||null,
  };
  if (!payload.entity_id || !payload.tax_year) { toast('Entity and year are required.','error'); return; }
  const r = await post('/api/filed-returns', payload);
  if (r?.status === 'ok') {
    toast('Filed return saved.','success');
    closeM('mFiledReturn');
    loadFiledReturns();
  } else {
    toast('Error: '+(r?.error||'Save failed'),'error');
  }
}

/* Tax Review */
async function loadTaxReviewYears() {
  const data = await fetch(P+'/api/stats/years').then(r=>r.json()).catch(()=>({years:[]}));
  const sel = document.getElementById('tr-year');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">— select year —</option>';
  (data.years||[]).forEach(y => {
    const opt = document.createElement('option');
    opt.value = y.year;
    opt.textContent = y.year + (y.doc_count ? ' ('+y.doc_count+' docs)' : '') + (y.has_filed_return ? ' ✓ filed' : '');
    sel.appendChild(opt);
  });
  if (prev) sel.value = prev;
  // Also load filed returns for review tab
  loadFiledReturns();
}

var _trAbortCtrl = null;
async function runTaxReview() {
  const year = document.getElementById('tr-year').value;
  const eid = document.getElementById('tr-entity').value;
  if (!year) { toast('Select a year to review.','error'); return; }
  const btn = document.getElementById('tr-run-btn');
  const stopBtn = document.getElementById('tr-stop-btn');
  const output = document.getElementById('tr-review-output');
  const content = document.getElementById('tr-review-content');
  const status = document.getElementById('tr-review-status');
  document.getElementById('tr-review-year').textContent = year;
  output.style.display = '';
  content.textContent = '';
  // Reset Q&A thread for new review
  const qaThread = document.getElementById('tr-qa-thread');
  if (qaThread) qaThread.innerHTML = '';
  _trQaMessages = [];
  btn.style.display = 'none';
  stopBtn.style.display = '';
  status.textContent = 'Reviewing…';
  _trAbortCtrl = new AbortController();
  const params = new URLSearchParams({year});
  if (eid) params.set('entity_id', eid);
  try {
    const resp = await fetch(P+'/api/tax-review?'+params, {headers:{'Accept':'text/event-stream'}, signal:_trAbortCtrl.signal});
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    outer: while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream:true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') { status.textContent = 'Complete'; break outer; }
        try {
          const d = JSON.parse(raw);
          if (d.chunk) { content.textContent += d.chunk; content.scrollTop = content.scrollHeight; }
          if (d.error) { content.textContent += '\n\nError: '+d.error; status.textContent = 'Error'; break outer; }
        } catch(e) {}
      }
    }
  } catch(e) {
    if (e.name === 'AbortError') { status.textContent = 'Stopped'; }
    else { status.textContent = 'Error: '+e.message; }
  }
  btn.style.display = '';
  stopBtn.style.display = 'none';
  _trAbortCtrl = null;
  if (status.textContent === 'Reviewing…') status.textContent = 'Complete';
  // Seed Q&A history with the initial review so follow-ups have full context
  const reviewText = document.getElementById('tr-review-content')?.textContent || '';
  if (reviewText) {
    _trQaMessages = [{role:'assistant', content: reviewText}];
    document.getElementById('tr-qa-input')?.focus();
  }
}
function stopTaxReview() { if (_trAbortCtrl) _trAbortCtrl.abort(); }

/* Tax Review Q&A */
var _trQaMessages = [];  // [{role:'user'|'assistant', content:str}]
var _trQaAbort = null;

function _trQaAppend(role, text, streaming) {
  const thread = document.getElementById('tr-qa-thread');
  const isUser = role === 'user';
  const id = streaming ? 'tr-qa-streaming' : null;
  const div = document.createElement('div');
  if (id) div.id = id;
  div.style.cssText = `margin-bottom:14px;padding:10px 14px;border-radius:8px;font-size:.875rem;line-height:1.65;white-space:pre-wrap;${
    isUser ? 'background:#e8f4fd;border-left:3px solid #1a3c5e;margin-left:20px' :
              'background:#f0f9f0;border-left:3px solid var(--income)'}`;
  div.innerHTML = `<div style="font-size:.72rem;color:var(--muted);margin-bottom:6px;font-weight:600">${isUser?'YOU':'AI ACCOUNTANT'}</div>${esc(text)}`;
  thread.appendChild(div);
  div.scrollIntoView({behavior:'smooth', block:'nearest'});
  return div;
}

async function trSendFollowup() {
  const input = document.getElementById('tr-qa-input');
  const msg = input.value.trim();
  if (!msg) return;
  const year = document.getElementById('tr-year').value;
  const eid = document.getElementById('tr-entity').value;
  const sendBtn = document.getElementById('tr-qa-send-btn');
  const stopBtn = document.getElementById('tr-qa-stop-btn');

  // Add user message to history and UI
  _trQaMessages.push({role:'user', content: msg});
  _trQaAppend('user', msg);
  input.value = '';
  sendBtn.disabled = true;
  sendBtn.style.display = 'none';
  stopBtn.style.display = '';

  // Create streaming div for assistant response
  const streamDiv = _trQaAppend('assistant', '', true);
  streamDiv.querySelector('div').nextSibling?.remove?.();
  let responseText = '';

  _trQaAbort = new AbortController();
  try {
    const resp = await fetch(P+'/api/tax-review/followup', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({year, entity_id: eid||null, messages: _trQaMessages}),
      signal: _trQaAbort.signal,
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    outer: while(true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream:true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') break outer;
        try {
          const d = JSON.parse(raw);
          if (d.chunk) {
            responseText += d.chunk;
            // Re-render the streaming div content
            const label = streamDiv.querySelector('div');
            if (label) label.nextSibling ? label.nextSibling.textContent = responseText : streamDiv.appendChild(Object.assign(document.createTextNode(responseText)));
            // Simpler: just update the full innerHTML safely
            streamDiv.innerHTML = `<div style="font-size:.72rem;color:var(--muted);margin-bottom:6px;font-weight:600">AI ACCOUNTANT</div>${esc(responseText)}`;
            streamDiv.scrollIntoView({behavior:'smooth', block:'nearest'});
          }
          if (d.error) { responseText += '\n[Error: '+d.error+']'; break outer; }
        } catch(e) {}
      }
    }
  } catch(e) {
    if (e.name !== 'AbortError') responseText += '\n[Error: '+e.message+']';
  }

  // Remove streaming id, save to history
  streamDiv.id = '';
  if (responseText) _trQaMessages.push({role:'assistant', content: responseText});
  sendBtn.disabled = false;
  sendBtn.style.display = '';
  stopBtn.style.display = 'none';
  _trQaAbort = null;
}

function trStopFollowup() { if (_trQaAbort) _trQaAbort.abort(); }


registerTabLoader("tax_review", loadTaxReviewYears);
