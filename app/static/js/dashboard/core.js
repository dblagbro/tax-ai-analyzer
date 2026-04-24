/* core.js — utilities, health polling, job-log modal, tab switcher */
/* Globals (P, _myUserId, _isAdmin, curSess) are defined inline in _scripts.html */


/* ---- Status light / health polling ---- */
async function refreshHealth() {
  try {
    const data = await fetch(P+'/api/health').then(r=>r.json()).catch(()=>null);
    if (!data) return;
    const names = ['tax-ai-analyzer','tax-paperless-web','tax-paperless-redis','tax-paperless-postgres','elasticsearch'];
    names.forEach(name => {
      const dot = document.getElementById('sd-'+name);
      const tip = document.getElementById('sdt-'+name);
      if (!dot || !tip) return;
      const info = data[name] || {status:'unknown'};
      dot.className = 'status-dot ' + (info.status === 'ok' ? 'ok' : info.status === 'warn' ? 'warn' : 'error');
      tip.textContent = name + ': ' + (info.message || info.status || 'unknown');
    });
  } catch(e) {}
}

/* ---- Password eye toggle ---- */
function togglePw(inputId, eyeEl) {
  const inp = document.getElementById(inputId);
  if (!inp) return;
  if (inp.type === 'password') { inp.type = 'text'; eyeEl.style.opacity = '1'; }
  else { inp.type = 'password'; eyeEl.style.opacity = '.45'; }
}

/* ---- Model dropdown ---- */
let _llmModels = {};
async function loadLlmModels() {
  try { _llmModels = await fetch(P+'/api/settings/llm-models').then(r=>r.json()); } catch(e) {}
  updateModelDropdown();
}
function updateModelDropdown(currentVal) {
  const prov = document.getElementById('s-prov')?.value || 'anthropic';
  const sel = document.getElementById('s-model');
  if (!sel) return;
  const prev = currentVal || sel.value;
  const models = _llmModels[prov] || [];
  sel.innerHTML = models.map(m=>`<option value="${m}"${m===prev?' selected':''}>${m}</option>`).join('');
  if (!sel.value && models.length) sel.value = models[0];
}

/* ---- Job Log Modal ---- */
let _jobLogTimer = null, _jobLogJobId = null, _jobLogOffset = 0;
function openJobLog(jobId, sourceType) {
  _jobLogJobId = jobId;
  _jobLogOffset = 0;
  const title = document.getElementById('jobLogTitle');
  const lines = document.getElementById('jobLogLines');
  const statusEl = document.getElementById('jobLogStatus');
  const metaEl = document.getElementById('jobLogMeta');
  const cancelBtn = document.getElementById('jobLogCancelBtn');
  if (title) title.textContent = 'Job #'+jobId+' — '+(sourceType||'');
  if (lines) lines.textContent = '';
  if (statusEl) { statusEl.textContent = 'running'; statusEl.className = 'badge badge-running'; }
  if (metaEl) metaEl.textContent = 'Fetching logs…';
  if (cancelBtn) cancelBtn.style.display = 'none';
  document.getElementById('mJobLog').classList.add('open');
  if (_jobLogTimer) clearInterval(_jobLogTimer);
  _pollJobLog();
  _jobLogTimer = setInterval(_pollJobLog, 2000);
}
async function _pollJobLog() {
  if (!_jobLogJobId) return;
  const data = await fetch(P+'/api/import/jobs/'+_jobLogJobId+'/logs?offset='+_jobLogOffset).then(r=>r.json()).catch(()=>null);
  const lines = document.getElementById('jobLogLines');
  if (data?.lines?.length && lines) {
    data.lines.forEach(l => { lines.textContent += l + '\n'; });
    _jobLogOffset = data.total;
    lines.scrollTop = lines.scrollHeight;
  }
  const job = await fetch(P+'/api/import/jobs/'+_jobLogJobId).then(r=>r.json()).catch(()=>null);
  if (!job) return;
  const statusEl = document.getElementById('jobLogStatus');
  const metaEl = document.getElementById('jobLogMeta');
  const cancelBtn = document.getElementById('jobLogCancelBtn');
  const isRunning = ['running','pending','cancelling'].includes(job.status);
  if (statusEl) { statusEl.textContent = job.status; statusEl.className = 'badge badge-'+job.status; }
  if (metaEl) metaEl.textContent = 'Imported: '+(job.count_imported??0)+' | Skipped: '+(job.count_skipped??0)+(job.error_msg?' | Error: '+job.error_msg.slice(0,60):'');
  if (cancelBtn) cancelBtn.style.display = (isRunning && _isAdmin) ? 'inline-flex' : 'none';
  if (!isRunning && _jobLogTimer) { clearInterval(_jobLogTimer); _jobLogTimer = null; }
  // Show fallback summary if no logs available (orphaned job or old job after restart)
  if (lines && !lines.textContent.trim() && data?.total === 0) {
    const isOrphaned = isRunning; // running but no logs = orphaned from prev container
    lines.textContent = isOrphaned
      ? '[Job was running in a previous container instance — logs not available]\n'
      : '[No log data available for this job]\n';
    lines.textContent += '─────────────────────────────────\n';
    lines.textContent += 'Status:    '+job.status+'\n';
    lines.textContent += 'Imported:  '+(job.count_imported??0)+'\n';
    lines.textContent += 'Skipped:   '+(job.count_skipped??0)+'\n';
    lines.textContent += 'Started:   '+(job.started_at||'?')+'\n';
    if (job.completed_at) lines.textContent += 'Completed: '+job.completed_at+'\n';
    if (job.error_msg) lines.textContent += 'Error:     '+job.error_msg+'\n';
    if (isOrphaned) lines.textContent += '\nTip: Click Cancel to mark this job as cancelled,\nthen start a new import — dedup will skip already-processed emails.\n';
  }
}
function closeJobLog() {
  if (_jobLogTimer) { clearInterval(_jobLogTimer); _jobLogTimer = null; }
  document.getElementById('mJobLog').classList.remove('open');
}
async function cancelJobFromLog() {
  if (!_jobLogJobId) return;
  if (!confirm('Cancel job #'+_jobLogJobId+'?')) return;
  const r = await post('/api/import/jobs/'+_jobLogJobId+'/cancel',{});
  toast(r.status==='cancelling'?'Cancelling…':'Not running.','info');
}

