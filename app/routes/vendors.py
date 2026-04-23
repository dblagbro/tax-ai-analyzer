"""Vendor listing + merge routes — clean up the vendor column across transactions."""
import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX
from app.db.core import get_connection

logger = logging.getLogger(__name__)
bp = Blueprint("vendors", __name__)


@bp.route(URL_PREFIX + "/api/vendors")
@login_required
def api_vendors_list():
    """Return unique vendor+vendor_normalized pairs with per-vendor stats.

    Query params:
      entity_id   optional
      year        optional
      search      substring filter on vendor
      group_by    'normalized' (default) groups by vendor_normalized (shows all raw
                  variants that collapse to the same canonical name), or 'raw'
                  to see every distinct raw vendor string.
    """
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    search = (request.args.get("search") or "").strip()
    group_by = (request.args.get("group_by") or "normalized").lower()

    where: list[str] = ["vendor IS NOT NULL", "vendor != ''"]
    params: list = []
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    if year:
        where.append("tax_year = ?")
        params.append(year)
    if search:
        where.append("(vendor LIKE ? OR vendor_normalized LIKE ?)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    w = "WHERE " + " AND ".join(where)

    conn = get_connection()
    try:
        if group_by == "raw":
            rows = conn.execute(
                f"""SELECT vendor,
                           MAX(vendor_normalized) as vendor_normalized,
                           COUNT(*) as count,
                           COALESCE(SUM(ABS(amount)), 0) as total,
                           MIN(date) as first_seen,
                           MAX(date) as last_seen
                    FROM transactions
                    {w}
                    GROUP BY vendor
                    ORDER BY total DESC
                    LIMIT 1000""",
                tuple(params),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT
                           COALESCE(NULLIF(vendor_normalized,''), vendor) as vendor_normalized,
                           GROUP_CONCAT(DISTINCT vendor) as raw_variants,
                           COUNT(*) as count,
                           COUNT(DISTINCT vendor) as variant_count,
                           COALESCE(SUM(ABS(amount)), 0) as total,
                           MIN(date) as first_seen,
                           MAX(date) as last_seen
                    FROM transactions
                    {w}
                    GROUP BY COALESCE(NULLIF(vendor_normalized,''), vendor)
                    ORDER BY total DESC
                    LIMIT 1000""",
                tuple(params),
            ).fetchall()
        return jsonify({
            "count": len(rows),
            "group_by": group_by,
            "vendors": [dict(r) for r in rows],
        })
    finally:
        conn.close()


def _do_merge(from_vendors: list[str], to_vendor: str,
              update_normalized: bool = True) -> tuple[int, int]:
    """Core merge operation. Returns (rows_affected, distinct_from_count).

    Raises ValueError for bad input.
    """
    if not from_vendors:
        raise ValueError("from_vendors empty")
    if not to_vendor:
        raise ValueError("to_vendor empty")
    if len(from_vendors) > 500:
        raise ValueError("too many source vendors (max 500)")

    cleaned = [str(v).strip() for v in from_vendors if str(v).strip()]
    if not cleaned:
        raise ValueError("all from_vendors are empty after trim")

    placeholders = ",".join("?" for _ in cleaned)
    conn = get_connection()
    try:
        affected = conn.execute(
            f"SELECT COUNT(*) FROM transactions WHERE vendor IN ({placeholders})",
            cleaned,
        ).fetchone()[0]

        if affected == 0:
            return 0, len(cleaned)

        if update_normalized:
            conn.execute(
                f"""UPDATE transactions
                    SET vendor = ?, vendor_normalized = ?
                    WHERE vendor IN ({placeholders})""",
                (to_vendor, to_vendor, *cleaned),
            )
        else:
            conn.execute(
                f"UPDATE transactions SET vendor = ? WHERE vendor IN ({placeholders})",
                (to_vendor, *cleaned),
            )
        conn.commit()
        return affected, len(cleaned)
    finally:
        conn.close()


@bp.route(URL_PREFIX + "/api/vendors/merge", methods=["POST"])
@login_required
def api_vendors_merge():
    """Merge multiple vendor strings into one canonical.

    Body: {
      "from_vendors": ["Amazon.com", "AMAZON", "AMZN MKTP US"],
      "to_vendor":    "Amazon",
      "update_normalized": true
    }
    """
    data = request.get_json() or {}
    from_vendors = data.get("from_vendors") or []
    if not isinstance(from_vendors, list):
        return jsonify({"error": "from_vendors must be a list"}), 400
    if not from_vendors:
        return jsonify({"error": "from_vendors (non-empty list) required"}), 400
    to_vendor = (data.get("to_vendor") or "").strip()
    update_normalized = bool(data.get("update_normalized", True))

    try:
        affected, from_count = _do_merge(from_vendors, to_vendor, update_normalized)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Vendor merge error")
        return jsonify({"error": str(e)}), 500

    if affected == 0:
        return jsonify({"status": "noop", "count": 0,
                        "message": "no transactions matched from_vendors"})

    sample = [str(v).strip() for v in from_vendors[:3] if str(v).strip()]
    db.log_activity(
        "vendor_merge",
        f"{affected} rows: {sample}{'...' if from_count > len(sample) else ''} → {to_vendor!r}",
        user_id=current_user.id,
    )
    return jsonify({"status": "merged", "count": affected,
                    "to_vendor": to_vendor, "from_count": from_count})


@bp.route(URL_PREFIX + "/api/vendors/rename", methods=["POST"])
@login_required
def api_vendors_rename():
    """Rename a single vendor. Body: {"from_vendor": "x", "to_vendor": "y"}"""
    data = request.get_json() or {}
    from_vendor = (data.get("from_vendor") or "").strip()
    to_vendor = (data.get("to_vendor") or "").strip()
    if not from_vendor or not to_vendor:
        return jsonify({"error": "from_vendor and to_vendor required"}), 400
    try:
        affected, _ = _do_merge([from_vendor], to_vendor,
                                bool(data.get("update_normalized", True)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db.log_activity(
        "vendor_rename",
        f"{affected} rows: {from_vendor!r} → {to_vendor!r}",
        user_id=current_user.id,
    )
    return jsonify({"status": "renamed", "count": affected,
                    "from_vendor": from_vendor, "to_vendor": to_vendor})
