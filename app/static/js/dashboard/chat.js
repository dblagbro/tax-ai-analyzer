/* chat.js — AI Chat tab: sessions, messages, sharing, PDF export */

/* Chat */
var _chatAbortCtrl = null;
var _searchDebounce = null;

async function loadSessions(q) {
  var url = P+'/api/chat/sessions'+(q?'?q='+encodeURIComponent(q):'');
  var sessions = await fetch(url).then(r=>r.json()).catch(()=>[]);
  var el = document.getElementById('sessList');
  if (!sessions.length) {
    el.innerHTML = '<div class="empty" style="padding:12px;font-size:.82rem">'+(q?'No results.':'No conversations')+'</div>';
    return;
  }
  el.innerHTML = sessions.map(s=>`
    <div class="chat-sess-item ${s.id==curSess?'active':''}" onclick="loadSess(${s.id})" id="sess-item-${s.id}">
      <div class="sess-title-row">
        <span class="sess-title">${esc(s.title||'Chat #'+s.id)}</span>
        ${s.is_shared?'<span style="font-size:.62rem;color:#7c3aed;flex-shrink:0">shared</span>':''}
        <button class="sess-menu-btn" onclick="toggleSessMenu(${s.id},event)" title="Options">⋯</button>
      </div>
      <div class="sess-date">${s.entity_name?'<span style="color:#1a3c5e">'+esc(s.entity_name)+'</span> · ':''} ${(s.updated_at||'').slice(0,16).replace('T',' ')}</div>
      <div class="sess-dropdown" id="sess-dd-${s.id}">
        <div class="sess-dd-item" onclick="renameSess(${s.id},event)">✏️ Rename</div>
        <div class="sess-dd-item" onclick="shareSessFromMenu(${s.id},event)">🔗 Share</div>
        <div class="sess-dd-item" onclick="exportSess(${s.id},event)">📄 Export PDF</div>
        <hr class="sess-dd-sep">
        <div class="sess-dd-item danger" onclick="deleteSess(${s.id},event)">🗑 Delete</div>
      </div>
    </div>`).join('');
  // Close dropdowns when clicking elsewhere
  document.addEventListener('click', closeSessMenus, {once:true, capture:true});
}

function toggleSessMenu(id, e) {
  e.stopPropagation();
  const dd = document.getElementById('sess-dd-'+id);
  const wasOpen = dd?.classList.contains('open');
  closeSessMenus();
  if (!wasOpen && dd) dd.classList.add('open');
}
function closeSessMenus() {
  document.querySelectorAll('.sess-dropdown.open').forEach(d=>d.classList.remove('open'));
}

async function renameSess(id, e) {
  e.stopPropagation(); closeSessMenus();
  const item = document.getElementById('sess-item-'+id);
  const titleEl = item?.querySelector('.sess-title');
  const current = titleEl?.textContent || '';
  // Inline rename: replace title span with input
  if (!titleEl) return;
  const inp = document.createElement('input');
  inp.className = 'form-control';
  inp.style.cssText = 'font-size:.8rem;padding:3px 7px;height:26px;flex:1';
  inp.value = current;
  titleEl.replaceWith(inp);
  inp.focus(); inp.select();
  const finish = async (save) => {
    const newTitle = inp.value.trim() || current;
    inp.replaceWith(titleEl);
    if (save && newTitle !== current) {
      titleEl.textContent = newTitle;
      await post('/api/chat/sessions/'+id+'/rename', {title: newTitle});
      if (id == curSess) document.getElementById('chatTitle').textContent = newTitle;
    }
  };
  inp.onblur = () => finish(true);
  inp.onkeydown = (ev) => { if(ev.key==='Enter'){ev.preventDefault();finish(true);} if(ev.key==='Escape'){finish(false);} };
}

async function shareSessFromMenu(id, e) {
  e.stopPropagation(); closeSessMenus();
  if (id != curSess) await loadSess(id);
  openShareModal();
}

async function exportSess(id, e) {
  e.stopPropagation(); closeSessMenus();
  window.open(P+'/api/chat/sessions/'+id+'/export', '_blank');
}

async function deleteSess(id, e) {
  e.stopPropagation(); closeSessMenus();
  if (!confirm('Delete this conversation? This cannot be undone.')) return;
  await fetch(P+'/api/chat/sessions/'+id, {method:'DELETE'});
  if (id == curSess) {
    curSess = null;
    document.getElementById('chatMsgs').innerHTML = '<div class="empty">Select or start a conversation.</div>';
    document.getElementById('chatTitle').textContent = 'Select a conversation';
    document.getElementById('chatSharedTag').style.display = 'none';
    document.getElementById('btnShare').style.display = 'none';
  }
  loadSessions();
  toast('Conversation deleted.', 'success');
}

