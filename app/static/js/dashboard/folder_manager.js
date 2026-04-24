/* folder_manager.js — File Organizer tab: archive migration, folder naming issues, dry-run/apply rename, filed-return folder import */

/* ============================================================
   File Organizer / Migration
   ============================================================ */
var _foPendingRenames = [];

// Years present in the source archive (detected via scan)
var _foArchiveYears = ["2008","2009","2010","2011","2012","2013","2014","2015","2016","2017","2018","2019","2020","2021","2022","2023","2024","2025"];

async function foLoadMigration() {
  const body = document.getElementById('fo-migration-body');
  const issuesCard = document.getElementById('fo-issues-card');
  body.innerHTML = '<div class="empty" style="padding:20px">Scanning archive and Paperless index… this may take 30–60 seconds.</div>';

  // Run coverage check (all years) and issues scan in parallel
  const [covData, issueData] = await Promise.all([
    fetch(P+'/api/folder-manager/coverage').then(r=>r.json()).catch(()=>({})),
    fetch(P+'/api/folder-manager/scan').then(r=>r.json()).catch(()=>({issues:[]})),
  ]);

  // Build per-year rows
  const byYear = {};
  (covData.files||[]).forEach(f => {
    if (!byYear[f.year]) byYear[f.year] = {total:0, in_pl:0, missing:[]};
    byYear[f.year].total++;
    if (f.in_paperless) byYear[f.year].in_pl++;
    else byYear[f.year].missing.push(f.name);
  });

  const years = Object.keys(byYear).sort((a,b)=>b.localeCompare(a));
  if (!years.length) {
    body.innerHTML = '<div class="empty" style="padding:20px">No files found in archive. Check that /mnt/s/ is mounted.</div>';
    return;
  }

  const totalFiles = covData.total_files||0;
  const totalIn = covData.in_paperless||0;
  const totalOut = covData.not_in_paperless||0;
  const overallPct = totalFiles ? Math.round(totalIn/totalFiles*100) : 0;

  body.innerHTML = `
    <div style="display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap">
      <div class="stat-card" style="min-width:140px"><div class="stat-label">Total Source PDFs</div><div class="stat-value">${totalFiles.toLocaleString()}</div></div>
      <div class="stat-card" style="min-width:140px"><div class="stat-label">In Paperless</div><div class="stat-value" style="color:var(--income)">${totalIn.toLocaleString()}</div></div>
      <div class="stat-card" style="min-width:140px"><div class="stat-label">Not Yet Imported</div><div class="stat-value" style="color:var(--expense)">${totalOut.toLocaleString()}</div></div>
      <div class="stat-card" style="min-width:140px"><div class="stat-label">Overall Progress</div><div class="stat-value">${overallPct}%</div></div>
    </div>
    <table>
      <thead><tr><th>Year</th><th>Source PDFs</th><th>In Paperless</th><th>Not Imported</th><th>Progress</th><th></th></tr></thead>
      <tbody>${years.map(yr => {
        const d = byYear[yr];
        const pct = d.total ? Math.round(d.in_pl/d.total*100) : 0;
        const color = pct===100?'var(--income)':pct>50?'#ffc107':'var(--expense)';
        const missing = d.total - d.in_pl;
        return `<tr>
          <td><strong>${esc(yr)}</strong></td>
          <td>${d.total}</td>
          <td style="color:var(--income)">${d.in_pl}</td>
          <td style="color:${missing?'var(--expense)':'var(--income)'}">${missing||'✓ all'}</td>
          <td style="min-width:140px">
            <div style="display:flex;align-items:center;gap:8px">
              <div style="flex:1;height:8px;background:#e9ecef;border-radius:4px;overflow:hidden">
                <div style="width:${pct}%;height:100%;background:${color};transition:width .3s"></div>
              </div>
              <span style="font-size:.75rem;color:var(--muted);min-width:32px">${pct}%</span>
            </div>
          </td>
          <td>
            ${missing ? `<button class="btn btn-sm btn-primary" onclick="foImportYear('${esc(yr)}')" style="white-space:nowrap">
              Import ${missing} files
            </button>` : '<span style="color:var(--income);font-size:.85rem">✓ Complete</span>'}
          </td>
        </tr>`;
      }).join('')}</tbody>
    </table>
    <p style="font-size:.78rem;color:var(--muted);margin-top:10px">
      Coverage is estimated by filename similarity — some files may be in Paperless under different names.
      "Import" copies files to the Paperless consume queue; Paperless will OCR, index, and tag them automatically.
    </p>`;

  // Show issues panel if any
  const issues = issueData.issues||[];
  if (issues.length) {
    issuesCard.style.display = '';
    const badge = document.getElementById('fo-issue-count');
    badge.textContent = issues.length + ' issues';
    const issuesBody = document.getElementById('fo-issues-body');
    issuesBody.innerHTML = `<table>
      <thead><tr><th>Current Name</th><th>Should Be</th><th>Reason</th><th>PDFs</th><th></th></tr></thead>
      <tbody>${issues.map(item=>`<tr>
        <td><strong style="color:var(--expense)">${esc(item.name)}</strong></td>
        <td><strong style="color:var(--income)">${esc(item.canonical)}</strong></td>
        <td style="font-size:.78rem;color:var(--muted)">${esc(item.issue)}</td>
        <td>${item.pdf_count}</td>
        <td><button class="btn btn-sm btn-outline" onclick="foRenameOne('${esc(item.path)}','${esc(item.canonical)}',this)">Fix</button></td>
      </tr>`).join('')}</tbody>
    </table>`;
  } else {
    issuesCard.style.display = 'none';
  }
}

