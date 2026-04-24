/* admin.js — Settings, Users, Analysis trigger, Activity-log filter view */

/* Settings */
async function loadSettings() {
  await loadLlmModels();
  const s = await fetch(P+'/api/settings').then(r=>r.json()).catch(()=>({}));
  // Set provider first, then update model dropdown with saved value
  const provEl = document.getElementById('s-prov');
  if (provEl && s.llm_provider) provEl.value = s.llm_provider;
  updateModelDropdown(s.llm_model || '');
  const m={'llm_api_key':'s-key','paperless_url':'s-purl','paperless_token':'s-ptok','smtp_host':'s-smh','smtp_port':'s-smp','smtp_user':'s-smu','smtp_pass':'s-smw','smtp_from':'s-smf','s3_bucket':'s-s3b','s3_region':'s-s3r','s3_access_key':'s-s3a','s3_secret_key':'s-s3s'};
  for(const [k,id] of Object.entries(m)){const el=document.getElementById(id);if(el&&s[k]!=null)el.value=s[k];}
  loadAccountantToken();
}

async function loadAccountantToken() {
  const st = document.getElementById('acct-portal-status');
  const urlBox = document.getElementById('acct-portal-url');
  const openBtn = document.getElementById('acct-portal-open');
  if (!st) return;
  const r = await fetch(P+'/api/accountant/token').then(r=>r.json()).catch(()=>({}));
  if (r.token) {
    st.innerHTML = '<span style="color:#28a745">&#10003; Portal active — link ready to share</span>';
    urlBox.style.display = 'block';
    urlBox.textContent = r.url;
    openBtn.style.display = 'inline-block';
    openBtn.href = r.url;
  } else {
    st.innerHTML = '<span style="color:var(--muted)">No portal link generated yet</span>';
    urlBox.style.display = 'none';
    openBtn.style.display = 'none';
  }
}

async function generateAccountantToken() {
  const r = await post('/api/accountant/token', {action:'generate'});
  if (r && r.token) {
    toast('Accountant portal link generated', 'success');
    loadAccountantToken();
  } else {
    toast('Failed to generate link', 'error');
  }
}

async function clearAccountantToken() {
  if (!confirm('Revoke the current accountant portal link? Anyone holding it will lose access.')) return;
  await post('/api/accountant/token', {action:'clear'});
  toast('Accountant portal link revoked', 'success');
  loadAccountantToken();
}
async function saveSettings() {
  const r = await post('/api/settings',{'llm_provider':document.getElementById('s-prov').value,'llm_model':document.getElementById('s-model').value,'llm_api_key':document.getElementById('s-key').value,'paperless_url':document.getElementById('s-purl').value,'paperless_token':document.getElementById('s-ptok').value,'smtp_host':document.getElementById('s-smh').value,'smtp_port':document.getElementById('s-smp').value,'smtp_user':document.getElementById('s-smu').value,'smtp_pass':document.getElementById('s-smw').value,'smtp_from':document.getElementById('s-smf').value,'s3_bucket':document.getElementById('s-s3b').value,'s3_region':document.getElementById('s-s3r').value,'s3_access_key':document.getElementById('s-s3a').value,'s3_secret_key':document.getElementById('s-s3s').value});
  r?.status==='saved' ? toast('Settings saved.','success') : toast('Error saving settings.','error');
}
async function testLlm(){const r=await post('/api/settings/test-llm',{});r?.status==='ok'?toast('LLM OK: '+r.model+' → '+r.response,'success'):toast('LLM error: '+r?.message,'error');}
async function testPaperless(){const r=await post('/api/settings/test-paperless',{});r?.status==='ok'?toast('Paperless OK: '+r.url,'success'):toast('Paperless error: '+(r?.message||'HTTP '+r?.code),'error');}

/* Analysis */
async function triggerAnalysis(){const r=await post('/api/analyze/trigger',{});r?.status==='started'?toast('Analysis started.','success'):r?.status==='already_running'?toast('Already running.'):toast('Error.','error');}

