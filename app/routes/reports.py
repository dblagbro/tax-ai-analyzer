"""Analytical reports — year-over-year, category breakdowns."""
import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.config import URL_PREFIX
from app.db.core import get_connection

logger = logging.getLogger(__name__)
bp = Blueprint("reports", __name__)


def _year_totals(conn, year: str, entity_id=None) -> dict:
    """Aggregate transactions + analyzed_documents for a single year."""
    params: list = [year]
    entity_clause = ""
    if entity_id is not None:
        entity_clause = " AND entity_id = ?"
        params.append(entity_id)

    # Transactions: prefer explicit income/expense category. When the category
    # is something generic like 'imported' (Gmail) or NULL, fall back to amount sign.
    # Bank importers set category='expense'/'income' explicitly so they count correctly.
    t_row = conn.execute(
        f"""SELECT
               COALESCE(SUM(CASE
                   WHEN category = 'income' THEN ABS(amount)
                   WHEN (category IS NULL OR category NOT IN ('income','expense','deduction','fee'))
                       AND amount > 0 THEN amount
                   ELSE 0
               END), 0) as income,
               COALESCE(SUM(CASE
                   WHEN category IN ('expense','deduction') THEN ABS(amount)
                   WHEN (category IS NULL OR category NOT IN ('income','expense','deduction','fee'))
                       AND amount < 0 THEN -amount
                   ELSE 0
               END), 0) as expense,
               COUNT(*) as count
            FROM transactions
            WHERE tax_year = ?{entity_clause}
              AND amount IS NOT NULL AND amount != 0""",
        tuple(params),
    ).fetchone()

    # Analyzed docs: categorized rows, excluding bank statements (would double-count)
    d_row = conn.execute(
        f"""SELECT
               COALESCE(SUM(CASE WHEN category = 'income' THEN amount ELSE 0 END), 0) as income,
               COALESCE(SUM(CASE WHEN category IN ('expense','deduction') THEN amount ELSE 0 END), 0) as expense,
               COUNT(*) as count
            FROM analyzed_documents
            WHERE tax_year = ?{entity_clause}
              AND amount IS NOT NULL AND amount > 0
              AND doc_type NOT IN ('credit_card_statement','bank_statement','mortgage_statement')
              AND (is_duplicate = 0 OR is_duplicate IS NULL)""",
        tuple(params),
    ).fetchone()

    return {
        "year": year,
        "transactions": {
            "income":  round(t_row["income"] or 0, 2),
            "expense": round(t_row["expense"] or 0, 2),
            "count":   t_row["count"],
        },
        "documents": {
            "income":  round(d_row["income"] or 0, 2),
            "expense": round(d_row["expense"] or 0, 2),
            "count":   d_row["count"],
        },
    }


