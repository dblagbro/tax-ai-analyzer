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
