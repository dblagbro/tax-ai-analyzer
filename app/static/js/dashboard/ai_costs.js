/* ai_costs.js — AI Costs tab: model usage stats, cost charts, recent calls */

async function loadAiCosts() {
  const days = document.getElementById('ac-days')?.value || 30;
  const data = await fetch(P+'/api/ai-costs?days='+days).then(r=>r.json()).catch(()=>({}));
  const s = data.stats || {};
  document.getElementById('ac-calls').textContent = (s.total_calls||0).toLocaleString();
  document.getElementById('ac-tokens').textContent = (s.total_tokens||0).toLocaleString();
  document.getElementById('ac-cost').textContent = '$'+(s.total_cost_usd||0).toFixed(4);
  document.getElementById('ac-success').textContent = ((s.success_rate||1)*100).toFixed(1)+'%';
  const bm = document.getElementById('ac-by-model');
  const byModel = s.by_model||{};
  bm.innerHTML = Object.keys(byModel).length ? Object.entries(byModel).sort((a,b)=>b[1].cost_usd-a[1].cost_usd).map(([m,v])=>
    `<tr><td>${esc(m)}<br><small style="color:var(--muted)">${esc(v.provider)}</small></td><td>${v.calls}</td><td>${((v.input_tokens||0)+(v.output_tokens||0)).toLocaleString()}</td><td>$${v.cost_usd.toFixed(5)}</td></tr>`
  ).join('') : '<tr><td colspan="4"><div class="empty">No data.</div></td></tr>';
  const bo = document.getElementById('ac-by-op');
  const byOp = s.by_operation||{};
  bo.innerHTML = Object.keys(byOp).length ? Object.entries(byOp).sort((a,b)=>b[1].cost_usd-a[1].cost_usd).map(([op,v])=>
    `<tr><td>${esc(op)}</td><td>${v.calls}</td><td>${(v.tokens||0).toLocaleString()}</td><td>$${v.cost_usd.toFixed(5)}</td></tr>`
  ).join('') : '<tr><td colspan="4"><div class="empty">No data.</div></td></tr>';
  const bd = document.getElementById('ac-by-day');
  const byDay = s.by_day||[];
  bd.innerHTML = byDay.length ? [...byDay].reverse().map(d=>
    `<tr><td>${esc(d.date)}</td><td>${d.calls}</td><td>${(d.tokens||0).toLocaleString()}</td><td>$${d.cost_usd.toFixed(5)}</td></tr>`
  ).join('') : '<tr><td colspan="4"><div class="empty">No data for this period.</div></td></tr>';
  const rc = await fetch(P+'/api/ai-costs/recent').then(r=>r.json()).catch(()=>({calls:[]}));
  const tbody = document.getElementById('ac-recent');
  tbody.innerHTML = (rc.calls||[]).length ? rc.calls.map(c=>
    `<tr><td style="font-size:.78rem">${(c.ts||'').slice(0,16).replace('T',' ')}</td><td>${esc(c.provider)}</td><td style="font-size:.78rem">${esc(c.model)}</td><td>${esc(c.operation)}</td><td>${(c.input_tokens||0).toLocaleString()}</td><td>${(c.output_tokens||0).toLocaleString()}</td><td>$${(c.cost_usd||0).toFixed(5)}</td><td>${c.success?'<span style="color:var(--income)">✓</span>':'<span style="color:var(--expense)">✗</span>'}</td></tr>`
  ).join('') : '<tr><td colspan="8"><div class="empty">No recent calls.</div></td></tr>';
}

