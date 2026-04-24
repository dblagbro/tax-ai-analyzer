/* setup_modals.js — Import Hub setup modals: Gmail, PayPal, and all bank importer IIFEs */

/* ============================================================
   Gmail Setup Modal
   ============================================================ */
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

/* ── PayPal Setup Modal ──────────────────────────────────────────────── */
(function() {
  var PP = {
    chatHistory: [],
    initialized: false,
    isStreaming: false,

    scrollBottom: function() {
      var msgs = document.getElementById('pp-chat-msgs');
      if (!msgs) return;
      requestAnimationFrame(function() { msgs.scrollTop = msgs.scrollHeight; });
    },

    appendMessage: function(role, content) {
      var msgs = document.getElementById('pp-chat-msgs');
      var div = document.createElement('div');
      div.className = 'gm-chat-msg ' + role;
      var av = document.createElement('div');
      av.className = 'gm-msg-av';
      av.textContent = role === 'user' ? 'You' : 'AI';
      var bubble = document.createElement('div');
      bubble.className = 'gm-msg-bubble';
      bubble.textContent = content;
      div.appendChild(av); div.appendChild(bubble);
      msgs.appendChild(div);
      PP.scrollBottom();
      return bubble;
    },

    showTyping: function() {
      var msgs = document.getElementById('pp-chat-msgs');
      var div = document.createElement('div');
      div.className = 'gm-chat-msg assistant';
      div.id = 'pp-typing';
      div.innerHTML = '<div class="gm-msg-av">AI</div><div class="gm-msg-bubble"><div class="gm-typing-dots"><span></span><span></span><span></span></div></div>';
      msgs.appendChild(div);
      PP.scrollBottom();
    },

    removeTyping: function() {
      var t = document.getElementById('pp-typing');
      if (t) t.remove();
    },

    initChat: function() {
      var initEl = document.getElementById('pp-chat-init');
      if (initEl) initEl.remove();
      PP.appendMessage('assistant',
        'Hi! I\'ll guide you through connecting your PayPal account. To start, do you have an existing PayPal Developer account, or are you setting one up for the first time?');
      PP.chatHistory = [];
      PP.initialized = true;
    },

    sendMessage: async function() {
      if (PP.isStreaming) return;
      var input = document.getElementById('pp-chat-input');
      var msg = input.value.trim();
      if (!msg) return;
      input.value = '';
      PP.appendMessage('user', msg);
      PP.showTyping();
      PP.isStreaming = true;
      document.getElementById('pp-send-btn').disabled = true;

      var accumulated = '';
      var streamBubble = null;
      try {
        var res = await fetch(P + '/api/import/paypal/setup-chat', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({message:msg, history:PP.chatHistory})
        });
        if (!res.ok) throw new Error(await res.text() || res.statusText);
        PP.removeTyping();
        var msgs = document.getElementById('pp-chat-msgs');
        var wrapDiv = document.createElement('div');
        wrapDiv.className = 'gm-chat-msg assistant';
        var avEl = document.createElement('div');
        avEl.className = 'gm-msg-av';
        avEl.textContent = 'AI';
        streamBubble = document.createElement('div');
        streamBubble.className = 'gm-msg-bubble';
        wrapDiv.appendChild(avEl); wrapDiv.appendChild(streamBubble);
        msgs.appendChild(wrapDiv);

        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buf = '';
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buf += decoder.decode(chunk.value, {stream:true});
          var lines = buf.split('\n');
          buf = lines.pop();
          for (var line of lines) {
            if (!line.startsWith('data: ')) continue;
            var payload = line.slice(6).trim();
            if (payload === '[DONE]') break;
            try {
              var parsed = JSON.parse(payload);
              if (parsed.text) {
                accumulated += parsed.text;
                streamBubble.textContent = accumulated;
                PP.scrollBottom();
              }
            } catch(e) {}
          }
        }
        PP.chatHistory.push({role:'user',content:msg});
        PP.chatHistory.push({role:'assistant',content:accumulated});

        // Offer fill-in shortcut if credentials mentioned
        if (/client.?id|client.?secret|credential/i.test(accumulated)) {
          var hint = document.createElement('div');
          hint.style.marginTop = '8px';
          hint.innerHTML = '<button class="btn btn-outline btn-sm" onclick="closeM(\'mPaypalSetup\')">&rarr; Fill in credentials below</button>';
          streamBubble.appendChild(hint);
        }
      } catch(e) {
        PP.removeTyping();
        PP.appendMessage('assistant', 'Error: ' + e.message);
      } finally {
        PP.isStreaming = false;
        document.getElementById('pp-send-btn').disabled = false;
        PP.scrollBottom();
      }
    },
  };

  // Public interface
  window.openPaypalSetupModal = function() {
    document.getElementById('mPaypalSetup').classList.add('open');
    if (!PP.initialized) { PP.initChat(); }
  };
  window.ppSendMessage = function() { PP.sendMessage(); };
  window.ppSendQuick = function(t) { document.getElementById('pp-chat-input').value=t; PP.sendMessage(); };
  window.ppInputKeydown = function(e) { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();PP.sendMessage();} };
  window.ppResetChat = function() {
    if(!confirm('Start a fresh setup conversation?'))return;
    PP.chatHistory=[];PP.initialized=false;
    document.getElementById('pp-chat-msgs').innerHTML='';
    PP.initChat();
  };
})();

