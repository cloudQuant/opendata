#!/usr/bin/env python3
"""A0.3 codemod: rewrite ``app`` package references to ``opendata``.

Idempotent, dry-run by default (pass ``--apply`` to write). Only the
self-developed tree (``opendata/``, ``tests/``, ``alembic/``, ``scripts/``) and
an explicit allowlist of root build/runtime files are touched. The vendored
``akshare/`` tree and the historical plan under ``docs/迭代计划/`` are never
modified.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOT = Path(__file__).resolve().parents[2]

PY_DIRS = ("opendata", "tests", "alembic", "scripts")
CONFIG_FILES = (
    "Makefile",
    "Dockerfile",
    "start_app.sh",
    "restart_app.sh",
    "dev_start.sh",
    "akshare_web.service",
    ".pre-commit-config.yaml",
)

PY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfrom app\.(?=[A-Za-z_])"), "from opendata."),
    (re.compile(r"\bfrom app import\b"), "from opendata import"),
    (re.compile(r"\bimport app\.(?=[A-Za-z_])"), "import opendata."),
    (re.compile(r"(?<=[\"'])app\.(?=[A-Za-z_])"), "opendata."),
    (re.compile(r"Path\([\"']app[\"']\)"), 'Path("opendata")'),
)

CONFIG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z0-9_.-])app/"), "opendata/"),
    (re.compile(r"\bapp\.(?=[A-Za-z_])"), "opendata."),
)


def apply_patterns(text: str, patterns: Iterable[tuple[re.Pattern[str], str]]) -> str:
    """Apply every pattern substitution in order and return the result."""
    for pattern, repl in patterns:
        text = pattern.sub(repl, text)
    return text


def changed_lines(before: str, after: str) -> int:
    """Count how many lines differ between two texts."""
    return sum(
        1 for old, new in zip(before.splitlines(), after.splitlines(), strict=False) if old != new
    )


def process(path: Path, patterns: tuple[tuple[re.Pattern[str], str], ...], apply: bool) -> int:
    """Rewrite one file, returning the number of changed lines (0 if untouched)."""
    before = path.read_text(encoding="utf-8")
    after = apply_patterns(before, patterns)
    if before == after:
        return 0
    if apply:
        path.write_text(after, encoding="utf-8")
    return changed_lines(before, after)


def python_files() -> list[Path]:
    """Return every Python file in the self-developed tree."""
    files: list[Path] = []
    for dirname in PY_DIRS:
        base = ROOT / dirname
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def main() -> int:
    """Run the rename codemod; dry-run unless ``--apply`` is passed."""
    apply = "--apply" in sys.argv
    tag = "[apply]" if apply else "[dry]"
    total = 0
    targets: list[tuple[Path, tuple[tuple[re.Pattern[str], str], ...]]] = [
        (path, PY_PATTERNS) for path in python_files()
    ]
    targets.extend(
        (ROOT / name, CONFIG_PATTERNS) for name in CONFIG_FILES if (ROOT / name).is_file()
    )
    for path, patterns in targets:
        hits = process(path, patterns, apply)
        if hits:
            total += hits
            print(f"{tag} {path.relative_to(ROOT)} ({hits})")
    print(f"{tag} total changed lines: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
