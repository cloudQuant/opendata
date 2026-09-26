#!/usr/bin/env python3
"""Check whether the arguments a logger call passes actually reach the log.

The tree contains both logger families, and they fail in opposite directions:

* ``loguru`` renders the message with ``str.format``, so a ``%s`` template plus
  arguments drops the arguments in silence - the operator reads the template.
* ``logging`` renders with ``%``, so a ``{}`` template raises inside ``emit`` and
  ``handleError`` swallows the report onto stderr.

A grep for ``%s`` therefore cannot judge a call: the receiver's family has to be
resolved first. This script resolves it and reports every call whose arguments
never render, exiting non-zero so the gate refuses a regression instead of
printing a reading somebody has to remember to look at.

Faces judged, covering the two blind spots the C38 census registered:

* bare-name receivers - ``logger.debug(...)``;
* attribute receivers - ``self.logger.debug(...)``, resolved through the
  enclosing class's ``self.<attr> = ...`` assignment.

A receiver whose family is not settled where it is defined - the injected
``self.logger = logger or _default_logger`` shape, stdlib on one side and loguru
on the other - is judged ``ambiguous``: no placeholder style is correct there, so
passing a template plus arguments is a finding and the message has to be
interpolated by the caller instead. Unresolved receivers, and calls whose
template is built at runtime, are counted and printed rather than judged, so the
size of the face this file cannot read stays visible. An interpolated message
that passes no arguments is not one of those faces: it has no arguments to lose.

Usage:
    python scripts/quality/loguru_render_check.py            # gate mode
    python scripts/quality/loguru_render_check.py --report   # readings only
    python scripts/quality/loguru_render_check.py --probe    # the premise, measured
"""

from __future__ import annotations

import argparse
import ast
import inspect
import io
import logging
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Final

import loguru
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories swept; printed with the reading so a shrink of the face is visible.
CHECK_ROOTS: Final = (
    "opendata",
    "opendata_http",
    "opendata_fuyao",
    "opendata_client",
    "scripts",
    "tests",
)
#: Build and dependency trees: generated or installed, not code this gate owns.
SKIP_DIR_PARTS: Final = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "htmlcov",
        "logs",
        "node_modules",
        "venv",
    }
)
#: Levelled methods that take a message template on both families.
LEVELS: Final = frozenset(
    {
        "critical",
        "debug",
        "error",
        "exception",
        "fatal",
        "info",
        "log",
        "notice",
        "success",
        "trace",
        "warn",
        "warning",
    }
)
#: Conversion specifiers recognised by ``logging``'s %-formatting.
PERCENT_SPECS: Final = ("%%", "%d", "%r", "%a", "%f", "%e", "%x", "%o", "%s")
#: Name shapes treated as a logger even when this file cannot resolve them.
LOGGER_NAME_HINTS: Final = ("logger", "log")

LOGURU: Final = "loguru"
STDLIB: Final = "stdlib"
AMBIGUOUS: Final = "ambiguous"
UNRESOLVED: Final = "unresolved"

