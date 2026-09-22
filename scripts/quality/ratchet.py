#!/usr/bin/env python3
"""Quality debt ratchet (quality spec §7).

Freezes the current debt of the **A1 legacy layer** and the **B ported layer**
so it can only go down:

* ``ruff`` violations on the self-developed tree (full rule set)
* ``mypy`` errors on the self-developed tree (strict config)
* ``bandit`` findings on the self-developed tree
* ``ruff E/F`` violations on the ported tree (syntax / undefined names only)
* direct ``requests.<verb>()`` call sites in the ported tree — the progressive
  request-layer consolidation indicator

A2 (new / touched) code has **no** snapshot by design: any violation there fails
outright, which is why the counts above are compared, not suppressed.

Fail-closed behaviour:

* a missing tool, missing snapshot or malformed snapshot is a failure;
* any metric above its snapshot is a failure;
* a change in scanned file counts or tool versions is a failure (silent scope
  changes are forbidden) unless ``--force-update`` is passed;
* ``--update`` refuses to write a snapshot that grew any metric.
"""

from __future__ import annotations

import argparse
import ast
import json

# subprocess is only ever called with a literal argv and shell disabled (B404).
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT_PATH = "docs/quality/ratchet.json"
SNAPSHOT_VERSION = 1

SELFDEV_PATHS = ("opendata", "scripts", "tests")
MYPY_PATHS = ("opendata",)
BANDIT_PATHS = ("opendata", "scripts")
PORTED_PATHS = ("akshare",)

HTTP_VERBS = frozenset({"get", "post", "put", "delete", "head", "patch", "request"})

METRIC_NAMES = (
    "ruff_selfdev",
    "mypy_selfdev",
    "bandit_selfdev",
    "ruff_ported",
    "direct_http_ported",
)


@dataclass(frozen=True)
class Snapshot:
    """A previously frozen set of debt counts plus its scan scope.

    Attributes:
        metrics: Metric name to frozen count.
        file_counts: Scanned package to number of files.
        tools: Tool name to reported version string.
    """

    metrics: dict[str, int]
    file_counts: dict[str, int]
    tools: dict[str, str]


class ToolError(RuntimeError):
    """Raised when a required quality tool cannot be executed."""


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv list without a shell and capture its output."""
    # argv is always a literal list built in code (never user input) and shell is
    # disabled, so command injection is not possible here.
    return subprocess.run(  # noqa: S603  # nosec B603
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )


def _tool_version(module: str) -> str:
    result = _run([sys.executable, "-m", module, "--version"])
    output = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not output:
        raise ToolError(f"{module} is not available: {output or 'no output'}")
    return output[0].strip()


def _py_files(package: str) -> list[Path]:
    base = REPO_ROOT / package
    if not base.is_dir():
        return []
    return [p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts]


def file_counts() -> dict[str, int]:
    """Count scanned files per package (scope fingerprint)."""
    counts: dict[str, int] = {}
    for package in (*SELFDEV_PATHS, *PORTED_PATHS):
        counts[package] = len(_py_files(package))
    return counts


def count_ruff(paths: tuple[str, ...], *, select: str | None = None) -> int:
    """Count ruff violations over ``paths``, honouring the project config."""
    args = [sys.executable, "-m", "ruff", "check", "--output-format=json", "--quiet"]
    if select is not None:
        args.extend(["--select", select])
    args.extend(paths)
    result = _run(args)
    if result.returncode not in (0, 1):
        raise ToolError(f"ruff failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError(f"ruff produced unparsable output: {exc}") from exc
    if not isinstance(payload, list):
        raise ToolError("ruff produced unexpected output shape")
    return len(payload)


def count_mypy(paths: tuple[str, ...]) -> int:
    """Count mypy ``error:`` lines over ``paths``."""
    args = [
        sys.executable,
        "-m",
        "mypy",
        "--no-color-output",
        "--no-error-summary",
        *paths,
    ]
    result = _run(args)
    if result.returncode not in (0, 1):
        raise ToolError(f"mypy failed: {result.stderr.strip() or result.stdout.strip()}")
    return sum(1 for line in result.stdout.splitlines() if ": error:" in line)


def count_bandit(paths: tuple[str, ...]) -> int:
    """Count bandit findings over ``paths``."""
    args = [
        sys.executable,
        "-m",
        "bandit",
        "-c",
        "bandit.yaml",
        "-f",
        "json",
        "-q",
        "-r",
        *paths,
    ]
    result = _run(args)
    if result.returncode not in (0, 1):
        raise ToolError(f"bandit failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ToolError(f"bandit produced unparsable output: {exc}") from exc
    if not isinstance(payload, dict) or "results" not in payload:
        raise ToolError("bandit produced unexpected output shape")
    results = payload["results"]
    if not isinstance(results, list):
        raise ToolError("bandit results must be a list")
    return len(results)


def count_direct_http(package: str) -> int:
    """Count ``requests.<verb>(...)`` call sites in a package (AST based)."""
    total = 0
    for path in _py_files(package):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise ToolError(f"{path} does not parse: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in HTTP_VERBS:
                value = func.value
                name = value.id if isinstance(value, ast.Name) else None
                if name == "requests":
                    total += 1
    return total


def measure() -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Measure every metric plus the scope fingerprint."""
    metrics = {
        "ruff_selfdev": count_ruff(SELFDEV_PATHS),
        "mypy_selfdev": count_mypy(MYPY_PATHS),
        "bandit_selfdev": count_bandit(BANDIT_PATHS),
        "ruff_ported": count_ruff(PORTED_PATHS, select="E,F"),
        "direct_http_ported": count_direct_http("akshare"),
    }
    tools = {
        "ruff": _tool_version("ruff"),
        "mypy": _tool_version("mypy"),
        "bandit": _tool_version("bandit"),
    }
    return metrics, file_counts(), tools


