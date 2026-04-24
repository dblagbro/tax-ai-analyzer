/* table_manager.js — TableManager class: sortable, filterable, resizable columns */

/* ============================================================
   TableManager — sortable, filterable, resizable columns
   ============================================================ */
class TableManager {
  constructor({tableId, data, columns, renderRow, filterRow}) {
    this.tableId = tableId;
    this.data = data;
    this.columns = columns; // [{key, label, type:'str'|'num'|'date', sortable, filterable}]
    this.renderRow = renderRow;
    this.filterRow = filterRow !== false; // default true
    this.sortKey = null;
    this.sortDir = 1; // 1=asc, -1=desc
    this.filters = {};
    this._build();
  }

  _build() {
    const table = document.getElementById(this.tableId);
    if (!table) return;
    // Build header
    const thead = table.querySelector('thead') || table.createTHead();
    thead.innerHTML = '';
    const hrow = thead.insertRow();
    this.columns.forEach((col, ci) => {
      const th = document.createElement('th');
      th.textContent = col.label;
      if (col.sortable !== false) {
        th.classList.add('sortable');
        th.addEventListener('click', (e) => {
          if (e.target.classList.contains('col-resize')) return;
          if (this.sortKey === col.key) this.sortDir *= -1;
          else { this.sortKey = col.key; this.sortDir = 1; }
          this._updateSortHeaders(thead);
          this._render();
        });
      }
      // Resize handle
      const rh = document.createElement('span');
      rh.className = 'col-resize';
      rh.addEventListener('mousedown', (e) => this._startResize(e, th));
      th.appendChild(rh);
      hrow.appendChild(th);
    });
    // Filter row
    if (this.filterRow) {
      const frow = thead.insertRow();
      frow.className = 'tbl-filter-row';
      this.columns.forEach((col) => {
        const th = document.createElement('th');
        if (col.filterable !== false) {
          const inp = document.createElement('input');
          inp.type = 'text';
          inp.placeholder = col.label + '…';
          inp.addEventListener('input', () => { this.filters[col.key] = inp.value.trim().toLowerCase(); this._render(); });
          th.appendChild(inp);
        }
        frow.appendChild(th);
      });
    }
    this._render();
  }

  _updateSortHeaders(thead) {
    thead.querySelectorAll('th').forEach(th => th.classList.remove('sort-asc','sort-desc'));
    this.columns.forEach((col, ci) => {
      if (col.key === this.sortKey) {
        const ths = thead.querySelectorAll('tr:first-child th');
        if (ths[ci]) ths[ci].classList.add(this.sortDir === 1 ? 'sort-asc' : 'sort-desc');
      }
    });
  }

  _render() {
    const table = document.getElementById(this.tableId);
    if (!table) return;
    let rows = [...this.data];
    // Filter
    Object.entries(this.filters).forEach(([k, v]) => {
      if (!v) return;
      rows = rows.filter(r => String(r[k]||'').toLowerCase().includes(v));
    });
    // Sort
    if (this.sortKey) {
      const col = this.columns.find(c=>c.key===this.sortKey);
      rows.sort((a, b) => {
        let av = a[this.sortKey], bv = b[this.sortKey];
        if (col?.type === 'num') { av = parseFloat(av)||0; bv = parseFloat(bv)||0; }
        else { av = String(av||'').toLowerCase(); bv = String(bv||'').toLowerCase(); }
        return av < bv ? -this.sortDir : av > bv ? this.sortDir : 0;
      });
    }
    let tbody = table.querySelector('tbody');
    if (!tbody) { tbody = document.createElement('tbody'); table.appendChild(tbody); }
    tbody.innerHTML = rows.length ? rows.map(r => this.renderRow(r)).join('') :
      `<tr><td colspan="${this.columns.length}"><div class="empty">No matching records.</div></td></tr>`;
  }

  setData(data) { this.data = data; this._render(); }

  _startResize(e, th) {
    e.preventDefault();
    const startX = e.clientX, startW = th.offsetWidth;
    const rh = th.querySelector('.col-resize');
    if (rh) rh.classList.add('dragging');
    const onMove = ev => { th.style.width = Math.max(50, startW + ev.clientX - startX) + 'px'; };
    const onUp = () => {
      if (rh) rh.classList.remove('dragging');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }
}
