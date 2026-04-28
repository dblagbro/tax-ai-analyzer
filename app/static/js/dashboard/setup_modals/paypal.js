/* setup_modals/paypal.js — PayPal Setup Modal
 *
 * Extracted verbatim from setup_modals.js during Phase 11E refactor.
 * IIFE installs:
 *   openPaypalSetupModal, ppSendMessage, ppSendQuick, ppInputKeydown, ppResetChat
 */
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