function sw(t) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-'+t)?.classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n => {
    if (n.getAttribute('onclick') && n.getAttribute('onclick').includes("'"+t+"'")) n.classList.add('active');
  });
  loadTab(t);
  const urlSlug = {tax_review:'tax-review', folder_manager:'folder-manager', ai_costs:'ai-costs'}[t] || t;
  history.replaceState(null,'', P + (t==='dashboard' ? '/' : '/'+urlSlug));
}

/* Tab-loader registry — each tab module registers its init function here
   (see registerTabLoader below). Keeps core.js decoupled from tab contents. */
const _tabLoaders = {};
function registerTabLoader(name, fn) { _tabLoaders[name] = fn; }

function loadTab(t) {
  // The Import tab owns a refresh interval; any other tab must stop it.
  if (t !== 'import' && typeof stopJobRefresh === 'function') stopJobRefresh();
  _tabLoaders[t]?.();
}

function applyGlobal() {
  const year = document.getElementById('g-year')?.value || '';
  const entity = document.getElementById('g-entity')?.value || '';
  // Sync per-tab dropdowns to global selection
  const map = [['tf-year','tf-entity'],['df-year','df-entity']];
  map.forEach(([yId, eId]) => {
    const yEl = document.getElementById(yId);
    const eEl = document.getElementById(eId);
    if (yEl && year) yEl.value = year;
    if (eEl && entity) eEl.value = entity;
  });
  loadStats();
  // Also reload whichever data tab is active
  const activePanel = document.querySelector('.tab-panel.active');
  if (activePanel) {
    if (activePanel.id === 'tab-transactions') loadTxns();
    else if (activePanel.id === 'tab-documents') loadDocs();
  }
}

/* Utils */
async function post(path,body){try{const r=await fetch(P+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}catch(e){console.error(e);return null;}}
function closeM(id){document.getElementById(id).classList.remove('open');}
document.querySelectorAll('.modal-overlay').forEach(el=>el.addEventListener('click',e=>{if(e.target===el)el.classList.remove('open');}));
function toast(m,t=''){const el=document.createElement('div');el.className='toast '+t;el.textContent=m;document.body.appendChild(el);setTimeout(()=>el.remove(),4000);}
function fmt(n){return Number(n||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fmtB(b){if(!b)return'0 B';const u=['B','KB','MB','GB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++;}return b.toFixed(1)+' '+u[i];}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function escColor(c){ // only allow hex colors (#abc, #aabbcc, #aabbccdd) — block CSS injection
  const s = String(c||'');
  return /^#[0-9a-fA-F]{3,8}$/.test(s) ? s : '#1a3c5e';
}
