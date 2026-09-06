"""Rules-only email triage — the no-LLM stand-in for ``_ai_review_email``.

Why (2026-09-06): when no LLM was reachable the Gmail importer "defaulted to
import" — every email that survived the fast pre-filter became a PDF in
Paperless and an amount-less transaction row. That is how the January-2023
run produced 160 rows of which 130 have no amount. This module makes the
same keep/discard call the LLM prompt asks for, from the subject, sender and
the first ~900 characters of the body, with deliberately HIGH precision.

Benchmarked against the 10,648 keep/discard decisions the LLM made in the
2026-03-19 run (subject + sender only, no body): precision 0.94, recall 0.60
before the regex fixes below; the live importer also passes the body snippet,
which is where order totals live, so real recall is higher.

  * strong financial subject (statement / receipt / invoice / bill /
    payment posted|received|confirmation / 1099 / W-2 / 1098 / premium /
    renewal / subscription / refund / deposit / toll)     → keep
  * weak signal (order confirmation / purchase / charged)  → keep only when a
    dollar amount is present (subject or snippet)
  * marketing / shipping / survey / security vocabulary  → drop, unless the
    subject is unmistakably a receipt/invoice/statement/tax form
  * otherwise a dollar amount in subject or snippet      → keep
  * nothing                                             → drop

Returns the same dict shape as the LLM review so runner.py needs no changes:
{relevant, reason, doc_type, vendor, amount, description}.
"""
from __future__ import annotations

import re
from typing import Optional


def _rx(patterns: list[str]) -> "re.Pattern[str]":
    # Each alternative carries its own boundaries. A single trailing \b after
    # the whole group silently broke every alternative ending in a digit or
    # symbol ("save 20%" never matched) — hence the per-pattern list.
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


_STRONG = _rx([
    r"\be-?receipts?\b", r"\breceipts?\b", r"\binvoices?\b", r"\be-?statements?\b", r"\bstatements?\b",
    r"\bbilling statement\b", r"\byour bill\b", r"\bbill is (?:ready|available)\b", r"\bnew bill\b",
    r"\bmonthly bill\b", r"\bbill payment\b",
    r"\bpayment (?:has |was |is |been )*(?:posted|received|confirmation|confirmed|due|reminder|"
    r"scheduled|processed|successful|authorized|complete|completed)\b",
    r"\byour payment\b", r"\bscheduling your .{0,30}payment\b", r"\bauthorized a payment\b",
    r"\bautopay\b", r"\bauto-pay\b",
    r"\b1099\b", r"\bw-?2\b", r"\b1098\b", r"\btax (?:forms?|documents?|statements?|notice)\b",
    r"\b(?:sales|income|property|school|estimated) tax(?:es)?\b",
    r"\bpremium\b", r"\brenewal\b", r"\bsubscription\b", r"\brefund\b", r"\bdeposit\b",
    r"\btoll\b", r"\bthank you for your (?:order|purchase|payment)\b",
])
_UNMISTAKABLE = _rx([
    r"\breceipts?\b", r"\binvoices?\b",
    r"\b(?<!rewards )(?<!privacy )(?<!mission )statements?\b(?! of )",  # not "Rewards Statement"
    r"\b1099\b", r"\bw-?2\b", r"\b1098\b", r"\btax (?:forms?|documents?|statements?)\b",
])
_WEAK = _rx([
    r"\border (?:confirmation|confirmed|summary|details|placed|update|received)\b", r"\byour order\b",
    r"\border #", r"\border number\b", r"\bpurchase\b", r"\bcharged\b", r"\btransaction\b",
    r"\btransfer\b", r"\bwithdrawal\b", r"\bpolicy\b", r"\bcontribution\b", r"\bpaid\b",
    r"\breturn of\b",
])
_DROP = _rx([
    r"\bshipped\b", r"\bshipping (?:confirmation|update)\b", r"\bdelivered\b", r"\bout for delivery\b",
    r"\barriv(?:ing|ed|es)\b", r"\bon its way\b", r"\btrack(?:ing)? (?:your|package|number)\b",
    r"\bsurvey\b", r"\bhow was\b", r"\brate your\b", r"\bfeedback\b",
    r"\breview your (?:purchase|order|experience)\b", r"\bnewsletter\b", r"\bwebinar\b",
    r"\d+ ?% ?off", r"\bpercent off\b", r"\$\d[\d,]* off\b", r"\bsale\b", r"\bdeals?\b", r"\boffers?\b",
    r"\brewards?\b", r"\bearn\b", r"\bcash ?back\b", r"\bpromo(?:tion|tional)?\b", r"\bintroducing\b",
    r"\bwelcome to\b", r"\bverify\b", r"\bpassword\b", r"\bsecurity alert\b", r"\bsign-?in\b",
    r"\bnew device\b", r"\bunsubscribe\b", r"\bdigest\b", r"\btips\b", r"\bguide\b", r"\bcommunity\b",
    r"\binvited\b", r"\binvitation\b", r"\bdon'?t miss\b", r"\blast chance\b", r"\blimited time\b",
    r"\bexclusive\b", r"\bsave (?:up to|\$|\d)", r"\bpayment method\b", r"\bbilling issues\b",
    r"\bshop\b", r"\bwin\b", r"\bgiveaway\b", r"\bcontest\b", r"\bclaim your\b", r"\bcongrat",
    r"\bperks?\b", r"\bbucks\b", r"\bfree\b", r"\bgaming\b", r"\blaptop\b", r"\bmonitor\b",
    r"\bperformance\b", r"\bbuild your own\b", r"\bheadquarters\b", r"\blifestyle\b",
    r"\btax season\b", r"\bspecial\b", r"\badded to your\b", r"\bget started\b", r"\bpatron\b",
    r"\?\?+", r"\bfor .{0,20}customers\b", r"\bcredits? for\b", r"\bearned\b", r"\bbiggest\b",
    r"\bactivation\b", r"\bupdate about\b", r"\bchanging\b", r"\bwill be limited\b",
    r"\brewards statement\b", r"\bprivacy statement\b", r"\bredeemed\b",
])
_AMOUNT = re.compile(
    r"(?:\$|USD\s?)\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})"
    r"|(\d{1,3}(?:,\d{3})*\.\d{2})\s?(?:USD|dollars)\b",
    re.IGNORECASE,
)


