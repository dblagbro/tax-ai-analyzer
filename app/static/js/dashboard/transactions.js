/* transactions.js — Transactions tab: list, reconcile, bulk-edit, vendor merge */

function txnSubTab(cat, btn) {
  document.querySelectorAll('.imp-tab[id^="txn-sub-"]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  _txnTM = null;
  // Swap panels: hide reconcile+vendors, show main txn table
  document.getElementById('txn-reconcile-view').style.display = 'none';
  document.getElementById('txn-vendors-view').style.display = 'none';
  document.getElementById('txn-main-card').style.display = '';
  const bulkBar = document.getElementById('txn-bulk-bar');
  loadTxns();
}

// Dispatch filter changes to whichever view is active
function refreshTxnView() {
  const recon = document.getElementById('txn-reconcile-view');
  if (recon && recon.style.display !== 'none') {
    loadReconcile();
  } else {
    loadTxns();
  }
}

// Unmatched / reconciliation view
let _reconSelectedTxn = null;
let _reconSelectedDoc = null;

async function showReconcile(btn) {
  document.querySelectorAll('.imp-tab[id^="txn-sub-"]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('txn-reconcile-view').style.display = '';
  document.getElementById('txn-vendors-view').style.display = 'none';
  document.getElementById('txn-main-card').style.display = 'none';
  await loadReconcile();
}

async function showVendors(btn) {
  document.querySelectorAll('.imp-tab[id^="txn-sub-"]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('txn-reconcile-view').style.display = 'none';
  document.getElementById('txn-vendors-view').style.display = '';
  document.getElementById('txn-main-card').style.display = 'none';
  await loadVendors();
}

async function loadReconcile() {
  _reconSelectedTxn = null;
  _reconSelectedDoc = null;
  document.getElementById('recon-link-btn').disabled = true;
  document.getElementById('recon-link-status').textContent = '';

  const p = new URLSearchParams({limit:500});
  const e=document.getElementById('tf-entity')?.value; if(e) p.set('entity_id',e);
  const y=document.getElementById('tf-year')?.value; if(y) p.set('year',y);

  // Audit-risk query uses same entity/year but own threshold
  const threshold = parseFloat(document.getElementById('audit-threshold')?.value) || 75;
  const auditParams = new URLSearchParams();
  if (e) auditParams.set('entity_id', e);
  if (y) auditParams.set('year', y);
  auditParams.set('threshold', threshold);

  const [txnResp, docResp, auditResp] = await Promise.all([
    fetch(P+'/api/transactions/unmatched?'+p).then(r=>r.json()).catch(()=>({transactions:[]})),
    fetch(P+'/api/documents/unmatched?'+p).then(r=>r.json()).catch(()=>({documents:[]})),
    fetch(P+'/api/transactions/audit-risk?'+auditParams).then(r=>r.json()).catch(()=>({count:0,total_amount:0})),
  ]);

  // Populate audit-risk card
  document.getElementById('audit-threshold-label').textContent = Math.round(auditResp.threshold || threshold);
  document.getElementById('audit-count').textContent = (auditResp.count ?? 0).toLocaleString();
  document.getElementById('audit-total').textContent = '$' + (auditResp.total_amount || 0).toLocaleString(undefined, {maximumFractionDigits:0});
  const txns = txnResp.transactions || [];
  const docs = docResp.documents || [];

  document.getElementById('recon-txn-count').textContent = txns.length;
  document.getElementById('recon-doc-count').textContent = docs.length;

  const txnBody = document.getElementById('recon-txn-body');
  txnBody.innerHTML = txns.length ? txns.map(t=>{
    const absAmt = Math.abs(t.amount||0);
    const amt = absAmt.toFixed(2);
    const isExpenseish = (t.category||'').toLowerCase() === 'expense' || (t.category||'').toLowerCase() === 'deduction';
    const isAuditRisk = isExpenseish && absAmt >= threshold;
    const riskFlag = isAuditRisk ? '<span title="IRS audit risk — receipt required for business expenses ≥ $'+threshold+'" style="color:#c0621c;margin-right:4px;font-weight:700">&#9888;</span>' : '';
    return `<tr data-id="${t.id}" style="cursor:pointer${isAuditRisk?';background:#fffaf0':''}" onclick="selectReconTxn(${t.id},this)">
      <td style="white-space:nowrap">${riskFlag}${esc((t.date||'').slice(0,10))}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.vendor||'')}">${esc(t.vendor||'')}</td>
      <td style="text-align:right;font-weight:600" class="${(t.amount||0)>=0?'pos':'neg'}">$${amt}</td>
      <td style="font-size:.72rem;color:var(--muted)">${esc(t.source||'')}</td>
    </tr>`;
  }).join('') : `<tr><td colspan="4" style="text-align:center;padding:20px;color:var(--muted)">&#10003; No unmatched transactions!</td></tr>`;

  const docBody = document.getElementById('recon-doc-body');
  docBody.innerHTML = docs.length ? docs.map(d=>{
    const amt = Math.abs(d.amount||0).toFixed(2);
    const purl = (typeof PAPERLESS_WEB_URL !== 'undefined' && PAPERLESS_WEB_URL && d.paperless_doc_id)
      ? `<a href="${PAPERLESS_WEB_URL}/documents/${d.paperless_doc_id}/preview/" target="_blank" onclick="event.stopPropagation()" style="color:#1a3c5e">&#128196;</a>` : '';
    return `<tr data-id="${d.id}" style="cursor:pointer" onclick="selectReconDoc(${d.id},this)">
      <td style="white-space:nowrap">${esc((d.date||'').slice(0,10))}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(d.vendor||'')}">${esc(d.vendor||'')}</td>
      <td style="text-align:right;font-weight:600">$${amt}</td>
      <td>${purl}</td>
    </tr>`;
  }).join('') : `<tr><td colspan="4" style="text-align:center;padding:20px;color:var(--muted)">&#10003; No orphan documents!</td></tr>`;
}

function selectReconTxn(id, row) {
  _reconSelectedTxn = id;
  document.querySelectorAll('#recon-txn-body tr').forEach(r => r.style.background = '');
  row.style.background = '#fff4e6';
  document.getElementById('recon-link-btn').disabled = !(_reconSelectedTxn && _reconSelectedDoc);
}

function selectReconDoc(id, row) {
  _reconSelectedDoc = id;
  document.querySelectorAll('#recon-doc-body tr').forEach(r => r.style.background = '');
  row.style.background = '#f0e7ff';
  document.getElementById('recon-link-btn').disabled = !(_reconSelectedTxn && _reconSelectedDoc);
}

function updateAuditThreshold() {
  // Only refresh if reconcile view is active (fast — single fetch)
  if (document.getElementById('txn-reconcile-view')?.style.display !== 'none') {
    loadReconcile();
  }
}

async function manualLinkSelected() {
  if (!_reconSelectedTxn || !_reconSelectedDoc) return;
  const status = document.getElementById('recon-link-status');
  status.innerHTML = '<span style="color:var(--muted)">Linking…</span>';
  const r = await post('/api/transactions/links/manual',
                       {txn_id: _reconSelectedTxn, doc_id: _reconSelectedDoc});
  if (r.status === 'created' || r.status === 'updated') {
    status.innerHTML = `<span style="color:#28a745">&#10003; Linked (${r.status})</span>`;
    toast('Link saved', 'success');
    await loadReconcile();  // refresh both sides
  } else {
    status.innerHTML = `<span style="color:#d32f2f">&#10007; ${esc(r.error||'Link failed')}</span>`;
  }
}

/* Transactions */
let _txnTM = null;

// Bulk selection state
const _bulkSelection = new Set();

function toggleTxnRow(id, cb) {
  if (cb.checked) _bulkSelection.add(id); else _bulkSelection.delete(id);
  updateBulkBar();
}

function toggleAllTxns(cb) {
  const visibleCbs = document.querySelectorAll('.txn-row-check');
  visibleCbs.forEach(rc => {
    rc.checked = cb.checked;
    const id = parseInt(rc.dataset.id);
    if (cb.checked) _bulkSelection.add(id); else _bulkSelection.delete(id);
  });
  updateBulkBar();
}

function clearBulkSelection() {
  _bulkSelection.clear();
  document.querySelectorAll('.txn-row-check').forEach(rc => rc.checked = false);
  const sa = document.getElementById('txn-select-all');
  if (sa) sa.checked = false;
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('txn-bulk-bar');
  if (!bar) return;
  const n = _bulkSelection.size;
  bar.style.display = n > 0 ? '' : 'none';
  const countEl = document.getElementById('txn-bulk-count');
  if (countEl) countEl.textContent = n;
}

async function applyBulkUpdate() {
  const n = _bulkSelection.size;
  if (!n) return;
  const changes = {};
  const cat = document.getElementById('bulk-category').value;
  const eid = document.getElementById('bulk-entity').value;
  const yr  = document.getElementById('bulk-year').value;
  const dt  = document.getElementById('bulk-doctype').value.trim();
  if (cat) changes.category = cat;
  if (eid !== '') changes.entity_id = eid === '_clear' ? null : parseInt(eid);
  if (yr) changes.tax_year = yr;
  if (dt) changes.doc_type = dt;
  if (!Object.keys(changes).length) { alert('Pick at least one field to change.'); return; }
  if (!confirm(`Apply changes to ${n} transaction${n===1?'':'s'}?\n${JSON.stringify(changes,null,2)}`)) return;
  const r = await post('/api/transactions/bulk', {
    action: 'update',
    ids: Array.from(_bulkSelection),
    changes: changes,
  });
  if (r.status === 'updated') {
    toast(`Updated ${r.count} transaction${r.count===1?'':'s'}`, 'success');
    clearBulkSelection();
    // Reset inputs
    document.getElementById('bulk-category').value = '';
    document.getElementById('bulk-entity').value = '';
    document.getElementById('bulk-year').value = '';
    document.getElementById('bulk-doctype').value = '';
    loadTxns();
  } else {
    alert(r.error || 'Bulk update failed');
  }
}

async function applyBulkDelete() {
  const n = _bulkSelection.size;
  if (!n) return;
  if (!confirm(`Permanently delete ${n} transaction${n===1?'':'s'}? This cannot be undone.`)) return;
  const r = await post('/api/transactions/bulk', {
    action: 'delete',
    ids: Array.from(_bulkSelection),
  });
  if (r.status === 'deleted') {
    toast(`Deleted ${r.count} transaction${r.count===1?'':'s'}`, 'success');
    clearBulkSelection();
    loadTxns();
  } else {
    alert(r.error || 'Bulk delete failed');
  }
}

async function loadTxns() {
  const p = new URLSearchParams({limit:500});
  const e=document.getElementById('tf-entity')?.value; if(e) p.set('entity_id',e);
  const y=document.getElementById('tf-year')?.value; if(y) p.set('year',y);
  const s=document.getElementById('tf-source')?.value; if(s) p.set('source',s);
  const d = await fetch(P+'/api/transactions?'+p).then(r=>r.json()).catch(()=>({transactions:[],total:0}));
  const txns = d.transactions || [];
  // Summary row
  const sumRow = document.getElementById('txn-summary-row');
  if (sumRow && txns.length) {
    const totalAmt = txns.reduce((s,t)=>s+Math.abs(t.amount||0),0);
    const cat = '';
    const label = cat ? (cat.charAt(0).toUpperCase()+cat.slice(1)) : 'Total';
    const color = cat==='income'?'#28a745':cat==='expense'?'#dc3545':cat==='deduction'?'#6f42c1':'#1a3c5e';
    sumRow.innerHTML = `<div style="background:#f8f9fb;border:1px solid #e0e4ea;border-radius:8px;padding:10px 16px;display:flex;gap:24px;align-items:center;font-size:.88rem">
      <span><strong style="color:${color}">${label}:</strong> $${fmt(totalAmt)}</span>
      <span style="color:var(--muted)">${txns.length} transactions</span>
      <a href="#" onclick="event.preventDefault();sw('reports')" style="color:#1a3c5e;font-size:.82rem;text-decoration:none">&#8594; Export as report</a>
    </div>`;
  } else if (sumRow) { sumRow.innerHTML=''; }

  const cols = [
    {key:'_select',     label:'<input type="checkbox" id="txn-select-all" onclick="toggleAllTxns(this)" title="Select all visible">', sortable:false, filterable:false},
    {key:'date',        label:'Date',        type:'date', sortable:true, filterable:true},
    {key:'description', label:'Description', type:'str',  sortable:true, filterable:true},
    {key:'vendor',      label:'Vendor',      type:'str',  sortable:true, filterable:true},
    {key:'category',    label:'Category',    type:'str',  sortable:true, filterable:true},
    {key:'source',      label:'Source',      type:'str',  sortable:true, filterable:true},
    {key:'entity_name', label:'Entity',      type:'str',  sortable:true, filterable:true},
    {key:'tax_year',    label:'Year',        type:'str',  sortable:true, filterable:true},
    {key:'amount',      label:'Amount',      type:'num',  sortable:true, filterable:false},
    {key:'_actions',    label:'',            sortable:false, filterable:false},
  ];

  const renderRow = (t) => {
    // Source cell — link to Paperless doc if paperless_doc_id present
    let srcCell;
    if (t.paperless_doc_id) {
      const previewUrl = PAPERLESS_WEB_URL + '/documents/' + t.paperless_doc_id + '/preview/';
      srcCell = `<a href="${previewUrl}" target="_blank" style="color:var(--navy);text-decoration:none;display:flex;align-items:center;gap:3px">
        <span>&#128196;</span>${esc(t.source||'')}
      </a>`;
    } else if (t.source_id) {
      srcCell = `<span title="${esc(t.source_id)}">${esc(t.source||'')}<span style="font-size:.7rem;color:var(--muted);margin-left:3px">#${esc(String(t.source_id).slice(-6))}</span></span>`;
    } else {
      srcCell = esc(t.source||'');
    }
    const checked = _bulkSelection.has(t.id) ? 'checked' : '';
    return `<tr>
      <td style="width:28px;padding:0 4px"><input type="checkbox" class="txn-row-check" data-id="${t.id}" ${checked} onclick="toggleTxnRow(${t.id},this)"></td>
      <td>${t.date||''}</td>
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.description||'')}">${esc(t.description||'')}</td>
      <td>${esc(t.vendor||'')}</td>
      <td><span class="badge badge-${t.category||'other'}">${t.category||''}</span></td>
      <td style="font-size:.82rem">${srcCell}</td>
      <td><a href="#" onclick="event.preventDefault();jumpToEntity(${t.entity_id})" style="color:var(--navy);text-decoration:none">${esc(t.entity_name||'')}</a></td>
      <td>${t.tax_year||''}</td>
      <td style="text-align:right;font-weight:600" class="${(t.amount||0)>=0?'pos':'neg'}">$${fmt(Math.abs(t.amount||0))}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-sm btn-outline" onclick="editTxn(${t.id})">Edit</button>
        ${t.link_count > 0
          ? `<button class="btn btn-sm" style="margin-left:4px;background:#e8f4fd;color:#1a3c5e;border:1px solid #b8d4ed" title="${t.link_count} linked document(s)" onclick="showTxnLinks(${t.id},this)">&#128279; ${t.link_count}</button>`
          : ''}
      </td>
    </tr>`;
  };

  const wrap = document.querySelector('#tab-transactions .tbl-wrap');
  if (_txnTM) {
    _txnTM.setData(txns);
  } else {
    wrap.innerHTML = '<table id="txn-table"></table>';
    _txnTM = new TableManager({tableId:'txn-table', data:txns, columns:cols, renderRow});
  }
  document.getElementById('txnInfo').textContent = txns.length ? `${txns.length} of ${d.total} loaded` : '';
  updateBulkBar();
}

function filterTxns() {
  // Legacy: now handled by TableManager filter row. No-op kept for compatibility.
}

async function runDedupScan() {
  const btn = event.currentTarget;
  btn.disabled = true;
  btn.textContent = '⏳ Scanning…';
  const r = await fetch(P+'/api/transactions/dedup/scan', {method:'POST'})
              .then(res=>res.json()).catch(()=>({error:'Request failed'}));
  btn.disabled = false;
  btn.innerHTML = '&#128279; Link Scan';
  if (r.error) { toast('Scan failed: '+r.error, 'error'); return; }
  toast(`Links: ${r.links_created} new, ${r.links_updated} updated (${r.scanned} txns scanned)`, 'success');
  loadTxns();
}

async function showTxnLinks(txnId, btn) {
  const r = await fetch(P+'/api/transactions/'+txnId+'/links').then(res=>res.json()).catch(()=>({links:[]}));
  const links = r.links || [];
  if (!links.length) { toast('No linked documents found.', 'info'); return; }
  let html = `<div style="font-weight:600;margin-bottom:8px">Linked Documents (${links.length})</div>`;
  links.forEach(l => {
    const purl = PAPERLESS_WEB_URL && l.paperless_doc_id
      ? `<a href="${PAPERLESS_WEB_URL}/documents/${l.paperless_doc_id}/preview/" target="_blank" style="color:#1a3c5e">&#128196; View</a>`
      : '';
    html += `<div style="border:1px solid #e0e4ea;border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:.82rem">
      <span style="font-weight:600">${esc(l.vendor||'')}</span>
      <span style="color:var(--muted);margin:0 8px">|</span>
      $${Math.abs(l.amount||0).toFixed(2)}
      <span style="color:var(--muted);margin:0 8px">|</span>
      ${l.date||''}
      <span style="color:var(--muted);margin:0 8px">|</span>
      <span style="color:#6f42c1">${Math.round((l.confidence||0)*100)}% confidence</span>
      <span style="margin-left:8px">${purl}</span>
    </div>`;
  });
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9000;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = `<div style="background:#fff;border-radius:10px;padding:24px;max-width:520px;width:90%;max-height:80vh;overflow-y:auto;box-shadow:0 4px 24px rgba(0,0,0,.2)">
    ${html}
    <button class="btn btn-outline" style="margin-top:12px" onclick="this.closest('[style*=fixed]').remove()">Close</button>
  </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

function openAddTxn() {
  document.getElementById('nt-date').value = new Date().toISOString().slice(0,10);
  ['nt-amt','nt-desc','nt-vendor'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
  const rec = document.getElementById('nt-receipt'); if (rec) rec.value = '';
  document.getElementById('mAddTxn').classList.add('open');
}

async function saveTxn() {
  const btn = document.getElementById('nt-save-btn');
  if (btn) btn.disabled = true;
  const r = await post('/api/transactions',{
    date:document.getElementById('nt-date').value,
    amount:parseFloat(document.getElementById('nt-amt').value)||0,
    description:document.getElementById('nt-desc').value,
    vendor:document.getElementById('nt-vendor')?.value || '',
    entity_id:parseInt(document.getElementById('nt-ent').value)||null,
    year:document.getElementById('nt-year').value,
    category:document.getElementById('nt-cat').value
  });
  if (r?.status !== 'created') {
    if (btn) btn.disabled = false;
    toast('Error: '+(r?.error||'unknown'),'error');
    return;
  }
  // Optional receipt attachment
  const fileInput = document.getElementById('nt-receipt');
  const file = fileInput?.files?.[0];
  if (file) {
    const fd = new FormData();
    fd.append('file', file);
    const attachR = await fetch(P+'/api/transactions/'+r.id+'/attach', {method:'POST', body:fd})
      .then(res => res.json()).catch(() => ({error:'upload failed'}));
    if (attachR.status === 'attached') {
      toast(`Saved + receipt linked (${attachR.bytes.toLocaleString()}B)`, 'success');
    } else {
      toast('Transaction saved, but receipt failed: '+(attachR.error||'unknown'), 'error');
    }
  } else {
    toast('Saved.', 'success');
  }
  if (btn) btn.disabled = false;
  closeM('mAddTxn');
  loadTxns();
}

function editTxn(id){toast('Edit #'+id+' (coming soon)');}

/* ───── Vendor merge ───── */
const _vendorSelection = new Set();
let _vendorData = [];  // cached for client-side filter

async function loadVendors() {
  _vendorSelection.clear();
  updateVendorMergeBar();
  const body = document.getElementById('vendor-body');
  body.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)"><div class="spinner"></div></td></tr>';

  const p = new URLSearchParams();
  const e = document.getElementById('tf-entity')?.value; if (e) p.set('entity_id', e);
  const y = document.getElementById('tf-year')?.value;   if (y) p.set('year', y);
  const search = document.getElementById('vendor-search')?.value.trim(); if (search) p.set('search', search);
  p.set('group_by', document.getElementById('vendor-group-raw')?.checked ? 'raw' : 'normalized');

  const r = await fetch(P+'/api/vendors?'+p).then(r=>r.json()).catch(()=>({vendors:[]}));
  _vendorData = r.vendors || [];
  renderVendorList();
  document.getElementById('vendor-count-label').textContent =
    `${_vendorData.length} unique vendor${_vendorData.length===1?'':'s'}`;
}

function renderVendorList() {
  const body = document.getElementById('vendor-body');
  const searchTerm = (document.getElementById('vendor-search')?.value || '').toLowerCase();
  const isRaw = document.getElementById('vendor-group-raw')?.checked;
  const filtered = searchTerm
    ? _vendorData.filter(v => {
        const name = (v.vendor_normalized || v.vendor || '').toLowerCase();
        const raws = (v.raw_variants || v.vendor || '').toLowerCase();
        return name.includes(searchTerm) || raws.includes(searchTerm);
      })
    : _vendorData;
  if (!filtered.length) {
    body.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--muted)">No vendors match.</td></tr>';
    return;
  }
  body.innerHTML = filtered.map(v => {
    // In raw-group mode we select by the exact 'vendor' string (one per row).
    // In normalized-group mode we select all raw variants collapsing into that canonical.
    const key = isRaw ? v.vendor : v.vendor_normalized;
    const selectable = isRaw ? [v.vendor] : (v.raw_variants || v.vendor_normalized || '').split(',').filter(Boolean);
    const selKey = JSON.stringify(selectable);
    const checked = Array.from(_vendorSelection).some(s => s.key === key) ? 'checked' : '';
    const canonical = v.vendor_normalized || v.vendor || '(blank)';
    const variants = isRaw ? '' : (v.variant_count > 1 ? `${v.variant_count} variants: ${esc((v.raw_variants||'').slice(0,80))}${(v.raw_variants||'').length>80?'…':''}` : '');
    const total = (v.total || 0).toLocaleString(undefined, {maximumFractionDigits:2});
    return `<tr>
      <td style="padding:0 4px"><input type="checkbox" class="vendor-check" data-key="${esc(key)}" data-from='${esc(selKey)}' ${checked} onclick="toggleVendorRow(this)"></td>
      <td><strong>${esc(canonical)}</strong></td>
      <td style="font-size:.78rem;color:var(--muted);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(v.raw_variants||'')}">${variants}</td>
      <td style="text-align:right;font-weight:600">${v.count||0}</td>
      <td style="text-align:right;font-weight:600">$${total}</td>
      <td style="font-size:.78rem;color:var(--muted);white-space:nowrap">${esc((v.first_seen||'').slice(0,10))}</td>
      <td style="font-size:.78rem;color:var(--muted);white-space:nowrap">${esc((v.last_seen||'').slice(0,10))}</td>
      <td style="white-space:nowrap"><button class="btn btn-sm btn-outline" onclick="renameVendor('${esc(key).replace(/'/g,"\\'")}')">Rename</button></td>
    </tr>`;
  }).join('');
}

function filterVendorList() { renderVendorList(); }

function toggleVendorRow(cb) {
  const key = cb.dataset.key;
  let fromList = [];
  try { fromList = JSON.parse(cb.dataset.from); } catch(e) { fromList = [key]; }
  if (cb.checked) {
    _vendorSelection.add(JSON.stringify({key, from: fromList}));
    // dedupe by key (a toggle on different element with same key)
  } else {
    for (const s of Array.from(_vendorSelection)) {
      try { if (JSON.parse(s).key === key) _vendorSelection.delete(s); } catch(e) {}
    }
  }
  updateVendorMergeBar();
}

function toggleAllVendors(cb) {
  const boxes = document.querySelectorAll('.vendor-check');
  boxes.forEach(b => {
    b.checked = cb.checked;
    toggleVendorRow(b);
  });
}

function clearVendorSelection() {
  _vendorSelection.clear();
  document.querySelectorAll('.vendor-check').forEach(b => b.checked = false);
  const sa = document.getElementById('vendor-select-all');
  if (sa) sa.checked = false;
  document.getElementById('vendor-merge-target').value = '';
  updateVendorMergeBar();
}

function updateVendorMergeBar() {
  const bar = document.getElementById('vendor-merge-bar');
  if (!bar) return;
  const n = _vendorSelection.size;
  bar.style.display = n > 0 ? '' : 'none';
  const countEl = document.getElementById('vendor-select-count');
  if (countEl) countEl.textContent = n;
  // Suggestion: if exactly one vendor selected with variants, prefill merge target
  if (n === 1) {
    try {
      const sel = JSON.parse(Array.from(_vendorSelection)[0]);
      const target = document.getElementById('vendor-merge-target');
      if (target && !target.value) target.value = sel.key;
    } catch(e) {}
  }
}

async function doVendorMerge() {
  const to_vendor = document.getElementById('vendor-merge-target').value.trim();
  if (!to_vendor) { alert('Enter a canonical vendor name to merge into.'); return; }
  // Collect all from_vendors by flattening each selected group's raw variants
  const fromVendors = [];
  for (const s of _vendorSelection) {
    try {
      const obj = JSON.parse(s);
      (obj.from || [obj.key]).forEach(v => fromVendors.push(v));
    } catch(e) {}
  }
  const uniqueFrom = Array.from(new Set(fromVendors.filter(Boolean)));
  if (!uniqueFrom.length) { alert('No source vendors selected.'); return; }
  if (!confirm(`Merge ${uniqueFrom.length} vendor name${uniqueFrom.length===1?'':'s'} into "${to_vendor}"?\n\nThis rewrites the vendor column on all matching transactions.`)) return;
  const r = await post('/api/vendors/merge', {
    from_vendors: uniqueFrom,
    to_vendor: to_vendor,
    update_normalized: true,
  });
  if (r.status === 'merged') {
    toast(`Merged ${r.count} transaction${r.count===1?'':'s'} into "${to_vendor}"`, 'success');
    clearVendorSelection();
    loadVendors();
  } else if (r.status === 'noop') {
    toast('No transactions matched the selected vendor names', 'info');
  } else {
    alert(r.error || 'Merge failed');
  }
}

async function renameVendor(currentName) {
  const newName = prompt(`Rename "${currentName}" to:`, currentName);
  if (!newName || newName.trim() === currentName) return;
  const r = await post('/api/vendors/rename', {
    from_vendor: currentName,
    to_vendor: newName.trim(),
  });
  if (r.status === 'renamed') {
    toast(`Renamed ${r.count} transaction${r.count===1?'':'s'}`, 'success');
    loadVendors();
  } else {
    alert(r.error || 'Rename failed');
  }
}

