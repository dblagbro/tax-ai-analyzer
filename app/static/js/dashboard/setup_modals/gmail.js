/* setup_modals/gmail.js — Gmail Setup Modal
 *
 * Extracted verbatim from setup_modals.js during Phase 11E refactor.
 * IIFE installs the following globals consumed by HTML onclick handlers:
 *   openGmailSetupModal, gmSwitchMode, gmSendMessage, gmSendQuick,
 *   gmInputKeydown, gmHandleFile, gmSubmitCredentials, gmResetChat
 */
(function() {
  var GM = {
    isStreaming: false,
    chatHistory: [],
    selectedFile: null,
    initialized: false,

    SYSTEM_PROMPT: "You are a friendly, concise Gmail OAuth setup assistant for a self-hosted financial document analyzer app. " +
      "You help users configure Gmail integration by walking them through Google Cloud Console step by step, interactively. " +
      "Key facts: (1) Gmail OAuth works with personal @gmail.com accounts, not just Google Workspace. " +
      "(2) On the OAuth consent screen, select 'External' user type — required for personal accounts. " +
      "(3) Credential type MUST be 'Desktop app', NOT 'Web application'. " +
      "(4) App stays in 'Testing' mode permanently — fine, just add themselves as a test user. " +
      "(5) After uploading credentials.json, click 'Connect Gmail Account' to authorize. " +
      "(6) OAuth callback is handled server-side at /import/gmail/auth/callback. " +
      "Keep responses short and actionable (2-5 sentences per step). Ask what the user currently sees on screen. " +
      "If they say they already have credentials.json, tell them to switch to the Manual Upload tab.",

    escHtml: function(s) {
      return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    },

    scrollBottom: function() {
      var msgs = document.getElementById('gm-chat-msgs');
      if (!msgs) return;
      requestAnimationFrame(function() { msgs.scrollTop = msgs.scrollHeight; });
    },

    appendMessage: function(role, content) {
      var msgs = document.getElementById('gm-chat-msgs');
      var div = document.createElement('div');
      div.className = 'gm-chat-msg ' + role;
      var av = document.createElement('div');
      av.className = 'gm-msg-av';
      av.textContent = role === 'user' ? 'You' : 'AI';
      var bubble = document.createElement('div');
      bubble.className = 'gm-msg-bubble';
      bubble.innerHTML = GM.escHtml(content).replace(/\n/g,'<br>');
      div.appendChild(av);
      div.appendChild(bubble);
      msgs.appendChild(div);
      GM.scrollBottom();
      return div;
    },

    showTyping: function() {
      var msgs = document.getElementById('gm-chat-msgs');
      var div = document.createElement('div');
      div.className = 'gm-chat-msg assistant';
      div.id = 'gm-typing';
      div.innerHTML = '<div class="gm-msg-av">AI</div><div class="gm-msg-bubble"><div class="gm-typing-dots"><span></span><span></span><span></span></div></div>';
      msgs.appendChild(div);
      GM.scrollBottom();
    },

    removeTyping: function() {
      var el = document.getElementById('gm-typing');
      if (el) { el.remove(); GM.scrollBottom(); }
    },

    initChat: function() {
      var initEl = document.getElementById('gm-chat-init');
      if (initEl) initEl.remove();
      GM.appendMessage('assistant',
        "Hi! I'm your Gmail setup assistant. I'll guide you through connecting your Gmail account.\n\n" +
        "To start: do you already have a Google Cloud project, or are you starting from scratch?"
      );
    },

    switchMode: function(mode) {
      document.getElementById('gm-panel-chat').style.display = mode === 'chat' ? '' : 'none';
      document.getElementById('gm-panel-manual').style.display = mode === 'manual' ? '' : 'none';
      document.getElementById('gm-tab-chat').classList.toggle('active', mode === 'chat');
      document.getElementById('gm-tab-manual').classList.toggle('active', mode === 'manual');
    },

    loadStatus: async function() {
      try {
        var s = await fetch(P + '/api/import/gmail/status').then(r=>r.json()).catch(()=>({}));
        var bar = document.getElementById('gm-status-bar');
        var callbackNote = (!s.authenticated && s.callback_url)
          ? '<div class="gm-status-item" style="width:100%;margin-top:6px;padding:8px 10px;background:#fff8e1;border:1px solid #ffd54f;border-radius:6px;font-size:.8rem;color:#555">'
            + '<strong style="color:#e65100">&#9888; Redirect URI not configured in Google Cloud.</strong><br>'
            + 'In Google Cloud Console → Credentials → edit your OAuth client → add this Authorized Redirect URI:<br>'
            + '<code style="background:#f5f5f5;padding:2px 6px;border-radius:3px;user-select:all;font-size:.8rem">'
            + esc(s.callback_url) + '</code>'
            + '</div>'
          : '';
        bar.innerHTML =
          '<div class="gm-status-item"><div class="gm-dot '+(s.has_credentials?'green':'red')+'"></div>' +
            '<span><strong>credentials.json</strong> — '+(s.has_credentials?'Loaded':'Not configured')+'</span></div>' +
          '<div class="gm-status-item"><div class="gm-dot '+(s.authenticated?'green':'yellow')+'"></div>' +
            '<span><strong>OAuth Token</strong> — '+(s.authenticated?'Authorized':'Not authorized')+'</span></div>' +
          callbackNote +
          (s.has_credentials && s.authenticated
            ? '<div style="margin-left:auto"><a href="'+P+'/import/gmail/start" class="btn btn-success btn-sm">&#9654; Run Gmail Import</a></div>'
            : '');
        var ca = document.getElementById('gm-connect-area');
        if (ca) ca.style.display = s.has_credentials ? '' : 'none';
      } catch(e) {}
    },

    sendMessage: async function() {
      if (GM.isStreaming) return;
      var input = document.getElementById('gm-chat-input');
      var msg = input.value.trim();
      if (!msg) return;
      input.value = '';
      var lower = msg.toLowerCase();
      if ((lower.indexOf('have credentials') !== -1 || lower.indexOf('credentials.json') !== -1)
          && lower.indexOf('upload') === -1 && lower.indexOf('ready') !== -1) {
        GM.appendMessage('user', msg);
        GM.appendMessage('assistant', "Great! Switch to the Manual Upload tab above to upload your credentials.json file. Once uploaded, you'll see a 'Connect Gmail Account' button.");
        GM.switchMode('manual');
        return;
      }
      GM.appendMessage('user', msg);
      GM.chatHistory.push({role:'user', content:msg});
      GM.isStreaming = true;
      var sendBtn = document.getElementById('gm-send-btn');
      sendBtn.disabled = true;
      sendBtn.innerHTML = '<span class="loader white"></span>';
      GM.showTyping();
      var accumulated = '';
      var streamBubble = null;
      try {
        var res = await fetch(P + '/api/import/gmail/setup-chat', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({message:msg, history:GM.chatHistory})
        });
        if (!res.ok) throw new Error(await res.text() || res.statusText);
        GM.removeTyping();
        var msgs = document.getElementById('gm-chat-msgs');
        var wrapDiv = document.createElement('div');
        wrapDiv.className = 'gm-chat-msg assistant';
        var avEl = document.createElement('div');
        avEl.className = 'gm-msg-av';
        avEl.textContent = 'AI';
        streamBubble = document.createElement('div');
        streamBubble.className = 'gm-msg-bubble';
        wrapDiv.appendChild(avEl);
        wrapDiv.appendChild(streamBubble);
        msgs.appendChild(wrapDiv);
        GM.scrollBottom();
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buf = '';
        while (true) {
          var result = await reader.read();
          if (result.done) break;
          buf += decoder.decode(result.value, {stream:true});
          var lines = buf.split('\n');
          buf = lines.pop();
          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.startsWith('data: ')) {
              var data = line.slice(6).trim();
              if (data === '[DONE]') break;
              try {
                var parsed = JSON.parse(data);
                var text = parsed.text || parsed.content || parsed.delta || '';
                accumulated += text;
                streamBubble.innerHTML = GM.escHtml(accumulated).replace(/\n/g,'<br>');
              } catch(pe) {}
            }
          }
        }
        GM.scrollBottom();
        if (!accumulated) {
          if (streamBubble) streamBubble.innerHTML = '<em style="color:#aaa">No response received.</em>';
        } else {
          GM.chatHistory.push({role:'assistant', content:accumulated});
          var al = accumulated.toLowerCase();
          if (al.indexOf('upload') !== -1 && al.indexOf('credentials') !== -1 && streamBubble) {
            var hint = document.createElement('div');
            hint.style.marginTop = '8px';
            hint.innerHTML = '<button class="btn btn-outline btn-sm" onclick="gmSwitchMode(\'manual\')">&rarr; Go to Manual Upload</button>';
            streamBubble.appendChild(hint);
          }
        }
      } catch(e) {
        GM.removeTyping();
        GM.appendMessage('assistant', 'Could not reach the assistant (' + GM.escHtml(e.message) + '). Use the Manual Upload tab to upload credentials.json directly.');
      } finally {
        GM.isStreaming = false;
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send';
      }
    },

    handleFile: function(file) {
      if (!file) return;
      GM.selectedFile = file;
      var zone = document.getElementById('gm-drop-zone');
      zone.innerHTML = '<div style="font-size:1.5rem;margin-bottom:6px">&#9989;</div>' +
        '<p style="font-size:.88rem;color:#333;margin:0"><strong>' + GM.escHtml(file.name) + '</strong></p>' +
        '<p style="font-size:.78rem;color:#888;margin-top:4px">' + Math.round(file.size/1024) + ' KB &mdash; click to change</p>';
      zone.onclick = function() { document.getElementById('gm-file-input').click(); };
    },

    submitCredentials: async function() {
      var alertEl = document.getElementById('gm-upload-alert');
      var pasteContent = document.getElementById('gm-paste-json').value.trim();
      var btn = document.getElementById('gm-upload-btn');
      if (!GM.selectedFile && !pasteContent) {
        alertEl.innerHTML = '<div class="alert alert-danger" style="margin:0 0 12px">Please upload a file or paste JSON.</div>';
        return;
      }
      if (!GM.selectedFile && pasteContent) {
        try { JSON.parse(pasteContent); } catch(e) {
          alertEl.innerHTML = '<div class="alert alert-danger" style="margin:0 0 12px">Invalid JSON. Check the pasted content.</div>';
          return;
        }
      }
      btn.disabled = true;
      btn.innerHTML = '<span class="loader white"></span> Uploading…';
      alertEl.innerHTML = '';
      var fd = new FormData();
      if (GM.selectedFile) {
        fd.append('credentials', GM.selectedFile);
      } else {
        fd.append('credentials', new Blob([pasteContent],{type:'application/json'}), 'credentials.json');
      }
      try {
        var res = await fetch(P + '/api/import/gmail/credentials', {method:'POST', body:fd});
        var rawText = await res.text();
        console.log('[gmail-upload] status=' + res.status + ' body=' + rawText.slice(0,300));
        var data = {}; try { data = JSON.parse(rawText); } catch(je) {}
        if (res.ok && data.status === 'saved') {
          alertEl.innerHTML = '<div class="alert alert-success" style="margin:0 0 12px">credentials.json uploaded! Refreshing status…</div>';
          setTimeout(function() { GM.loadStatus(); alertEl.innerHTML = ''; }, 1800);
        } else {
          alertEl.innerHTML = '<div class="alert alert-danger" style="margin:0 0 12px">HTTP ' + res.status + ': ' + GM.escHtml(data.error||data.message||rawText.slice(0,200)||'Upload failed') + '</div>';
        }
      } catch(e) {
        alertEl.innerHTML = '<div class="alert alert-danger" style="margin:0 0 12px">Upload failed: ' + GM.escHtml(e.message) + '</div>';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Upload credentials.json';
      }
    }
  };

  // Wire up drag-and-drop (deferred until modal opens)
  window._gmDndInit = false;
  function gmDndInit() {
    if (window._gmDndInit) return;
    window._gmDndInit = true;
    var zone = document.getElementById('gm-drop-zone');
    if (!zone) return;
    zone.addEventListener('dragover', function(e){e.preventDefault();zone.classList.add('drag-over');});
    zone.addEventListener('dragleave', function(){zone.classList.remove('drag-over');});
    zone.addEventListener('drop', function(e){e.preventDefault();zone.classList.remove('drag-over');var f=e.dataTransfer.files[0];if(f)GM.handleFile(f);});
  }

  // Public interface
  window.openGmailSetupModal = function() {
    document.getElementById('mGmailSetup').classList.add('open');
    gmDndInit();
    GM.loadStatus();
    if (!GM.initialized) {
      GM.initialized = true;
      GM.initChat();
    }
  };
  window.gmSwitchMode = function(m) { GM.switchMode(m); };
  window.gmSendMessage = function() { GM.sendMessage(); };
  window.gmSendQuick = function(t) { document.getElementById('gm-chat-input').value=t; GM.sendMessage(); };
  window.gmInputKeydown = function(e) { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();GM.sendMessage();} };
  window.gmHandleFile = function(f) { GM.handleFile(f); };
  window.gmSubmitCredentials = function() { GM.submitCredentials(); };
  window.gmResetChat = function() {
    if(!confirm('Start a fresh setup conversation?'))return;
    GM.chatHistory=[];GM.initialized=false;
    document.getElementById('gm-chat-msgs').innerHTML='';
    GM.initChat();GM.initialized=true;
  };
})();