/* ── US Alliance FCU importer ─────────────────────────────────────────── */
(function() {
  var _usaJobId = null;
  var _usaLogTimer = null;
  var _usaLogOffset = 0;

  window.usaCopySnippet = function() {
    var el = document.getElementById('usa-cookie-snippet');
    var btn = document.getElementById('usa-snippet-copy-btn');
    if (!el) return;
    var text = el.textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() {
        btn.textContent = '✓ Copied!';
        setTimeout(function(){ btn.innerHTML = '&#128203; Copy'; }, 2000);
      }).catch(function() {
        _usaFallbackCopy(text, btn);
      });
    } else {
      _usaFallbackCopy(text, btn);
    }
  };

  function _usaFallbackCopy(text, btn) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try {
      document.execCommand('copy');
      if (btn) { btn.textContent = '✓ Copied!'; setTimeout(function(){ btn.innerHTML = '&#128203; Copy'; }, 2000); }
    } catch(e) {
      alert('Copy failed — please select the snippet text manually.');
    }
    document.body.removeChild(ta);
  }

  window.saveUsaCreds = async function() {
    var u = document.getElementById('usa-username').value.trim();
    var p = document.getElementById('usa-password').value.trim();
    if (!u || !p) { alert('Enter both username and password.'); return; }
    var r = await post('/api/import/usalliance/credentials', {username: u, password: p});
    if (r.status === 'saved') {
      document.getElementById('usa-status-badge').innerHTML =
        '<span style="color:#4caf50">&#10003; Credentials saved</span>';
      document.getElementById('usa-password').value = '';
    } else {
      alert(r.error || 'Save failed.');
    }
  };

  window.loadUsaStatus = async function() {
    var r = await fetch(P + '/api/import/usalliance/status').then(r => r.json()).catch(() => ({}));
    var badge = document.getElementById('usa-status-badge');
    if (!badge) return;
    if (r.configured) {
      badge.innerHTML = '<span style="color:#4caf50">&#10003; Credentials configured (' + (r.username_preview||'') + ')</span>';
    } else {
      badge.innerHTML = '<span style="color:#f57c00">&#9888; No credentials saved — enter them below</span>';
    }
    // Cookie status
    var ckEl = document.getElementById('usa-cookie-status');
    if (ckEl) {
      if (r.cookies_saved) {
        ckEl.innerHTML = '<span style="color:#4caf50">&#10003; ' + r.cookies_count + ' cookies saved — cookie auth active</span>';
        // Show bot notice as info, not warning
        var notice = document.getElementById('usa-bot-notice');
        if (notice) notice.style.display = 'none';
      } else {
        ckEl.innerHTML = '<span style="color:var(--muted)">No cookies saved — using credential login</span>';
      }
    }
  };

  window.saveUsaCookies = async function() {
    var raw = document.getElementById('usa-cookies-input').value.trim();
    if (!raw) { alert('Paste your cookies JSON first.'); return; }
    var res = document.getElementById('usa-cookie-result');
    res.innerHTML = '<span style="color:var(--muted)">Saving…</span>';
    var r = await post('/api/import/usalliance/cookies', {cookies: raw});
    if (r.status === 'saved') {
      res.innerHTML = '<span style="color:#4caf50">&#10003; ' + esc(r.message) + '</span>';
      document.getElementById('usa-cookies-input').value = '';
      document.getElementById('usa-bot-notice').style.display = 'none';
      await loadUsaStatus();
    } else {
      res.innerHTML = '<span style="color:#d32f2f">&#10007; ' + esc(r.error || 'Failed to save cookies') + '</span>';
    }
  };

  window.clearUsaCookies = async function() {
    if (!confirm('Clear saved browser cookies?')) return;
    var r = await fetch(P + '/api/import/usalliance/cookies', {method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
    document.getElementById('usa-cookie-status').innerHTML = '<span style="color:var(--muted)">Cookies cleared</span>';
    document.getElementById('usa-cookie-result').innerHTML = '';
    await loadUsaStatus();
  };

  window.testUsaCreds = async function() {
    var u = document.getElementById('usa-username').value.trim();
    var p = document.getElementById('usa-password').value.trim();
    var btn = document.getElementById('usa-test-btn');
    var res = document.getElementById('usa-test-result');
    // Save first if fields are filled
    if (u && p) { await post('/api/import/usalliance/credentials', {username: u, password: p}); }
    btn.disabled = true; btn.textContent = 'Testing…';
    res.innerHTML = '<span style="color:var(--muted)">Attempting login…</span>';
    var r = await post('/api/import/usalliance/test', {}).catch(() => ({error: 'Request failed'}));
    btn.disabled = false; btn.innerHTML = '&#9654; Test Login';
    if (r && r.status === 'ok') {
      res.innerHTML = '<span style="color:var(--green)">&#10003; ' + (r.message || 'Login successful') + '</span>';
    } else {
      res.innerHTML = '<span style="color:var(--red)">&#10007; ' + esc(r && r.error || 'Login failed — check credentials') + '</span>';
    }
  };

  window.startUsaImport = async function() {
    var yearsRaw = document.getElementById('usa-years-input').value.trim();
    var years = yearsRaw.split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('usa-entity').value;
    if (!years.length) { alert('Enter at least one year (e.g. 2021,2022).'); return; }
    var r = await post('/api/import/usalliance/start', {entity_id: eid||null, years: years});
    if (r.error) { alert(r.error); return; }
    _usaJobId = r.job_id;
    _usaLogOffset = 0;
    document.getElementById('usa-log-panel').style.display = '';
    document.getElementById('usa-log-lines').innerHTML = '';
    document.getElementById('usa-log-status').textContent = 'running';
    document.getElementById('usa-start-btn').disabled = true;
    _pollUsaLogs();
  };

  function _pollUsaLogs() {
    if (!_usaJobId) return;
    fetch(P + '/api/import/jobs/' + _usaJobId + '/logs?offset=' + _usaLogOffset)
      .then(r => r.json()).then(data => {
        if (data.lines && data.lines.length) {
          var box = document.getElementById('usa-log-lines');
          data.lines.forEach(function(line) {
            var div = document.createElement('div');
            div.textContent = line;
            // Highlight MFA prompt
            if (line.toLowerCase().includes('mfa') || line.toLowerCase().includes('verification')) {
              div.style.color = '#ffc107';
              document.getElementById('usa-mfa-box').style.display = '';
            }
            // Highlight bot detection error
            if (line.toLowerCase().includes('bot detection') || line.toLowerCase().includes('unable to log')) {
              div.style.color = '#ff7043';
              var notice = document.getElementById('usa-bot-notice');
              if (notice) notice.style.display = '';
            }
            box.appendChild(div);
          });
          box.scrollTop = box.scrollHeight;
          _usaLogOffset = data.total;
        }
        // Check job status
        fetch(P + '/api/import/jobs/' + _usaJobId)
          .then(r => r.json()).then(function(job) {
            var done = job.status === 'completed' || job.status === 'error';
            document.getElementById('usa-log-status').textContent = job.status || 'running';
            if (done) {
              document.getElementById('usa-start-btn').disabled = false;
              document.getElementById('usa-mfa-box').style.display = 'none';
              clearTimeout(_usaLogTimer);
              _usaLogTimer = null;
            } else {
              _usaLogTimer = setTimeout(_pollUsaLogs, 2500);
            }
          }).catch(() => { _usaLogTimer = setTimeout(_pollUsaLogs, 5000); });
      }).catch(() => { _usaLogTimer = setTimeout(_pollUsaLogs, 5000); });
  }

  window.submitUsaMfa = async function() {
    var code = document.getElementById('usa-mfa-code').value.trim();
    if (!code || !_usaJobId) return;
    var r = await post('/api/import/usalliance/mfa', {job_id: _usaJobId, code: code});
    if (r.status === 'ok') {
      document.getElementById('usa-mfa-code').value = '';
      document.getElementById('usa-mfa-box').style.display = 'none';
    } else {
      alert(r.error || 'Failed to submit MFA code.');
    }
  };

  // Load status when tab is activated
  var _origImpTab = window.impTab;
  window.impTab = function(name, btn) {
    if (_origImpTab) _origImpTab(name, btn);
    if (name === 'usalliance') loadUsaStatus();
  };
})();

