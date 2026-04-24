/* import_hub.js — Import Hub tab: source selectors, jobs list, Gmail import polling */

/* Import */
function impTab(t,btn) {
  document.querySelectorAll('.imp-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.imp-tab').forEach(b=>b.classList.remove('active'));
  document.getElementById('ip-'+t)?.classList.add('active');
  if(btn) btn.classList.add('active');
}

var _gmLogTimer = null;
var _gmLogOffset = 0;
var _gmLogJobId = null;

async function startGmail() {
  const eid = document.getElementById('gm-entity').value;
  const yrsRaw = (document.getElementById('gm-years-input')?.value || '').trim();
  const yrs = yrsRaw.split(/[\s,]+/).filter(Boolean);
  if(!yrs.length){toast('Enter at least one year (e.g. 2020,2021,2022).','error');return;}
  const r = await post('/api/import/gmail/start',{entity_id:eid||null,years:yrs});
  if(r?.error){toast('Error: '+r.error,'error');return;}
  if(r?.job_id){
    toast('Gmail import started (12 parallel month-workers). Job #'+r.job_id,'success');
    loadJobs();
    _startGmailLogPoller(r.job_id);
  }
}

function _startGmailLogPoller(jobId) {
  _gmLogJobId = jobId;
  _gmLogOffset = 0;
  const panel = document.getElementById('gm-log-panel');
  const lines = document.getElementById('gm-log-lines');
  const statusEl = document.getElementById('gm-log-status');
  const cancelBtn = document.getElementById('gm-cancel-btn');
  panel.style.display='';
  lines.textContent='';
  statusEl.textContent='running…';
  statusEl.style.color='';
  document.getElementById('gm-start-btn').disabled=true;
  cancelBtn.style.display='inline-block';
  if(_gmLogTimer) clearInterval(_gmLogTimer);
  _gmLogTimer = setInterval(async function(){
    const data = await fetch(P+'/api/import/jobs/'+_gmLogJobId+'/logs?offset='+_gmLogOffset).then(r=>r.json()).catch(()=>null);
    if(data?.lines?.length){
      data.lines.forEach(function(l){ lines.textContent += l + '\n'; });
      _gmLogOffset = data.total;
      lines.scrollTop = lines.scrollHeight;
    }
    const job = await fetch(P+'/api/import/jobs/'+_gmLogJobId).then(r=>r.json()).catch(()=>null);
    if(job && !['running','pending','cancelling'].includes(job.status)){
      clearInterval(_gmLogTimer); _gmLogTimer=null;
      cancelBtn.style.display='none';
      const done = job.status==='completed';
      const cancelled = job.status==='cancelled';
      statusEl.textContent = done
        ? '✓ complete — '+job.count_imported+' imported'
        : cancelled ? '⏹ cancelled — '+job.count_imported+' imported so far'
        : '✗ '+job.status+(job.error_msg?' — '+job.error_msg.slice(0,60):'');
      statusEl.style.color = done ? '#56d364' : cancelled ? '#ffc107' : '#f85149';
      document.getElementById('gm-start-btn').disabled=false;
      loadJobs();
    }
  }, 2000);
}

async function cancelGmailImport() {
  if(!_gmLogJobId) return;
  if(!confirm('Cancel the running import? Progress so far will be saved and deduplication will skip already-processed emails on restart.')) return;
  const r = await post('/api/import/jobs/'+_gmLogJobId+'/cancel',{});
  toast(r.status==='cancelling'?'Cancelling import…':'Not currently running.','info');
  document.getElementById('gm-cancel-btn').style.display='none';
}

async function uploadCreds(input) {
  const fd = new FormData(); fd.append('credentials', input.files[0]);
  const r = await fetch(P+'/api/import/gmail/credentials',{method:'POST',body:fd}).then(r=>r.json()).catch(()=>({}));
  r.status==='saved' ? toast('credentials.json saved.','success') : toast('Error: '+r.error,'error');
}

