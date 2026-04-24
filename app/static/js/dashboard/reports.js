/* reports.js — Reports tab: export generate/download, Year-over-Year compare */

/* Reports */
async function genExport(y,e) {
  toast('Generating '+e+'/'+y+'…');
  const r = await post('/api/export/'+y+'/'+e,{});
  r?.status==='ok' ? (toast('Generated '+r.files?.length+' files.','success'),loadExports()) : toast('Error: '+r?.message,'error');
}
function dlExport(y,e,f){window.location=P+'/api/export/'+y+'/'+e+'/download/'+f;}
async function loadExports() {
  const files = await fetch(P+'/api/export/list').then(r=>r.json()).catch(()=>[]);
  const tb = document.getElementById('exportFiles');
  tb.innerHTML = files.length ? files.map(f=>`<tr><td>${esc(f.filename)}</td><td style="font-size:.78rem;color:var(--muted)">${esc(f.path)}</td><td>${fmtB(f.size)}</td><td>${(f.modified||'').slice(0,16).replace('T',' ')}</td></tr>`).join('') : '<tr><td colspan="4"><div class="empty">No export files yet.</div></td></tr>';
}


/* ───── Year-over-Year report ───── */
async function loadYoY() {
  const yearsRaw = (document.getElementById('yoy-years')?.value || '').trim();
  const years = yearsRaw.split(/[\s,]+/).filter(Boolean);
  const entityId = document.getElementById('yoy-entity')?.value || '';
  const resEl = document.getElementById('yoy-result');
  if (years.length < 2) {
    resEl.innerHTML = '<div class="empty" style="padding:30px;color:#d32f2f;text-align:center">Enter at least 2 years.</div>';
    return;
  }
  resEl.innerHTML = '<div class="empty" style="padding:40px;color:var(--muted);text-align:center"><div class="spinner"></div></div>';
  const p = new URLSearchParams({years: years.join(',')});
  if (entityId) p.set('entity_id', entityId);
  const r = await fetch(P+'/api/reports/yoy?'+p).then(r=>r.json()).catch(()=>({error:'request failed'}));
  if (r.error) { resEl.innerHTML = `<div class="empty" style="padding:30px;color:#d32f2f;text-align:center">Error: ${esc(r.error)}</div>`; return; }

  // Build a per-year summary table
  const cs = 'background:#fff;border:1px solid #e0e4ea;border-radius:10px;padding:16px 20px';
  const header = `<div style="display:grid;grid-template-columns:repeat(${r.per_year.length},1fr);gap:12px;margin-bottom:14px">`;
  const cards = r.per_year.map(y => {
    // Combined: take max per category to avoid double-counting when both sources report
    const income = Math.max(y.transactions.income, y.documents.income);
    const expense = Math.max(y.transactions.expense, y.documents.expense);
    const totalCount = y.transactions.count + y.documents.count;
    const net = income - expense;
    return `<div style="${cs}">
      <div style="font-size:1.1rem;font-weight:700;color:#1a3c5e;margin-bottom:10px">${esc(y.year)}</div>
      <div style="display:flex;justify-content:space-between;margin-bottom:6px"><span style="color:#28a745;font-size:.82rem">Income</span><span style="font-weight:700;color:#28a745">$${fmt(income)}</span></div>
      <div style="display:flex;justify-content:space-between;margin-bottom:6px"><span style="color:#dc3545;font-size:.82rem">Expenses</span><span style="font-weight:700;color:#dc3545">$${fmt(expense)}</span></div>
      <div style="display:flex;justify-content:space-between;border-top:1px solid #e0e4ea;padding-top:6px;margin-top:6px"><span style="font-size:.82rem;color:var(--muted)">Net</span><span style="font-weight:700;color:${net>=0?'#28a745':'#dc3545'}">${net<0?'-':''}$${fmt(Math.abs(net))}</span></div>
      <div style="display:flex;justify-content:space-between;margin-top:8px;font-size:.74rem;color:var(--muted)"><span>${y.transactions.count} txns + ${y.documents.count} docs</span><span>${totalCount}</span></div>
    </div>`;
  }).join('');

  // Deltas row
  let deltasHtml = '';
  if (r.deltas && r.deltas.length) {
    deltasHtml = `<div style="background:#f8f9fb;border:1px solid #e0e4ea;border-radius:10px;padding:14px 18px;margin-bottom:14px">
      <div style="font-weight:700;font-size:.9rem;color:#1a3c5e;margin-bottom:10px">Year-over-Year Change</div>
      ${r.deltas.map(d => {
        const pctFmt = (v) => v === null ? '—' : (v === Infinity ? '∞' : (v>=0?'+':'')+v+'%');
        const incColor = d.income_change >= 0 ? '#28a745' : '#dc3545';
        const expColor = d.expense_change >= 0 ? '#dc3545' : '#28a745';  // expense UP is red
        return `<div style="display:flex;gap:20px;font-size:.85rem;margin-bottom:6px">
          <span style="font-weight:600;color:#1a3c5e;min-width:140px">${esc(d.prev_year)} → ${esc(d.current_year)}</span>
          <span>Income: <strong style="color:${incColor}">${d.income_change>=0?'+':''}$${fmt(Math.abs(d.income_change))}</strong> <span style="color:var(--muted)">(${pctFmt(d.income_change_pct)})</span></span>
          <span>Expenses: <strong style="color:${expColor}">${d.expense_change>=0?'+':''}$${fmt(Math.abs(d.expense_change))}</strong> <span style="color:var(--muted)">(${pctFmt(d.expense_change_pct)})</span></span>
        </div>`;
      }).join('')}
    </div>`;
  }

  // Top expense vendors per year
  const vendorCols = years.map(y => {
    const list = (r.top_expense_vendors[y] || []).slice(0, 10);
    return `<div style="${cs}">
      <div style="font-weight:700;color:#1a3c5e;margin-bottom:10px;font-size:.95rem">${esc(y)} · Top Expenses</div>
      ${list.length ? list.map((v,i) => `<div style="display:flex;justify-content:space-between;font-size:.82rem;padding:4px 0;border-bottom:1px solid #f0f2f5">
        <span style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(v.vendor)}">${i+1}. ${esc(v.vendor)}</span>
        <span style="font-weight:600;color:#dc3545">$${fmt(v.total)}</span>
      </div>`).join('') : '<div style="font-size:.82rem;color:var(--muted)">No data</div>'}
    </div>`;
  }).join('');

  resEl.innerHTML = `${header}${cards}</div>
    ${deltasHtml}
    <div style="display:grid;grid-template-columns:repeat(${years.length},1fr);gap:12px">${vendorCols}</div>`;
}

function downloadYoYCsv() {
  const yearsRaw = (document.getElementById('yoy-years')?.value || '').trim();
  const years = yearsRaw.split(/[\s,]+/).filter(Boolean);
  const entityId = document.getElementById('yoy-entity')?.value || '';
  if (years.length < 2) { alert('Enter at least 2 years.'); return; }
  const p = new URLSearchParams({years: years.join(',')});
  if (entityId) p.set('entity_id', entityId);
  // Client-side CSV synthesis from the existing JSON endpoint — no new route needed
  fetch(P+'/api/reports/yoy?'+p).then(r=>r.json()).then(data => {
    if (data.error) { alert(data.error); return; }
    const lines = ['year,income,expense,net,count'];
    data.per_year.forEach(y => {
      const d = y.transactions.count > 0 ? y.transactions : y.documents;
      lines.push([y.year, d.income, d.expense, (d.income - d.expense).toFixed(2), d.count].join(','));
    });
    const csv = lines.join('\n');
    const blob = new Blob([csv], {type:'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `yoy_${years.join('_')}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  });
}


registerTabLoader("reports", loadExports);