def _top_vendors(conn, year: str, limit: int = 10, entity_id=None,
                 flow: str = "expense") -> list[dict]:
    """Top N vendors by total abs(amount) for a year, from transactions.

    flow: 'income' (amount>0) or 'expense' (amount<0) or 'any'.
    """
    params: list = [year]
    entity_clause = ""
    if entity_id is not None:
        entity_clause = " AND entity_id = ?"
        params.append(entity_id)

    # Prefer explicit category. If category is generic ('imported') or NULL, use sign.
    if flow == "income":
        flow_clause = " AND (category = 'income' OR ((category IS NULL OR category NOT IN ('income','expense','deduction','fee')) AND amount > 0))"
    elif flow == "expense":
        flow_clause = " AND (category IN ('expense','deduction') OR ((category IS NULL OR category NOT IN ('income','expense','deduction','fee')) AND amount < 0))"
    else:
        flow_clause = ""

    rows = conn.execute(
        f"""SELECT
               COALESCE(NULLIF(vendor_normalized,''), vendor) as vendor,
               COUNT(*) as count,
               COALESCE(SUM(ABS(amount)), 0) as total
            FROM transactions
            WHERE tax_year = ?{entity_clause}{flow_clause}
              AND amount IS NOT NULL
              AND vendor IS NOT NULL AND vendor != ''
            GROUP BY vendor
            ORDER BY total DESC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [
        {"vendor": r["vendor"], "count": r["count"], "total": round(r["total"], 2)}
        for r in rows
    ]


@bp.route(URL_PREFIX + "/api/reports/yoy")
@login_required
def api_yoy():
    """Year-over-year comparison. Query params:
      years=2023,2024     required, 2–5 years
      entity_id=<int>     optional
      top_vendors=10      optional (default 10, max 50)
    """
    years_raw = request.args.get("years", "")
    years = [y.strip() for y in years_raw.split(",") if y.strip()]
    # dedup while preserving order
    seen = set()
    years = [y for y in years if not (y in seen or seen.add(y))]
    if len(years) < 2:
        return jsonify({"error": "at least 2 years required (e.g. years=2023,2024)"}), 400
    if len(years) > 5:
        return jsonify({"error": "at most 5 years supported per report"}), 400
    for y in years:
        if not (len(y) == 4 and y.isdigit()):
            return jsonify({"error": f"invalid year: {y!r}"}), 400

    entity_id = request.args.get("entity_id", type=int)
    try:
        top_n = min(max(int(request.args.get("top_vendors", 10)), 1), 50)
    except ValueError:
        return jsonify({"error": "top_vendors must be numeric"}), 400

    conn = get_connection()
    try:
        per_year = [_year_totals(conn, y, entity_id=entity_id) for y in years]
        top_expense_per_year = {
            y: _top_vendors(conn, y, limit=top_n, entity_id=entity_id, flow="expense")
            for y in years
        }
        top_income_per_year = {
            y: _top_vendors(conn, y, limit=top_n, entity_id=entity_id, flow="income")
            for y in years
        }

        # Deltas: prev → current (pair-wise, chronological order)
        sorted_years = sorted(years)
        deltas = []
        for i in range(1, len(sorted_years)):
            prev, cur = sorted_years[i - 1], sorted_years[i]
            prev_row = next(r for r in per_year if r["year"] == prev)
            cur_row = next(r for r in per_year if r["year"] == cur)

            def _combined(r):
                # Per-category max(transactions, documents) to avoid double-counting
                # when both sources report the same event; taking the larger is an
                # approximation that's right when one source dominates.
                return {
                    "income":  max(r["transactions"]["income"],  r["documents"]["income"]),
                    "expense": max(r["transactions"]["expense"], r["documents"]["expense"]),
                    "count":   r["transactions"]["count"] + r["documents"]["count"],
                }

            p, c = _combined(prev_row), _combined(cur_row)
            def _pct(cur, prev):
                if prev == 0:
                    return None if cur == 0 else float("inf")
                return round((cur - prev) / prev * 100, 1)

            deltas.append({
                "prev_year": prev,
                "current_year": cur,
                "income_change":       round(c["income"] - p["income"], 2),
                "income_change_pct":   _pct(c["income"], p["income"]),
                "expense_change":      round(c["expense"] - p["expense"], 2),
                "expense_change_pct":  _pct(c["expense"], p["expense"]),
            })
    finally:
        conn.close()

    return jsonify({
        "years": years,
        "entity_id": entity_id,
        "per_year": per_year,
        "deltas": deltas,
        "top_expense_vendors": top_expense_per_year,
        "top_income_vendors": top_income_per_year,
    })


# ── Coverage-gap report (2026-09-06) ──────────────────────────────────────────
#
# Answers "what's MISSING for this tax year" — the question that decides
# whether the accountant handoff is complete. Flags:
#   - months with fewer than `min_txns` transactions (likely a statement not
#     yet imported)
#   - expected tax-form doc_types with zero docs (W-2/1099/1098 not uploaded)
#   - transaction sources present in other years but absent this year
#     (a bank you had last year but no feed this year)
#   - entities with no activity at all

_EXPECTED_TAX_FORMS = ("W-2", "1099-NEC", "1099-K", "1099-INT", "1099-DIV",
                       "1099-MISC", "mortgage_statement", "property_tax")


def _monthly_coverage(conn, year: str, entity_id=None) -> list[dict]:
    """Per calendar month, zero-filled for all 12:
      transactions  – rows in `transactions`
      with_amount   – of those, rows carrying a non-zero amount (Gmail import
                      creates amount-less pointer rows; they are not coverage)
      documents     – analyzed (non-duplicate) documents dated in that month
      total         – Σ|amount| of transactions
      sources       – distinct transaction sources
    """
    params: list = [year]
    entity_clause = ""
    if entity_id is not None:
        entity_clause = " AND entity_id = ?"
        params.append(entity_id)
    rows = conn.execute(
        f"""SELECT substr(date,1,7) AS ym, COUNT(*) AS n,
                   SUM(CASE WHEN amount IS NOT NULL AND amount != 0 THEN 1 ELSE 0 END) AS with_amount,
                   COALESCE(SUM(ABS(amount)),0) AS total,
                   COUNT(DISTINCT source) AS sources
            FROM transactions
            WHERE tax_year = ?{entity_clause}
              AND date IS NOT NULL AND date != ''
            GROUP BY ym""",
        tuple(params),
    ).fetchall()
    by_month = {r["ym"]: r for r in rows}
    doc_rows = conn.execute(
        f"""SELECT substr(date,1,7) AS ym, COUNT(*) AS n
            FROM analyzed_documents
            WHERE tax_year = ?{entity_clause}
              AND date IS NOT NULL AND date != ''
              AND (is_duplicate = 0 OR is_duplicate IS NULL)
            GROUP BY ym""",
        tuple(params),
    ).fetchall()
    docs_by_month = {r["ym"]: r["n"] for r in doc_rows}
    out = []
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        r = by_month.get(ym)
        out.append({
            "month": ym,
            "transactions": r["n"] if r else 0,
            "with_amount": (r["with_amount"] or 0) if r else 0,
            "documents": docs_by_month.get(ym, 0),
            "total": round(r["total"], 2) if r else 0.0,
            "sources": r["sources"] if r else 0,
        })
    return out


@bp.route(URL_PREFIX + "/api/reports/gaps")
@login_required
def api_gaps():
    """Coverage-gap report for one tax year. Query params:
      year=2023           required
      entity_id=<int>     optional (omit for all entities combined)
      min_txns=10         optional — months below this are flagged
    """
    year = (request.args.get("year") or "").strip()
    if not (len(year) == 4 and year.isdigit()):
        return jsonify({"error": "year=YYYY required"}), 400
    entity_id = request.args.get("entity_id", type=int)
    try:
        min_txns = max(int(request.args.get("min_txns", 10)), 0)
    except ValueError:
        return jsonify({"error": "min_txns must be numeric"}), 400

    conn = get_connection()
    try:
        months = _monthly_coverage(conn, year, entity_id=entity_id)
        # A month counts as covered when EITHER real transactions or analyzed
        # documents reach the threshold — a month of PDF statements analyzed by
        # the daemon is coverage even if no bank feed was imported.
        sparse_months = [m["month"] for m in months
                         if m["transactions"] < min_txns and m["documents"] < min_txns]

        # Tax forms present / missing
        params: list = [year]
        entity_clause = ""
        if entity_id is not None:
            entity_clause = " AND entity_id = ?"
            params.append(entity_id)
        present_forms = {
            r["doc_type"] for r in conn.execute(
                f"""SELECT DISTINCT doc_type FROM analyzed_documents
                    WHERE tax_year = ?{entity_clause}
                      AND (is_duplicate = 0 OR is_duplicate IS NULL)""",
                tuple(params),
            ).fetchall()
        }
        missing_forms = [f for f in _EXPECTED_TAX_FORMS if f not in present_forms]

        # Sources seen in ANY year vs this year — a bank that went quiet
        all_sources = {
            r["source"] for r in conn.execute(
                f"SELECT DISTINCT source FROM transactions WHERE source IS NOT NULL{entity_clause.replace('entity_id', 'entity_id') if entity_id is not None else ''}",
                tuple(params[1:]) if entity_id is not None else (),
            ).fetchall()
        }
        this_year_sources = {
            r["source"] for r in conn.execute(
                f"SELECT DISTINCT source FROM transactions WHERE tax_year = ?{entity_clause} AND source IS NOT NULL",
                tuple(params),
            ).fetchall()
        }
        silent_sources = sorted(all_sources - this_year_sources)

        # Entities with zero activity this year (only when not filtering)
        quiet_entities = []
        if entity_id is None:
            rows = conn.execute(
                """SELECT e.slug, e.name,
                          (SELECT COUNT(*) FROM transactions t
                             WHERE t.entity_id = e.id AND t.tax_year = ?) AS txns,
                          (SELECT COUNT(*) FROM analyzed_documents d
                             WHERE d.entity_id = e.id AND d.tax_year = ?) AS docs
                   FROM entities e WHERE COALESCE(e.archived, 0) = 0""",
                (year, year),
            ).fetchall()
            quiet_entities = [
                {"slug": r["slug"], "name": r["name"], "transactions": r["txns"], "documents": r["docs"]}
                for r in rows if r["txns"] == 0 and r["docs"] == 0
            ]

        # Uncategorized backlog — docs tagged 'other' that a re-analysis might rescue
        uncategorized = conn.execute(
            f"""SELECT COUNT(*) FROM analyzed_documents
                WHERE tax_year = ?{entity_clause}
                  AND (category = 'other' OR category IS NULL OR category = '')""",
            tuple(params),
        ).fetchone()[0]
        no_year = conn.execute(
            f"""SELECT COUNT(*) FROM analyzed_documents
                WHERE (tax_year IS NULL OR tax_year = ''){entity_clause}""",
            tuple(params[1:]),
        ).fetchone()[0]
    finally:
        conn.close()

    total_txns = sum(m["transactions"] for m in months)
    covered_months = 12 - len(sparse_months)  # same rule as sparse_months above

    return jsonify({
        "year": year,
        "entity_id": entity_id,
        "min_txns": min_txns,
        "summary": {
            "total_transactions": total_txns,
            "months_covered": covered_months,
            "months_sparse": 12 - covered_months,
            "coverage_pct": round(covered_months / 12 * 100),
        },
        "months": months,
        "sparse_months": sparse_months,
        "missing_tax_forms": missing_forms,
        "present_tax_forms": sorted(present_forms & set(_EXPECTED_TAX_FORMS)),
        "silent_sources": silent_sources,
        "quiet_entities": quiet_entities,
        "uncategorized_docs": uncategorized,
        "docs_missing_tax_year": no_year,
    })