async function savePaypalCreds() {
  const cid = document.getElementById('pp-cid').value.trim();
  const csec = document.getElementById('pp-csec').value.trim();
  const sandbox = document.getElementById('pp-sandbox').checked;
  if(!cid||!csec){toast('Enter Client ID and Secret.','error');return;}
  const r = await post('/api/import/paypal/credentials',{client_id:cid,client_secret:csec,sandbox});
  if(r?.status==='ok'){
    toast(r.message,'success');
    document.getElementById('pp-status-badge').innerHTML='<span class="badge badge-completed">Connected</span>';
  } else {
    toast(r?.message||'Saved (test failed — check credentials).','warn');
    document.getElementById('pp-status-badge').innerHTML='<span class="badge badge-warn">Saved — verify credentials</span>';
  }
}

async function pullPaypal() {
  const eid = document.getElementById('pp-entity').value;
  const years = document.getElementById('pp-years').value;
  const r = await post('/api/import/paypal/pull',{entity_id:eid||null,years:years.split(',').map(y=>y.trim()).filter(Boolean)});
  r?.job_id ? (toast('PayPal pull started. Job #'+r.job_id,'success'),loadJobs()) : toast('Error: '+(r?.error||'Check credentials first.'),'error');
}

async function importVenmo() {
  const f = document.getElementById('vmFile').files[0];
  if(!f){toast('Select CSV first.','error');return;}
  const fd = new FormData(); fd.append('file',f);
  fd.append('entity_id',document.getElementById('vm-entity').value);
  fd.append('year',document.getElementById('vm-year').value);
  const r = await fetch(P+'/api/import/venmo/csv',{method:'POST',body:fd}).then(r=>r.json()).catch(()=>({}));
  r.job_id ? (toast('Venmo import started.','success'),loadJobs()) : toast('Error: '+(r.error||'?'),'error');
}

async function importBank() {
  const f = document.getElementById('bkFile').files[0];
  if(!f){toast('Select CSV first.','error');return;}
  const fd = new FormData(); fd.append('file',f);
  fd.append('entity_id',document.getElementById('bk-entity').value);
  fd.append('year',document.getElementById('bk-year').value);
  fd.append('date_col',document.getElementById('bk-date').value);
  fd.append('desc_col',document.getElementById('bk-desc').value);
  fd.append('amount_col',document.getElementById('bk-amt').value);
  const r = await fetch(P+'/api/import/bank-csv',{method:'POST',body:fd}).then(r=>r.json()).catch(()=>({}));
  r.job_id ? (toast('Bank import started.','success'),loadJobs()) : toast('Error: '+(r.error||'?'),'error');
}

async function importOFX() {
  const f = document.getElementById('ofxFile').files[0];
  if(!f){toast('Select OFX/QFX file first.','error');return;}
  const fd = new FormData(); fd.append('file',f);
  fd.append('entity_id',document.getElementById('ofx-entity').value);
  fd.append('year',document.getElementById('ofx-year').value);
  const r = await fetch(P+'/api/import/bank-ofx',{method:'POST',body:fd}).then(r=>r.json()).catch(()=>({}));
  r.job_id ? (toast('OFX import started. Job #'+r.job_id,'success'),loadJobs()) : toast('Error: '+(r.error||'?'),'error');
}

async function scanLocalFs() {
  const path = document.getElementById('lf-path').value.trim();
  if(!path){toast('Enter a directory path.','error');return;}
  const r = await post('/api/import/local/scan',{path});
  if(r?.error){toast('Error: '+r.error,'error');return;}
  const prev = document.getElementById('lf-preview');
  prev.style.display='block';
  let html = `<strong>Found ${r.total} files:</strong> ${r.counts.pdf} PDFs, ${r.counts.csv} CSVs, ${r.counts.ofx} OFX/QFX`;
  if (r.suggested_entity) {
    html += ` &nbsp;|&nbsp; <span style="color:var(--green)">&#10003; Auto-detected entity: <strong>${esc(r.suggested_entity.name)}</strong></span>`;
    // Pre-select the entity dropdown
    var sel = document.getElementById('lf-entity');
    for (var i=0; i<sel.options.length; i++) {
      if (sel.options[i].value == r.suggested_entity.id) { sel.selectedIndex=i; break; }
    }
    html += ` <a href="#" onclick="event.preventDefault();document.getElementById('lf-entity').focus()" style="font-size:.8rem">(change)</a>`;
  } else {
    html += ` &nbsp;|&nbsp; <span style="color:var(--muted)">No entity auto-detected — please select one below</span>`;
  }
  prev.innerHTML = html;
}

