"""Static + shape validation for LLM-generated bank importer source.

Runs three cheap checks before we accept the output:
  1. Compile: `compile(source, filename, "exec")` — catches syntax errors.
  2. Shape:   parse the AST and assert the module exports the public surface
              (`run_import`, `set_mfa_code`, `SOURCE`) with the right call
              signature for `run_import`.
  3. Imports: every `from app.importers.base_bank_importer import (...)` name
              actually exists in the real module. Catches the "model
              hallucinated a helper" failure mode early.

We deliberately do NOT actually exec/import the generated source — that would
run module-level code from an untrusted LLM. AST inspection is enough.

Returns a 2-tuple: (status, notes_str)
  status ∈ {"pass", "syntax_error", "shape_error", "import_error"}
"""
from __future__ import annotations

import ast
import logging

logger = logging.getLogger(__name__)


REQUIRED_PUBLIC_NAMES = ("run_import", "set_mfa_code", "SOURCE")
REQUIRED_RUN_IMPORT_PARAMS = (
    "username", "password", "years", "consume_path", "entity_slug", "job_id",
)


def validate(source: str) -> tuple[str, str]:
    """Run all checks in order. Stops at the first failing layer."""
    # 1. Syntax / compile
    try:
        tree = ast.parse(source)
        compile(source, "<generated_importer>", "exec")
    except SyntaxError as e:
        return "syntax_error", f"SyntaxError: {e.msg} at line {e.lineno} col {e.offset}"

    # 2. Public-surface shape
    shape_err = _check_shape(tree)
    if shape_err:
        return "shape_error", shape_err

    # 3. Imports against base_bank_importer
    import_err = _check_base_imports(tree)
    if import_err:
        return "import_error", import_err

    return "pass", ""


def _check_shape(tree: ast.Module) -> str:
    """Confirm the module exports the required surface."""
    found_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    found_assigns: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found_funcs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found_assigns.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found_assigns.add(node.target.id)

    missing = []
    for name in REQUIRED_PUBLIC_NAMES:
        if name == "SOURCE":
            if name not in found_assigns:
                missing.append(f"top-level constant {name}")
        else:
            if name not in found_funcs:
                missing.append(f"function {name}()")

    if missing:
        return "missing public surface: " + "; ".join(missing)

    # Verify run_import signature contains the expected parameters
    fn = found_funcs.get("run_import")
    if fn:
        param_names = {a.arg for a in fn.args.args} \
            | {a.arg for a in fn.args.kwonlyargs}
        miss_params = [p for p in REQUIRED_RUN_IMPORT_PARAMS if p not in param_names]
        if miss_params:
            return f"run_import() missing parameters: {miss_params}"

    return ""


def _check_base_imports(tree: ast.Module) -> str:
    """If the source imports from app.importers.base_bank_importer, every
    name imported must exist in the real module."""
    needed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) \
                and node.module == "app.importers.base_bank_importer":
            for alias in node.names:
                if alias.name == "*":
                    return "wildcard imports are disallowed for base_bank_importer"
                needed.add(alias.name)

    if not needed:
        return ""  # nothing to verify

    try:
        from app.importers import base_bank_importer
    except Exception as e:
        return f"could not load base_bank_importer for verification: {e}"

    available = set(dir(base_bank_importer))
    hallucinated = [n for n in sorted(needed) if n not in available]
    if hallucinated:
        return ("imports nonexistent names from base_bank_importer: "
                + ", ".join(hallucinated))
    return ""
