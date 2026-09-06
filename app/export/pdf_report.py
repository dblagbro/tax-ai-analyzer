"""Generate PDF summary report for an entity/year using database."""
import os
from datetime import datetime
from app import db
from app.config import EXPORT_PATH


def export_pdf(year: str, entity_slug: str, documents: list = None) -> str:
    """Generate an accountant-ready PDF report. Gets data from DB unless documents list is provided."""
    from weasyprint import HTML

    filename = f"summary_{year}_{entity_slug}.pdf"
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)

    # Fetch entity info
    entity_obj = db.get_entity(slug=entity_slug)
    entity_display = entity_obj["name"] if entity_obj else entity_slug.replace("_", " ").title()
    entity_type = entity_obj.get("type", "personal") if entity_obj else "personal"

    # Get data from DB if not passed in
    if documents is None:
        entity_id = entity_obj["id"] if entity_obj else None
        db_docs = db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=10000)
        txns = db.list_transactions(entity_id=entity_id, tax_year=year, limit=10000)
        # Merge into a unified list for rendering
        documents = []
        for d in db_docs:
            documents.append({
                "date": d.get("date"),
                "vendor": d.get("vendor"),
                "doc_type": d.get("doc_type"),
                "category": d.get("category"),
                "amount": d.get("amount"),
                "description": "",
                "source": "paperless",
                "paperless_doc_id": d.get("paperless_doc_id"),
                "confidence": d.get("confidence"),
            })
        for t in txns:
            documents.append({
                "date": t.get("date"),
                "vendor": t.get("vendor"),
                "doc_type": t.get("doc_type"),
                "category": t.get("category"),
                "amount": t.get("amount"),
                "description": t.get("description"),
                "source": t.get("source"),
                "paperless_doc_id": t.get("paperless_doc_id"),
                "confidence": None,
            })

    # Group by category
    income_docs = [d for d in documents if d.get("category") == "income"]
    deduction_docs = [d for d in documents if d.get("category") in ("deduction", "expense")]
    other_docs = [d for d in documents if d.get("category") not in ("income", "deduction", "expense")]

    total_income = sum(float(d.get("amount") or 0) for d in income_docs)
    total_deductions = sum(float(d.get("amount") or 0) for d in deduction_docs)
    net = total_income - total_deductions

    # Group expenses by category for summary table
    expense_by_cat: dict[str, float] = {}
    for d in deduction_docs:
        cat = d.get("doc_type") or d.get("category") or "other"
        expense_by_cat[cat] = expense_by_cat.get(cat, 0) + float(d.get("amount") or 0)

    # W-2 and 1099 income lines
    w2_docs = [d for d in income_docs if str(d.get("doc_type", "")).upper() in ("W-2", "W2")]
    nec_docs = [d for d in income_docs if "1099" in str(d.get("doc_type", "")).upper()]

    def doc_rows(docs):
        rows = ""
        for d in sorted(docs, key=lambda x: x.get("date") or ""):
            conf_str = f"{float(d.get('confidence') or 0)*100:.0f}%" if d.get("confidence") is not None else "—"
            rows += f"""<tr>
              <td>{d.get('date','')}</td>
              <td>{d.get('vendor','')}</td>
              <td>{d.get('doc_type','')}</td>
              <td style="text-align:right">${float(d.get('amount') or 0):,.2f}</td>
              <td>{(d.get('description') or '')[:60]}</td>
              <td style="text-align:center;color:#666">{conf_str}</td>
            </tr>"""
        return rows

    def expense_category_rows():
        rows = ""
        for cat, amt in sorted(expense_by_cat.items(), key=lambda x: -x[1]):
            rows += f"""<tr>
              <td>{cat.replace('_',' ').title()}</td>
              <td style="text-align:right">${amt:,.2f}</td>
            </tr>"""
        return rows

    net_color = "#28a745" if net >= 0 else "#dc3545"
    generated_ts = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    # ── Coverage & gaps (2026-09-06) ──────────────────────────────────────────
    # The accountant's first question is "is this complete?" — answer it on
    # page 1 instead of making them infer it from thin sections below.
    gaps_section = ""
    try:
        from app.routes.reports import _monthly_coverage, _EXPECTED_TAX_FORMS
        from app.db.core import get_connection as _gc
        _conn = _gc()
        try:
            _months = _monthly_coverage(_conn, year, entity_id=(entity_obj["id"] if entity_obj else None))
            _present = {
                r[0] for r in _conn.execute(
                    "SELECT DISTINCT doc_type FROM analyzed_documents "
                    "WHERE tax_year = ? AND (? IS NULL OR entity_id = ?) "
                    "AND (is_duplicate = 0 OR is_duplicate IS NULL)",
                    (year, entity_obj["id"] if entity_obj else None,
                     entity_obj["id"] if entity_obj else None),
                ).fetchall()
            }
        finally:
            _conn.close()
        _missing_forms = [f for f in _EXPECTED_TAX_FORMS if f not in _present]
        def _thin(m):  # same rule as /api/reports/gaps: amount-bearing txns OR docs
            return m.get("with_amount", m["transactions"]) < 10 and m.get("documents", 0) < 10
        _sparse = [m for m in _months if _thin(m)]
        _covered = 12 - len(_sparse)
        _cov_color = "#28a745" if _covered >= 11 else ("#e0a800" if _covered >= 6 else "#dc3545")
        _month_cells = "".join(
            f'<td style="text-align:center;padding:4px;background:{"#f8d7da" if _thin(m) else "#d4edda"}">'
            f'<div style="font-size:8pt;color:#666">{m["month"][5:]}</div>'
            f'<div style="font-weight:bold">{m.get("with_amount", m["transactions"])}</div>'
            f'<div style="font-size:7pt;color:#666">{m.get("documents", 0)} docs</div></td>'
            for m in _months
        )
        _missing_html = (
            "".join(f'<span style="display:inline-block;background:#f8d7da;color:#721c24;'
                    f'padding:2px 6px;margin:2px;border-radius:3px;font-size:8pt">{f}</span>'
                    for f in _missing_forms)
            if _missing_forms else '<span style="color:#28a745">All expected forms present</span>'
        )
        gaps_section = f"""
<h2>Coverage &amp; Gaps</h2>
<div style="display:table;width:100%;margin:8px 0">
  <div style="display:table-cell;width:30%;padding:8px 12px;border:1px solid #ddd;background:#f8f9fa;vertical-align:top">
    <div class="summary-label">Months with data</div>
    <div class="summary-amount" style="color:{_cov_color}">{_covered} / 12</div>
    <div style="font-size:8pt;color:#666">≥10 transactions or ≥10 documents = covered</div>
  </div>
  <div style="display:table-cell;padding:8px 12px;border:1px solid #ddd;vertical-align:top">
    <div class="summary-label">Expected tax forms not yet on file</div>
    <div style="margin-top:6px">{_missing_html}</div>
  </div>
</div>
<table style="margin-top:6px"><tr>{_month_cells}</tr></table>
<div style="font-size:8pt;color:#856404;margin-top:4px">
  Red months have fewer than 10 transactions with amounts and fewer than 10 documents — usually a bank or card statement that hasn't been imported yet.
  Sections below only reflect what has been ingested; totals will change as coverage fills in.
</div>"""
    except Exception as _e:  # never let the gap panel break the whole report
        gaps_section = f'<div style="font-size:8pt;color:#999">(coverage panel unavailable: {_e})</div>'

    # ── Source document manifest (2026-09-06) ─────────────────────────────────
    # Every figure above traces back to a document. List them all so the
    # accountant can spot-check any line against the original.
    _manifest_docs = sorted(
        [d for d in documents if d.get("paperless_doc_id")],
        key=lambda x: (x.get("date") or "", x.get("vendor") or ""),
    )
    def _is_provisional(d) -> bool:
        # rules-only classification stored while the LLM was unreachable
        return '"provisional": true' in (d.get("extracted_json") or "")
    _n_provisional = sum(1 for d in _manifest_docs if _is_provisional(d))
    _manifest_rows = "".join(
        f"<tr><td>{d.get('date') or ''}</td>"
        f"<td>{d.get('vendor') or ''}</td>"
        f"<td>{d.get('doc_type') or ''}"
        + ("<span style='color:#856404;font-size:7pt'> (provisional — unverified)</span>" if _is_provisional(d) else "")
        + "</td>"
        f"<td>{d.get('category') or ''}</td>"
        f"<td style='text-align:right'>${float(d.get('amount') or 0):,.2f}</td>"
        f"<td style='font-family:monospace;font-size:8pt'>"
        f"<a href='https://www.voipguru.org/tax-paperless/documents/{d.get('paperless_doc_id')}/'>#{d.get('paperless_doc_id')}</a></td></tr>"
        for d in _manifest_docs
    )
    _provisional_note = (
        f"<div style='font-size:8pt;color:#856404;margin-bottom:6px'>{_n_provisional} document(s) marked "
        f"<em>provisional</em> were classified by keyword rules while the AI service was unavailable; "
        f"they are re-checked automatically and should be verified against the original before relying on them.</div>"
        if _n_provisional else ""
    )
    manifest_section = (
        f"""<h2 style="page-break-before:always">Source Document Manifest <span class="section-count">({len(_manifest_docs)} documents)</span></h2>
<div style="font-size:8pt;color:#666;margin-bottom:6px">
  Every line in this report traces to a stored document. Click a Paperless ID to open the original.
</div>
{_provisional_note}
<table>
  <tr><th>Date</th><th>Party</th><th>Type</th><th>Category</th><th>Amount</th><th>Paperless</th></tr>
  {_manifest_rows}
</table>"""
        if _manifest_docs else ""
    )

    # Pre-compute sections that can't be inlined in f-strings
    other_docs_section = (
        f"""<h2>Other Documents <span class="section-count">({len(other_docs)} total)</span></h2>
<table>
  <tr><th>Date</th><th>Party</th><th>Type</th><th>Amount</th><th>Description</th><th>Conf.</th></tr>
  {doc_rows(other_docs)}
</table>"""
        if other_docs else ""
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  @page {{ margin: 1in; }}
  body {{ font-family: Arial, sans-serif; font-size: 10pt; color: #333; line-height: 1.4; }}
  h1 {{ color: #1a3c5e; border-bottom: 3px solid #1a3c5e; padding-bottom: 8px; font-size: 18pt; }}
  h2 {{ color: #1a3c5e; margin-top: 24px; font-size: 13pt; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
  h3 {{ color: #444; font-size: 11pt; margin-top: 16px; }}
  .meta {{ color: #666; font-size: 9pt; margin-bottom: 20px; }}
  .summary-grid {{ display: table; width: 100%; border-collapse: collapse; margin: 16px 0; }}
  .summary-cell {{ display: table-cell; width: 33%; padding: 12px 16px; border: 1px solid #ddd; background: #f8f9fa; vertical-align: top; }}
  .summary-label {{ font-size: 9pt; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
  .summary-amount {{ font-size: 18pt; font-weight: bold; margin-top: 4px; }}
  .income-color {{ color: #28a745; }}
  .expense-color {{ color: #dc3545; }}
  .net-color {{ color: {net_color}; }}
  .entity-badge {{ display: inline-block; background: #1a3c5e; color: white; padding: 2px 8px;
                   border-radius: 3px; font-size: 9pt; margin-left: 8px; vertical-align: middle; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 9pt; }}
  th {{ background: #1a3c5e; color: white; padding: 6px 8px; text-align: left; font-weight: bold; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
  tr:nth-child(even) {{ background: #f8f9fa; }}
  .total-row {{ font-weight: bold; background: #e9ecef !important; }}
  .disclaimer {{ margin-top: 40px; padding: 12px; background: #fff3cd; border: 1px solid #ffc107;
                 border-radius: 4px; font-size: 8pt; color: #856404; }}
  .footer {{ margin-top: 20px; font-size: 8pt; color: #aaa; text-align: center;
             border-top: 1px solid #eee; padding-top: 8px; }}
  .section-count {{ font-size: 9pt; color: #888; font-weight: normal; margin-left: 8px; }}
</style>
</head>
<body>

<h1>Tax Summary Report — {year}
  <span class="entity-badge">{entity_type.upper()}</span>
</h1>
<div class="meta">
  <strong>Entity:</strong> {entity_display} &nbsp;&nbsp;
  <strong>Tax Year:</strong> {year} &nbsp;&nbsp;
  <strong>Generated:</strong> {generated_ts} &nbsp;&nbsp;
  <strong>Documents Analyzed:</strong> {len(documents)}
</div>

<!-- Summary boxes -->
<div class="summary-grid">
  <div class="summary-cell">
    <div class="summary-label">Total Income</div>
    <div class="summary-amount income-color">${total_income:,.2f}</div>
    <div style="font-size:9pt;color:#666;margin-top:4px">{len(income_docs)} document(s)</div>
  </div>
  <div class="summary-cell">
    <div class="summary-label">Total Deductions / Expenses</div>
    <div class="summary-amount expense-color">${total_deductions:,.2f}</div>
    <div style="font-size:9pt;color:#666;margin-top:4px">{len(deduction_docs)} document(s)</div>
  </div>
  <div class="summary-cell">
    <div class="summary-label">Net Taxable Income</div>
    <div class="summary-amount net-color">${net:,.2f}</div>
    <div style="font-size:9pt;color:#666;margin-top:4px">before credits &amp; adjustments</div>
  </div>
</div>

{gaps_section}

<!-- Income section -->
<h2>Income Documents <span class="section-count">({len(income_docs)} total)</span></h2>

{"<h3>W-2 Wages</h3><table><tr><th>Date</th><th>Employer</th><th>Type</th><th>Amount</th><th>Notes</th><th>Conf.</th></tr>" + (doc_rows(w2_docs) or '<tr><td colspan="6" style="color:#999">None found</td></tr>') + "</table>" if True else ""}

{"<h3>1099 Income (Consulting / Self-Employment / Other)</h3><table><tr><th>Date</th><th>Payer</th><th>Type</th><th>Amount</th><th>Notes</th><th>Conf.</th></tr>" + (doc_rows(nec_docs) or '<tr><td colspan="6" style="color:#999">None found</td></tr>') + "</table>" if True else ""}

{(lambda other_income: ("<h3>Other Income</h3><table><tr><th>Date</th><th>Source</th><th>Type</th><th>Amount</th><th>Notes</th><th>Conf.</th></tr>" + doc_rows(other_income) + "</table>") if other_income else "")([ d for d in income_docs if d not in w2_docs and d not in nec_docs ])}

<!-- Expense summary table -->
<h2>Expenses by Category</h2>
<table style="max-width:500px">
  <tr><th>Category</th><th style="text-align:right">Amount</th></tr>
  {expense_category_rows() or '<tr><td colspan="2" style="color:#999">No expenses found</td></tr>'}
  <tr class="total-row">
    <td>TOTAL DEDUCTIONS</td>
    <td style="text-align:right">${total_deductions:,.2f}</td>
  </tr>
</table>

<!-- Full deduction detail -->
<h2>Deductions &amp; Expenses — Detail <span class="section-count">({len(deduction_docs)} total)</span></h2>
<table>
  <tr><th>Date</th><th>Payee / Vendor</th><th>Type</th><th>Amount</th><th>Description</th><th>Conf.</th></tr>
  {doc_rows(deduction_docs) or '<tr><td colspan="6" style="color:#999">None found</td></tr>'}
  <tr class="total-row">
    <td colspan="3">TOTAL</td>
    <td style="text-align:right">${total_deductions:,.2f}</td>
    <td colspan="2"></td>
  </tr>
</table>

{other_docs_section}

{manifest_section}

<div class="disclaimer">
  <strong>For Accountant Review Only</strong> — This report was generated by an AI document analysis
  system and has not been verified by a licensed tax professional. Amounts, classifications, and dates
  may be inaccurate. This document does not constitute legal, tax, or financial advice. Please verify
  all figures against original source documents before filing any tax return.
  Confidence scores indicate AI certainty; items below 70% should be manually reviewed.
</div>

<div class="footer">
  Tax AI Analyzer &mdash; {entity_display} / {year} &mdash; Generated {generated_ts} &mdash; CONFIDENTIAL
</div>

</body>
</html>"""

    HTML(string=html).write_pdf(dest)
    return dest