async function runLocalFs() {
  const path = document.getElementById('lf-path').value.trim();
  if(!path){toast('Enter a directory path.','error');return;}
  const eid = document.getElementById('lf-entity').value;
  const yr = document.getElementById('lf-year').value;
  const r = await post('/api/import/local/run',{path,entity_id:eid||null,year:yr||null});
  r?.job_id ? (toast('Local folder import started. Job #'+r.job_id,'success'),loadJobs()) : toast('Error: '+(r?.error||'?'),'error');
}

async function importUrl() {
  const url = document.getElementById('urlInput').value.trim();
  if(!url){toast('Enter URL.','error');return;}
  const r = await post('/api/import/url',{url,entity_id:document.getElementById('url-entity').value||null});
  r?.job_id ? (toast('URL import started.','success'),loadJobs()) : toast('Error.','error');
}

var _jobRefreshTimer = null;

let _jobsCache = [];
let _jobsSort = {col: 'id', dir: -1}; // default: newest first

function sortJobs(col) {
  if (_jobsSort.col === col) _jobsSort.dir *= -1;
  else { _jobsSort.col = col; _jobsSort.dir = col === 'id' ? -1 : 1; }
  renderJobs(_jobsCache);
}

function renderJobs(jobs) {
  const el = document.getElementById('jobsList');
  if (!el) return;
  function fmtAge(iso) {
    if(!iso) return '';
    const s = Math.round((Date.now()-new Date(iso+'Z').getTime())/1000);
    if(s<60) return s+'s ago'; if(s<3600) return Math.round(s/60)+'m ago'; return Math.round(s/3600)+'h ago';
  }
  function durSecs(j) {
    if (j.started_at && j.completed_at)
      return Math.round((new Date(j.completed_at+'Z')-new Date(j.started_at+'Z'))/1000);
    return j.started_at ? (Date.now()-new Date(j.started_at+'Z').getTime())/1000 : 0;
  }
  const col = _jobsSort.col, dir = _jobsSort.dir;
  const sorted = [...jobs].sort((a,b)=>{
    let av, bv;
    if (col==='id')          { av=a.id; bv=b.id; }
    else if (col==='source') { av=a.source_type||''; bv=b.source_type||''; }
    else if (col==='status') { av=a.status||''; bv=b.status||''; }
    else if (col==='count')  { av=a.count_imported||0; bv=b.count_imported||0; }
    else if (col==='started'){ av=a.started_at||''; bv=b.started_at||''; }
    else if (col==='dur')    { av=durSecs(a); bv=durSecs(b); }
    else                     { av=a.id; bv=b.id; }
    if (av < bv) return -dir; if (av > bv) return dir; return 0;
  });
  function thBtn(label, key) {
    const active = _jobsSort.col === key;
    const arrow = active ? (_jobsSort.dir > 0 ? ' ▲' : ' ▼') : ' ⇅';
    return `<th style="cursor:pointer;user-select:none${active?';color:var(--primary)':''}" onclick="sortJobs('${key}')">${label}${arrow}</th>`;
  }
  try {
    el.innerHTML='<div class="tbl-wrap"><table><thead><tr>'
      +thBtn('ID','id')+thBtn('Source','source')+'<th>Entity</th>'
      +thBtn('Status','status')+thBtn('Count','count')+thBtn('Started','started')
      +thBtn('Duration','dur')+'<th>Detail</th><th></th>'
      +'</tr></thead><tbody>'
      +sorted.map(j=>{
        try {
        const dur = j.started_at && j.completed_at
          ? durSecs(j)+'s'
          : j.started_at ? '⏱ '+fmtAge(j.started_at) : '';
        const yearsStr = (() => { try { return JSON.parse(j.config_json||'{}').years||''; } catch(e){ return ''; } })();
        const errMsg = j.error_msg || '';
        const detail = errMsg
          ? `<span style="color:var(--red);font-size:.78rem" title="${esc(errMsg)}">${esc(errMsg.slice(0,80))}${errMsg.length>80?'…':''}</span>`
          : (yearsStr ? `<span style="font-size:.75rem;color:var(--muted)">${esc(String(yearsStr))}</span>` : '');
        const isRunning = ['running','pending','cancelling'].includes(j.status);
        const cancelBtn = isRunning
          ? `<button class="btn btn-sm" style="color:#e53e3e;border-color:#e53e3e;padding:2px 8px;font-size:.72rem"
               onclick="cancelJob(${j.id})">&#9632; Cancel</button>`
          : '';
        const logsBtn = `<button class="btn btn-sm btn-outline" style="padding:2px 8px;font-size:.72rem"
             onclick="openJobLog(${j.id},'${j.source_type||''}')">&#128196; Logs</button>`;
        const delBtn = _isAdmin && !isRunning
          ? `<button class="btn btn-sm" style="color:#e53e3e;padding:2px 6px;font-size:.72rem;background:transparent;border:1px solid #e53e3e"
               onclick="deleteJob(${j.id})" title="Delete job">&#128465;</button>`
          : '';
        return `<tr>
          <td>${j.id}</td>
          <td><strong>${j.source_type||''}</strong></td>
          <td>${esc(j.entity_name||'—')}</td>
          <td><span class="badge badge-${j.status||'pending'}">${j.status||'pending'}</span></td>
          <td>${j.count_imported != null ? j.count_imported : '—'}</td>
          <td style="font-size:.8rem">${(j.started_at||'').slice(0,16).replace('T',' ')}</td>
          <td style="font-size:.8rem">${dur}</td>
          <td>${detail}</td>
          <td style="white-space:nowrap">${cancelBtn} ${logsBtn} ${delBtn}</td></tr>`;
        } catch(e) { return '<tr><td colspan="9" style="color:var(--red);font-size:.78rem">Job render error: '+String(e)+'</td></tr>'; }
      }).join('')+'</tbody></table></div>';
  } catch(e) {
    el.innerHTML = '<div class="empty" style="color:var(--red)">Error rendering jobs: '+String(e)+'</div>';
  }
}

