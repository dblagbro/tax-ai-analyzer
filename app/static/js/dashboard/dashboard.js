/* dashboard.js — overview tab: stat cards, activity, recent jobs, jump helpers */

/* Stats */
async function loadStats() {
  const year = document.getElementById('g-year')?.value || '';
  const entity = document.getElementById('g-entity')?.value || '';
  const p = new URLSearchParams();
  if (year) p.set('year', year);
  if (entity) p.set('entity_id', entity);
  const r = await fetch(P+'/api/stats'+(p.toString()?'?'+p:'')).then(r=>r.json()).catch(()=>({}));
  document.getElementById('s-docs').textContent = r.total_docs ?? 0;
  document.getElementById('s-income').textContent = '$'+fmt(r.total_income);
  document.getElementById('s-expenses').textContent = '$'+fmt(r.total_expenses);
  const n = r.net ?? 0;
  const el = document.getElementById('s-net');
  el.textContent = (n<0?'-$':'$')+fmt(Math.abs(n));
  el.className = 'stat-num '+(n>=0?'pos':'neg');

  // Audit-risk card: in-parallel fetch, degrades gracefully
  fetch(P+'/api/transactions/audit-risk'+(p.toString()?'?'+p:'')).then(r=>r.json()).then(ar => {
    const rel = document.getElementById('s-audit-risk');
    if (rel) rel.textContent = (ar.count ?? 0).toString();
  }).catch(() => {});
  const g = document.getElementById('entityGrid');
  const ents = Object.entries(r.by_entity||{});
  g.innerHTML = ents.length ? ents.map(([sl,e])=>`<div class="entity-card" style="border-left-color:${escColor(e.color)};cursor:pointer" title="Filter by ${esc(e.name||sl)}" onclick="jumpToEntity(${Number(e.id)||0})" onmouseover="this.style.boxShadow='0 4px 16px rgba(0,0,0,.12)'" onmouseout="this.style.boxShadow=''">
    <div class="entity-name">${esc(e.name||sl)} <span style="font-size:.7rem;color:#888;font-weight:400">&#8594; filter</span></div>
    <div class="entity-stats">
      <div class="entity-stat" onclick="event.stopPropagation();jumpToTxnsEntity('income',${e.id})" style="cursor:pointer" title="View income"><div class="val pos">$${fmt(e.income)}</div><div class="lbl">Income &#8599;</div></div>
      <div class="entity-stat" onclick="event.stopPropagation();jumpToTxnsEntity('expense',${e.id})" style="cursor:pointer" title="View expenses"><div class="val neg">$${fmt(e.expenses)}</div><div class="lbl">Expenses &#8600;</div></div>
      <div class="entity-stat" onclick="event.stopPropagation();jumpToDocs(${e.id})" style="cursor:pointer" title="View documents"><div class="val">${e.doc_count}</div><div class="lbl">Docs &#8594;</div></div>
      <div class="entity-stat"><div class="val ${e.net>=0?'pos':'neg'}">${e.net<0?'-':''}$${fmt(Math.abs(e.net))}</div><div class="lbl">Net Income/Loss</div></div>
    </div></div>`).join('') : '<div class="empty">No entity data yet. Import documents or transactions.</div>';
}

async function loadAct() {
  const rows = await fetch(P+'/api/activity?limit=30').then(r=>r.json()).catch(()=>[]);
  const el = document.getElementById('actList');
  el.innerHTML = rows.length ? rows.map(a=>`<div class="act-item"><span class="act-ts">${(a.created_at||'').slice(0,16).replace('T',' ')}</span><span>${esc(a.action)}: ${esc(a.detail||'')}</span></div>`).join('') : '<div class="empty">No activity yet.</div>';
}

async function loadRecentJobs() {
  const jobs = await fetch(P+'/api/import/jobs').then(r=>r.json()).catch(()=>[]);
  const el = document.getElementById('recentJobs');
  el.innerHTML = jobs.length ? jobs.slice(0,8).map(j=>`<div class="job-item">
    <span class="job-src">${j.source_type||''}</span>
    <span><span class="badge badge-${j.status||'pending'}">${j.status||'pending'}</span></span>
    <span style="flex:1;color:var(--muted);font-size:.82rem">${esc(j.error_msg||'')} ${j.count_imported!=null?j.count_imported+' imported':''}</span>
    <span style="font-size:.72rem;color:var(--muted)">${(j.created_at||'').slice(0,16).replace('T',' ')}</span>
  </div>`).join('') : '<div class="empty">No imports yet.</div>';
}

/* Navigation helpers — jump to filtered views from dashboard cards */
function jumpToTxns(cat) {
  const subMap = {income:'txn-sub-income', expense:'txn-sub-expense', deduction:'txn-sub-deduction', '':'txn-sub-all'};
  document.querySelectorAll('.imp-tab[id^="txn-sub-"]').forEach(b=>b.classList.remove('active'));
  const targetBtn = document.getElementById(subMap[cat]||'txn-sub-all');
  if (targetBtn) targetBtn.classList.add('active');
  sw('transactions');
}
function jumpToTxnsEntity(cat, entityId) {
  const eEl = document.getElementById('tf-entity');
  if (eEl) eEl.value = entityId;
  const gEl = document.getElementById('g-entity');
  if (gEl) gEl.value = entityId;
  jumpToTxns(cat);
}
function jumpToEntity(entityId) {
  const gEl = document.getElementById('g-entity');
  if (gEl) { gEl.value = entityId; applyGlobal(); }
  sw('transactions');
}
function jumpToAuditRisk() {
  // Switch to transactions tab, Unmatched sub-tab, load reconcile view
  sw('transactions');
  setTimeout(() => {
    const btn = document.getElementById('txn-sub-unmatched');
    if (btn) showReconcile(btn);
  }, 50);
}
function jumpToDocs(entityId) {
  const gEl = document.getElementById('g-entity');
  if (gEl) { gEl.value = entityId; applyGlobal(); }
  sw('documents');
}