/* Users */
async function loadUsers() {
  const users = await fetch(P+'/api/users').then(r=>r.json()).catch(()=>[]);
  const tb = document.getElementById('usersBody');
  tb.innerHTML = users.length ? users.map(u=>`<tr>
    <td><strong>${esc(u.username)}</strong></td><td>${esc(u.email||'')}</td>
    <td><span class="badge ${u.role==='admin'?'badge-income':'badge-other'}">${u.role}</span></td>
    <td>${u.active?'✓':''}</td>
    <td>${(u.last_login||'').slice(0,16).replace('T',' ')}</td>
    <td style="display:flex;gap:6px">
      <button class="btn btn-sm btn-outline" onclick="openReset(${u.id})">Reset PW</button>
      ${u.id != _myUserId ? '<button class="btn btn-sm btn-danger" onclick="delUser('+u.id+')">Delete</button>' : ''}
    </td>
  </tr>`).join('') : '<tr><td colspan="6"><div class="empty">No users.</div></td></tr>';
}
function openAddUser(){document.getElementById('mAddUser').classList.add('open');}
async function saveUser(){
  const r=await post('/api/users',{username:document.getElementById('nu-name').value.trim(),email:document.getElementById('nu-email').value.trim(),password:document.getElementById('nu-pass').value,is_admin:document.getElementById('nu-admin').checked});
  r?.id?(closeM('mAddUser'),toast('User created.','success'),loadUsers()):toast('Error: '+(r?.error||'?'),'error');
}
function openReset(id){document.getElementById('rp-uid').value=id;document.getElementById('mResetPw').classList.add('open');}
async function doResetPw(){
  const pw=document.getElementById('rp-pw').value;
  if(!pw||pw.length<6){toast('Min 6 chars.','error');return;}
  const r=await post('/api/users/'+document.getElementById('rp-uid').value+'/reset-password',{password:pw});
  r?.status==='ok'?(closeM('mResetPw'),toast('Password reset.','success')):toast('Error.','error');
}
async function delUser(id){if(!confirm('Delete user?'))return;const r=await fetch(P+'/api/users/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));r.status==='deleted'?(toast('Deleted.','success'),loadUsers()):toast('Error.','error');}


/* ───── Activity log filter view ───── */
let _actOffset = 0;
const _actPageSize = 100;
let _actTotal = 0;
let _actDebounceTimer = null;

function debouncedLoadActivity() {
  clearTimeout(_actDebounceTimer);
  _actDebounceTimer = setTimeout(() => { _actOffset = 0; loadActivity(); }, 300);
}

async function loadActivity() {
  await Promise.all([_ensureActionOptions(), _ensureUserOptions()]);
  const p = new URLSearchParams();
  const s = document.getElementById('act-search')?.value.trim();
  const a = document.getElementById('act-action')?.value;
  const u = document.getElementById('act-user')?.value;
  const since = document.getElementById('act-since')?.value;
  const until = document.getElementById('act-until')?.value;
  if (s) p.set('search', s);
  if (a) p.set('action', a);
  if (u) p.set('user_id', u);
  if (since) p.set('since', since);
  if (until) p.set('until', until);
  p.set('limit', _actPageSize);
  p.set('offset', _actOffset);

  const body = document.getElementById('act-body');
  body.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted)"><div class="spinner"></div></td></tr>';

  const r = await fetch(P+'/api/activity?'+p).then(r=>r.json()).catch(()=>({rows:[],total:0}));
  // Endpoint returns a flat list if no filters; a {rows,total} if filtered
  const rows = Array.isArray(r) ? r : (r.rows || []);
  _actTotal = Array.isArray(r) ? rows.length : (r.total || 0);

  const countEl = document.getElementById('act-count');
  if (countEl) {
    const from = _actOffset + (rows.length ? 1 : 0);
    const to = _actOffset + rows.length;
    countEl.textContent = `${from.toLocaleString()}–${to.toLocaleString()} of ${_actTotal.toLocaleString()}`;
  }
  const pageLabel = document.getElementById('act-page-label');
  if (pageLabel) pageLabel.textContent = `Page ${Math.floor(_actOffset/_actPageSize)+1} of ${Math.max(1, Math.ceil(_actTotal/_actPageSize))}`;
  const prevBtn = document.getElementById('act-prev-btn');
  const nextBtn = document.getElementById('act-next-btn');
  if (prevBtn) prevBtn.disabled = _actOffset === 0;
  if (nextBtn) nextBtn.disabled = _actOffset + rows.length >= _actTotal;

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted)">No activity matches the filters.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(a => `<tr>
    <td style="white-space:nowrap;font-size:.78rem;color:var(--muted)">${esc((a.created_at||'').slice(0,19).replace('T',' '))}</td>
    <td style="white-space:nowrap"><span class="tag" style="font-size:.76rem">${esc(a.action||'')}</span></td>
    <td style="max-width:400px;overflow:hidden;text-overflow:ellipsis" title="${esc(a.detail||'')}">${esc(a.detail||'')}</td>
    <td>${esc(a.username||'—')}</td>
    <td>${esc(a.entity_name||'')}</td>
  </tr>`).join('');
}

async function _ensureActionOptions() {
  const sel = document.getElementById('act-action');
  if (!sel || sel.dataset.loaded) return;
  const opts = await fetch(P+'/api/activity/actions').then(r=>r.json()).catch(()=>[]);
  (opts || []).forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.action;
    opt.textContent = `${o.action} (${o.count.toLocaleString()})`;
    sel.appendChild(opt);
  });
  sel.dataset.loaded = '1';
}

async function _ensureUserOptions() {
  const sel = document.getElementById('act-user');
  if (!sel || sel.dataset.loaded) return;
  const users = await fetch(P+'/api/users').then(r=>r.json()).catch(()=>[]);
  (users || []).forEach(u => {
    const opt = document.createElement('option');
    opt.value = u.id;
    opt.textContent = u.username;
    sel.appendChild(opt);
  });
  sel.dataset.loaded = '1';
}

function clearActivityFilters() {
  ['act-search','act-action','act-user','act-since','act-until'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  _actOffset = 0;
  loadActivity();
}

function actPagePrev() {
  if (_actOffset === 0) return;
  _actOffset = Math.max(0, _actOffset - _actPageSize);
  loadActivity();
}
function actPageNext() {
  if (_actOffset + _actPageSize >= _actTotal) return;
  _actOffset += _actPageSize;
  loadActivity();
}

/* ── User Profile modal ──────────────────────────────────────────────── */
async function openProfile() {
  document.getElementById('uMenu').classList.remove('open');
  var d = await fetch(P+'/api/user/profile').then(r=>r.json()).catch(()=>({}));
  var p = d.profile || {};
  document.getElementById('prof-fullname').value = p.full_name || '';
  document.getElementById('prof-email').value = p.email || '';
  document.getElementById('prof-phone').value = p.phone || '';
  document.getElementById('prof-address').value = p.address || '';
  document.getElementById('prof-city').value = p.city || '';
  document.getElementById('prof-state').value = p.state || '';
  document.getElementById('prof-zip').value = p.zip || '';
  document.getElementById('prof-notify-email').checked = !!p.notify_email;
  document.getElementById('prof-notify-import').checked = !!p.notify_import_complete;
  document.getElementById('mUserProfile').classList.add('open');
}
async function saveProfile() {
  var body = {
    full_name: document.getElementById('prof-fullname').value.trim(),
    email: document.getElementById('prof-email').value.trim(),
    phone: document.getElementById('prof-phone').value.trim(),
    address: document.getElementById('prof-address').value.trim(),
    city: document.getElementById('prof-city').value.trim(),
    state: document.getElementById('prof-state').value.trim(),
    zip: document.getElementById('prof-zip').value.trim(),
    notify_email: document.getElementById('prof-notify-email').checked,
    notify_import_complete: document.getElementById('prof-notify-import').checked,
  };
  var r = await post('/api/user/profile', body);
  if (!r || r.error) { toast((r&&r.error)||'Save failed','error'); return; }
  toast('Profile saved');
  document.getElementById('mUserProfile').classList.remove('open');
}

/* ── Help / About modals ─────────────────────────────────────────────── */
function openHelp() {
  document.getElementById('uMenu').classList.remove('open');
  document.getElementById('mHelp').classList.add('open');
}
function openAbout() {
  document.getElementById('uMenu').classList.remove('open');
  document.getElementById('mAbout').classList.add('open');
}
function showHelpSection(id, btn) {
  document.querySelectorAll('.help-section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.help-nav button').forEach(b=>b.classList.remove('active'));
  document.getElementById('help-'+id).classList.add('active');
  btn.classList.add('active');
}

registerTabLoader("settings", loadSettings);
registerTabLoader("users", loadUsers);
registerTabLoader("activity", loadActivity);