async function loadJobs() {
  const el = document.getElementById('jobsList');
  let jobs = [];
  try {
    const r = await fetch(P+'/api/import/jobs');
    if (r.ok) jobs = await r.json();
  } catch(e) {
    if (el) el.innerHTML = '<div class="empty" style="color:var(--red)">Failed to load jobs — check console.</div>';
    return;
  }
  if (!Array.isArray(jobs)) jobs = [];
  _jobsCache = jobs;
  if(!el) return;
  const hasRunning = jobs.some(j=>j.status==='running'||j.status==='pending');
  const hdr = document.getElementById('jobs-refresh-ind');
  if(hdr) hdr.textContent = hasRunning ? '↻ Auto-refreshing…' : 'Last refreshed: '+new Date().toLocaleTimeString();
  if(!jobs.length){el.innerHTML='<div class="empty">No import jobs yet.</div>';return;}
  renderJobs(jobs);
}

async function cancelJob(jobId) {
  if(!confirm('Cancel job #'+jobId+'? Progress so far is saved — restarting will skip already-processed emails.')) return;
  const r = await post('/api/import/jobs/'+jobId+'/cancel',{});
  toast(r.status==='cancelling'?'Cancelling job #'+jobId+'…':'Job not currently running.','info');
  setTimeout(loadJobs, 1000);
}

async function deleteJob(jobId) {
  if(!confirm('Delete job #'+jobId+' from history? This cannot be undone.')) return;
  const r = await fetch(P+'/api/import/jobs/'+jobId,{method:'DELETE'}).then(res=>res.json()).catch(()=>({}));
  if(r.status==='deleted') { toast('Job #'+jobId+' deleted.','success'); loadJobs(); }
  else toast('Error: '+(r.error||'unknown'),'error');
}

