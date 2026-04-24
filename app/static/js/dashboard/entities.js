/* entities.js — Entity Management tab: tree render, add/edit/archive/merge entities, profile-adjacent helpers */

// ── Entity Management ────────────────────────────────────────────────────────

var _entityTree = [];

async function loadEntityTree() {
  if (document.getElementById('tab-entities').style.display === 'none') return;
  var r = await fetch(P+'/api/entities/tree');
  if (!r.ok) { toast('Failed to load entities','error'); return; }
  _entityTree = await r.json();
  // Also fetch archived for merge selects
  renderEntityTree(_entityTree);
}

function _entityTypeLabel(t) {
  return {person:'Person',dba:'DBA',llc:'LLC',corporation:'Corporation',
          partnership:'Partnership',trust:'Trust/Estate',nonprofit:'Nonprofit',
          farm:'Farm/Ranch',other:'Other',personal:'Personal',business:'Business'}[t]||t||'—';
}

function renderEntityTree(tree) {
  var el = document.getElementById('entityTreeBody');
  if (!tree || !tree.length) { el.innerHTML='<div class="empty">No entities yet.</div>'; return; }
  el.innerHTML = '<ul class="ent-tree">'+tree.map(renderEntityNode).join('')+'</ul>'
    + '<div style="margin-top:16px;display:flex;gap:8px">'
    + '<button class="btn btn-outline btn-sm" onclick="openMergeEntity()">🔀 Merge / Acquire</button></div>';
}

function renderEntityNode(e) {
  var children = (e.children||[]);
  var meta = [];
  try {
    var md = JSON.parse(e.metadata_json||'{}');
    if (md.ein) meta.push('EIN: '+md.ein);
    if (md.ssn) meta.push('SSN on file');
    if (md.state_formation) meta.push(md.state_formation);
    if (md.phone) meta.push(md.phone);
  } catch(_){}
  if (e.tax_id) meta.push('Tax ID: '+e.tax_id);

  return `<li>
    <div class="ent-node" onclick="openEditEntity(${e.id})">
      <span class="ent-node-color" style="background:${esc(e.color||'#1a3c5e')}"></span>
      <span class="ent-node-type">${_entityTypeLabel(e.type)}</span>
      <span class="ent-node-name">${esc(e.display_name||e.name)}</span>
      <span class="ent-node-meta">${meta.map(esc).join(' · ')}</span>
      <span class="ent-node-actions">
        <button class="btn btn-sm btn-outline" onclick="event.stopPropagation();openEditEntity(${e.id})" title="Edit">✏️</button>
        <button class="btn btn-sm" style="color:#dc3545;border-color:#dc3545;background:transparent" onclick="event.stopPropagation();archiveEntity(${e.id},'${esc(e.name)}')" title="Archive">🗄️</button>
      </span>
    </div>`
    + (children.length ? `<ul class="ent-tree" style="margin-left:24px;padding-left:0;border-left:2px solid var(--border)">${children.map(renderEntityNode).join('')}</ul>` : '')
    + '</li>';
}

function _flatEntities(tree) {
  var out = [];
  function walk(nodes){ nodes.forEach(n=>{ out.push(n); if(n.children) walk(n.children); }); }
  walk(tree||[]);
  return out;
}

function openAddEntity() {
  document.getElementById('mEntityEditTitle').textContent='Add Entity';
  document.getElementById('ee-id').value='';
  ['ee-name','ee-display','ee-state-tax-id','ee-filing-state','ee-ein','ee-employer-id',
   'ee-state-form','ee-state-reg','ee-biz-addr','ee-reg-agent','ee-fiscal-year-end',
   'ee-trade-name','ee-dba-reg','ee-dba-state','ee-addr','ee-phone','ee-email',
   'ee-website','ee-desc','ee-ssn'].forEach(id=>{var el=document.getElementById(id);if(el)el.value='';});
  document.getElementById('ee-dob').value='';
  document.getElementById('ee-dba-exp').value='';
  document.getElementById('ee-color').value='#1a3c5e';
  document.getElementById('ee-type').value='person';
  // Populate parent select
  _populateParentSelect(null);
  updateEntityTypeFields();
  document.getElementById('mEntityEdit').classList.add('open');
}