async function foImportYear(year) {
  const entity = document.getElementById('fo-entity-sel')?.value || 'personal';
  if (!confirm(`Import all not-yet-imported PDFs for ${year} into Paperless?\n\nThis copies them to the consume queue. Paperless will OCR and index them. Your originals are not moved.`)) return;
  const r = await post('/api/folder-manager/queue', {year, entity_slug: entity, dry_run: false});
  if (r?.status === 'error') { toast('Error: '+r.message, 'error'); return; }
  const queued = r?.queued?.length || 0;
  const skipped = r?.skipped_count || 0;
  toast(`Queued ${queued} files for ${year}. Skipped ${skipped} already present. Paperless will process them shortly.`, 'success');
  setTimeout(foLoadMigration, 2000);
}

async function foScanIssues() {
  // Just an alias — load migration loads both
  foLoadMigration();
}
  const year = document.getElementById('fo-year')?.value || '';
  const body = document.getElementById('fo-issues-body');
  const badge = document.getElementById('fo-issue-count');
  body.innerHTML = '<div class="empty" style="padding:20px">Scanning…</div>';
  const params = year ? '?year='+year : '';
  const data = await fetch(P+'/api/folder-manager/scan'+params).then(r=>r.json()).catch(()=>({issues:[]}));
  const issues = data.issues || [];
  badge.textContent = issues.length;
  badge.style.display = issues.length ? '' : 'none';
  if (!issues.length) {
    body.innerHTML = '<div class="empty" style="padding:20px;color:var(--income)">✓ No naming issues found. All folders look consistent.</div>';
    return;
  }
  body.innerHTML = `<table>
    <thead><tr><th>Path</th><th>Current Name</th><th>Canonical Name</th><th>Issue</th><th>PDFs</th><th></th></tr></thead>
    <tbody>${issues.map((item,i) => `<tr>
      <td style="font-size:.75rem;color:var(--muted);max-width:280px;word-break:break-all">${esc(item.path.replace('/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax/','…/tax/'))}</td>
      <td><strong style="color:var(--expense)">${esc(item.name)}</strong></td>
      <td><strong style="color:var(--income)">${esc(item.canonical)}</strong></td>
      <td style="font-size:.78rem;color:var(--muted)">${esc(item.issue)}</td>
      <td>${item.pdf_count}</td>
      <td><button class="btn btn-sm btn-outline" onclick="foRenameOne('${esc(item.path)}','${esc(item.canonical)}',this)">Fix</button></td>
    </tr>`).join('')}</tbody>
  </table>`;
  _foPendingRenames = issues;
}