function startJobRefresh() {
  if(_jobRefreshTimer) return;
  _jobRefreshTimer = setInterval(function(){
    const jobs_el = document.getElementById('jobsList');
    if(!jobs_el) { stopJobRefresh(); return; }
    loadJobs();
  }, 5000);
}

function stopJobRefresh() {
  if(_jobRefreshTimer){ clearInterval(_jobRefreshTimer); _jobRefreshTimer=null; }
}

async function loadGmailImportStatus() {
  const s = await fetch(P+'/api/import/gmail/status').then(r=>r.json()).catch(()=>({}));
  const statusEl = document.getElementById('gm-import-status');
  const termsEl = document.getElementById('gm-search-terms');
  if(statusEl) {
    const cred = s.has_credentials;
    const auth = s.authenticated;
    let html = `<span style="display:inline-flex;align-items:center;gap:6px;margin-right:12px">
        <span style="width:8px;height:8px;border-radius:50%;background:${cred?'#28a745':'#dc3545'};display:inline-block"></span>
        credentials.json ${cred?'loaded':'not configured'}
       </span>`+
      `<span style="display:inline-flex;align-items:center;gap:6px">
        <span style="width:8px;height:8px;border-radius:50%;background:${auth?'#28a745':'#ffc107'};display:inline-block"></span>
        OAuth ${auth?'authorized — ready to import':'not authorized'}
       </span>`;
    if(!auth && s.callback_url) {
      html += `<div style="margin-top:8px;padding:8px 10px;background:#fff8e1;border:1px solid #ffd54f;border-radius:6px;font-size:.78rem;color:#555">
        <strong style="color:#e65100">&#9888; Action required:</strong> Add this Authorized Redirect URI to your Google Cloud credential:<br>
        <code style="background:#f5f5f5;padding:2px 6px;border-radius:3px;user-select:all">${esc(s.callback_url)}</code>
        <span style="display:block;margin-top:4px;color:#888">Go to console.cloud.google.com → Credentials → edit your OAuth client → Authorized redirect URIs</span>
      </div>`;
    }
    statusEl.innerHTML = html;
  }
  if(termsEl && s.search_terms) {
    termsEl.innerHTML = s.search_terms.map(t=>`<span style="display:inline-block;background:#f0f4ff;border:1px solid #c8d4f0;border-radius:4px;padding:1px 7px;margin:2px 3px 2px 0;font-family:monospace">${esc(t)}</span>`).join('');
    // Store for editing
    termsEl.dataset.raw = s.search_terms.join(' ');
  }
}

window._gmTermsRaw = '';
window.editSearchTerms = function() {
  const termsEl = document.getElementById('gm-search-terms');
  const editor = document.getElementById('gm-terms-editor');
  const hint = document.getElementById('gm-terms-hint');
  editor.value = termsEl.dataset.raw || '';
  termsEl.style.display = 'none';
  editor.style.display = '';
  hint.style.display = '';
  document.getElementById('gm-terms-edit-btn').style.display = 'none';
  document.getElementById('gm-terms-save-btn').style.display = '';
  document.getElementById('gm-terms-cancel-btn').style.display = '';
};
window.cancelSearchTerms = function() {
  document.getElementById('gm-search-terms').style.display = '';
  document.getElementById('gm-terms-editor').style.display = 'none';
  document.getElementById('gm-terms-hint').style.display = 'none';
  document.getElementById('gm-terms-edit-btn').style.display = '';
  document.getElementById('gm-terms-save-btn').style.display = 'none';
  document.getElementById('gm-terms-cancel-btn').style.display = 'none';
};
window.saveSearchTerms = async function() {
  const terms = document.getElementById('gm-terms-editor').value.trim();
  const r = await post('/api/import/gmail/search-terms', {terms});
  if(r?.status === 'ok') {
    toast('Search terms saved.', 'success');
    cancelSearchTerms();
    loadGmailImportStatus();
  } else {
    toast('Save failed: ' + (r?.error||'?'), 'error');
  }
};