function openEditEntity(id) {
  var flat = _flatEntities(_entityTree);
  var e = flat.find(x=>x.id==id);
  if (!e) return;
  document.getElementById('mEntityEditTitle').textContent='Edit: '+e.name;
  document.getElementById('ee-id').value=e.id;
  document.getElementById('ee-name').value=e.name||'';
  document.getElementById('ee-display').value=e.display_name||'';
  document.getElementById('ee-type').value=e.type||'person';
  document.getElementById('ee-color').value=e.color||'#1a3c5e';
  document.getElementById('ee-desc').value=e.description||'';
  // Populate parent select
  _populateParentSelect(e.parent_entity_id, e.id);
  // Metadata: merge parent's metadata as fallback placeholders
  var parentMd = {};
  if (e.parent_entity_id) {
    var parent = flat.find(x=>x.id==e.parent_entity_id);
    if (parent) { try { parentMd = JSON.parse(parent.metadata_json||'{}'); } catch(_){} }
  }
  try {
    var md = JSON.parse(e.metadata_json||'{}');
    // setVal: use own value; if blank, show inherited value as placeholder
    var setVal = (elId, k) => {
      var el = document.getElementById(elId);
      if (!el) return;
      el.value = md[k] || '';
      var inherited = parentMd[k] || '';
      el.placeholder = inherited ? ('Inherited: '+inherited) : (el.dataset.defaultPlaceholder||'');
      if (!el.dataset.defaultPlaceholder) el.dataset.defaultPlaceholder = el.placeholder;
    };
    setVal('ee-ssn','ssn'); setVal('ee-dob','dob'); setVal('ee-addr','address');
    setVal('ee-state-tax-id','state_tax_id'); setVal('ee-filing-state','filing_state');
    setVal('ee-ein','ein'); setVal('ee-employer-id','employer_id');
    setVal('ee-state-form','state_formation'); setVal('ee-state-reg','state_reg_number');
    setVal('ee-biz-addr','business_address'); setVal('ee-reg-agent','registered_agent');
    setVal('ee-fiscal-year-end','fiscal_year_end'); setVal('ee-trade-name','trade_name');
    setVal('ee-dba-reg','dba_reg_number'); setVal('ee-dba-state','dba_state');
    setVal('ee-dba-exp','dba_expiration'); setVal('ee-phone','phone');
    setVal('ee-email','email'); setVal('ee-website','website');
    document.getElementById('ee-tax-id')?document.getElementById('ee-tax-id').value=e.tax_id||'':null;
  } catch(_){}
  // Show inherited-from-parent note
  var noteEl = document.getElementById('ee-inherit-note');
  if (noteEl) {
    if (e.parent_entity_id) {
      var pname = flat.find(x=>x.id==e.parent_entity_id);
      noteEl.textContent = pname ? ('Fields shown as "Inherited: ..." come from parent: '+( pname.display_name||pname.name)) : '';
      noteEl.style.display = '';
    } else { noteEl.style.display = 'none'; }
  }
  updateEntityTypeFields();
  document.getElementById('mEntityEdit').classList.add('open');
}

function _populateParentSelect(currentParentId, excludeId) {
  var sel = document.getElementById('ee-parent');
  sel.innerHTML = '<option value="">— none (root entity) —</option>';
  var flat = _flatEntities(_entityTree);
  flat.forEach(e=>{
    if (excludeId && e.id==excludeId) return;
    var opt = document.createElement('option');
    opt.value = e.id;
    opt.textContent = e.display_name||e.name;
    if (e.id==currentParentId) opt.selected=true;
    sel.appendChild(opt);
  });
}

