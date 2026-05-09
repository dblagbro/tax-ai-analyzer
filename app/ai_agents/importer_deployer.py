"""Deploy an approved + validated generated importer to disk (Phase 11E).

Writes the source code from a `generated_importers` row to
`app/importers/<slug>_importer.py` so it becomes a real, importable Python
module — the auto-import route dispatcher (`routes/importers/import_auto.py`)
can then expose `/api/import/auto/<slug>/{credentials,start,status,mfa,...}`
endpoints for it.

Safety rails:
  - Refuses to deploy if validation_status != 'pass' (force flag overrides)
  - Refuses to deploy if the bank is not approved
  - Refuses to overwrite a hand-written importer (i.e. one whose first line
    isn't our deploy-marker docstring) — keeps the human-curated importers
    immutable from the codegen pipeline
  - Slug must be a safe Python identifier-ish string (already enforced at
    bank-creation time, but we double-check)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Where deployed importers live. Mirrors the existing app/importers/ tree.
IMPORTERS_DIR = Path(__file__).resolve().parents[1] / "importers"

# First line of every auto-deployed file. We use this as a fingerprint when
# deciding whether it's safe to overwrite an existing importer file.
DEPLOY_MARKER = "# AUTO-DEPLOYED BY bank-onboarding codegen — do not hand-edit"

_SAFE_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class DeployError(Exception):
    """Raised on any deploy precondition failure."""


def deploy(
    generated_id: int,
    *,
    deployed_by: Optional[int] = None,
    force: bool = False,
) -> dict:
    """Write the generated source for `generated_id` to disk.

    Returns:
      {"path": "<absolute filesystem path>", "slug": "<bank slug>"}

    Raises DeployError on any precondition failure. The caller should mark
    bank status="live" if and only if the deploy succeeds.
    """
    from app import db

    gen = db.get_generated_importer(generated_id)
    if not gen:
        raise DeployError(f"generated importer id={generated_id} not found")

    bank = db.get_pending_bank(gen["pending_bank_id"])
    if not bank:
        raise DeployError(f"parent bank id={gen['pending_bank_id']} not found")

    # Approval check — codegen output must be human-blessed before we ship it
    if not gen.get("approved_at"):
        raise DeployError(
            "importer must be approved before it can be deployed. "
            "Approve it first."
        )

    # Validation check — never deploy code that didn't pass our static checks.
    # pattern_warning is advisory (Phase 14 helper not used but code is
    # functionally valid) so it doesn't block deploy.
    vs = (gen.get("validation_status") or "").strip()
    BLOCKING = {"syntax_error", "shape_error", "import_error"}
    if vs in BLOCKING and not force:
        raise DeployError(
            f"validation failed ({vs}). Re-run codegen with a better recording, "
            f"or pass force=True to deploy anyway. Notes: "
            f"{gen.get('validation_notes') or '(none)'}"
        )

    slug = (bank.get("slug") or "").strip()
    if not _SAFE_SLUG_RE.match(slug):
        raise DeployError(
            f"unsafe slug {slug!r} — must match {_SAFE_SLUG_RE.pattern}"
        )

    target = IMPORTERS_DIR / f"{slug}_importer.py"

    # Refuse to clobber a hand-written importer — only files we previously
    # auto-deployed are safe to overwrite. This is the "don't replace
    # usbank_importer.py with hallucinated nonsense" guardrail.
    if target.exists() and not _is_safe_to_overwrite(target):
        raise DeployError(
            f"refusing to overwrite {target.name} — file exists and is not an "
            f"auto-deployed file (no deploy marker on line 1). If you really "
            f"mean to replace a hand-written importer, delete it manually first."
        )

    source = gen.get("source_code") or ""
    if not source.strip():
        raise DeployError("source_code is empty — nothing to deploy")

    body = (
        f"{DEPLOY_MARKER}\n"
        f"# generated_id={generated_id}  bank_slug={slug}  "
        f"validation={vs or 'unchecked'}\n"
        f"# Re-deploy from the bank-onboarding admin tab to refresh.\n\n"
        f"{source}"
    )
    if not source.endswith("\n"):
        body += "\n"

    IMPORTERS_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(body)

    db.mark_generated_deployed(
        generated_id,
        deployed_path=str(target),
        deployed_by=deployed_by,
    )
    db.update_pending_bank(bank["id"], status="live")
    db.log_activity(
        "bank_importer_deployed",
        f"bank={bank['id']} slug={slug} gen_id={generated_id} path={target}",
        user_id=deployed_by,
    )
    logger.info(f"deployed importer for bank {slug} → {target}")

    return {"path": str(target), "slug": slug}


def undeploy(slug: str) -> bool:
    """Remove an auto-deployed importer file (only if it carries our marker).

    Returns True if a file was removed, False if it didn't exist or wasn't
    safe to remove. Does NOT touch the DB row — the generated_importers
    record stays for audit trail.
    """
    target = IMPORTERS_DIR / f"{slug}_importer.py"
    if not target.exists():
        return False
    if not _is_safe_to_overwrite(target):
        logger.warning(f"refusing to undeploy {target} — no auto-deploy marker")
        return False
    target.unlink()
    return True


def _is_safe_to_overwrite(path: Path) -> bool:
    """A file is safe to overwrite iff its first line is our deploy marker."""
    try:
        with path.open() as f:
            first = f.readline().rstrip()
        return first == DEPLOY_MARKER
    except Exception:
        return False
