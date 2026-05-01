"""DB helpers for the bank-onboarding queue (Phase 11).

Three tables:
  - pending_banks       : the queue of bank-import requests submitted by users
  - bank_recordings     : HAR + narration uploaded against a pending bank
  - generated_importers : Playwright source code emitted by the AI codegen agent

Schema is created in db/core.py:init_db(). This module only contains CRUD.
"""
from __future__ import annotations

import re
from typing import Optional

from app.db.core import get_connection


_SLUG_RE = re.compile(r"[^\w]+")


def _slugify(name: str) -> str:
    """Match the convention the rest of the app uses for entity/bank slugs."""
    s = _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")
    return s or "bank"


# ── pending_banks ─────────────────────────────────────────────────────────────

def list_pending_banks(status: Optional[str] = None) -> list[dict]:
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM pending_banks WHERE status=? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pending_banks ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_bank(bank_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pending_banks WHERE id=?", (bank_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending_bank_by_slug(slug: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pending_banks WHERE slug=?", (slug,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_pending_bank(
    display_name: str,
    login_url: str,
    statements_url: str = "",
    platform_hint: str = "",
    submitted_by: Optional[int] = None,
    notes: str = "",
    slug: Optional[str] = None,
) -> int:
    """Insert a new pending bank. Returns its id. Slugs must be unique;
    appends -2/-3 etc. if needed."""
    base = slug or _slugify(display_name)
    conn = get_connection()
    try:
        # Resolve slug collisions
        candidate = base
        n = 1
        while conn.execute(
            "SELECT 1 FROM pending_banks WHERE slug=?", (candidate,)
        ).fetchone():
            n += 1
            candidate = f"{base}_{n}"
        cur = conn.execute(
            "INSERT INTO pending_banks(slug,display_name,login_url,statements_url,"
            "platform_hint,submitted_by,notes) VALUES(?,?,?,?,?,?,?)",
            (candidate, display_name, login_url, statements_url,
             platform_hint, submitted_by, notes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


_VALID_STATUSES = {
    "pending", "recording", "recorded", "processing",
    "generated", "approved", "rejected", "live",
}


def update_pending_bank(
    bank_id: int,
    *,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    statements_url: Optional[str] = None,
    platform_hint: Optional[str] = None,
) -> bool:
    """Update mutable fields. Returns True on success, False if id not found."""
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    sets, params = [], []
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if notes is not None:
        sets.append("notes=?")
        params.append(notes)
    if statements_url is not None:
        sets.append("statements_url=?")
        params.append(statements_url)
    if platform_hint is not None:
        sets.append("platform_hint=?")
        params.append(platform_hint)
    if not sets:
        return False
    sets.append("updated_at=datetime('now')")
    params.append(bank_id)
    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE pending_banks SET {','.join(sets)} WHERE id=?", params,
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_pending_bank(bank_id: int) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM pending_banks WHERE id=?", (bank_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── bank_recordings ───────────────────────────────────────────────────────────

def add_recording(
    pending_bank_id: int,
    har_path: Optional[str],
    narration_text: str = "",
    dom_snapshot_path: Optional[str] = None,
    byte_size: int = 0,
) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO bank_recordings(pending_bank_id,har_path,narration_text,"
            "dom_snapshot_path,byte_size) VALUES(?,?,?,?,?)",
            (pending_bank_id, har_path, narration_text, dom_snapshot_path, byte_size),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_recordings(pending_bank_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM bank_recordings WHERE pending_bank_id=? "
            "ORDER BY captured_at DESC",
            (pending_bank_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recording(recording_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bank_recordings WHERE id=?", (recording_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── generated_importers ───────────────────────────────────────────────────────

def add_generated_importer(
    pending_bank_id: int,
    source_code: str,
    *,
    recording_id: Optional[int] = None,
    test_code: str = "",
    llm_model: str = "",
    llm_tokens_in: int = 0,
    llm_tokens_out: int = 0,
    generation_notes: str = "",
    validation_status: str = "",
    validation_notes: str = "",
    parent_id: Optional[int] = None,
    feedback_text: str = "",
) -> int:
    """Insert a new generated importer row.

    parent_id chains a regenerated draft to its previous version (Phase 11F);
    feedback_text is the admin's critique that triggered the regeneration.
    Both default to None/'' for fresh first-pass generations.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO generated_importers(pending_bank_id,recording_id,source_code,"
            "test_code,llm_model,llm_tokens_in,llm_tokens_out,generation_notes,"
            "validation_status,validation_notes,parent_id,feedback_text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pending_bank_id, recording_id, source_code, test_code, llm_model,
             llm_tokens_in, llm_tokens_out, generation_notes,
             validation_status, validation_notes, parent_id, feedback_text),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_generated_importers(pending_bank_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM generated_importers WHERE pending_bank_id=? "
            "ORDER BY generated_at DESC",
            (pending_bank_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_generated_importer(generated_id: int) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM generated_importers WHERE id=?", (generated_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def approve_generated_importer(generated_id: int, approved_by: int) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE generated_importers SET approved_by=?, "
            "approved_at=datetime('now') WHERE id=?",
            (approved_by, generated_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_generated_deployed(
    generated_id: int, *, deployed_path: str, deployed_by: Optional[int] = None,
) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE generated_importers SET deployed_path=?, "
            "deployed_at=datetime('now'), deployed_by=? WHERE id=?",
            (deployed_path, deployed_by, generated_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_deployed_importers() -> list[dict]:
    """All approved+deployed importers, joined with their pending_bank slug.
    Used by the auto-import route dispatcher at startup."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT g.id AS generated_id, g.deployed_path, g.deployed_at, "
            "p.id AS bank_id, p.slug, p.display_name, p.status "
            "FROM generated_importers g "
            "JOIN pending_banks p ON p.id = g.pending_bank_id "
            "WHERE g.deployed_at IS NOT NULL AND g.deployed_path != '' "
            "ORDER BY g.deployed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
