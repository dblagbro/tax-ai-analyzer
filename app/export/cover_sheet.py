"""Accountant cover sheet — one PDF that answers "what do you have, what is it
worth, and what is still missing" for a tax year.

Why (2026-10-01): the per-entity summary PDF lists hundreds of documents; the
accountant's first questions are simpler. This sheet shows
  1. the figures printed on the tax forms on file (W-2, 1098, 1099-INT, 1095-C),
     read straight from the PDFs in the tax archive,
  2. which bank / card statements were imported and whether each reconciled,
  3. deposits that are not payroll (each needs a one-line explanation),
  4. bills paid from checking (utilities, insurance) and card spend by category,
  5. every card / loan that was paid during the year, and whether a statement
     for it is on file — i.e. the list of what is still missing.

No LLM. Everything is computed from the transactions table and from text
extracted with pdftotext, so it can be regenerated at any time.
"""
from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import zipfile
from collections import defaultdict
from datetime import datetime

from app.config import EXPORT_PATH
from app.db.core import get_connection

ARCHIVE_ROOT = os.environ.get(
    "TAX_ARCHIVE_ROOT", "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax")
_SSN = re.compile(r"\b(?:\d{3}|XXX)-(?:\d{2}|XX)-\d{4}\b")
_AMT = r"\$?([\d,]+\.\d{2})"


def _pdf_text(path: str = "", data: bytes = b"") -> str:
    try:
        if data:
            p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, timeout=60)
            out = p.stdout.decode("utf-8", "replace")
        else:
            out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True,
                                 text=True, timeout=60).stdout
        return _SSN.sub("[redacted]", out or "")
    except Exception:
        return ""


def _f(s: str) -> float:
    return float(s.replace(",", "").replace("$", ""))


def _first(rx: str, text: str, flags=re.I):
    m = re.search(rx, text, flags)
    return m.groups() if m else None


# ── tax forms ────────────────────────────────────────────────────────────────