function searchSessions(q) {
  clearTimeout(_searchDebounce);
  _searchDebounce = setTimeout(()=>loadSessions(q||''), 300);
}

async function newSession() {
  var r = await post('/api/chat/sessions',{
    title:'Chat '+new Date().toLocaleDateString(),
    entity_id:parseInt(document.getElementById('chat-entity').value)||null,
    year:document.getElementById('chat-year').value||null
  });
  if(r?.id){curSess=r.id;loadSessions();loadSess(r.id);}
}

async function loadSess(id) {
  curSess=id;
  var d = await fetch(P+'/api/chat/sessions/'+id+'/messages').then(r=>r.json()).catch(()=>({}));
  var sess = d.session||{};
  document.getElementById('chatTitle').textContent = sess.title||'Chat #'+id;
  var sharedTag = document.getElementById('chatSharedTag');
  if (d.can_write === false && sess.user_id != _myUserId) {
    sharedTag.style.display='block';
  } else {
    sharedTag.style.display='none';
  }
  document.getElementById('btnShare').style.display = 'inline-flex';
  var readOnly = d.can_write === false;
  document.getElementById('chatIn').disabled = readOnly;
  document.getElementById('sendBtn').disabled = readOnly;
  _renderMessages(d.messages||[], readOnly);
  document.querySelectorAll('.chat-sess-item').forEach(el=>el.classList.toggle('active',el.getAttribute('onclick')?.includes('('+id+')')));
}

function _renderMessages(msgs, readOnly) {
  var c = document.getElementById('chatMsgs');
  if (!msgs.length) { c.innerHTML='<div class="empty">No messages yet.</div>'; return; }
  c.innerHTML = msgs.map(m=>{
    var editBtn = (!readOnly && m.role==='user')
      ? `<button class="chat-edit-btn" onclick="editMsg(${m.id},this)" title="Edit & resend">&#9998;</button>`
      : '';
    return `<div class="chat-msg ${m.role}" data-msg-id="${m.id}">
      <div class="chat-msg-body">${esc(m.content)}</div>
      <div class="chat-msg-actions">${editBtn}</div>
    </div>`;
  }).join('');
  c.scrollTop = c.scrollHeight;
}

function editMsg(msgId, btn) {
  var bubble = btn.closest('.chat-msg');
  if (bubble.classList.contains('chat-msg-editing')) return; // already editing
  var body = bubble.querySelector('.chat-msg-body');
  var currentText = body.textContent;
  // Build inline editor
  var ta = document.createElement('textarea');
  ta.className = 'chat-edit-inline';
  ta.value = currentText;
  var btns = document.createElement('div');
  btns.className = 'chat-edit-btns';
  btns.innerHTML = '<button class="chat-edit-save">Save & Resend</button><button class="chat-edit-cancel">Cancel</button>';
  bubble.classList.add('chat-msg-editing');
  bubble.appendChild(ta);
  bubble.appendChild(btns);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  async function doSave() {
    var newText = ta.value.trim();
    cleanup();
    if (!newText || newText === currentText.trim()) return;
    await post('/api/chat/sessions/'+curSess+'/edit', {from_message_id: msgId, message: newText});
    await loadSess(curSess);
    document.getElementById('chatIn').value = newText;
    sendMsg();
  }
  function cleanup() {
    bubble.classList.remove('chat-msg-editing');
    ta.remove();
    btns.remove();
  }
  btns.querySelector('.chat-edit-save').onclick = doSave;
  btns.querySelector('.chat-edit-cancel').onclick = cleanup;
  ta.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSave(); }
    if (e.key === 'Escape') { cleanup(); }
  });
}

