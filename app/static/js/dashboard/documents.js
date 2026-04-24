/* documents.js — Documents tab: table, file browser, override modal, bulk ops */

/* Documents — shared view state */
/* PAPERLESS_WEB_URL is injected inline in _scripts.html */
function paperlessDocUrl(docId) {
  if (!docId) return null;
  return PAPERLESS_WEB_URL + '/documents/' + docId + '/';
}
let _docView = 'table';
let _docTM = null;

// Document bulk selection
const _docBulkSelection = new Set();

function toggleDocRow(id, cb) {
  if (cb.checked) _docBulkSelection.add(id); else _docBulkSelection.delete(id);
  updateDocBulkBar();
}

function toggleAllDocs(cb) {
  document.querySelectorAll('.doc-row-check').forEach(rc => {
    rc.checked = cb.checked;
    const id = parseInt(rc.dataset.id);
    if (cb.checked) _docBulkSelection.add(id); else _docBulkSelection.delete(id);
  });
  updateDocBulkBar();
}

function clearDocBulkSelection() {
  _docBulkSelection.clear();
  document.querySelectorAll('.doc-row-check').forEach(rc => rc.checked = false);
  const sa = document.getElementById('doc-select-all');
  if (sa) sa.checked = false;
  updateDocBulkBar();
}

function updateDocBulkBar() {
  const bar = document.getElementById('doc-bulk-bar');
  if (!bar) return;
  const n = _docBulkSelection.size;
  bar.style.display = n > 0 ? '' : 'none';
  const countEl = document.getElementById('doc-bulk-count');
  if (countEl) countEl.textContent = n;
}

async function applyDocBulkUpdate() {
  const n = _docBulkSelection.size;
  if (!n) return;
  const changes = {};
  const cat = document.getElementById('doc-bulk-category').value;
  const eid = document.getElementById('doc-bulk-entity').value;
  const yr  = document.getElementById('doc-bulk-year').value;
  const dt  = document.getElementById('doc-bulk-doctype').value.trim();
  const dupSel = document.getElementById('doc-bulk-dup').value;
  if (cat) changes.category = cat;
  if (eid !== '') changes.entity_id = eid === '_clear' ? null : parseInt(eid);
  if (yr) changes.tax_year = yr;
  if (dt) changes.doc_type = dt;
  if (dupSel !== '') changes.is_duplicate = dupSel === '1';
  if (!Object.keys(changes).length) { alert('Pick at least one field to change.'); return; }
  if (!confirm(`Apply changes to ${n} document${n===1?'':'s'}?\n${JSON.stringify(changes,null,2)}`)) return;
  const r = await post('/api/documents/bulk', {
    action: 'update', ids: Array.from(_docBulkSelection), changes: changes,
  });
  if (r.status === 'updated') {
    toast(`Updated ${r.count} document${r.count===1?'':'s'}`, 'success');
    clearDocBulkSelection();
    document.getElementById('doc-bulk-category').value = '';
    document.getElementById('doc-bulk-entity').value = '';
    document.getElementById('doc-bulk-year').value = '';
    document.getElementById('doc-bulk-doctype').value = '';
    document.getElementById('doc-bulk-dup').value = '';
    docReload();
  } else {
    alert(r.error || 'Bulk update failed');
  }
}

async function applyDocBulkDelete() {
  const n = _docBulkSelection.size;
  if (!n) return;
  if (!confirm(`Permanently delete ${n} document record${n===1?'':'s'}? Their links will be removed.`)) return;
  const r = await post('/api/documents/bulk', {
    action: 'delete', ids: Array.from(_docBulkSelection),
  });
  if (r.status === 'deleted') {
    toast(`Deleted ${r.count} document record${r.count===1?'':'s'}`, 'success');
    clearDocBulkSelection();
    docReload();
  } else {
    alert(r.error || 'Bulk delete failed');
  }
}

function docSetView(v) {
  _docView = v;
  document.getElementById('doc-view-table').style.display = v==='table' ? '' : 'none';
  document.getElementById('doc-view-fb').style.display = v==='filebrowser' ? '' : 'none';
  document.getElementById('doc-fb-info').style.display = v==='filebrowser' ? '' : 'none';
  document.getElementById('doc-view-table-btn').classList.toggle('active', v==='table');
  document.getElementById('doc-view-fb-btn').classList.toggle('active', v==='filebrowser');
  docReload();
}

function docReload() {
  if (_docView === 'filebrowser') loadFb();
  else loadDocs();
}

