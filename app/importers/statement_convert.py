"""Convert manually-downloaded bank/card statements (CSV/PDF) to OFX via
`ofxstatement`, then hand off to the existing OFX importer.

Why this exists (2026-09-06, 2023 tax-prep push):
  The Playwright bank scrapers all errored in April (US Bank locked, US
  Alliance auth failed) and the LLM proxy key is being reissued. Neither is
  needed here. The user downloads a statement from the bank portal, picks
  the matching ofxstatement plugin, and March–December fill in with ZERO
  LLM calls and ZERO browser automation.

ofxstatement (https://github.com/kedder/ofxstatement) is a plugin-based
converter: ~30 bank plugins on PyPI (`ofxstatement-<bank>`), each knowing
that bank's CSV/PDF layout. Output is standard OFX 1.x/2.x which
app.importers.ofx_importer.parse_ofx already understands.

We shell out to the CLI rather than importing the library so a broken
third-party plugin can't take the Flask worker down with it.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

# Guard against pathological uploads. A year of statements is well under this.
MAX_STATEMENT_BYTES = 25 * 1024 * 1024
_CONVERT_TIMEOUT_S = 120

# Generic fallback plugins that ship with ofxstatement itself and don't need a
# per-bank package. Listed first so the UI offers them even before any bank
# plugin is installed.
_BUILTIN_HINTS = {
    "ofx":      "Already OFX — pass-through (no conversion)",
    "csv":      "Generic CSV (ofxstatement built-in; column mapping via config)",
}


def ofxstatement_available() -> bool:
    return shutil.which("ofxstatement") is not None


def list_plugins() -> list[dict]:
    """Return installed ofxstatement plugins as [{name, description}].

    Uses `ofxstatement list-plugins`. Returns [] (never raises) if the CLI is
    missing, so the UI can show an "install ofxstatement-<bank>" hint instead
    of a 500.
    """
    if not ofxstatement_available():
        return []
    try:
        out = subprocess.run(
            ["ofxstatement", "list-plugins"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as e:
        logger.warning(f"ofxstatement list-plugins failed: {e!r}")
        return []
    plugins: list[dict] = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        # Format is typically:  "<name>  <description>"  — tolerate either spacing
        if not line or line.lower().startswith(("the following", "no plugins", "plugin")):
            continue
        parts = line.split(None, 1)
        name = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else ""
        if name:
            plugins.append({"name": name, "description": desc})
    # Always expose the two built-ins so the dropdown is never empty
    seen = {p["name"] for p in plugins}
    for name, desc in _BUILTIN_HINTS.items():
        if name not in seen:
            plugins.append({"name": name, "description": desc})
    return plugins


def convert_to_ofx(
    data: bytes,
    filename: str,
    plugin: str,
    log=logger.info,
) -> bytes:
    """Run `ofxstatement convert -t <plugin> <in> <out>` and return OFX bytes.

    `plugin="ofx"` short-circuits: the upload is already OFX, return as-is.

    Raises RuntimeError with the CLI's stderr on failure so the caller can
    surface a useful message ("plugin X not installed", "unrecognised CSV
    header", etc.) instead of a bare non-zero exit.
    """
    if not data:
        raise RuntimeError("empty upload")
    if len(data) > MAX_STATEMENT_BYTES:
        raise RuntimeError(f"statement exceeds {MAX_STATEMENT_BYTES // 1_000_000} MB cap")
    plugin = (plugin or "").strip()
    if not plugin:
        raise RuntimeError("plugin required (run list_plugins() to see options)")
    if plugin == "ofx":
        return data
    if not ofxstatement_available():
        raise RuntimeError(
            "ofxstatement CLI not installed in this image — add it to "
            "requirements.txt and rebuild (see 2026-09-06 requirements note)"
        )

    ext = os.path.splitext(filename or "")[1].lower() or ".dat"
    with tempfile.TemporaryDirectory(prefix="ofxconv_") as td:
        src = os.path.join(td, f"in{ext}")
        dst = os.path.join(td, "out.ofx")
        with open(src, "wb") as f:
            f.write(data)
        cmd = ["ofxstatement", "convert", "-t", plugin, src, dst]
        log(f"statement_convert: {' '.join(cmd[:4])} <{filename}>")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_CONVERT_TIMEOUT_S, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ofxstatement timed out after {_CONVERT_TIMEOUT_S}s")
        if proc.returncode != 0 or not os.path.exists(dst):
            err = (proc.stderr or proc.stdout or "").strip()[-600:]
            hint = ""
            if "No plugin" in err or "not found" in err.lower():
                hint = f"  (install with: pip install ofxstatement-{plugin})"
            raise RuntimeError(f"ofxstatement failed (rc={proc.returncode}): {err}{hint}")
        with open(dst, "rb") as f:
            ofx = f.read()
    if not ofx.strip():
        raise RuntimeError("ofxstatement produced an empty OFX file — wrong plugin for this layout?")
    log(f"statement_convert: produced {len(ofx):,} bytes of OFX")
    return ofx