async function sendMsg() {
  var input = document.getElementById('chatIn');
  var msg = input.value.trim();
  if(!msg) return;
  if(!curSess) { await newSession(); }
  if(!curSess){toast('Could not create session','error');return;}
  input.value=''; input.style.height='auto';
  var c = document.getElementById('chatMsgs');
  c.querySelector('.empty')?.remove();
  var ub = document.createElement('div'); ub.className='chat-msg user';
  ub.innerHTML='<div class="chat-msg-body">'+esc(msg)+'</div><div class="chat-msg-actions"></div>';
  c.appendChild(ub);
  var ab = document.createElement('div'); ab.className='chat-msg assistant thinking';
  ab.innerHTML='<div class="chat-msg-body">Thinking…</div>';
  c.appendChild(ab);
  c.scrollTop=c.scrollHeight;
  document.getElementById('sendBtn').style.display='none';
  document.getElementById('stopBtn').style.display='inline-flex';
  _chatAbortCtrl = new AbortController();
  try {
    var resp = await fetch(P+'/api/chat/sessions/'+curSess+'/send',{
      method:'POST', signal:_chatAbortCtrl.signal,
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,stream:true})
    });
    if(resp.headers.get('content-type')?.includes('text/event-stream')) {
      ab.querySelector('.chat-msg-body').textContent='';
      ab.classList.remove('thinking');
      var reader=resp.body.getReader(); var dec=new TextDecoder(); var buf='';
      while(true) {
        var {done,value}=await reader.read(); if(done) break;
        buf+=dec.decode(value,{stream:true});
        var lines=buf.split('\n'); buf=lines.pop();
        for(var l of lines) {
          if(!l.startsWith('data: ')) continue;
          var dstr=l.slice(6).trim(); if(dstr==='[DONE]') break;
          try{var o=JSON.parse(dstr);if(o.text)ab.querySelector('.chat-msg-body').textContent+=o.text;}catch{}
          c.scrollTop=c.scrollHeight;
        }
      }
    } else {
      var dj=await resp.json();
      ab.classList.remove('thinking');
      ab.querySelector('.chat-msg-body').textContent=dj.response||dj.error||'No response.';
    }
  } catch(e) {
    ab.classList.remove('thinking');
    if(e.name!=='AbortError') ab.querySelector('.chat-msg-body').textContent='Connection error: '+e.message;
    else ab.querySelector('.chat-msg-body').textContent+=' [stopped]';
  }
  finally {
    document.getElementById('sendBtn').style.display='inline-flex';
    document.getElementById('stopBtn').style.display='none';
    _chatAbortCtrl = null;
    // Reload messages to attach real IDs (needed for edit buttons)
    try {
      var r2 = await fetch(P+'/api/chat/sessions/'+curSess+'/messages');
      if (r2.ok) { var d2 = await r2.json(); _renderMessages(d2.messages||[], false); }
    } catch(_){}
    c.scrollTop=c.scrollHeight;
  }
}

async function stopChat() {
  if(_chatAbortCtrl) _chatAbortCtrl.abort();
  if(curSess) await post('/api/chat/sessions/'+curSess+'/stop',{}).catch(()=>{});
}

function chatKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}}
function resize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px';}

async function deleteSession() {
  if(!curSess||!confirm('Delete this conversation? This cannot be undone.')) return;
  await fetch(P+'/api/chat/sessions/'+curSess,{method:'DELETE'});
  curSess=null;
  document.getElementById('btnShare').style.display='none';
  document.getElementById('chatMsgs').innerHTML='<div class="empty">Select or start a conversation.</div>';
  document.getElementById('chatTitle').textContent='Select a conversation';
  document.getElementById('chatSharedTag').style.display='none';
  loadSessions();
}

async function openShareModal() {
  if(!curSess) return;
  document.getElementById('shareMsg').textContent='';
  document.getElementById('shareUsername').value='';
  document.getElementById('shareCanWrite').checked=false;
  // Load existing shares
  var d = await fetch(P+'/api/chat/sessions/'+curSess+'/messages').then(r=>r.json()).catch(()=>({}));
  var shares = d.shares||[];
  var listEl = document.getElementById('shareCurrentList');
  if(shares.length) {
    listEl.innerHTML='<strong>Currently shared with:</strong><br>'+shares.map(s=>
      `<span style="display:inline-flex;align-items:center;gap:6px;margin:2px 4px 2px 0;background:#f1f5f9;padding:2px 8px;border-radius:12px;font-size:.78rem">
        ${esc(s.username)} ${s.can_write?'(read+write)':'(read)'}
        <a href="#" onclick="unshare(${s.shared_with_user_id});return false" style="color:#e53e3e;font-size:.9rem">&times;</a>
      </span>`).join('');
  } else {
    listEl.innerHTML='<span style="color:var(--muted);font-size:.8rem">Not shared with anyone yet.</span>';
  }
  document.getElementById('mChatShare').classList.add('open');
}

async function unshare(userId) {
  if(!curSess) return;
  await fetch(P+'/api/chat/sessions/'+curSess+'/share/'+userId,{method:'DELETE'});
  openShareModal();
}

async function doShareChat() {
  var username = document.getElementById('shareUsername').value.trim();
  var canWrite = document.getElementById('shareCanWrite').checked;
  if(!username){document.getElementById('shareMsg').textContent='Enter a username.';return;}
  var r = await post('/api/chat/sessions/'+curSess+'/share',{username,can_write:canWrite});
  var msg = document.getElementById('shareMsg');
  if(r.status==='shared'){msg.style.color='green';msg.textContent='Shared with '+r.shared_with+'.';}
  else{msg.style.color='red';msg.textContent=r.error||'Failed.';}
  openShareModal();
}