/* ── Capital One, US Bank, Merrick Bank, Chime, SimpleFIN importers ───── */
(function() {
  // Generic log poller factory — shared by all new importers
  function makePoller(prefix, mfaBoxId) {
    var jobId = null, offset = 0, timer = null;
    return {
      start: function(jid) {
        jobId = jid; offset = 0;
        document.getElementById(prefix+'-log-panel').style.display = '';
        document.getElementById(prefix+'-log-lines').innerHTML = '';
        document.getElementById(prefix+'-log-status').textContent = 'running';
        document.getElementById(prefix+'-start-btn').disabled = true;
        this.poll();
      },
      poll: function() {
        var self = this;
        if (!jobId) return;
        fetch(P + '/api/import/jobs/' + jobId + '/logs?offset=' + offset)
          .then(function(r){ return r.json(); })
          .then(function(data) {
            if (data.lines && data.lines.length) {
              var box = document.getElementById(prefix+'-log-lines');
              data.lines.forEach(function(line) {
                var div = document.createElement('div');
                div.textContent = line;
                if (line.toLowerCase().includes('mfa') || line.toLowerCase().includes('verification')) {
                  div.style.color = '#ffc107';
                  if (mfaBoxId) document.getElementById(mfaBoxId).style.display = '';
                }
                box.appendChild(div);
              });
              box.scrollTop = box.scrollHeight;
              offset = data.total;
            }
            fetch(P + '/api/import/jobs/' + jobId)
              .then(function(r){ return r.json(); })
              .then(function(job) {
                var done = job.status === 'completed' || job.status === 'error';
                document.getElementById(prefix+'-log-status').textContent = job.status || 'running';
                if (done) {
                  document.getElementById(prefix+'-start-btn').disabled = false;
                  if (mfaBoxId) document.getElementById(mfaBoxId).style.display = 'none';
                  loadJobs();
                } else {
                  timer = setTimeout(function(){ self.poll(); }, 2500);
                }
              }).catch(function(){ timer = setTimeout(function(){ self.poll(); }, 5000); });
          }).catch(function(){ timer = setTimeout(function(){ self.poll(); }, 5000); });
      },
      getJobId: function() { return jobId; }
    };
  }

  // Generic credential + cookie helpers
  function makeBankHelpers(bank, prefix, statusId, cookieStatusId, cookieResultId) {
    return {
      saveCreds: async function() {
        var u = document.getElementById(prefix+'-username').value.trim();
        var p = document.getElementById(prefix+'-password').value.trim();
        if (!u || !p) { alert('Enter both username and password.'); return; }
        var r = await post('/api/import/'+bank+'/credentials', {username:u, password:p});
        if (r.status === 'saved') {
          document.getElementById(statusId).innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved</span>';
          document.getElementById(prefix+'-password').value = '';
        } else { alert(r.error || 'Save failed.'); }
      },
      loadStatus: async function() {
        var r = await fetch(P+'/api/import/'+bank+'/status').then(function(r){return r.json();}).catch(function(){return {};});
        var badge = document.getElementById(statusId);
        if (!badge) return;
        badge.innerHTML = r.configured
          ? '<span style="color:#4caf50">&#10003; Credentials configured (' + (r.username_preview||'') + ')</span>'
          : '<span style="color:#f57c00">&#9888; No credentials saved</span>';
        var ck = document.getElementById(cookieStatusId);
        if (ck) {
          ck.innerHTML = r.cookies_saved
            ? '<span style="color:#4caf50">&#10003; '+r.cookies_count+' cookies saved — cookie auth active</span>'
            : '<span style="color:var(--muted)">No cookies — using credential login</span>';
        }
      },
      saveCookies: async function(inputId) {
        var raw = document.getElementById(inputId).value.trim();
        if (!raw) { alert('Paste your cookies JSON first.'); return; }
        var res = document.getElementById(cookieResultId);
        res.innerHTML = '<span style="color:var(--muted)">Saving…</span>';
        var r = await post('/api/import/'+bank+'/cookies', {cookies:raw});
        if (r.status === 'saved') {
          res.innerHTML = '<span style="color:#4caf50">&#10003; '+esc(r.message)+'</span>';
          document.getElementById(inputId).value = '';
          this.loadStatus();
        } else {
          res.innerHTML = '<span style="color:#d32f2f">&#10007; '+esc(r.error||'Failed')+'</span>';
        }
      },
      clearCookies: async function() {
        if (!confirm('Clear saved cookies?')) return;
        await fetch(P+'/api/import/'+bank+'/cookies', {method:'DELETE'}).then(function(r){return r.json();}).catch(function(){});
        document.getElementById(cookieStatusId).innerHTML = '<span style="color:var(--muted)">Cookies cleared</span>';
        document.getElementById(cookieResultId).innerHTML = '';
        this.loadStatus();
      }
    };
  }

  // ── Capital One ──
  var _coPoller = makePoller('co', 'co-mfa-box');
  var _coHelper = makeBankHelpers('capitalone', 'co', 'co-status-badge', 'co-cookie-status', 'co-cookie-result');

  window.saveCoOneCreds = function() { _coHelper.saveCreds(); };
  window.loadCoStatus   = function() { _coHelper.loadStatus(); };
  window.saveCoCookies  = function() { _coHelper.saveCookies('co-cookies-input'); };
  window.clearCoCookies = function() { _coHelper.clearCookies(); };

  window.startCoOneImport = async function() {
    var yrs = (document.getElementById('co-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('co-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/capitalone/start', {entity_id:eid||null, years:yrs});
    if (r.error) { alert(r.error); return; }
    _coPoller.start(r.job_id);
    toast('Capital One import started. Job #'+r.job_id, 'success');
  };

  window.submitCoMfa = async function() {
    var code = document.getElementById('co-mfa-code').value.trim();
    var jid  = _coPoller.getJobId();
    if (!code || !jid) return;
    var r = await post('/api/import/capitalone/mfa', {job_id:jid, code:code});
    if (r.status === 'ok') {
      document.getElementById('co-mfa-code').value = '';
      document.getElementById('co-mfa-box').style.display = 'none';
    } else { alert(r.error || 'Failed to submit MFA.'); }
  };

  // ── US Bank ──
  var _usbPoller = makePoller('usb', 'usb-mfa-box');
  var _usbHelper = makeBankHelpers('usbank', 'usb', 'usb-status-badge', 'usb-cookie-status', 'usb-cookie-result');

  window.saveUsbCreds   = function() { _usbHelper.saveCreds(); };
  window.loadUsbStatus  = function() { _usbHelper.loadStatus(); };
  window.saveUsbCookies = function() { _usbHelper.saveCookies('usb-cookies-input'); };
  window.clearUsbCookies= function() { _usbHelper.clearCookies(); };

  window.startUsbImport = async function() {
    var yrs = (document.getElementById('usb-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('usb-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/usbank/start', {entity_id:eid||null, years:yrs});
    if (r.error) { alert(r.error); return; }
    _usbPoller.start(r.job_id);
    toast('US Bank import started. Job #'+r.job_id, 'success');
  };

  window.submitUsbMfa = async function() {
    var code = document.getElementById('usb-mfa-code').value.trim();
    var jid  = _usbPoller.getJobId();
    if (!code || !jid) return;
    var r = await post('/api/import/usbank/mfa', {job_id:jid, code:code});
    if (r.status === 'ok') {
      document.getElementById('usb-mfa-code').value = '';
      document.getElementById('usb-mfa-box').style.display = 'none';
    } else { alert(r.error || 'Failed to submit MFA.'); }
  };

  // ── Merrick Bank ──
  var _mrkPoller = makePoller('mrk', 'mrk-mfa-box');
  var _mrkHelper = makeBankHelpers('merrick', 'mrk', 'mrk-status-badge', 'mrk-cookie-status', 'mrk-cookie-result');

  window.saveMrkCreds   = function() { _mrkHelper.saveCreds(); };
  window.loadMrkStatus  = function() { _mrkHelper.loadStatus(); };
  window.saveMrkCookies = function() { _mrkHelper.saveCookies('mrk-cookies-input'); };
  window.clearMrkCookies= function() { _mrkHelper.clearCookies(); };

  window.startMrkImport = async function() {
    var yrs = (document.getElementById('mrk-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('mrk-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/merrick/start', {entity_id:eid||null, years:yrs});
    if (r.error) { alert(r.error); return; }
    _mrkPoller.start(r.job_id);
    toast('Merrick Bank import started. Job #'+r.job_id, 'success');
  };

  window.submitMrkMfa = async function() {
    var code = document.getElementById('mrk-mfa-code').value.trim();
    var jid  = _mrkPoller.getJobId();
    if (!code || !jid) return;
    var r = await post('/api/import/merrick/mfa', {job_id:jid, code:code});
    if (r.status === 'ok') {
      document.getElementById('mrk-mfa-code').value = '';
      document.getElementById('mrk-mfa-box').style.display = 'none';
    } else { alert(r.error || 'Failed to submit MFA.'); }
  };

  // ── Chime Playwright importer ──
  var _chmPoller = makePoller('chm', 'chm-mfa-box');
  var _chmJobId = null;

  window.chmSaveCreds = async function() {
    var em = (document.getElementById('chm-email')||{}).value || '';
    var pw = (document.getElementById('chm-password')||{}).value || '';
    em = em.trim(); pw = pw.trim();
    if (!em || !pw) { alert('Enter both email and password.'); return; }
    var r = await post('/api/import/chime/credentials', {email:em, password:pw});
    if (r.status === 'saved') {
      document.getElementById('chm-status').innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved</span>';
      document.getElementById('chm-password').value = '';
    } else { alert(r.error || 'Save failed.'); }
  };
  window.chmSaveCookies = async function() {
    var raw = (document.getElementById('chm-cookies')||{}).value || '';
    raw = raw.trim();
    if (!raw) { alert('Paste cookies JSON first.'); return; }
    var r = await post('/api/import/chime/cookies', {cookies:raw});
    if (r.status === 'saved') {
      toast(r.message, 'success');
      document.getElementById('chm-cookies').value = '';
      loadChimeStatus();
    } else { alert(r.error || 'Failed.'); }
  };
  window.chmClearCookies = async function() {
    if (!confirm('Clear saved Chime cookies?')) return;
    await fetch(P+'/api/import/chime/cookies', {method:'DELETE'}).then(function(r){return r.json();}).catch(function(){});
    loadChimeStatus();
  };

  window.loadChimeStatus = async function() {
    var r = await fetch(P+'/api/import/chime/status').then(function(r){return r.json();}).catch(function(){return {};});
    var el = document.getElementById('chm-status');
    if (!el) return;
    if (r.configured) {
      el.innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved ('+esc(r.email_preview)+')'
        + (r.cookies_saved ? ' + '+r.cookies_count+' cookies' : '') + '</span>';
    } else {
      el.innerHTML = '<span style="color:#f57c00">&#9888; Enter Chime credentials below</span>';
    }
  };

  window.chmSubmitMfa = async function() {
    var code = document.getElementById('chm-mfa-code').value.trim();
    if (!code || !_chmJobId) return;
    await post('/api/import/chime/mfa', {job_id: _chmJobId, code: code});
    document.getElementById('chm-mfa-code').value = '';
    toast('MFA code submitted', 'success');
  };

  window.startChime = async function() {
    var yrs = (document.getElementById('chm-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('chm-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    document.getElementById('chm-start-btn').disabled = true;
    var r = await post('/api/import/chime/start', {entity_id:eid||null, years:yrs});
    document.getElementById('chm-start-btn').disabled = false;
    if (r.error) { alert(r.error); return; }
    _chmJobId = r.job_id;
    _chmPoller.start(r.job_id);
    toast('Chime import started. Job #'+r.job_id, 'success');
  };

  // ── Verizon Playwright importer ──
  var _vznPoller = makePoller('vzn', 'vzn-mfa-box');
  var _vznJobId = null;

  window.vznSaveCreds = async function() {
    var u = (document.getElementById('vzn-username')||{}).value || '';
    var pw = (document.getElementById('vzn-password')||{}).value || '';
    u = u.trim(); pw = pw.trim();
    if (!u || !pw) { alert('Enter both username and password.'); return; }
    var r = await post('/api/import/verizon/credentials', {username:u, password:pw});
    if (r.status === 'saved') {
      document.getElementById('vzn-status').innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved</span>';
      document.getElementById('vzn-password').value = '';
    } else { alert(r.error || 'Save failed.'); }
  };
  window.vznSaveCookies = async function() {
    var raw = (document.getElementById('vzn-cookies')||{}).value || '';
    raw = raw.trim();
    if (!raw) { alert('Paste cookies JSON first.'); return; }
    var r = await post('/api/import/verizon/cookies', {cookies:raw});
    if (r.status === 'saved') {
      toast(r.message, 'success');
      document.getElementById('vzn-cookies').value = '';
      loadVznStatus();
    } else { alert(r.error || 'Failed.'); }
  };
  window.vznClearCookies = async function() {
    if (!confirm('Clear saved Verizon cookies?')) return;
    await fetch(P+'/api/import/verizon/cookies', {method:'DELETE'}).then(function(r){return r.json();}).catch(function(){});
    loadVznStatus();
  };

  window.loadVznStatus = async function() {
    var r = await fetch(P+'/api/import/verizon/status').then(function(r){return r.json();}).catch(function(){return {};});
    var el = document.getElementById('vzn-status');
    if (!el) return;
    if (r.configured) {
      el.innerHTML = '<span style="color:#4caf50">&#10003; Credentials saved ('+esc(r.username_preview)+')'
        + (r.cookies_saved ? ' + '+r.cookies_count+' cookies' : '') + '</span>';
    } else {
      el.innerHTML = '<span style="color:#f57c00">&#9888; Enter My Verizon credentials below</span>';
    }
  };

  window.vznSubmitMfa = async function() {
    var code = document.getElementById('vzn-mfa-code').value.trim();
    if (!code || !_vznJobId) return;
    await post('/api/import/verizon/mfa', {job_id: _vznJobId, code: code});
    document.getElementById('vzn-mfa-code').value = '';
    toast('MFA submitted', 'success');
  };

  window.startVerizon = async function() {
    var yrs = (document.getElementById('vzn-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('vzn-entity').value;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    document.getElementById('vzn-start-btn').disabled = true;
    var r = await post('/api/import/verizon/start', {entity_id:eid||null, years:yrs});
    document.getElementById('vzn-start-btn').disabled = false;
    if (r.error) { alert(r.error); return; }
    _vznJobId = r.job_id;
    _vznPoller.start(r.job_id);
    toast('Verizon import started. Job #'+r.job_id, 'success');
  };

  // ── SimpleFIN Bridge ──
  var _sfinPoller = makePoller('sfin', null);

  window.loadSfinStatus = async function() {
    var r = await fetch(P+'/api/import/simplefin/status').then(function(r){return r.json();}).catch(function(){return {};});
    var badge = document.getElementById('sfin-status-badge');
    if (!badge) return;
    badge.innerHTML = r.connected
      ? '<span style="color:#4caf50">&#10003; Connected — '+esc(r.preview||'SimpleFIN Bridge')+'</span>'
      : '<span style="color:#f57c00">&#9888; Not connected — claim a token below</span>';
    document.getElementById('sfin-start-btn').disabled = !r.connected;
  };

  window.claimSimpleFin = async function() {
    var token = document.getElementById('sfin-token').value.trim();
    if (!token) { alert('Paste your SimpleFIN setup URL or token first.'); return; }
    var res = document.getElementById('sfin-claim-result');
    res.innerHTML = '<span style="color:var(--muted)">Claiming token…</span>';
    var r = await post('/api/import/simplefin/claim', {setup_url: token});
    if (r.status === 'connected') {
      res.innerHTML = '<span style="color:#4caf50">&#10003; '+esc(r.message)+'</span>';
      document.getElementById('sfin-token').value = '';
      loadSfinStatus();
    } else {
      res.innerHTML = '<span style="color:#d32f2f">&#10007; '+esc(r.error||'Claim failed')+'</span>';
    }
  };

  window.startSfinImport = async function() {
    var yrs = (document.getElementById('sfin-years-input').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('sfin-entity').value;
    var filterRaw = (document.getElementById('sfin-account-filter').value||'').trim();
    var acctFilter = filterRaw ? filterRaw.split(/[\s,]+/).filter(Boolean) : null;
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    var r = await post('/api/import/simplefin/start', {entity_id:eid||null, years:yrs, account_filter:acctFilter});
    if (r.error) { alert(r.error); return; }
    _sfinPoller.start(r.job_id);
    toast('SimpleFIN pull started. Job #'+r.job_id, 'success');
  };

  // ── IMAP ──
  var _imapPoller = makePoller('imap', null);
  var _imapProviders = {};

  window.loadImapStatus = async function() {
    var r = await fetch(P+'/api/import/imap/status').then(function(r){return r.json();}).catch(function(){return {};});
    var badge = document.getElementById('imap-status-badge');
    if (!badge) return;
    if (r.configured) {
      badge.innerHTML = '<span style="color:#4caf50">&#10003; Configured — '+esc(r.username||'')+' @ '+esc(r.host||'')+'</span>';
    } else if (r.host || r.username) {
      badge.innerHTML = '<span style="color:#f57c00">&#9888; Partial — save password to enable imports</span>';
    } else {
      badge.innerHTML = '<span style="color:var(--muted)">Not configured</span>';
    }
    // Pre-populate form
    if (r.provider) document.getElementById('imap-provider').value = r.provider;
    if (r.host)     document.getElementById('imap-host').value = r.host;
    if (r.port)     document.getElementById('imap-port').value = r.port;
    if (r.username) document.getElementById('imap-username').value = r.username;
    if (r.folder)   document.getElementById('imap-folder').value = r.folder;
    document.getElementById('imap-use-ssl').checked = (r.use_ssl !== false);
    // Load providers for preset dropdown
    if (!Object.keys(_imapProviders).length) {
      try {
        var pr = await fetch(P+'/api/import/imap/providers').then(function(r){return r.json();});
        _imapProviders = pr.providers || {};
      } catch(e) {}
    }
  };

  window.imapApplyPreset = function() {
    var prov = document.getElementById('imap-provider').value;
    var preset = _imapProviders[prov];
    if (preset && preset.host) {
      document.getElementById('imap-host').value = preset.host;
      document.getElementById('imap-port').value = preset.port;
    }
  };

  window.imapSaveSettings = async function() {
    var payload = {
      provider: document.getElementById('imap-provider').value,
      host:     document.getElementById('imap-host').value.trim(),
      port:     parseInt(document.getElementById('imap-port').value) || 993,
      username: document.getElementById('imap-username').value.trim(),
      password: document.getElementById('imap-password').value,  // may be blank → keep existing
      folder:   document.getElementById('imap-folder').value.trim() || 'INBOX',
      use_ssl:  document.getElementById('imap-use-ssl').checked,
    };
    if (!payload.host || !payload.username) { alert('Host and username required.'); return; }
    var r = await post('/api/import/imap/settings', payload);
    if (r.status === 'saved') {
      toast('IMAP settings saved' + (r.password_updated ? ' (password updated)' : ''), 'success');
      document.getElementById('imap-password').value = '';
      loadImapStatus();
    } else {
      alert(r.error || 'Save failed');
    }
  };

  window.imapTestConnection = async function() {
    var el = document.getElementById('imap-test-result');
    el.innerHTML = '<span style="color:var(--muted)">Testing…</span>';
    var r = await post('/api/import/imap/test', {});
    if (r.ok) {
      var folders = (r.folders || []).slice(0, 8).join(', ');
      el.innerHTML = '<span style="color:#4caf50">&#10003; Connected</span>' +
        (folders ? '<br><span style="font-size:.74rem;color:var(--muted)">folders: '+esc(folders)+(r.folders.length>8?'…':'')+'</span>' : '');
    } else {
      el.innerHTML = '<span style="color:#d32f2f">&#10007; ' + esc(r.error || 'failed') + '</span>';
    }
  };

  window.startImapImport = async function() {
    var yrs = (document.getElementById('imap-years').value||'').split(/[\s,]+/).filter(Boolean);
    var eid = document.getElementById('imap-entity').value;
    var terms = document.getElementById('imap-search-terms').value.trim();
    if (!yrs.length) { alert('Enter at least one year.'); return; }
    document.getElementById('imap-start-btn').disabled = true;
    var r = await post('/api/import/imap/start', {
      entity_id: eid || null,
      years: yrs,
      search_terms: terms || null,
    });
    document.getElementById('imap-start-btn').disabled = false;
    if (r.error) { alert(r.error); return; }
    _imapPoller.start(r.job_id);
    toast('IMAP import started. Job #'+r.job_id, 'success');
  };

  // ── Plaid ──
  var _plaidPoller = makePoller('plaid', null);

  window.loadPlaidStatus = async function() {
    var r = await fetch(P+'/api/import/plaid/status').then(function(r){return r.json();}).catch(function(){return {};});
    var badge = document.getElementById('plaid-status-badge');
    var list = document.getElementById('plaid-items-list');
    if (!badge) return;
    if (r.configured) {
      badge.innerHTML = '<span style="color:#4caf50">&#10003; Configured ('+esc(r.env||'sandbox')+') · '+(r.item_count||0)+' bank'+(r.item_count===1?'':'s')+' connected</span>';
      var envSel = document.getElementById('plaid-env'); if (envSel && r.env) envSel.value = r.env;
    } else {
      badge.innerHTML = '<span style="color:#f57c00">&#9888; Not configured — enter Plaid client_id + secret below</span>';
    }
    document.getElementById('plaid-connect-btn').disabled = !r.configured;
    document.getElementById('plaid-sync-btn').disabled = !r.configured || !(r.items && r.items.length);
    if (list) {
      if (r.items && r.items.length) {
        list.innerHTML = r.items.map(function(it){
          var last = it.last_sync ? new Date(it.last_sync).toLocaleString() : 'never';
          return '<div style="border:1px solid #e0e4ea;border-radius:6px;padding:8px 12px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">'
            + '<div><strong>'+esc(it.institution_name||it.item_id)+'</strong>'
            + '<div style="font-size:.74rem;color:var(--muted)">last sync: '+esc(last)+' · status: '+esc(it.status)+'</div></div>'
            + '<div style="display:flex;gap:6px">'
            + '<button class="btn btn-sm btn-primary" onclick="syncOnePlaid(\''+esc(it.item_id)+'\')">Sync</button>'
            + '<button class="btn btn-sm btn-outline" onclick="removePlaidItem(\''+esc(it.item_id)+'\')">&#128465;</button>'
            + '</div></div>';
        }).join('');
      } else {
        list.innerHTML = '<span style="color:var(--muted)">No banks connected yet.</span>';
      }
    }
  };

  window.savePlaidSettings = async function() {
    var ci = (document.getElementById('plaid-client-id')||{}).value || '';
    var sec = (document.getElementById('plaid-secret')||{}).value || '';
    var env = (document.getElementById('plaid-env')||{}).value || 'sandbox';
    if (!ci.trim() || !sec.trim()) { alert('Enter both client_id and secret.'); return; }
    var r = await post('/api/import/plaid/settings', {client_id:ci.trim(), secret:sec.trim(), env:env});
    if (r.status === 'saved') {
      document.getElementById('plaid-secret').value = '';
      toast('Plaid credentials saved ('+esc(env)+')', 'success');
      loadPlaidStatus();
    } else {
      alert(r.error || 'Save failed');
    }
  };

  window.openPlaidLink = async function() {
    if (typeof Plaid === 'undefined') { alert('Plaid Link SDK not loaded. Reload the page.'); return; }
    var msg = document.getElementById('plaid-connect-msg');
    msg.textContent = 'Creating link token…';
    var tok = await post('/api/import/plaid/link-token', {});
    if (!tok.link_token) { msg.textContent = 'Error: '+(tok.error||'failed to create link token'); return; }
    var entityId = document.getElementById('plaid-entity').value || null;
    msg.textContent = 'Opening Plaid Link…';
    var handler = Plaid.create({
      token: tok.link_token,
      onSuccess: async function(public_token, metadata) {
        msg.textContent = 'Exchanging token…';
        var r = await post('/api/import/plaid/exchange', {
          public_token: public_token,
          institution_id: (metadata.institution||{}).institution_id || null,
          institution_name: (metadata.institution||{}).name || null,
          entity_id: entityId ? parseInt(entityId) : null,
        });
        if (r.status === 'ok') {
          msg.innerHTML = '<span style="color:#28a745">&#10003; Connected '+esc((r.item||{}).institution_name||'bank')+'</span>';
          toast('Bank connected', 'success');
          loadPlaidStatus();
        } else {
          msg.innerHTML = '<span style="color:#d32f2f">Error: '+esc(r.error||'exchange failed')+'</span>';
        }
      },
      onExit: function(err, metadata) {
        msg.textContent = err ? 'Plaid Link cancelled: '+(err.error_message||err.error_code||'') : '';
      },
    });
    handler.open();
  };

  window.syncAllPlaid = async function() {
    var entityId = document.getElementById('plaid-entity').value || null;
    var r = await post('/api/import/plaid/start', {entity_id: entityId ? parseInt(entityId) : null});
    if (r.status === 'started') {
      _plaidPoller.start(r.job_id);
      toast('Plaid sync started (job #'+r.job_id+')', 'success');
    } else {
      alert(r.error || 'Failed to start sync');
    }
  };

  window.syncOnePlaid = async function(itemId) {
    var r = await post('/api/import/plaid/start', {item_id: itemId});
    if (r.status === 'started') {
      _plaidPoller.start(r.job_id);
      toast('Syncing item (job #'+r.job_id+')', 'success');
    } else {
      alert(r.error || 'Sync failed');
    }
  };

  window.removePlaidItem = async function(itemId) {
    if (!confirm('Disconnect this bank? Transactions already imported will be kept.')) return;
    var r = await fetch(P+'/api/import/plaid/items/'+encodeURIComponent(itemId), {method:'DELETE'})
              .then(function(r){return r.json();}).catch(function(){return {error:'request failed'};});
    if (r.status === 'removed') {
      toast('Disconnected', 'success');
      loadPlaidStatus();
    } else {
      alert(r.error || 'Failed to disconnect');
    }
  };

  // ── Status loading on tab switch ──
  var _origImpTab2 = window.impTab;
  window.impTab = function(name, btn) {
    if (_origImpTab2) _origImpTab2(name, btn);
    if (name === 'capitalone') loadCoStatus();
    if (name === 'usbank')     loadUsbStatus();
    if (name === 'merrick')    loadMrkStatus();
    if (name === 'chime')      loadChimeStatus();
    if (name === 'verizon')    loadVznStatus();
    if (name === 'simplefin')  loadSfinStatus();
    if (name === 'plaid')      loadPlaidStatus();
    if (name === 'imap')       loadImapStatus();
  };

  // Also load status on page init for Import tab
  var _origInitImport = window._initTabFns && window._initTabFns.import;
  document.addEventListener('DOMContentLoaded', function() {
    // If the import tab is initially active, pre-load statuses
    if (document.getElementById('ip-capitalone')) loadCoStatus();
    if (document.getElementById('sfin-status-badge')) loadSfinStatus();
    if (document.getElementById('chm-status')) loadChimeStatus();
    if (document.getElementById('vzn-status')) loadVznStatus();
    if (document.getElementById('plaid-status-badge')) loadPlaidStatus();
  });
})();