async function loadDocs() {
  const p = new URLSearchParams({limit:500});
  const e=document.getElementById('df-entity')?.value; if(e) p.set('entity_id',e);
  const y=document.getElementById('df-year')?.value; if(y) p.set('year',y);
  const c=document.getElementById('df-cat')?.value; if(c) p.set('category',c);
  const d = await fetch(P+'/api/documents?'+p).then(r=>r.json()).catch(()=>({documents:[]}));
  const docs = d.documents || [];

  const cols = [
    {key:'_select',          label:'<input type="checkbox" id="doc-select-all" onclick="toggleAllDocs(this)" title="Select all visible">', sortable:false, filterable:false},
    {key:'paperless_doc_id', label:'ID',       type:'num',  sortable:true,  filterable:false},
    {key:'title',            label:'Title',     type:'str',  sortable:true,  filterable:true},
    {key:'doc_type',         label:'Type',      type:'str',  sortable:true,  filterable:true},
    {key:'category',         label:'Category',  type:'str',  sortable:true,  filterable:true},
    {key:'entity_name',      label:'Entity',    type:'str',  sortable:true,  filterable:true},
    {key:'tax_year',         label:'Year',      type:'str',  sortable:true,  filterable:true},
    {key:'vendor',           label:'Vendor',    type:'str',  sortable:true,  filterable:true},
    {key:'amount',           label:'Amount',    type:'num',  sortable:true,  filterable:false},
    {key:'confidence',       label:'Conf.',     type:'num',  sortable:true,  filterable:false},
    {key:'analyzed_at',      label:'Analyzed',  type:'date', sortable:true,  filterable:false},
    {key:'_actions',         label:'',          sortable:false, filterable:false},
  ];

  const renderRow = (doc) => {
    const docId = doc.paperless_doc_id || doc.doc_id;
    const plUrl = paperlessDocUrl(docId);
    const rowClick = plUrl ? `onclick="window.open('${plUrl}','_blank')" style="cursor:pointer"` : '';
    const stopProp = `onclick="event.stopPropagation()"`;
    const checked = _docBulkSelection.has(doc.id) ? 'checked' : '';
    return `<tr ${rowClick}>
      <td style="width:28px;padding:0 4px" onclick="event.stopPropagation()"><input type="checkbox" class="doc-row-check" data-id="${doc.id}" ${checked} onclick="toggleDocRow(${doc.id},this)"></td>
      <td>${docId||'—'}</td>
      <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(doc.title||'')}">
        ${esc(doc.title||'Untitled')}
      </td>
      <td><span class="tag">${esc(doc.doc_type||'?')}</span></td>
      <td><span class="badge badge-${doc.category||'other'}">${esc(doc.category||'?')}</span></td>
      <td>${esc(doc.entity_name||doc.entity_slug||'')}</td>
      <td>${doc.tax_year||''}</td>
      <td>${esc(doc.vendor||'')}</td>
      <td style="text-align:right;font-weight:600">$${fmt(Math.abs(doc.amount||0))}</td>
      <td>${doc.confidence?Math.round(doc.confidence*100)+'%':'—'}</td>
      <td style="font-size:.78rem;color:var(--muted)">${(doc.analyzed_at||'').slice(0,16).replace('T',' ')}</td>
      <td style="display:flex;gap:4px;flex-wrap:nowrap" ${stopProp}>
        <button class="btn btn-sm btn-outline" onclick="recat(${docId})">Re-analyze</button>
      </td>
    </tr>`;
  };

  if (_docTM) {
    _docTM.setData(docs);
  } else {
    // Replace tbody-only table with full table for TableManager
    const wrap = document.querySelector('#doc-view-table .tbl-wrap');
    wrap.innerHTML = '<table id="doc-table"></table>';
    _docTM = new TableManager({tableId:'doc-table', data:docs, columns:cols, renderRow});
  }
  updateDocBulkBar();
}

