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
import re
import shutil
import subprocess
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

# Guard against pathological uploads. A year of statements is well under this.
MAX_STATEMENT_BYTES = 25 * 1024 * 1024
_CONVERT_TIMEOUT_S = 120

# Converters that need no third-party ofxstatement plugin. Listed FIRST in the
# UI because, for this user's banks (US Bank, US Alliance, Capital One, Chime,
# Merrick, Amex, Synchrony, TD), no PyPI plugin exists — the built-in PDF
# statement parser (app.importers.pdf_statement) is the path that actually
# fills March–December 2023.
_BUILTIN_HINTS = {
    "pdf-auto": "PDF statement (any US bank/card) — built-in parser, auto-detects bank vs card",
    "pdf-card": "PDF credit-card statement — built-in parser (charges = money out)",
    "pdf-bank": "PDF bank/checking statement — built-in parser (deposits in, withdrawals out)",
    "ofx":      "Already OFX/QFX/QBO — pass-through (no conversion)",
}
BUILTIN_PLUGINS = tuple(_BUILTIN_HINTS)


def is_builtin(plugin: str) -> bool:
    return (plugin or "").strip() in _BUILTIN_HINTS


def ofxstatement_available() -> bool:
    return shutil.which("ofxstatement") is not None


_PLUGIN_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s{2,}(.*\S)\s*$")


def list_plugins() -> list[dict]:
    """Return available converters as [{name, description}] — built-ins first,
    then whatever `ofxstatement list-plugins` reports.

    Never raises. If the ofxstatement CLI is missing the built-ins are still
    returned (the PDF parser and OFX pass-through don't need it).
    """
    plugins: list[dict] = [{"name": n, "description": d} for n, d in _BUILTIN_HINTS.items()]
    if not ofxstatement_available():
        return plugins
    try:
        out = subprocess.run(
            ["ofxstatement", "list-plugins"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as e:
        logger.warning(f"ofxstatement list-plugins failed: {e!r}")
        return plugins
    stdout = out.stdout or ""
    # "No plugins available. Install plugin eggs or create your own.\nSee https://…"
    if "no plugins available" in stdout.lower():
        return plugins
    seen = {p["name"] for p in plugins}
    for line in stdout.splitlines():
        low = line.strip().lower()
        if not low or low.startswith(("the following", "see ", "http")):
            continue
        m = _PLUGIN_LINE.match(line)
        if not m:
            # tolerate single-space separation: "<name> <desc>"
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or "://" in parts[0] or not re.match(r"^[A-Za-z0-9][\w.\-]*$", parts[0]):
                continue
            name, desc = parts[0], parts[1].strip()
        else:
            name, desc = m.group(1), m.group(2)
        if name not in seen:
            seen.add(name)
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
    if plugin.startswith("pdf-"):
        raise RuntimeError(
            f"{plugin} is the built-in PDF statement parser — use "
            "app.importers.pdf_statement.parse_pdf_statement, not convert_to_ofx"
        )
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