#: What ``--probe`` logs, in the two idioms plus the placeholder-free one.
PROBE_CASES: Final = (
    ("%s args on a loguru logger", "diff report unavailable for execution %s: %s"),
    ("{} args on a loguru logger", "diff report unavailable for execution {}: {}"),
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


@dataclass(frozen=True)
class Site:
    """One logger call that passes arguments alongside a template.

    Attributes:
        file: Repo-relative POSIX path.
        line: 1-based line of the call.
        receiver: Source text of the object called on, e.g. ``self.logger``.
        level: Method name.
        family: ``loguru`` / ``stdlib`` / ``ambiguous`` / ``unresolved``.
        template: The message the call passes, or its source text when that is
            not a literal.
        literal: Whether ``template`` is the string the call passes. A template
            built at runtime cannot be judged here, and says so rather than
            being left out of the reading.
    """

    file: str
    line: int
    receiver: str
    level: str
    family: str
    template: str
    literal: bool = True

    @property
    def where(self) -> str:
        """``path:line`` for the call."""
        return f"{self.file}:{self.line}"

    @property
    def short_template(self) -> str:
        """The template, clipped to one readable line."""
        return self.template if len(self.template) <= 72 else f"{self.template[:69]}..."

    @property
    def why_unjudged(self) -> str | None:
        """Why this call is reported but not judged, or ``None`` if it is judged."""
        if self.family == UNRESOLVED:
            return "receiver binding not judged"
        if not self.literal:
            return "template built at runtime"
        return None


@dataclass(frozen=True)
class Sweep:
    """What a sweep judged, and the face it judged.

    Attributes:
        findings: Calls whose arguments never render.
        unjudged: Logger-shaped receivers whose binding this script cannot resolve.
        calls_with_args: Total calls that pass arguments alongside a template.
        per_root: ``.py`` files swept per declared root - ``0`` means the declared
            scope no longer matches the tree, which is a shrink of the face.
    """

    findings: list[Site]
    unjudged: list[Site]
    calls_with_args: int
    per_root: dict[str, int]


def probe() -> int:
    """Report what each family does to each template style, measured here.

    The whole classification rests on this premise, so all four directions are
    measured instead of quoted - including the asymmetry that lets the defect
    survive: loguru fails silently, ``logging`` fails loudly on stderr.

    Destructive by design: loguru has one global logger, so the probe removes its
    sinks and restores a plain stderr one. That is why the test suite asserts the
    rendering through a temporary file sink instead of calling this.

    Returns:
        ``0`` - the probe reports.
    """
    captured: list[str] = []
    # loguru installs a stderr sink at import time; it would echo these records
    # with a wall-clock timestamp, so the reading could not be diffed across runs.
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

    stdlib = logging.getLogger("loguru-render-check-probe")
    for label, template in STDLIB_PROBE_CASES:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        stdlib.addHandler(handler)
        # logging swallows a formatting failure and reports it on stderr; that
        # report is captured too, so "the other family fails loudly" is a reading.
        stderr_box = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = stderr_box
        try:
            stdlib.error(template, 7, RuntimeError("boom"))
        finally:
            sys.stderr = real_stderr
            stdlib.removeHandler(handler)
        print(
            f"    {label:32} -> {(stream.getvalue().strip() or '<nothing reached the handler>')!r}"
        )
        for line in stderr_box.getvalue().splitlines():
            if line and not line[0].isspace() and not line.startswith(IGNORED_STDERR_LINES):
                print(f"    {'(logging reported on stderr)':32} -> {line!r}")
    return 0


def _is_logger_name(name: str) -> bool:
    """Whether a bound name or attribute looks like a logger."""
    stripped = name.strip("_")
    return stripped in LOGGER_NAME_HINTS or stripped.endswith("logger")


def _origins_of(tree: ast.AST) -> dict[str, str]:
    """Map every imported name visible in a tree to the dotted path it came from."""
    origins: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                origins[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                origins[item.asname or item.name] = f"{node.module or ''}.{item.name}"
    return origins


def _family_of_name(name: str, origins: dict[str, str], hints: dict[str, str]) -> str | None:
    """Judge a name from an annotation hint of its scope, else from its import."""
    if name in hints:
        return hints[name]
    if name.startswith("logging."):
        return STDLIB
    if name.startswith("loguru."):
        return LOGURU
    origin = origins.get(name)
    if origin == "loguru" or origin == "loguru.logger":
        return LOGURU
    if origin is not None and origin.startswith("logging."):
        return STDLIB
    return None


def _family_of(
    expr: ast.expr, origins: dict[str, str], hints: dict[str, str] | None = None
) -> str | None:
    """Judge the logger family an expression evaluates to.

    Args:
        expr: Right-hand side of an assignment, or one side of an ``or`` chain.
        origins: Imported name -> dotted path.
        hints: Name -> family, from parameter annotations of the enclosing scope.

    Returns:
        ``loguru``, ``stdlib``, ``ambiguous``, or ``None`` when not judgeable.
    """
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
        known = {
            family
            for family in (_family_of(v, origins, hints) for v in expr.values)
            if family is not None
        }
        if len(known) > 1:
            return AMBIGUOUS
        return next(iter(known)) if known else None
    if isinstance(expr, ast.Name):
        return _family_of_name(expr.id, origins, hints or {})
    if not isinstance(expr, ast.Call):
        return None
    rendered = ast.unparse(expr.func)
    if rendered == "getLogger" or rendered.endswith(".getLogger"):
        origin = origins.get(rendered, rendered)
        return STDLIB if origin.startswith("logging") else LOGURU
    if rendered.endswith("logger"):
        return _family_of_name(rendered.rsplit(".", 1)[0], origins, hints or {})
    return _family_of_name(rendered, origins, hints or {})


def _name_bindings(node: ast.AST, origins: dict[str, str]) -> dict[str, str]:
    """Collect ``name = <logger expression>`` assignments visible under ``node``."""
    found: dict[str, str] = {}
    for child in ast.walk(node):
        pairs: list[tuple[str, ast.expr]] = []
        if isinstance(child, ast.Assign):
            pairs = [(t.id, child.value) for t in child.targets if isinstance(t, ast.Name)]
        elif (
            isinstance(child, ast.AnnAssign)
            and isinstance(child.target, ast.Name)
            and child.value is not None
        ):
            pairs = [(child.target.id, child.value)]
        for name, value in pairs:
            family = _family_of(value, origins)
            if family is not None and name not in found:
                found[name] = family
    return found


def _parameter_hints(node: ast.AST) -> dict[str, str]:
    """Read annotations such as ``logger: logging.Logger | None = None``."""
    hints: dict[str, str] = {}
    for child in ast.walk(node):
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = [*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs]
        for arg in arguments:
            if arg.annotation is None:
                continue
            rendered = ast.unparse(arg.annotation)
            if "logging.Logger" in rendered or rendered.endswith("Logger"):
                hints.setdefault(arg.arg, STDLIB)
            elif "loguru" in rendered:
                hints.setdefault(arg.arg, LOGURU)
    return hints


def _self_attributes(node: ast.AST, origins: dict[str, str]) -> dict[str, str]:
    """Collect ``self.<attr> = <logger expression>`` assignments in a class body."""
    hints = _parameter_hints(node)
    found: dict[str, str] = {}
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for target in child.targets:
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and _is_logger_name(target.attr)
            ):
                continue
            family = _family_of(child.value, origins, hints)
            if family is not None:
                found.setdefault(target.attr, family)
    return found


def _receiver_and_family(
    call: ast.Call, names: dict[str, str], attributes: dict[str, str], origins: dict[str, str]
) -> tuple[str, str] | None:
    """Resolve what a levelled call is invoked on, and that object's family.

    Args:
        call: The judged call.
        names: Logger-name bindings in effect at the call.
        attributes: ``self.<attr>`` bindings of the enclosing class.
        origins: Imported names of the module.

    Returns:
        ``(receiver text, family)``, or ``None`` if this is not a logger call.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in LEVELS:
        return None
    receiver_node = func.value
    if isinstance(receiver_node, ast.Name):
        name = receiver_node.id
        family = names.get(name) or _family_of_name(name, origins, names)
        if family is not None:
            return name, family
        return (name, UNRESOLVED) if _is_logger_name(name) else None
    if isinstance(receiver_node, ast.Attribute) and _is_logger_name(receiver_node.attr):
        base = receiver_node.value
        family = UNRESOLVED
        if isinstance(base, ast.Name) and base.id == "self":
            family = attributes.get(receiver_node.attr, UNRESOLVED)
        return ast.unparse(receiver_node), family
    return None


def _site_of(
    call: ast.Call,
    rel: str,
    names: dict[str, str],
    attributes: dict[str, str],
    origins: dict[str, str],
) -> Site | None:
    """Judge one call: a string template plus arguments that never render."""
    resolved = _receiver_and_family(call, names, attributes, origins)
    if resolved is None or not isinstance(call.func, ast.Attribute):
        return None
    receiver, family = resolved
    level = call.func.attr
    index = 1 if level == "log" else 0
    if len(call.args) <= index:
        return None
    template_node = call.args[index]
    if isinstance(template_node, ast.Constant):
        if not isinstance(template_node.value, str):
            return None
        template, literal = template_node.value, True
    else:
        # An interpolated (f-string) message is finished before either renderer
        # sees it, so it carries no arguments to lose and the check below lets it
        # through. A message built at runtime - including an f-string that *does*
        # pass arguments - is disclosed rather than judged.
        template, literal = ast.unparse(template_node), False
    if len(call.args) - index - 1 + len(call.keywords) <= 0:
        return None
    return Site(rel, call.lineno, receiver, level, family, template, literal)


def sites_of(source: str, rel: str = "<source>") -> list[Site]:
    """Find every levelled call in a module that passes arguments with a template.

    Args:
        source: Module contents.
        rel: Path to report on the rows.

    Returns:
        Rows sorted by line, judged rows and ``UNRESOLVED`` rows alike.
    """
    tree = ast.parse(source)
    origins = _origins_of(tree)
    rows: list[Site] = []

    def walk(node: ast.AST, names: dict[str, str], attributes: dict[str, str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(
                    child,
                    {**names, **_name_bindings(child, origins)},
                    {**attributes, **_self_attributes(child, origins)},
                )
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                walk(
                    child,
                    {**names, **_parameter_hints(child), **_name_bindings(child, origins)},
                    attributes,
                )
            else:
                if isinstance(child, ast.Call):
                    site = _site_of(child, rel, names, attributes, origins)
                    if site is not None:
                        rows.append(site)
                walk(child, names, attributes)

    walk(tree, _name_bindings(tree, origins), {})
    return sorted(rows, key=lambda row: row.line)


def verdict_of(site: Site) -> str | None:
    """Reason the call's arguments never reach the log, or ``None`` if they do.

    Args:
        site: The judged call.

    Returns:
        A reason string, or ``None`` when the template substitutes its arguments.
    """
    if not site.literal:
        return None
    has_brace = "{" in site.template
    has_percent = any(spec in site.template for spec in PERCENT_SPECS)
    if site.family == LOGURU:
        if has_brace:
            return None
        return "%s args on a loguru logger" if has_percent else "no placeholder for the args"
    if site.family == STDLIB:
        if has_percent:
            return None
        return "{} args on a logging logger" if has_brace else "no % spec for the args"
    if site.family == AMBIGUOUS:
        return "receiver family not settled: interpolate the message at the call site"
    return None


def scan(roots: tuple[str, ...] = CHECK_ROOTS) -> Sweep:
    """Sweep the tree.

    Args:
        roots: Directories to walk, relative to the repo root.

    Returns:
        Findings and the face they were judged over, sorted by path then line.
    """
    findings: list[Site] = []
    unjudged: list[Site] = []
    total = 0
    per_root: dict[str, int] = {}
    for root in roots:
        base = REPO_ROOT / root
        files = sorted(base.rglob("*.py")) if base.is_dir() else []
        swept = 0
        for path in files:
            if any(part in SKIP_DIR_PARTS for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            swept += 1
            try:
                rows = sites_of(source, path.relative_to(REPO_ROOT).as_posix())
            except SyntaxError:
                continue
            total += len(rows)
            for site in rows:
                if site.why_unjudged:
                    unjudged.append(site)
                elif verdict_of(site) is not None:
                    findings.append(site)
        per_root[root] = swept
    return Sweep(
        findings=sorted(findings, key=lambda s: (s.file, s.line)),
        unjudged=sorted(unjudged, key=lambda s: (s.file, s.line)),
        calls_with_args=total,
        per_root=per_root,
    )


def run(argv: list[str] | None = None) -> int:
    """Report the findings and decide the exit code.

    Args:
        argv: Command line arguments after the program name.

    Returns:
        ``0`` when nothing is dropped - or always, under ``--report`` - else ``1``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", action="store_true", help="print the readings without failing the caller"
    )
    parser.add_argument("--probe", action="store_true", help="measure the premise instead")
    args = parser.parse_args(argv)
    if args.probe:
        return probe()

    sweep = scan()
    for site in sweep.findings:
        reason = verdict_of(site)
        print(f"FINDING {site.where} {site.receiver}.{site.level}: {reason}")
        print(f"    family={site.family}  template={site.short_template!r}")
    for site in sweep.unjudged:
        reason = site.why_unjudged or "not judged"
        print(f"note    {site.where} {site.receiver}.{site.level}: {reason}")
    empty = [root for root, files in sweep.per_root.items() if files == 0]
    for root, files in sweep.per_root.items():
        print(f"swept   {root}: {files} file(s)" + ("  <- SCOPE_EMPTY" if files == 0 else ""))
    print(
        f"\nloguru-render-check: calls-with-args={sweep.calls_with_args}"
        f" findings={len(sweep.findings)} unjudged={len(sweep.unjudged)}"
    )
    if empty:
        print(f"FAIL: declared scope matches no file: {' '.join(empty)}", file=sys.stderr)
        return 1
    if sweep.findings and not args.report:
        print("FAIL: the logger arguments above never reach the log.", file=sys.stderr)
        return 1
    print("OK: every logger call passes arguments its renderer can substitute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