async function backfillTitles() {
  const btn = document.getElementById('backfill-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="loader"></span> Populating…';
  const r = await post('/api/documents/backfill-titles', {});
  btn.disabled = false;
  btn.innerHTML = '&#128196; Populate Titles';
  if (r?.status === 'ok') {
    toast(`Updated ${r.updated} title${r.updated!==1?'s':''}. Reloading…`, 'success');
    _docTM = null;
    setTimeout(docReload, 800);
  } else {
    toast('Error: '+(r?.message||'?'), 'error');
  }
}

async function recat(id) { toast('Re-analyzing…'); await post('/api/documents/'+id+'/recategorize',{}); }

function openDocOverride(docJson) {
  let doc;
  try { doc = typeof docJson === 'string' ? JSON.parse(docJson) : docJson; } catch(e) { toast('Error parsing doc','error'); return; }
  const id = doc.paperless_doc_id || doc.doc_id;
  document.getElementById('mOverride-doc-id').value = id;
  document.getElementById('mOverride-doc-title').textContent = doc.title || 'Document ' + id;
  document.getElementById('mOverride-doc-type').value = doc.doc_type || 'other';
  document.getElementById('mOverride-category').value = doc.category || 'other';
  document.getElementById('mOverride-vendor').value = doc.vendor || '';
  document.getElementById('mOverride-amount').value = doc.amount || '';
  document.getElementById('mOverride-date').value = doc.date || '';
  document.getElementById('mOverride-year').value = doc.tax_year || '';
  openM('mDocOverride');
}

async function saveDocOverride() {
  const id = document.getElementById('mOverride-doc-id').value;
  const amtRaw = document.getElementById('mOverride-amount').value;
  const payload = {
    doc_type: document.getElementById('mOverride-doc-type').value,
    category: document.getElementById('mOverride-category').value,
    vendor: document.getElementById('mOverride-vendor').value,
    amount: amtRaw !== '' ? parseFloat(amtRaw) : null,
    date: document.getElementById('mOverride-date').value || null,
    tax_year: document.getElementById('mOverride-year').value || null,
  };
  // Remove null fields except amount (allow null amount to clear it)
  Object.keys(payload).forEach(k => { if (payload[k] === null && k !== 'amount') delete payload[k]; });
  const r = await post('/api/documents/' + id + '/override', payload);
  if (!r || r.error) { toast((r && r.error) || 'Save failed', 'error'); return; }
  closeM('mDocOverride');
  toast('Classification corrected');
  setTimeout(() => { loadDocs && loadDocs(); loadSummary && loadSummary(); }, 500);
}

/* File Browser */
async function loadFb() {
  const tree = document.getElementById('doc-fb-tree');
  tree.innerHTML = '<div class="fb-empty"><div class="spinner"></div></div>';
  const p = new URLSearchParams({limit:500});
  const e=document.getElementById('df-entity')?.value; if(e) p.set('entity_id',e);
  const y=document.getElementById('df-year')?.value; if(y) p.set('year',y);
  const c=document.getElementById('df-cat')?.value; if(c) p.set('category',c);
  const d = await fetch(P+'/api/documents?'+p).then(r=>r.json()).catch(()=>({documents:[]}));
  const _fbDocs = d.documents || [];
  if (!_fbDocs.length) {
    tree.innerHTML = '<div class="fb-empty">No analyzed documents yet. Click "Run Analysis" to start.</div>';
    return;
  }
  document.getElementById('doc-fb-info').textContent = _fbDocs.length + ' documents';
  tree.innerHTML = fbBuildHtml(_fbDocs);
}

function fbBuildHtml(docs) {
  // Group: year → entity → doc_type → [docs]
  const years = {};
  docs.forEach(doc => {
    const y = doc.tax_year || 'Unknown Year';
    const en = doc.entity_name || doc.entity_slug || 'Unassigned';
    const eid = doc.entity_id || 0;
    const dt = doc.doc_type || 'Other';
    if (!years[y]) years[y] = {docs:[], entities:{}};
    if (!years[y].entities[en]) years[y].entities[en] = {id:eid, docs:[], types:{}};
    if (!years[y].entities[en].types[dt]) years[y].entities[en].types[dt] = [];
    years[y].entities[en].types[dt].push(doc);
    years[y].entities[en].docs.push(doc);
    years[y].docs.push(doc);
  });

  return Object.entries(years).sort((a,b)=>b[0].localeCompare(a[0])).map(([yr, yd]) => {
    const totalIncome = yd.docs.filter(d=>d.category==='income').reduce((s,d)=>s+(d.amount||0),0);
    const totalExp = yd.docs.filter(d=>['expense','deduction'].includes(d.category)).reduce((s,d)=>s+(d.amount||0),0);
    const yid = 'fb-y-'+yr;
    const entitiesHtml = Object.entries(yd.entities).sort((a,b)=>a[0].localeCompare(b[0])).map(([en, ed]) => {
      const eid2 = 'fb-e-'+yr+'-'+ed.id;
      const eIncome = ed.docs.filter(d=>d.category==='income').reduce((s,d)=>s+(d.amount||0),0);
      const eExp = ed.docs.filter(d=>['expense','deduction'].includes(d.category)).reduce((s,d)=>s+(d.amount||0),0);
      const typesHtml = Object.entries(ed.types).sort((a,b)=>a[0].localeCompare(b[0])).map(([dt, tdocs]) => {
        const tid = 'fb-t-'+yr+'-'+ed.id+'-'+dt.replace(/\W/g,'_');
        const docsHtml = tdocs.map(doc => {
          const docId = doc.paperless_doc_id || doc.doc_id;
          const plUrl = paperlessDocUrl(docId);
          const amt = doc.amount ? '$'+fmt(doc.amount) : '';
          const amtClass = doc.category==='income' ? 'pos' : (doc.amount ? 'neg' : '');
          const docJson = JSON.stringify(doc).replace(/\\/g,'\\\\').replace(/`/g,'\\`').replace(/\$/g,'\\$');
          const inner = `<span class="fb-doc-icon">&#128196;</span>
              <span class="fb-doc-title" title="${esc(doc.title||'')}">${esc(doc.title||'Untitled')}</span>
              <span class="fb-doc-vendor">${esc(doc.vendor||'')}</span>
              <span class="fb-doc-amt ${amtClass}">${amt}</span>
              ${plUrl ? '<span class="fb-doc-link">Open &#8599;</span>' : ''}`;
          const rowContent = plUrl
            ? `<a style="flex:1;display:flex;align-items:center;gap:8px;text-decoration:none;color:inherit;overflow:hidden;" href="${plUrl}" target="_blank">${inner}</a>`
            : `<div style="flex:1;display:flex;align-items:center;gap:8px;overflow:hidden;">${inner}</div>`;
          return `<div class="fb-doc-row" style="display:flex;align-items:center;">
            ${rowContent}
            <button class="btn" onclick="event.stopPropagation();openDocOverride(\`${docJson}\`)" title="Correct classification" style="font-size:.7rem;padding:2px 6px;margin-left:4px;flex-shrink:0;color:#888;border:1px solid #ddd;background:none;">&#9998;</button>
          </div>`;
        }).join('');
        return `<div class="fb-type-block">
          <div class="fb-type-hdr" onclick="fbToggle('${tid}')">
            <span>&#128194;</span>
            <span class="fb-type-label">${esc(dt)}</span>
            <span class="fb-type-count">${tdocs.length} doc${tdocs.length!==1?'s':''}</span>
            <span class="fb-caret" id="${tid}-caret">&#9660;</span>
          </div>
          <div id="${tid}" class="fb-docs-list">${docsHtml}</div>
        </div>`;
      }).join('');
      return `<div class="fb-entity-block">
        <div class="fb-entity-hdr" onclick="fbToggle('${eid2}')">
          <span>&#128101;</span>
          <span class="fb-entity-name">${esc(en)}</span>
          <span class="fb-entity-meta">
            <span>${ed.docs.length} docs</span>
            ${eIncome?'<span class="pos">+$'+fmt(eIncome)+'</span>':''}
            ${eExp?'<span class="neg">-$'+fmt(eExp)+'</span>':''}
          </span>
          <span class="fb-caret" id="${eid2}-caret">&#9660;</span>
        </div>
        <div id="${eid2}" class="fb-entity-body">${typesHtml}</div>
      </div>`;
    }).join('');
    return `<div class="fb-year-block">
      <div class="fb-year-hdr" onclick="fbToggle('${yid}')">
        <span style="font-size:1.1rem">&#128197;</span>
        <span class="fb-year-title">Tax Year ${esc(yr)}</span>
        <span class="fb-year-meta">
          <span>${yd.docs.length} documents</span>
          ${totalIncome?'<span>Income: $'+fmt(totalIncome)+'</span>':''}
          ${totalExp?'<span>Expenses: $'+fmt(totalExp)+'</span>':''}
        </span>
        <span class="fb-caret" id="${yid}-caret">&#9650;</span>
      </div>
      <div id="${yid}" class="fb-year-body">${entitiesHtml}</div>
    </div>`;
  }).join('');
}

function fbToggle(id) {
  const el = document.getElementById(id);
  const caret = document.getElementById(id+'-caret');
  if (!el) return;
  const wasCollapsed = el.style.display === 'none';
  el.style.display = wasCollapsed ? '' : 'none';
  // ▲ = expanded (content visible), ▼ = collapsed (content hidden)
  if (caret) caret.innerHTML = wasCollapsed ? '&#9650;' : '&#9660;';
}

