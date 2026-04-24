/* mileage.js — Mileage tab: list, add/edit form, CSV export */

/* ───── Mileage log ───── */
async function loadMileage() {
  const p = new URLSearchParams({limit:1000});
  const e = document.getElementById('mf-entity')?.value; if(e) p.set('entity_id',e);
  const y = document.getElementById('mf-year')?.value;   if(y) p.set('year',y);
  if (document.getElementById('mf-business-only')?.checked) p.set('business_only','1');
  const d = await fetch(P+'/api/mileage?'+p).then(r=>r.json()).catch(()=>({entries:[],summary:{}}));

  // Summary cards
  const s = d.summary || {};
  const summaryEl = document.getElementById('mil-summary');
  if (summaryEl) {
    const total = (s.total_miles || 0).toFixed(1);
    const biz   = (s.business_miles || 0).toFixed(1);
    const pers  = (s.personal_miles || 0).toFixed(1);
    const ded   = (s.deduction_amount || 0).toFixed(2);
    const rate  = s.rate ? `$${s.rate.toFixed(3)}/mi` : 'IRS default';
    const yr    = document.getElementById('mf-year')?.value || 'all years';
    const cs = 'background:#fff;border:1px solid #e0e4ea;border-radius:10px;padding:14px 18px;text-align:center';
    const ls = 'font-size:.72rem;color:#888;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em';
    const vs = 'font-size:1.4rem;font-weight:700';
    summaryEl.innerHTML = `
      <div style="${cs}"><div style="${ls}">Business Miles</div><div style="${vs};color:#28a745">${biz}</div></div>
      <div style="${cs}"><div style="${ls}">Personal Miles</div><div style="${vs};color:#6c757d">${pers}</div></div>
      <div style="${cs}"><div style="${ls}">Total Miles</div><div style="${vs}">${total}</div></div>
      <div style="${cs}"><div style="${ls}">Deduction (${esc(yr)})</div><div style="${vs};color:#6f42c1">$${ded}</div></div>
      <div style="${cs}"><div style="${ls}">Rate</div><div style="${vs};font-size:1rem;color:var(--muted)">${esc(rate)}</div></div>
      <div style="${cs}"><div style="${ls}">Entries</div><div style="${vs}">${s.count || 0}</div></div>
    `;
  }

  // Row list
  const tbody = document.getElementById('mileage-body');
  if (!tbody) return;
  const rows = d.entries || [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:20px;color:var(--muted)">No mileage logged yet. Click "+ Add Entry" to start.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const miles = parseFloat(r.miles || 0);
    const rate  = parseFloat(r.rate_per_mile || 0);
    const ded   = r.business ? (miles * rate).toFixed(2) : '—';
    const route = (r.from_location || r.to_location)
      ? `${esc(r.from_location || '?')} → ${esc(r.to_location || '?')}` : '';
    return `<tr>
      <td style="white-space:nowrap">${esc((r.date||'').slice(0,10))}</td>
      <td style="text-align:right">${miles.toFixed(1)}</td>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.purpose||'')}">${esc(r.purpose||'')}</td>
      <td style="font-size:.78rem;color:var(--muted)">${route}</td>
      <td>${esc(r.vehicle||'')}</td>
      <td>${r.business ? '<span class="badge badge-income">business</span>' : '<span class="badge badge-other">personal</span>'}</td>
      <td style="text-align:right;font-size:.78rem">$${rate.toFixed(3)}</td>
      <td style="text-align:right;font-weight:600;color:${r.business?'#6f42c1':'#aaa'}">${r.business?'$'+ded:'—'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-sm btn-outline" onclick="editMileage(${r.id})">Edit</button>
        <button class="btn btn-sm btn-outline" onclick="deleteMileageEntry(${r.id})" style="color:#c0392b;border-color:#e6a8a1">&#128465;</button>
      </td>
    </tr>`;
  }).join('');
}

function openMileageForm(entry) {
  document.getElementById('mil-id').value = entry?.id || '';
  document.getElementById('mil-form-title').textContent = entry ? 'Edit Mileage Entry' : 'Add Mileage Entry';
  document.getElementById('mil-date').value = entry?.date?.slice(0,10) || new Date().toISOString().slice(0,10);
  document.getElementById('mil-miles').value = entry?.miles ?? '';
  document.getElementById('mil-entity').value = entry?.entity_id ?? '';
  document.getElementById('mil-vehicle').value = entry?.vehicle || '';
  document.getElementById('mil-purpose').value = entry?.purpose || '';
  document.getElementById('mil-from').value = entry?.from_location || '';
  document.getElementById('mil-to').value = entry?.to_location || '';
  document.getElementById('mil-odo-start').value = entry?.odometer_start ?? '';
  document.getElementById('mil-odo-end').value = entry?.odometer_end ?? '';
  document.getElementById('mil-rate').value = entry?.rate_per_mile ?? '';
  document.getElementById('mil-business').checked = entry ? !!entry.business : true;
  document.getElementById('mil-notes').value = entry?.notes || '';
  const modal = document.getElementById('mileage-modal');
  modal.style.display = 'flex';
}

function closeMileageForm() {
  document.getElementById('mileage-modal').style.display = 'none';
}

async function editMileage(id) {
  const r = await fetch(P+'/api/mileage/'+id).then(r=>r.json()).catch(()=>null);
  if (!r || r.error) { alert('Could not load entry'); return; }
  openMileageForm(r);
}

async function saveMileageEntry() {
  const id = document.getElementById('mil-id').value;
  const payload = {
    date: document.getElementById('mil-date').value,
    miles: parseFloat(document.getElementById('mil-miles').value) || 0,
    entity_id: document.getElementById('mil-entity').value || null,
    vehicle: document.getElementById('mil-vehicle').value.trim(),
    purpose: document.getElementById('mil-purpose').value.trim(),
    from_location: document.getElementById('mil-from').value.trim(),
    to_location: document.getElementById('mil-to').value.trim(),
    odometer_start: document.getElementById('mil-odo-start').value || null,
    odometer_end: document.getElementById('mil-odo-end').value || null,
    rate_per_mile: document.getElementById('mil-rate').value || null,
    business: document.getElementById('mil-business').checked,
    notes: document.getElementById('mil-notes').value.trim(),
  };
  if (!payload.date || !payload.miles || payload.miles <= 0) {
    alert('Date and a positive miles value are required.');
    return;
  }
  const btn = document.getElementById('mil-save-btn');
  btn.disabled = true;
  let r;
  if (id) {
    r = await post('/api/mileage/'+id, payload);
    if (r.updated) { toast('Entry updated', 'success'); closeMileageForm(); loadMileage(); }
    else { alert(r.error || 'Update failed'); }
  } else {
    r = await post('/api/mileage', payload);
    if (r.status === 'created') { toast('Entry added', 'success'); closeMileageForm(); loadMileage(); }
    else { alert(r.error || 'Save failed'); }
  }
  btn.disabled = false;
}

async function deleteMileageEntry(id) {
  if (!confirm('Delete this mileage entry?')) return;
  const r = await fetch(P+'/api/mileage/'+id, {method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if (r.deleted) { toast('Entry deleted', 'success'); loadMileage(); }
  else { alert(r.error || 'Delete failed'); }
}

function exportMileageCSV() {
  const p = new URLSearchParams();
  const e = document.getElementById('mf-entity')?.value; if(e) p.set('entity_id',e);
  const y = document.getElementById('mf-year')?.value;   if(y) p.set('year',y);
  window.location = P+'/api/mileage/export.csv' + (p.toString() ? '?'+p : '');
}