function updateEntityTypeFields() {
  var type = document.getElementById('ee-type').value;
  document.querySelectorAll('.ee-type-section').forEach(el=>el.style.display='none');
  if (type==='person') document.getElementById('ee-person-fields').style.display='';
  else if (['llc','corporation','partnership','nonprofit','farm','other','business'].includes(type))
    document.getElementById('ee-biz-fields').style.display='';
  else if (type==='dba') {
    document.getElementById('ee-dba-fields').style.display='';
    document.getElementById('ee-biz-fields').style.display='';
  } else if (type==='trust')
    document.getElementById('ee-biz-fields').style.display=''; // trust uses EIN
}

async function saveEntity() {
  var id = document.getElementById('ee-id').value;
  var name = document.getElementById('ee-name').value.trim();
  if (!name) { toast('Name is required','error'); return; }

  function gv(eid){ var el=document.getElementById(eid); return el?el.value.trim():''; }

  var metadata = {
    ssn: gv('ee-ssn'), dob: gv('ee-dob'), address: gv('ee-addr'),
    state_tax_id: gv('ee-state-tax-id'), filing_state: gv('ee-filing-state'),
    ein: gv('ee-ein'), employer_id: gv('ee-employer-id'),
    state_formation: gv('ee-state-form'), state_reg_number: gv('ee-state-reg'),
    business_address: gv('ee-biz-addr'), registered_agent: gv('ee-reg-agent'),
    fiscal_year_end: gv('ee-fiscal-year-end'), trade_name: gv('ee-trade-name'),
    dba_reg_number: gv('ee-dba-reg'), dba_state: gv('ee-dba-state'),
    dba_expiration: gv('ee-dba-exp'), phone: gv('ee-phone'),
    email: gv('ee-email'), website: gv('ee-website'),
  };
  // Strip empty strings
  Object.keys(metadata).forEach(k=>{ if(!metadata[k]) delete metadata[k]; });

  var payload = {
    name, display_name: gv('ee-display')||name,
    type: document.getElementById('ee-type').value,
    color: gv('ee-color')||'#1a3c5e',
    description: gv('ee-desc'),
    parent_entity_id: document.getElementById('ee-parent').value||null,
    metadata,
  };

  var url = id ? '/api/entities/'+id : '/api/entities';
  var r = await post(url, payload);
  if (!r || r.error) { toast((r&&r.error)||'Save failed','error'); return; }
  toast(id?'Entity saved':'Entity created');
  document.getElementById('mEntityEdit').classList.remove('open');
  loadEntityTree();
  // Reload page to refresh entity dropdowns everywhere
  setTimeout(()=>location.reload(), 800);
}

async function archiveEntity(id, name) {
  if (!confirm(`Archive entity "${name}"? It will be hidden from all dropdowns but records are preserved.`)) return;
  var r = await post('/api/entities/'+id+'/archive', {});
  if (!r || r.error) { toast((r&&r.error)||'Failed','error'); return; }
  toast('Entity archived');
  loadEntityTree();
}

function openMergeEntity() {
  var flat = _flatEntities(_entityTree);
  ['merge-source','merge-target'].forEach(selId=>{
    var sel = document.getElementById(selId);
    sel.innerHTML = flat.map(e=>`<option value="${e.id}">${esc(e.display_name||e.name)} (${_entityTypeLabel(e.type)})</option>`).join('');
  });
  document.getElementById('merge-confirm').checked=false;
  document.getElementById('mEntityMerge').classList.add('open');
}

async function doMergeEntity() {
  if (!document.getElementById('merge-confirm').checked) {
    toast('Please check the confirmation box','error'); return;
  }
  var src = document.getElementById('merge-source').value;
  var tgt = document.getElementById('merge-target').value;
  if (src==tgt) { toast('Source and target must be different','error'); return; }
  var r = await post('/api/entities/'+src+'/merge', {target_entity_id:parseInt(tgt)});
  if (!r || r.error) { toast((r&&r.error)||'Merge failed','error'); return; }
  toast('Entities merged successfully');
  document.getElementById('mEntityMerge').classList.remove('open');
  loadEntityTree();
  setTimeout(()=>location.reload(), 800);
}

// Register with the tab-loader registry (Phase 9); the previous IIFE that
// monkey-patched window.sw has been removed for consistency with the other
// 12 tab modules.
registerTabLoader("entities", loadEntityTree);
