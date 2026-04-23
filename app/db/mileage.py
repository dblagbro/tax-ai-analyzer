"""Mileage log CRUD.

IRS standard mileage rate history (business use):
  2020: 0.575  2021: 0.560  2022: 0.585 (Jan-Jun) / 0.625 (Jul-Dec)
  2023: 0.655  2024: 0.670  2025: 0.670  2026: 0.700 (projected; verify at tax time)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.db.core import get_connection

logger = logging.getLogger(__name__)

IRS_MILEAGE_RATES: dict[str, float] = {
    "2020": 0.575,
    "2021": 0.560,
    "2022": 0.605,  # effective blended rate for simplicity
    "2023": 0.655,
    "2024": 0.670,
    "2025": 0.670,
    "2026": 0.700,
}


def irs_rate_for_year(year: str, default: float = 0.670) -> float:
    return IRS_MILEAGE_RATES.get(str(year), default)


def add_mileage(
    date: str,
    miles: float,
    *,
    entity_id: Optional[int] = None,
    tax_year: Optional[str] = None,
    purpose: str = "",
    from_location: str = "",
    to_location: str = "",
    business: bool = True,
    vehicle: str = "",
    odometer_start: Optional[float] = None,
    odometer_end: Optional[float] = None,
    notes: str = "",
    rate_per_mile: Optional[float] = None,
) -> int:
    if not date or miles is None or float(miles) <= 0:
        raise ValueError("date and positive miles are required")

    if not tax_year:
        tax_year = date[:4]
    if rate_per_mile is None:
        rate_per_mile = irs_rate_for_year(tax_year)

    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO mileage_log
               (entity_id, tax_year, date, miles, purpose, from_location,
                to_location, business, vehicle, odometer_start, odometer_end,
                notes, rate_per_mile)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_id, tax_year, date, float(miles), purpose,
             from_location, to_location, 1 if business else 0, vehicle,
             odometer_start, odometer_end, notes, rate_per_mile),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_mileage(
    entity_id: Optional[int] = None,
    tax_year: Optional[str] = None,
    business_only: bool = False,
    limit: int = 500,
) -> list[dict]:
    where: list[str] = []
    params: list = []
    if entity_id is not None:
        where.append("m.entity_id = ?")
        params.append(entity_id)
    if tax_year:
        where.append("m.tax_year = ?")
        params.append(tax_year)
    if business_only:
        where.append("m.business = 1")
    params.append(limit)
    w = f"WHERE {' AND '.join(where)}" if where else ""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT m.*, e.name as entity_name
               FROM mileage_log m
               LEFT JOIN entities e ON e.id = m.entity_id
               {w} ORDER BY m.date DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_mileage(mileage_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        r = conn.execute(
            "SELECT * FROM mileage_log WHERE id=?", (mileage_id,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def update_mileage(mileage_id: int, **kwargs) -> bool:
    allowed = {
        "date", "miles", "entity_id", "tax_year", "purpose", "from_location",
        "to_location", "business", "vehicle", "odometer_start", "odometer_end",
        "notes", "rate_per_mile",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    if "business" in fields:
        fields["business"] = 1 if fields["business"] else 0
    sets = ", ".join(f"{k}=?" for k in fields)
    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE mileage_log SET {sets} WHERE id=?",
            (*fields.values(), mileage_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_mileage(mileage_id: int) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM mileage_log WHERE id=?", (mileage_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mileage_summary(
    entity_id: Optional[int] = None,
    tax_year: Optional[str] = None,
) -> dict:
    """Return {total_miles, business_miles, personal_miles, deduction_amount, rate, count}."""
    where: list[str] = []
    params: list = []
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    if tax_year:
        where.append("tax_year = ?")
        params.append(tax_year)
    w = f"WHERE {' AND '.join(where)}" if where else ""
    conn = get_connection()
    try:
        row = conn.execute(
            f"""SELECT
                   COUNT(*) as count,
                   COALESCE(SUM(miles), 0) as total_miles,
                   COALESCE(SUM(CASE WHEN business=1 THEN miles ELSE 0 END), 0) as business_miles,
                   COALESCE(SUM(CASE WHEN business=0 THEN miles ELSE 0 END), 0) as personal_miles,
                   COALESCE(SUM(CASE WHEN business=1 THEN miles * COALESCE(rate_per_mile, 0) ELSE 0 END), 0) as deduction_amount
               FROM mileage_log {w}""",
            tuple(params),
        ).fetchone()
        result = dict(row) if row else {}
        result["rate"] = irs_rate_for_year(tax_year) if tax_year else None
        return result
    finally:
        conn.close()
