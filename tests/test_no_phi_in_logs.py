"""Phase 15 baseline — fail CI if a logger call interpolates a PHI-shaped name.

Repo convention is ``logger.info("template %s", value)`` — lazy formatting that
the logging library only resolves when the handler is actually enabled, *and*
that keeps PHI out of the format string itself. f-strings inside ``logger.*``
calls bypass that — the value is materialized into the message before any
redaction layer can see it.

This test is intentionally conservative: it allows ``%s`` template formatting
(positional args are still risky if they're raw PHI, but a separate dynamic
redaction layer will handle them later). It only fires on:

  1. Any f-string passed to ``logger.<level>(...)``.
  2. Any ``+`` string-concatenation passed to ``logger.<level>(...)``.

If either pattern is genuinely needed (e.g. a non-PHI debug aid), wrap the
value through a redactor first or add an explicit ``# noqa: PHI-LOG`` marker
to the line and extend ``_ALLOWED_MARKERS`` below.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "docstats"
_LOG_LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_ALLOWED_MARKER = "noqa: PHI-LOG"


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _LOG_LEVELS:
        return False
    # Match `logger.X(...)` or `_logger.X(...)` or `LOG.X(...)` — anything where
    # the receiver name contains "log" (case-insensitive).
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return "log" in receiver.id.lower()
    if isinstance(receiver, ast.Attribute):
        return "log" in receiver.attr.lower()
    return False


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    src_lines = source.splitlines()
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_logger_call(node):
            continue
        line_text = src_lines[node.lineno - 1] if node.lineno - 1 < len(src_lines) else ""
        if _ALLOWED_MARKER in line_text:
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):
                bad.append(f"{path}:{node.lineno}: f-string passed to logger call")
                break
            if (
                isinstance(arg, ast.BinOp)
                and isinstance(arg.op, ast.Add)
                and isinstance(arg.left, ast.Constant)
                and isinstance(arg.left.value, str)
            ):
                bad.append(f"{path}:{node.lineno}: string concatenation in logger call")
                break
    return bad


@pytest.mark.compliance
def test_no_fstrings_or_concat_in_logger_calls():
    violations: list[str] = []
    for path in _SRC_ROOT.rglob("*.py"):
        violations.extend(_violations_in_file(path))
    assert not violations, (
        "Logger calls must use lazy %-formatting so PHI redaction can hook in. "
        "Found:\n  " + "\n  ".join(violations)
    )
