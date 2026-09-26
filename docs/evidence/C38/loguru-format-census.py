"""Census of loguru call sites that pass arguments a loguru template cannot use.

Judge face for C38: loguru renders a message with ``str.format``, so positional
arguments are substituted by ``{}`` placeholders only. Written in the stdlib
``logging`` idiom (``%s``), the arguments are dropped in silence and the
operator reads the template. ``logging`` formats ``%s`` correctly, so a plain
grep for ``%s`` cannot tell the two families apart - this script resolves the
logger binding of every call site before judging it.

Usage:
    python docs/evidence/C38/loguru-format-census.py [root ...]
    python docs/evidence/C38/loguru-format-census.py --probe

Exits 0 always: it reports readings, and the pass/fail call belongs to the
archive rather than to the measurement. ``--probe`` measures the premise the
classification rests on instead of asserting it.
"""

from __future__ import annotations

import ast
import inspect
import io
import logging
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Final

import loguru
from loguru import logger

#: What the probe logs, in the two idioms plus the placeholder-free one.
PROBE_CASES: Final = (
    ("%s args on a loguru logger", "Background download failed for execution %s: %s"),
    ("{} args on a loguru logger", "Background download failed for execution {}: {}"),
    ("args with no placeholder", "no placeholder at all"),
)
#: The mirror image, on the other family - measured so the contrast is a reading.
STDLIB_PROBE_CASES: Final = (
    ("stdlib logging, %s idiom", "stdlib logging keeps %s: %s"),
    ("stdlib logging, {} template", "stdlib logging keeps {}: {}"),
)
#: Frames of ``logging``'s error report; their text is interpreter-dependent.
IGNORED_STDERR_LINES: Final = (
    "--- Logging error ---",
    "Traceback (most recent call last):",
    "Call stack:",
)


def probe() -> int:
    """Report what loguru does to each template style, measured here.

    The census classifies ``%s`` as a defect only on a loguru logger, so that
    premise is measured rather than quoted: loguru applies the arguments with
    ``str.format``, which a ``%s`` template has no slot for, while ``logging``
    applies them with ``%`` - the same call is a defect in one family and
    idiomatic in the other. Both directions are measured, including the way each
    family fails: loguru drops the arguments in silence, logging raises inside
    ``emit`` and swallows it in ``handleError``.

    Returns:
        0 - the probe reports.
    """
    captured: list[str] = []
    # loguru installs a stderr sink at import time. It would echo these records
    # with a wall-clock timestamp, so the reading below could not be diffed
    # across runs; the probe owns the sink for the duration of the measurement.
    logger.remove()
    handler_id = logger.add(
        lambda message: captured.append(str(message.record["message"])), format="{message}"
    )
    try:
        for _label, template in PROBE_CASES:
            logger.error(template, 7, RuntimeError("boom"))
    finally:
        logger.remove(handler_id)
        logger.add(sys.stderr)

    source = inspect.getsourcefile(inspect.getmodule(logger) or loguru)
    if source is None:
        raise SystemExit("probe: cannot locate the loguru source file")
    applied = next(
        (
            f"{number}: {line.strip()}"
            for number, line in enumerate(Path(source).read_text(encoding="utf-8").splitlines(), 1)
            if ".format(*args, **kwargs)" in line
        ),
        "<no .format(*args, **kwargs) line found>",
    )
    print(f"loguru {version('loguru')}  arguments are applied at {source}")
    print(f"    {applied}")
    print("  what a handler actually receives, per template:")
    for (label, _template), message in zip(PROBE_CASES, captured, strict=True):
        print(f"    {label:32} -> {message!r}")

    stdlib = logging.getLogger("census-probe")
    for label, template in STDLIB_PROBE_CASES:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        stdlib.addHandler(handler)
        # logging swallows a formatting failure and reports it on stderr. That
        # report is captured too, so "the other family fails loudly" is a reading
        # here rather than a sentence about how logging is supposed to work.
        stderr_box = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = stderr_box
        try:
            stdlib.error(template, 7, RuntimeError("boom"))
        finally:
            sys.stderr = real_stderr
            stdlib.removeHandler(handler)
        reached = stream.getvalue().strip() or "<nothing reached the handler>"
        print(f"    {label:32} -> {reached!r}")
        for line in stderr_box.getvalue().splitlines():
            # Keep the report's own lines (which call failed, with what arguments)
            # and drop the frame dump: the traceback text varies with CPython.
            if line and not line[0].isspace() and not line.startswith(IGNORED_STDERR_LINES):
                print(f"    {'(logging reported on stderr)':32} -> {line!r}")
    return 0


