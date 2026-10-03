#!/usr/bin/env python3
"""Candidate business-supply purchases for a year, from the order records on file.

Sources: Amazon order-history export (item-level), the eBay purchase list, and
the text of Home Depot / Walmart / Lowe's / Harbor Freight / Staples receipt PDFs.
Each item is tagged by keyword into a supply category. These are CANDIDATES for
Devin to confirm (business vs personal) — nothing here is a deduction by itself.

    python3 scripts/supplies_candidates.py --year 2023
"""
import argparse, csv, glob, os, re, subprocess, sys

ARCHIVE = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
CATS = [
    ("Office supplies & printing", r"\bink\b|toner|printer|paper|staple|envelope|label|tape\b|pen\b|pens\b|marker|folder|binder|notebook|post-?it|clipboard|laminat|shipping box|mailer|bubble"),
    ("Network & cabling supplies", r"cat ?5|cat ?6|ethernet|patch cable|rj-?45|keystone|punch ?down|crimp|cable tester|fiber|sfp|poe|switch\b|router|access point|unifi|ubiquiti|coax|hdmi|usb|adapter|extension cord|power strip|surge|ups\b|battery backup"),
    ("Fasteners & install hardware", r"zip ?ties?|cable ties?|screw|bolt|nut\b|nuts\b|washer|anchor|drywall|velcro|cable clip|staple gun|conduit|raceway|grommet|mount|bracket|electrical tape|heat ?shrink|wire nut|connector kit|junction box|outlet box"),
    ("Tools", r"drill|driver|bit set|socket|wrench|plier|crimper|stripper|multimeter|tester|ladder|saw\b|hammer|level\b|tape measure|flashlight|headlamp|tool ?bag|tool ?box|soldering|solder|heat gun|fish tape|milwaukee|dewalt|ridgid|makita|ryobi|klein|knife|utility knife"),
    ("Work clothing & safety", r"work boots?|boots|gloves|safety glass|hi-?vis|reflective|carhartt|dickies|work pants|work shirt|coverall|hard hat|ear protection|knee pads|respirator|mask\b"),
    ("Computer parts & storage", r"\bssd\b|nvme|hard drive|\bhdd\b|exos|seagate|western digital|ram\b|ddr4|ddr5|memory|graphics card|gpu|motherboard|power supply|\bpsu\b|cpu cooler|thermal paste|laptop|desktop|monitor|keyboard|mouse|docking|sata|sas\b|raid"),
    ("Phones & telecom", r"phone|handset|headset|voip|sip\b|avaya|polycom|yealink|panasonic kx|cordless|range extender|sim card"),
    ("Cameras & security", r"camera|nvr|dvr|cctv|reolink|amcrest|hikvision|lens|ptz"),
    ("Welding & fabrication", r"weld|mig\b|tig\b|flux|electrode|acetylene|oxygen|grinder|cut-?off|flap disc|abrasive|clamp|vise|metal"),
]
CRX = [(c, re.compile(p, re.I)) for c, p in CATS]

def cat(t):
    for c, rx in CRX:
        if rx.search(t): return c
    return ""

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--year", required=True); a = ap.parse_args()
    R = os.path.join(ARCHIVE, a.year, "Receipts"); rows = []
    # Amazon item-level export
    for x in glob.glob(os.path.join(ARCHIVE, a.year, "**", "*Amazon*OrderHistory*.xlsx"), recursive=True)[:1]:
        import openpyxl
        ws = openpyxl.load_workbook(x, read_only=True, data_only=True).active
        it = list(ws.iter_rows(values_only=True)); h = [str(v) for v in it[0]]
        ti, di, pi = h.index("Product Name"), h.index("Order Date"), h.index("Total Owed")
        oi = h.index("Order ID")
        for r in it[1:]:
            d = str(r[di])[:10]
            if d[:4] != a.year: continue
            t = str(r[ti] or ""); c = cat(t)
            if c: rows.append({"date": d, "source": "Amazon", "item": t[:140], "amount": r[pi], "category": c, "order": r[oi], "file": os.path.relpath(x, os.path.join(ARCHIVE, a.year))})
    # eBay list
    eb = os.path.join(R, f"_eBay_{a.year}_purchases.csv")
    if os.path.isfile(eb):
        for r in csv.DictReader(open(eb, encoding="utf-8")):
            c = cat(r["item"])
            if c: rows.append({"date": r["date"], "source": "eBay", "item": r["item"][:140], "amount": r["total"], "category": c, "order": r["order"], "file": r["file"]})
    # store receipts: one row per receipt, items summarised from the text
    for p in sorted(glob.glob(os.path.join(R, "*.pdf"))):
        b = os.path.basename(p)
        if not re.search(r"home_?depot|walmart|lowe|harbor_?freight|staples|tractor_supply|fallsburg|menards|ace_hardware", b, re.I): continue
        t = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True).stdout
        lines = [re.sub(r"\s{2,}", " ", l).strip() for l in t.splitlines() if l.strip()]
        hits = sorted({c for l in lines for c in [cat(l)] if c})
        if not hits: continue
        items = [l[:60] for l in lines if cat(l)][:6]
        m = re.search(r"(?:order total|total|grand total)[^\n$]*\$\s?([\d,]+\.\d{2})", t, re.I)
        dm = re.match(r"(\d{4})_(\d{2})_(\d{2})", b)
        rows.append({"date": f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else "", "source": re.split(r"_", b)[3] if dm else "store",
                     "item": " | ".join(items), "amount": m.group(1) if m else "", "category": "; ".join(hits), "order": "", "file": "Receipts/" + b})
    rows.sort(key=lambda r: (r["date"], r["source"]))
    out = os.path.join(R, f"_Business_supplies_candidates_{a.year}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "source", "item", "amount", "category", "business_yes_no", "order", "file"])
        w.writeheader(); [w.writerow({**r, "business_yes_no": ""}) for r in rows]
    tot = {}
    for r in rows:
        try: v = float(str(r["amount"]).replace(",", ""))
        except ValueError: v = 0
        k = r["category"].split(";")[0]; tot[k] = tot.get(k, [0, 0.0]); tot[k][0] += 1; tot[k][1] += v
    print(f"{len(rows)} candidate items -> {out}")
    for k, (n, v) in sorted(tot.items(), key=lambda x: -x[1][1]): print(f"   {n:>4}  ${v:>10,.2f}  {k}")

if __name__ == "__main__": sys.exit(main())