def parse_w2(text: str) -> dict:
    out: dict = {}
    g = _first(r"1\s+Wages, tips, other comp\.[^\n]*\n\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text)
    if g:
        out["Box 1 — Wages, tips, other compensation"] = _f(g[0])
        out["Box 2 — Federal income tax withheld"] = _f(g[1])
    g = _first(r"3\s+Social security wages[^\n]*\n\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text)
    if g:
        out["Box 3 — Social security wages"] = _f(g[0])
        out["Box 4 — Social security tax withheld"] = _f(g[1])
    g = _first(r"5\s+Medicare wages and tips[^\n]*\n\s*([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", text)
    if g:
        out["Box 5 — Medicare wages"] = _f(g[0])
        out["Box 6 — Medicare tax withheld"] = _f(g[1])
    for code, label in (("D", "Box 12 D — 401(k) deferrals"), ("W", "Box 12 W — HSA (employer + pre-tax)"),
                        ("DD", "Box 12 DD — Employer health coverage cost")):
        g = _first(rf"(?:12[a-d]\s+)?\b{code}\s+([\d,]+\.\d{{2}})\b", text, 0)
        if g:
            out[label] = _f(g[0])
    g = _first(r"\b([A-Z]{2})\s+[\d-]{6,}\s+([\d,]+\.\d{2})\s*\n\s*17 State income tax[^\n]*\n\s*([\d,]+\.\d{2})", text, 0)
    if g:
        out[f"Box 16 — {g[0]} state wages"] = _f(g[1])
        out[f"Box 17 — {g[0]} state income tax withheld"] = _f(g[2])
    g = _first(r"c\s+Employer.s name, address, and ZIP code\s*\n\s*([A-Z][A-Z0-9 ,.&'-]+?)\s*(?:\n|\s{3,})", text, 0)
    if g:
        out["_payer"] = g[0].strip()
    return out


def parse_1098(text: str) -> dict:
    out: dict = {}
    g = _first(rf"MORTGAGE INTEREST RECEIVED FROM PAYER/BORROWER\(S\)\s+{_AMT}", text)
    if g:
        out["Box 1 — Mortgage interest received"] = _f(g[0])
    g = _first(rf"WOODRIDGE[^\n]*?{_AMT}\s+(\d\d/\d\d/\d{{4}})", text) or _first(rf"{_AMT}\s+(\d\d/\d\d/\d{{4}})\s", text)
    if g:
        out["Box 2 — Outstanding mortgage principal"] = _f(g[0])
        out["_origination"] = g[1]
    g = _first(r"10 Real estate taxes(?:\n[^\n]*){0,4}?\n\s*\$([\d,]+\.\d{2})\s*\n", text)
    if g:
        out["Box 10 — Real estate taxes paid from escrow"] = _f(g[0])
    for kind in ("City", "County", "Town", "Village", "School"):
        tot = sum(_f(x) for x in re.findall(rf"{kind} tax payment[^\n]*?\$-([\d,]+\.\d{{2}})", text))
        if tot:
            out[f"   of which {kind.lower()} tax (escrow history)"] = tot
    tot = sum(_f(x) for x in re.findall(r"Hazard insurance pmt[^\n]*?\$-([\d,]+\.\d{2})", text))
    if tot:
        out["Homeowner's insurance paid from escrow"] = tot
    return out


def parse_1099_int(text: str) -> dict:
    g = _first(r"1 Interest income[\s\S]{0,400}?\$([\d,]+\.\d{2})", text)   # first $ figure after the box label
    out = {"Box 1 — Interest income": _f(g[0])} if g else {}
    g = _first(r"PAYER.S name[^\n]*\n[^\n]*\n\s*([A-Z][A-Z ,.&]+N\.? ?A\.?)", text, 0)
    if g:
        out["_payer"] = g[0].strip()
    return out


_FORM_SIGNATURES = [
    ("W-2", re.compile(r"w-2 and earnings summary|wage and tax\s+(?:w-?2\s+)?statement|wages, tips, other comp", re.I)),
    ("1098", re.compile(r"mortgage interest received from payer|form\s+1098\b|mortgage\s+interest\s+statement", re.I)),
    ("1099-INT", re.compile(r"form\s+1099-int", re.I)),
    ("1095-C", re.compile(r"form\s+1095-c", re.I)),
]
_NAME_HINTS = [("W-2", r"w-?2"), ("1098", r"1098"), ("1099-INT", r"1099-?int"), ("1095-C", r"1095")]


def form_kind(text: str, name: str = "") -> str:
    """Which tax form a PDF is, judged by its CONTENT; the filename is only a
    fallback. (In the 2022 folder the files named 'W2' and '1095c' are
    swapped — trusting names would have reported the W-2 as missing.)"""
    for kind, rx in _FORM_SIGNATURES:
        if rx.search(text or ""):
            return kind
    if not (text or "").strip():
        low = (name or "").lower()
        for kind, pat in _NAME_HINTS:
            if re.search(pat, low):
                return kind
    return ""


def _form_entry(kind: str, text: str, source: str, name: str) -> dict:
    note = ""
    named = next((k for k, pat in _NAME_HINTS if re.search(pat, name.lower())), "")
    if named and named != kind:
        note = f"Note: the file is NAMED like a {named} but its content is a {kind}."
    if kind == "W-2":
        lines = parse_w2(text)
        return {"form": "W-2", "source": source, "payer": lines.pop("_payer", ""), "lines": lines, "note": note}
    if kind == "1098":
        lines = parse_1098(text)
        orig = lines.pop("_origination", "")
        return {"form": "1098 (mortgage)", "source": source, "payer": "Wells Fargo Bank N.A.", "lines": lines,
                "note": (f"Loan originated {orig}. " if orig else "") + note}
    if kind == "1099-INT":
        lines = parse_1099_int(text)
        return {"form": "1099-INT", "source": source, "payer": lines.pop("_payer", "") or "", "lines": lines, "note": note}
    return {"form": "1095-C", "source": source, "payer": "", "lines": {},
            "note": ("Employer-offered health coverage statement (information only). " + note).strip()}


def collect_tax_forms(year: str) -> list[dict]:
    """[{form, source, payer, lines: {label: amount}, note}] for every tax form
    PDF at the top level of the year's archive folder (loose or inside a zip)."""
    base = os.path.join(ARCHIVE_ROOT, year)
    forms: list[dict] = []
    for p in sorted(glob.glob(os.path.join(base, "*.pdf"))):
        name = os.path.basename(p)
        if name.startswith("_"):            # our own generated cover sheet / summary
            continue
        text = _pdf_text(p)
        kind = form_kind(text, name)
        if kind:
            forms.append(_form_entry(kind, text, name, name))
    for z in sorted(glob.glob(os.path.join(base, "*.zip"))):
        try:
            with zipfile.ZipFile(z) as zf:
                for n in zf.namelist():
                    if not n.lower().endswith(".pdf"):
                        continue
                    text = _pdf_text(data=zf.read(n))
                    kind = form_kind(text, n)
                    if kind:
                        forms.append(_form_entry(kind, text, f"{os.path.basename(z)} → {n}", n))
        except Exception:
            continue
    order = {"W-2": 0, "1098 (mortgage)": 1, "1099-INT": 2, "1095-C": 3}
    forms.sort(key=lambda f: order.get(f["form"], 9))
    return forms


# ── statement data ───────────────────────────────────────────────────────────

def _txns(year: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE source='pdf_statement' AND tax_year=? ORDER BY date", (year,)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["m"] = json.loads(d.get("metadata_json") or "{}")
        except Exception:
            d["m"] = {}
        out.append(d)
    return out


def _payee(desc: str) -> str:
    v = re.sub(r"^(?:EXT (?:WD|DEP)|WD|PIN|DEP|IAT Withdrawal IAT|Withdrawal|Deposit)\s+", "", desc or "")
    v = re.sub(r"\s+\d{6}\w*", "", v)
    v = re.sub(r"\s+Y?\s?- .*$", "", v)
    v = re.sub(r"\s+#?\d[\w*#-]*.*$", "", v)
    return (v or desc or "").strip().upper()[:34]


_ISSUER_LABEL = [
    (r"capital one", "Capital One (all cards)"), (r"discover", "Discover"), (r"bhg|^web$", "BHG Financial"),
    (r"citi card|citibank", "Citi cards"), (r"home depot", "Home Depot (Citi)"), (r"pentagon|penfed", "PenFed"),
    (r"amex|american express", "American Express"), (r"amz|amazon", "Amazon Store Card (Synchrony)"),
    (r"citizensbank", "Citizens Bank card"), (r"ebay", "eBay Mastercard (Synchrony)"), (r"merrick", "Merrick Bank"),
    (r"td bank", "TD Bank card"), (r"chase", "Chase card"), (r"lowes", "Lowe's (Synchrony)"),
    (r"lendingclub", "LendingClub loan"), (r"citizens bank of", "Citizens Bank loan"), (r"upgrade", "Upgrade loan"),
]


def _issuer(desc: str) -> str:
    p = _payee(desc).lower()
    for rx, label in _ISSUER_LABEL:
        if re.search(rx, p):
            return label
    return _payee(desc).title()


def build_cover_data(year: str) -> dict:
    tx = _txns(year)
    files: dict = defaultdict(lambda: {"n": 0, "out": 0.0, "in": 0.0, "kind": "", "acct": "", "months": set()})
    for t in tx:
        key = (t["m"].get("statement_kind", ""), t["m"].get("account_last4", ""))
        f = files[key]
        f["n"] += 1
        f["kind"], f["acct"] = key
        f["months"].add((t["date"] or "")[:7])
        if t["amount"] < 0:
            f["out"] += -t["amount"]
        else:
            f["in"] += t["amount"]

    deposits = [t for t in tx if t["category"] == "income" and t["amount"] >= 100]
    bills: dict = defaultdict(float)
    for t in tx:
        if t["m"].get("statement_kind") == "bank" and t["category"] == "expense":
            bills[_payee(t["vendor"] or t["description"])] += -t["amount"]
    card_cat: dict = defaultdict(float)
    for t in tx:
        if t["m"].get("statement_kind") == "card" and t["category"] == "expense":
            sec = t["m"].get("statement_section") or ""
            sec = "Purchases (uncategorised)" if sec.upper() in ("", "PURCHASES", "TRANSACTIONS") else sec
            card_cat[sec] += -t["amount"]
    transfers: dict = defaultdict(lambda: [0, 0.0])
    paid: dict = defaultdict(lambda: [0, 0.0, set()])
    for t in tx:
        k = t["m"].get("transfer_kind")
        if t["category"] == "transfer" and k:
            transfers[k][0] += 1
            transfers[k][1] += t["amount"]
            if k in ("card_payment", "loan_payment"):
                p = paid[(k, _issuer(t["description"]))]
                p[0] += 1
                p[1] += -t["amount"]
                p[2].add(t["date"][:7])
    on_file = {a for (k, a) in files if k == "card" and a}
    return {"tx": tx, "files": files, "deposits": deposits, "bills": bills, "card_cat": card_cat,
            "transfers": transfers, "paid": paid, "cards_on_file": on_file,
            "forms": collect_tax_forms(year)}


# ── rendering ────────────────────────────────────────────────────────────────

def _now_local() -> str:
    """Eastern time when tz data is available (the container clock is UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%B %d, %Y %I:%M %p %Z")
    except Exception:
        return datetime.utcnow().strftime("%B %d, %Y %H:%M UTC")


def _money(v: float) -> str:
    return f"${v:,.2f}"


def export_cover_sheet(year: str, entity_slug: str = "personal") -> str:
    from weasyprint import HTML
    d = build_cover_data(year)
    e = html.escape
    dest_dir = os.path.join(EXPORT_PATH, year)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{year}_Tax_Prep_Cover_Sheet.pdf")

    forms_html = ""
    for f in d["forms"]:
        rows = "".join(f"<tr><td>{e(k)}</td><td class='r'>{_money(v)}</td></tr>" for k, v in f["lines"].items())
        note = f"<div class='note'>{e(f.get('note') or '')}</div>" if f.get("note") else ""
        forms_html += (f"<h3>{e(f['form'])}{' — ' + e(f['payer']) if f['payer'] else ''}</h3>"
                       f"<div class='src'>{e(f['source'])}</div>{note}"
                       + (f"<table>{rows}</table>" if rows else ""))
    if not forms_html:
        forms_html = "<p class='warn'>No tax forms found in the archive folder for this year.</p>"

    acct_rows = "".join(
        f"<tr><td>{'Card' if f['kind'] == 'card' else 'Bank'} …{e(f['acct'] or '?')}</td><td class='r'>{f['n']}</td>"
        f"<td class='r'>{len(f['months'])}</td><td class='r'>{_money(f['out'])}</td><td class='r'>{_money(f['in'])}</td></tr>"
        for _k, f in sorted(d["files"].items()))
    dep_rows = "".join(
        f"<tr><td>{e(t['date'])}</td><td>{e(t['description'][:60])}</td><td class='r'>{_money(t['amount'])}</td><td></td></tr>"
        for t in d["deposits"])
    dep_total = sum(t["amount"] for t in d["deposits"])
    bill_rows = "".join(f"<tr><td>{e(k.title())}</td><td class='r'>{_money(v)}</td></tr>"
                        for k, v in sorted(d["bills"].items(), key=lambda x: -x[1]) if v >= 150)
    cat_rows = "".join(f"<tr><td>{e(k)}</td><td class='r'>{_money(v)}</td></tr>"
                       for k, v in sorted(d["card_cat"].items(), key=lambda x: -x[1]))
    tk_label = {"payroll_net_pay": "Payroll deposits (net pay — wages are on the W-2)",
                "mortgage_payment": "Mortgage payments (interest/taxes are on Form 1098)",
                "card_payment": "Credit-card payments", "loan_payment": "Loan payments",
                "p2p_transfer": "PayPal / Venmo / Zelle (net)", "cash": "ATM cash withdrawals",
                "internal_transfer": "Transfers between own accounts (net)",
                "balance_transfer": "Balance-transfer deposits (loan proceeds, not income)",
                "loan_proceeds": "Loan proceeds wired in (debt, not income)"}
    tr_rows = "".join(f"<tr><td>{e(tk_label.get(k, k))}</td><td class='r'>{v[0]}</td><td class='r'>{_money(v[1])}</td></tr>"
                      for k, v in sorted(d["transfers"].items(), key=lambda x: x[1][1]))
    card_files = {a: f for (k, a), f in d["files"].items() if k == "card"}
    have_stmt = {}
    disc = [f for a, f in card_files.items() if a == "6338"]
    if disc:
        have_stmt["Discover"] = f"Monthly statements on file ({len(disc[0]['months'])} months of activity)"
    capone = [a for a in card_files if a != "6338"]
    if capone:
        have_stmt["Capital One (all cards)"] = "On file for cards …" + ", …".join(sorted(capone))
    paid_rows = "".join(
        f"<tr><td>{e(name)}</td><td>{'card' if k == 'card_payment' else 'loan'}</td><td class='r'>{v[0]}</td>"
        f"<td class='r'>{_money(v[1])}</td><td class='{'ok' if name in have_stmt else 'warn'}'>"
        f"{e(have_stmt.get(name, 'NOT ON FILE — download statements / year-end summary'))}</td></tr>"
        for (k, name), v in sorted(d["paid"].items(), key=lambda x: (x[0][0], -x[1][1])))

    html_doc = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: letter; margin: 0.6in; @bottom-right {{ content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #777; }} }}
body {{ font-family: Helvetica, Arial, sans-serif; font-size: 9.5pt; color: #222; line-height: 1.35; }}
h1 {{ font-size: 17pt; margin: 0 0 2px; }} h2 {{ font-size: 12pt; margin: 16px 0 4px; border-bottom: 1.5px solid #333; padding-bottom: 2px; }}
h3 {{ font-size: 10.5pt; margin: 10px 0 1px; }}
table {{ border-collapse: collapse; width: 100%; margin: 4px 0 6px; }} td, th {{ border-bottom: 1px solid #ddd; padding: 3px 6px; text-align: left; vertical-align: top; }}
th {{ background: #f1f3f5; font-size: 8.5pt; }} .r {{ text-align: right; white-space: nowrap; }}
.src, .note, .sub {{ font-size: 8pt; color: #666; }} .warn {{ color: #a94400; }} .ok {{ color: #1b7a34; }}
.box {{ border: 1px solid #ccc; background: #fafafa; padding: 6px 10px; margin: 6px 0; font-size: 8.5pt; }}
</style></head><body>
<h1>{e(year)} Tax Preparation — Cover Sheet</h1>
<div class="sub">Devin P. Blagbrough · generated {_now_local()} · figures are extracted from the source documents listed; originals are in the accompanying folders.</div>

<h2>1. Tax forms on file</h2>
{forms_html}

<h2>2. Statements imported ({len(d['tx'])} transactions)</h2>
<table><tr><th>Account</th><th class="r">Transactions</th><th class="r">Months</th><th class="r">Money out</th><th class="r">Money in</th></tr>{acct_rows}</table>
<div class="note">At import each statement is checked against the totals it prints (opening balance + activity = closing balance; purchases total; year-end total); exceptions are recorded in the import log.</div>

<h2>3. Deposits that are not payroll — please review</h2>
<table><tr><th>Date</th><th>Description on statement</th><th class="r">Amount</th><th style="width:34%">What it was (to fill in)</th></tr>{dep_rows}
<tr><td colspan="2"><strong>Total</strong></td><td class="r"><strong>{_money(dep_total)}</strong></td><td></td></tr></table>

<h2>4. Bills paid directly from checking (≥ $150 for the year)</h2>
<table><tr><th>Payee</th><th class="r">Total {e(year)}</th></tr>{bill_rows}</table>

<h2>5. Credit-card spending by category</h2>
<table><tr><th>Category (as printed by the card issuer)</th><th class="r">Total</th></tr>{cat_rows}</table>

<h2>6. Money movements excluded from income / expense</h2>
<table><tr><th>Type</th><th class="r">Count</th><th class="r">Net</th></tr>{tr_rows}</table>

<h2>7. Cards and loans paid during {e(year)} — statement coverage</h2>
<table><tr><th>Account</th><th>Type</th><th class="r">Payments</th><th class="r">Paid from checking</th><th>Statements on file?</th></tr>{paid_rows}</table>
<div class="box">Payments are identified from the checking-account statements, so this list is complete for anything paid from that account.
Rows marked NOT ON FILE are cards whose purchases are not itemised anywhere in this package yet.</div>
</body></html>"""
    HTML(string=html_doc).write_pdf(dest)
    return dest
