"""Mileage log routes."""
import csv
import io
import logging
import math
from datetime import datetime

from flask import Blueprint, Response, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.config import URL_PREFIX


def _validate_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False

logger = logging.getLogger(__name__)
bp = Blueprint("mileage", __name__)


@bp.route(URL_PREFIX + "/api/mileage", methods=["GET"])
@login_required
def api_mileage_list():
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    business_only = request.args.get("business_only", "").lower() in ("1", "true", "yes")
    limit = min(int(request.args.get("limit", 500)), 5000)
    rows = db.list_mileage(entity_id=entity_id, tax_year=year,
                           business_only=business_only, limit=limit)
    summary = db.mileage_summary(entity_id=entity_id, tax_year=year)
    return jsonify({"count": len(rows), "entries": rows, "summary": summary})


@bp.route(URL_PREFIX + "/api/mileage", methods=["POST"])
@login_required
def api_mileage_create():
    data = request.get_json() or {}
    date = (data.get("date") or "").strip()[:10]
    miles_raw = data.get("miles")
    if not date:
        return jsonify({"error": "date required (YYYY-MM-DD)"}), 400
    if not _validate_iso_date(date):
        return jsonify({"error": "date must be valid ISO YYYY-MM-DD"}), 400
    try:
        miles = float(miles_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "miles must be numeric"}), 400
    if not math.isfinite(miles):
        return jsonify({"error": "miles must be a finite number"}), 400
    if miles <= 0:
        return jsonify({"error": "miles must be > 0"}), 400
    if miles > 100000:
        return jsonify({"error": "miles exceeds reasonable bound (100000)"}), 400

    entity_id = data.get("entity_id")
    if entity_id not in (None, ""):
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError):
            return jsonify({"error": "entity_id must be int"}), 400
    else:
        entity_id = None

    rate = data.get("rate_per_mile")
    if rate not in (None, ""):
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return jsonify({"error": "rate_per_mile must be numeric"}), 400
    else:
        rate = None

    odo_s = data.get("odometer_start")
    odo_e = data.get("odometer_end")
    try:
        odo_s = float(odo_s) if odo_s not in (None, "") else None
        odo_e = float(odo_e) if odo_e not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "odometer values must be numeric"}), 400

    try:
        mid = db.add_mileage(
            date=date, miles=miles,
            entity_id=entity_id,
            tax_year=data.get("tax_year") or date[:4],
            purpose=(data.get("purpose") or "")[:200],
            from_location=(data.get("from_location") or "")[:120],
            to_location=(data.get("to_location") or "")[:120],
            business=bool(data.get("business", True)),
            vehicle=(data.get("vehicle") or "")[:80],
            odometer_start=odo_s,
            odometer_end=odo_e,
            notes=(data.get("notes") or "")[:500],
            rate_per_mile=rate,
        )
        db.log_activity("mileage_add", f"{miles} mi on {date}", user_id=current_user.id)
        return jsonify({"id": mid, "status": "created"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route(URL_PREFIX + "/api/mileage/<int:mid>", methods=["GET"])
@login_required
def api_mileage_get(mid):
    row = db.get_mileage(mid)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)


@bp.route(URL_PREFIX + "/api/mileage/<int:mid>", methods=["POST"])
@login_required
def api_mileage_update(mid):
    if not db.get_mileage(mid):
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    if "date" in data and data["date"]:
        d = str(data["date"]).strip()[:10]
        if not _validate_iso_date(d):
            return jsonify({"error": "date must be valid ISO YYYY-MM-DD"}), 400
        data["date"] = d
    # coerce numerics if present
    for k in ("miles", "rate_per_mile", "odometer_start", "odometer_end"):
        if k in data and data[k] not in (None, ""):
            try:
                data[k] = float(data[k])
            except (TypeError, ValueError):
                return jsonify({"error": f"{k} must be numeric"}), 400
            if not math.isfinite(data[k]):
                return jsonify({"error": f"{k} must be a finite number"}), 400
    if "miles" in data and isinstance(data["miles"], float) and data["miles"] <= 0:
        return jsonify({"error": "miles must be > 0"}), 400
    if "entity_id" in data and data["entity_id"] not in (None, ""):
        try:
            data["entity_id"] = int(data["entity_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "entity_id must be int"}), 400
    if data.get("entity_id") == "":
        data["entity_id"] = None
    ok = db.update_mileage(mid, **data)
    if ok:
        db.log_activity("mileage_update", f"id={mid}", user_id=current_user.id)
    return jsonify({"updated": ok})


@bp.route(URL_PREFIX + "/api/mileage/<int:mid>", methods=["DELETE"])
@login_required
def api_mileage_delete(mid):
    ok = db.delete_mileage(mid)
    if ok:
        db.log_activity("mileage_delete", f"id={mid}", user_id=current_user.id)
    return jsonify({"deleted": ok})


@bp.route(URL_PREFIX + "/api/mileage/export.csv", methods=["GET"])
@login_required
def api_mileage_export_csv():
    entity_id = request.args.get("entity_id", type=int)
    year = request.args.get("year")
    rows = db.list_mileage(entity_id=entity_id, tax_year=year, limit=50000)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "miles", "business", "purpose", "from", "to",
                "vehicle", "odometer_start", "odometer_end",
                "rate_per_mile", "deduction", "tax_year", "entity", "notes"])
    for r in rows:
        miles = float(r.get("miles") or 0)
        rate = float(r.get("rate_per_mile") or 0)
        deduction = round(miles * rate, 2) if r.get("business") else 0
        w.writerow([
            r.get("date", ""), miles, "yes" if r.get("business") else "no",
            r.get("purpose", ""), r.get("from_location", ""), r.get("to_location", ""),
            r.get("vehicle", ""), r.get("odometer_start", "") or "",
            r.get("odometer_end", "") or "", rate, deduction,
            r.get("tax_year", ""), r.get("entity_name", ""), r.get("notes", ""),
        ])
    filename = f"mileage_{year or 'all'}_{entity_id or 'all'}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