async function foRenameOne(src, newName, btn) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  const r = await post('/api/folder-manager/rename', {src, new_name: newName, dry_run: false, merge: true});
  btn.disabled = false;
  if (r?.status === 'renamed' || r?.status === 'merged') {
    toast('Renamed to '+newName, 'success');
    btn.textContent = '✓'; btn.style.color = 'var(--income)';
    foScanIssues();
  } else if (r?.status === 'conflict') {
    if (confirm('Folder "'+newName+'" already exists. Merge contents?')) {
      const r2 = await post('/api/folder-manager/rename', {src, new_name: newName, dry_run: false, merge: true});
      r2?.status === 'merged' ? (toast('Merged into '+newName, 'success'), foScanIssues()) : toast('Error: '+(r2?.message||'?'), 'error');
    }
    btn.textContent = orig; btn.disabled = false;
  } else {
    toast('Error: '+(r?.message||'?'), 'error');
    btn.textContent = orig;
  }
}

async function foDryRunAll() {
  const year = document.getElementById('fo-year')?.value || '';
  const card = document.getElementById('fo-preview-card');
  const preBody = document.getElementById('fo-preview-body');
  const modeEl = document.getElementById('fo-preview-mode');
  const applyBtn = document.getElementById('fo-apply-btn');
  card.style.display = '';
  modeEl.textContent = 'DRY RUN';
  modeEl.className = 'badge badge-other';
  preBody.innerHTML = '<div class="empty">Running preview…</div>';
  applyBtn.style.display = 'none';
  const r = await post('/api/folder-manager/rename-all', {dry_run: true, year: year||null});
  const results = r?.results || [];
  if (!results.length) { preBody.innerHTML = '<div class="empty" style="color:var(--income)">✓ Nothing to rename.</div>'; return; }
  preBody.innerHTML = `<table><thead><tr><th>From</th><th>To</th><th>Action</th></tr></thead>
    <tbody>${results.map(r=>`<tr>
      <td style="font-size:.78rem">${esc((r.src||'').replace('/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax/','…/tax/'))}</td>
      <td><strong>${esc(r.canonical_name||r.dst||'')}</strong></td>
      <td><span class="badge badge-${r.status==='dry_run'?'other':'income'}">${esc(r.status)}</span></td>
    </tr>`).join('')}</tbody></table>`;
  applyBtn.style.display = '';
  card.scrollIntoView({behavior:'smooth'});
}

async function foApplyAll() {
  if (!confirm('Apply all folder renames now? This will modify the source archive. A dry-run preview will show first.')) return;
  await foDryRunAll();
  const applyBtn = document.getElementById('fo-apply-btn');
  applyBtn.style.display = '';
}

async function foExecuteRename() {
  const year = document.getElementById('fo-year')?.value || '';
  const btn = document.getElementById('fo-apply-btn');
  const modeEl = document.getElementById('fo-preview-mode');
  btn.disabled = true; btn.textContent = 'Applying…';
  const r = await post('/api/folder-manager/rename-all', {dry_run: false, year: year||null});
  btn.disabled = false; btn.textContent = 'Execute Renames';
  const results = r?.results || [];
  const ok = results.filter(r=>r.status==='renamed'||r.status==='merged').length;
  const err = results.filter(r=>r.status==='error').length;
  modeEl.textContent = 'APPLIED'; modeEl.className = 'badge badge-income';
  toast(`Done: ${ok} renamed${err?' ('+err+' errors)':''}`, err?'error':'success');
  foScanIssues();
}