def _as_int_dict(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    result: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, int):
            return None
        result[key] = value
    return result


def _as_str_dict(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        result[key] = value
    return result


def load_snapshot() -> Snapshot | None:
    """Load and validate the snapshot, or return ``None`` when unusable."""
    path = REPO_ROOT / SNAPSHOT_PATH
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != SNAPSHOT_VERSION:
        return None
    metrics = _as_int_dict(raw.get("metrics"))
    counts = _as_int_dict(raw.get("file_counts"))
    tools = _as_str_dict(raw.get("tools"))
    if metrics is None or counts is None or tools is None:
        return None
    return Snapshot(metrics=metrics, file_counts=counts, tools=tools)


def _scope_changes(snapshot: Snapshot, counts: dict[str, int], tools: dict[str, str]) -> list[str]:
    """Return scope violations: dropped packages, fewer files, changed tools.

    Adding files to an already-scanned package is normal development and is
    governed by the A2 zero-tolerance gate instead; *losing* files would silently
    shrink the measured range, which is exactly what must not happen.
    """
    changes: list[str] = []
    for package in sorted(set(counts) | set(snapshot.file_counts)):
        old = snapshot.file_counts.get(package)
        new = counts.get(package)
        if old == new:
            continue
        if old is None:
            changes.append(f"package added to scope {package}: (absent) -> {new}")
        elif new is None:
            changes.append(f"package dropped from scope {package}: {old} -> (absent)")
        elif new < old:
            changes.append(f"files removed from {package}: {old} -> {new}")
    for tool in sorted(set(tools) | set(snapshot.tools)):
        old_version = snapshot.tools.get(tool)
        new_version = tools.get(tool)
        if old_version != new_version:
            changes.append(f"tool {tool}: {old_version} -> {new_version}")
    return changes


def check() -> int:
    """Compare current debt against the snapshot; fail on any increase."""
    snapshot = load_snapshot()
    if snapshot is None:
        print(
            f"FAIL: snapshot {SNAPSHOT_PATH} is missing or invalid "
            "(quality spec §0: missing evidence is a failure). Generate it with --update.",
            file=sys.stderr,
        )
        return 1

    try:
        metrics, counts, tools = measure()
    except ToolError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    changes = _scope_changes(snapshot, counts, tools)
    if changes:
        print(
            "FAIL: scan scope changed silently (use --force-update after review):",
            file=sys.stderr,
        )
        for change in changes:
            print(f"  {change}", file=sys.stderr)
        return 1

    regressions = {
        name: (snapshot.metrics.get(name), metrics[name])
        for name in METRIC_NAMES
        if metrics[name] > snapshot.metrics.get(name, 0)
    }
    if regressions:
        print("FAIL: quality debt increased:", file=sys.stderr)
        for name, (old, new) in sorted(regressions.items()):
            print(f"  {name}: {old} -> {new}", file=sys.stderr)
        return 1

    improvements = {
        name: (snapshot.metrics.get(name), metrics[name])
        for name in METRIC_NAMES
        if metrics[name] < snapshot.metrics.get(name, 0)
    }
    print("OK: quality debt did not increase.")
    for name in METRIC_NAMES:
        old = snapshot.metrics.get(name)
        marker = " (improved)" if name in improvements else ""
        print(f"  {name}: {metrics[name]} (snapshot {old}){marker}")
    if improvements:
        print("NOTE: run --update to freeze the new low.")
    return 0


def update(*, force: bool) -> int:
    """Rewrite the snapshot, refusing to grow any metric unless ``force`` is set."""
    try:
        metrics, counts, tools = measure()
    except ToolError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    existing = load_snapshot()
    if existing is not None and not force:
        if _scope_changes(existing, counts, tools):
            print(
                "FAIL: scope changed; pass --force-update after review.",
                file=sys.stderr,
            )
            return 1
        grown = {
            name: (existing.metrics.get(name, 0), metrics[name])
            for name in METRIC_NAMES
            if metrics[name] > existing.metrics.get(name, 0)
        }
        if grown:
            print("FAIL: refusing to grow debt:", file=sys.stderr)
            for name, (old, new) in sorted(grown.items()):
                print(f"  {name}: {old} -> {new}", file=sys.stderr)
            return 1

    payload = {
        "version": SNAPSHOT_VERSION,
        "metrics": metrics,
        "file_counts": counts,
        "tools": tools,
    }
    path = REPO_ROOT / SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {SNAPSHOT_PATH}:")
    for name in METRIC_NAMES:
        print(f"  {name}: {metrics[name]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the ratchet check or snapshot update from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="freeze current counts as the new low",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="allow scope changes or growth (requires review)",
    )
    args = parser.parse_args(argv)

    if args.update or args.force_update:
        return update(force=args.force_update)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
