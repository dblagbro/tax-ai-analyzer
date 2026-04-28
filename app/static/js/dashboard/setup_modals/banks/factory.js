/* setup_modals/banks/factory.js — shared bank importer factories
 *
 * Phase 11E refactor: extracted from the combined banks IIFE in
 * setup_modals.js. Installs `window.__bankFactory` early in script load
 * order so each per-bank IIFE (capitalone.js, usbank.js, ...) can grab
 * makePoller/makeBankHelpers synchronously without a setTimeout polling
 * dance. usalliance.js was written before the factory existed and still
 * uses setTimeout fallback — left as-is.
 *
 * Provides:
 *   window.__bankFactory.makePoller(prefix, mfaBoxId)   → log-poller object
 *   window.__bankFactory.makeBankHelpers(bank, prefix,
 *       statusId, cookieStatusId, cookieResultId)        → cred/cookie helpers
 */
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

  window.__bankFactory = {
    makePoller: makePoller,
    makeBankHelpers: makeBankHelpers,
  };
})();