def extract_amount(*texts: Optional[str]) -> Optional[float]:
    for t in texts:
        m = _AMOUNT.search(t or "")
        if m:
            raw = m.group(1) or m.group(2)
            try:
                return float(raw.replace(",", ""))
            except (TypeError, ValueError):
                continue
    return None


def _doc_type(subject: str) -> str:
    s = subject.lower()
    if re.search(r"1099|w-?2\b|1098|tax (?:form|document|statement)", s):
        return "tax_notice"
    if "statement" in s:
        return "statement"
    if "invoice" in s:
        return "invoice"
    if "receipt" in s or "order" in s or "purchase" in s:
        return "receipt"
    if "bill" in s or "payment" in s or "premium" in s or "renewal" in s or "toll" in s:
        return "bill"
    return "other"


def _vendor(sender: str) -> str:
    name = re.sub(r"<.*?>", "", sender or "").strip().strip('"').strip()
    if not name and "@" in (sender or ""):
        name = sender.split("@", 1)[1].split(">")[0].split(".")[0]
    return name[:60]


def rules_review_email(subject: str, sender: str, body_snippet: str, date_str: str = "") -> dict:
    subj = subject or ""
    amount = extract_amount(subj, body_snippet)
    strong = bool(_STRONG.search(subj))
    unmistakable = bool(_UNMISTAKABLE.search(subj))
    weak = bool(_WEAK.search(subj))
    drop = bool(_DROP.search(subj))

    if drop and not unmistakable:
        relevant, reason = False, "rules: marketing/shipping/notification subject"
    elif strong:
        relevant, reason = True, "rules: financial subject" + (" + amount" if amount is not None else "")
    elif weak and amount is not None:
        relevant, reason = True, "rules: order/purchase subject with amount"
    elif amount is not None:
        relevant, reason = True, "rules: dollar amount in subject/snippet"
    else:
        relevant, reason = False, "rules: no financial signal"

    return {
        "relevant": relevant,
        "reason": reason,
        "doc_type": _doc_type(subj) if relevant else "other",
        "vendor": _vendor(sender),
        "amount": amount,
        "description": (re.sub(r"[^\w\s-]", "", subj).strip()[:40] or "email"),
        "method": "rules",
    }