#: Method names that take a message template on both logger families.
LEVELS: Final = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "critical",
        "fatal",
        "exception",
        "log",
        "trace",
        "success",
        "notice",
    }
)
#: Conversion specifiers recognised by ``logging``'s %-formatting.
PERCENT_SPECS: Final = ("%%", "%s", "%d", "%r", "%a", "%f", "%e", "%x", "%o")
#: Name shapes treated as a logger even when this file cannot resolve them.
LOGGER_NAMES: Final = ("logger", "log", "LOG")


def bindings_of(tree: ast.Module) -> dict[str, str]:
    """Map every logger name a module exposes to its family.

    Args:
        tree: Parsed module.

    Returns:
        Name -> ``loguru`` / ``stdlib``, for imports and module-level
        assignments alike. Names this function cannot judge are absent, and
        the caller reports them as unresolved instead of skipping them.
    """
    origins: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                origins[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                origins[item.asname or item.name] = f"{node.module or ''}.{item.name}"

    bindings: dict[str, str] = {}
    for name, origin in origins.items():
        if origin == "loguru.logger":
            bindings[name] = "loguru"
        elif origin.startswith("logging."):
            bindings[name] = "stdlib"
    for node in tree.body:
        for target, value in _module_assignments(node):
            family = _family_of(value, origins)
            if family is not None:
                bindings[target] = family
    return bindings


def _module_assignments(node: ast.stmt) -> list[tuple[str, ast.expr]]:
    """Return ``(name, value)`` pairs a module-level statement binds."""
    if isinstance(node, ast.Assign):
        return [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
        return [(node.target.id, node.value)]
    return []


def _family_of(value: ast.expr, origins: dict[str, str]) -> str | None:
    """Judge the family of ``x = <factory>()`` at module level."""
    if not isinstance(value, ast.Call):
        return None
    rendered = ast.unparse(value.func)
    origin = origins.get(rendered, rendered)
    if origin.endswith(".getLogger") or rendered.endswith("getLogger"):
        return "stdlib" if origin.startswith("logging") or rendered == "getLogger" else "loguru"
    if origin == "loguru.logger":
        return "loguru"
    if origin.startswith("logging."):
        return "stdlib"
    if rendered.startswith("logging."):
        return "stdlib"
    return None


def _verdict(template: str, family: str) -> str:
    """Decide whether the call's arguments survive rendering.

    Args:
        template: The message the call passes.
        family: ``loguru``, ``stdlib`` or ``unresolved``.

    Returns:
        ``ok (...)`` or ``DROPPED (...)`` with the reason.
    """
    has_brace = "{" in template
    has_percent = any(spec in template for spec in PERCENT_SPECS)
    if family == "loguru":
        if has_brace:
            return "ok (loguru braces)"
        reason = "%s args on a loguru logger" if has_percent else "no placeholder for the args"
        return f"DROPPED ({reason})"
    if family == "stdlib":
        return "ok (logging percent)" if has_percent else "DROPPED (no % spec for the args)"
    return "UNRESOLVED (binding not judged)"


def call_sites(source: str) -> list[tuple[int, str, str, str, str]]:
    """Find every levelled call in a module that passes arguments.

    Args:
        source: Module contents.

    Returns:
        ``(line, logger, level, template, verdict)`` rows.
    """
    tree = ast.parse(source)
    bindings = bindings_of(tree)
    rows: list[tuple[int, str, str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        level = node.func.attr
        name = node.func.value.id
        if level not in LEVELS:
            continue
        family = bindings.get(name)
        if family is None:
            if not (name in LOGGER_NAMES or name.rstrip("_").endswith("logger")):
                continue
            family = "unresolved"
        index = 1 if level == "log" else 0
        if len(node.args) <= index:
            continue
        template_node = node.args[index]
        if not isinstance(template_node, ast.Constant):
            continue
        template = template_node.value
        if not isinstance(template, str):
            continue
        if len(node.args) - index - 1 + len(node.keywords) <= 0:
            continue
        rows.append((node.lineno, name, level, template, _verdict(template, family)))
    return rows


def main(roots: list[str]) -> int:
    """Print every call site whose arguments never reach the log.

    Args:
        roots: Directories to walk.

    Returns:
        0 - the script only reports.
    """
    total = 0
    bad: list[str] = []
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" in path.parts or "opendata_http" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for line, name, level, template, verdict in call_sites(source):
                total += 1
                if verdict.startswith("ok"):
                    continue
                short = template if len(template) <= 72 else f"{template[:69]}..."
                bad.append(f"{path}:{line} {name}.{level}: {verdict}\n    template={short!r}")
    for row in bad:
        print(row)
    print(f"\ncensus: calls-with-args={total} arguments-never-render={len(bad)}")
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments[:1] == ["--probe"]:
        raise SystemExit(probe())
    raise SystemExit(main(arguments or ["opendata", "scripts"]))