async function foCoverage() {
  const year = document.getElementById('fo-year')?.value || '';
  const body = document.getElementById('fo-coverage-body');
  const summary = document.getElementById('fo-coverage-summary');
  const queueBtn = document.getElementById('fo-queue-btn');
  body.innerHTML = '<div class="empty" style="padding:20px">Checking coverage — this may take a moment…</div>';
  summary.style.display = 'none';
  const params = year ? '?year='+year : '';
  const data = await fetch(P+'/api/folder-manager/coverage'+params).then(r=>r.json()).catch(()=>({}));
  if (data.error) { body.innerHTML = `<div class="empty" style="color:var(--expense)">${esc(data.error)}</div>`; return; }
  document.getElementById('fo-cov-total').textContent = (data.total_files||0).toLocaleString();
  document.getElementById('fo-cov-in').textContent = (data.in_paperless||0).toLocaleString();
  document.getElementById('fo-cov-out').textContent = (data.not_in_paperless||0).toLocaleString();
  summary.style.display = '';
  const missing = (data.files||[]).filter(f=>!f.in_paperless);
  queueBtn.style.display = missing.length ? '' : 'none';
  if (!data.files?.length) { body.innerHTML = '<div class="empty">No PDF files found.</div>'; return; }
  // Group by year
  const byYear = {};
  (data.files||[]).forEach(f=>{ (byYear[f.year]||(byYear[f.year]=[])).push(f); });
  body.innerHTML = Object.entries(byYear).sort((a,b)=>b[0].localeCompare(a[0])).map(([yr, files]) => {
    const inPl = files.filter(f=>f.in_paperless).length;
    const pct = Math.round(inPl/files.length*100);
    return `<div style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
        <strong>${esc(yr)}</strong>
        <div style="flex:1;height:8px;background:#e9ecef;border-radius:4px;overflow:hidden">
          <div style="width:${pct}%;height:100%;background:${pct>80?'var(--income)':pct>40?'#ffc107':'var(--expense)'}"></div>
        </div>
        <span style="font-size:.8rem;color:var(--muted)">${inPl}/${files.length} (${pct}%)</span>
        <button class="btn btn-sm btn-outline" onclick="foQueueYear(false,'${esc(yr)}')" style="font-size:.75rem">Queue Missing</button>
      </div>
      ${files.filter(f=>!f.in_paperless).slice(0,20).map(f=>`<div style="font-size:.75rem;color:var(--muted);padding:2px 0 2px 16px">
        ✗ ${esc(f.name)}
      </div>`).join('')}
      ${files.filter(f=>!f.in_paperless).length > 20 ? `<div style="font-size:.75rem;color:var(--muted);padding:2px 0 2px 16px">…and ${files.filter(f=>!f.in_paperless).length-20} more not in Paperless</div>` : ''}
    </div>`;
  }).join('');
}

async function foQueueYear(dryRun=true, year=null) {
  const yr = year || document.getElementById('fo-year')?.value;
  if (!yr) { toast('Select a year to queue', 'error'); return; }
  const entity = '{% if entities %}{{ entities[0].slug }}{% else %}personal{% endif %}';
  if (!dryRun && !confirm(`Copy all PDFs for ${yr} to the Paperless consume queue?\nThis will NOT move the originals — it copies them.`)) return;
  const r = await post('/api/folder-manager/queue', {year: yr, entity_slug: entity, dry_run: dryRun});
  if (r?.status === 'error') { toast('Error: '+r.message, 'error'); return; }
  const queued = r?.queued?.length || 0;
  const skipped = r?.skipped_count || 0;
  if (dryRun) {
    toast(`Preview: ${queued} files would be queued, ${skipped} already present`, '');
    if (queued && confirm(`Queue ${queued} files for Paperless ingestion?`)) foQueueYear(false, yr);
  } else {
    toast(`Queued ${queued} files for Paperless. Skipped ${skipped} already present.`, 'success');
    foCoverage();
  }
}

/* Import Filed Return from Tax Folder */
async function importFiledReturnFromFolder() {
  const year = document.getElementById('tr-year').value;
  const eid = document.getElementById('tr-entity').value;
  if (!year) { toast('Select a year first.', 'error'); return; }
  const btn = document.getElementById('tr-import-folder-btn');
  const origText = btn.textContent;
  btn.disabled = true; btn.textContent = 'Importing…';
  const r = await post('/api/filed-returns/import-from-folder', {year, entity_id: eid ? parseInt(eid) : null});
  btn.disabled = false; btn.textContent = origText;
  if (r?.status === 'ok') {
    toast('Imported from: '+r.source_name, 'success');
    if (r.all_pdfs_found?.length > 1) {
      console.log('Other PDFs in folder (not selected):', r.all_pdfs_found.filter(n=>n!==r.source_name));
    }
    loadFiledReturns();
    loadTaxReviewYears();
  } else {
    toast('Import failed: '+(r?.error||'?'), 'error');
  }
}
