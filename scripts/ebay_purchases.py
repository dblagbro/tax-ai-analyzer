#!/usr/bin/env python3
"""List every eBay purchase in a tax year from the saved eBay email PDFs.

Reads tax/<year>/Receipts/*eBay*ORDER*CONFIRMED*.pdf (the importer's PDFs of
eBay's "ORDER CONFIRMED" emails), pulls item titles, order number, totals and
the card used, flags items that look like technology/network/computer gear,
and writes tax/<year>/Receipts/_eBay_<year>_purchases.csv.

    python3 scripts/ebay_purchases.py --year 2023
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import subprocess
import sys

ARCHIVE_ROOT = "/mnt/s/documents/doc_backup/devin_backup/devin_personal/tax"
_TECH = re.compile(
    r"laptop|notebook|thinkpad|latitude|dell|lenovo|\bhp\b|chromebook|\bssd\b|nvme|\bhdd\b|hard drive|"
    r"\bram\b|ddr[345]|memory|cpu|processor|ryzen|intel|\bi[3579]-|gpu|graphics|motherboard|\bpsu\b|power supply|"
    r"router|switch|unifi|ubiquiti|access point|\bap\b|wifi|wi-fi|ethernet|cat ?6|sfp|poe|firewall|mikrotik|"
    r"camera|nvr|dvr|amcrest|reolink|phone|voip|avaya|polycom|cisco|yealink|headset|sip\b|"
    r"server|rack|\bnas\b|synology|seagate|western digital|\bwd\b|toshiba|samsung|monitor|display|"
    r"usb|hdmi|adapter|cable|charger|battery|\bups\b|apc\b|keyboard|mouse|docking|dock\b|"
    r"raspberry|arduino|esp32|sensor|relay|module|microsd|sd card|flash|thinkdiag|obd|scanner|printer|toner|ink\b|"
    r"mesh|antenna|lte|modem|starlink|tablet|ipad|android|pixel|iphone|watch|fitbit|drone|gopro|lens|tripod",
    re.I)


def text(p: str) -> str:
    return subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True).stdout


def parse(p: str) -> list[dict]:
    t = text(p)
    lines = [re.sub(r"\s{2,}", " ", l).strip() for l in t.splitlines()]
    base = os.path.basename(p)
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})_", base)
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
    order = next((re.search(r"Order number:\s*([\d-]+)", l).group(1) for l in lines if re.search(r"Order number:\s*[\d-]+", l)), "")
    card = ""
    for l in lines:
        mm = re.search(r"Total charged to.*?(x\s?-?\s?\d{4})", l)
        if mm:
            card = mm.group(1).replace(" ", "")
            break
    # grand total: the "$x.xx" line right after "Order total:" block, else "Total: $x"
    total = ""
    for i, l in enumerate(lines):
        if l.startswith("Order total"):
            for l2 in lines[i:i + 8]:
                mm = re.fullmatch(r"\$([\d,]+\.\d{2})", l2)
                if mm:
                    total = mm.group(1)
            break
    if not total:   # newer layout: "Total charged to x-6591  $84.23" or "Total charged to  $878.04"
        mm = re.search(r"Total charged to[^\n$]*\$\s?([\d,]+\.\d{2})", t)
        total = mm.group(1) if mm else ""
    if not total:
        mm = re.search(r"Total:\s*\$([\d,]+\.\d{2})", t)
        total = mm.group(1) if mm else ""
    subtotal = ""
    mm = re.search(r"Subtotal(?: \(\d+ items?\))?\s*\$\s?([\d,]+\.\d{2})", t)
    if mm:
        subtotal = mm.group(1)
    if not total:   # last resort: the importer put the first body amount in the filename
        mm = re.search(r"-(\d+\.\d{2})(?:_[0-9a-f]{6})?\.pdf$", base)
        total = mm.group(1) if mm and mm.group(1) != "0.00" else ""
    # items: lines between "Order summary" and "Order number"/"Order details" that are not Total lines
    items = []
    try:
        s = lines.index("Order summary")
        for l in lines[s + 1:s + 25]:
            if l.startswith(("Order number", "Order details", "Item ID", "Estimated delivery")):
                if l.startswith("Order number") or l.startswith("Order details"):
                    break
                continue
            if l.startswith("Total:") or not l:
                continue
            items.append(l)
    except ValueError:
        pass
    item = " ".join(items)[:160]
    if not item:   # newer layout puts no title near the summary; use the subject line kept in the filename
        item = re.sub(r"^\d{4}_\d{2}_\d{2}_eBay_(?:eBay_)?(?:ORDER_CONFIRMED_|order_confirmed_|eBay_order_|order_)?", "", base)
        item = re.sub(r"-\d+\.\d{2}(?:_[0-9a-f]{6})?\.pdf$", "", item).replace("Your_orders_confirmed_for_", "").replace("_", " ")[:120]
    return [{"date": date, "item": item, "total": total, "subtotal": subtotal, "order": order, "card": card,
             "tech": "yes" if _TECH.search(item) else "", "file": "Receipts/" + base}]


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--year", required=True); a = ap.parse_args()
    rdir = os.path.join(ARCHIVE_ROOT, a.year, "Receipts")
    files = sorted(p for p in glob.glob(os.path.join(rdir, "*.pdf")) if "ebay" in os.path.basename(p).lower())
    # full subject lines from the importer's manifest (filenames are truncated)
    subjects = {}
    man = f"/mnt/s/documents/tax-organizer/export/gmail_pdfs/{a.year}/_manifest.csv"
    if os.path.isfile(man):
        with open(man, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                subjects[r["file"]] = r["subject"]
    rows = []
    for p in files:
        t = text(p)
        if "Order summary" in t or "order is confirmed" in t.lower():
            for r in parse(p):
                subj = subjects.get(os.path.basename(p), "")
                if subj and len(r["item"]) < 60:
                    better = re.sub(r"^.*?(?:ORDER CONFIRMED|orders? confirmed(?: for)?):?\s*", "", subj, flags=re.I).strip()
                    if better:
                        r["item"] = better[:160]
                        r["tech"] = "yes" if _TECH.search(better) else ""
                rows.append(r)
    # the same order can be present twice (re-imported email rendered to a different file)
    uniq, seen = [], set()
    for r in sorted(rows, key=lambda r: r["date"]):
        key = r["order"] or (r["date"], r["item"][:40], r["total"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    rows = uniq
    out = os.path.join(rdir, f"_eBay_{a.year}_purchases.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "item", "total", "subtotal", "tech", "order", "card", "file"]); w.writeheader(); w.writerows(rows)
    tot = sum(float(r["total"].replace(",", "")) for r in rows if r["total"])
    tech = [r for r in rows if r["tech"]]
    ttot = sum(float(r["total"].replace(",", "")) for r in tech if r["total"])
    print(f"{len(rows)} eBay orders in {a.year}: ${tot:,.2f} total | {len(tech)} look like technology: ${ttot:,.2f} | {sum(1 for r in rows if not r['total'])} without a readable total")
    print("written:", out)
    for r in tech:
        print(f"   {r['date']}  ${r['total']:>8}  {r['item'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
