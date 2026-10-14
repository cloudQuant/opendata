#!/usr/bin/env python3
"""Widen stale offline seam fakes so they accept the ``transport=`` keyword.

This is the repair step for ``stale_fake_census.py``: it imports that census and uses
its judgment (nothing is re-derived here), then inserts ``**_kwargs`` into the
parameter list of each fake the census charges as stale. Edits are made by character
offset, so a signature spanning several lines or carrying parenthesized annotations is
widened at the right character rather than by string guessing.

The insert text depends on what already precedes the parameter list terminator: a
trailing comma (the exploded-signature style this repo uses) needs only the new
parameter on its own line, while a tight ``def f(a, b)`` needs the comma too. Every
staged file is re-parsed before anything is written, so a mis-planned edit cannot leave
a half-repaired tree.

Default is a dry run that prints the planned insertions. ``--apply`` writes them.

``--arm`` proves the repair can differ from doing nothing: every stale seed the census
flags must raise ``TypeError`` on a ``transport=`` call before repair, must not raise
after it, and the census must then report zero stale sites for the repaired text. The
seeds cover both signature layouts, because the insert text is what is under test.
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tempfile
import tokenize
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stale_fake_census as census  # noqa: E402  (judge shared with the census)

REPO = census.REPO
PARAM = "**_kwargs"


def _label(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.name


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _token_at(tokens: list[tokenize.TokenInfo], row: int, col: int) -> int:
    for index, token in enumerate(tokens):
        if token.start == (row, col):
            return index
    raise AssertionError(f"no token starts at {row}:{col}")


def _closing_offset(tokens: list[tokenize.TokenInfo], source: str, node: ast.AST) -> int:
    """Return the offset of the parameter list's terminator for one callable node."""
    starts = _line_starts(source)
    index = _token_at(tokens, node.lineno, node.col_offset)
    is_def = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    depth = 0
    for token in tokens[index:]:
        if token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
                continue
            if token.string in ")]}":
                depth -= 1
            if depth == 0:
                if is_def and token.string == ")":
                    return starts[token.start[0] - 1] + token.start[1]
                if not is_def and token.string == ":":
                    return starts[token.start[0] - 1] + token.start[1]
        if token.type == tokenize.ENDMARKER:
            break
    raise AssertionError(f"cannot locate the parameter terminator for a {type(node).__name__}")


def _insertion(source: str, terminator: int) -> tuple[int, str]:
    """Return (offset, text) that appends one keyword parameter before ``terminator``."""
    head = source[:terminator].rstrip()
    gap = source[len(head) : terminator]
    last = head[-1] if head else ""
    if last == "(":
        return len(head), PARAM
    if last != ",":
        return len(head), f", {PARAM}"
    if "\n" not in gap:
        return len(head), PARAM
    line = head[head.rfind("\n") + 1 :]
    indent = line[: len(line) - len(line.lstrip())]
    return len(head), f"\n{indent}{PARAM},"


def plan(site: dict[str, Any]) -> tuple[Path, int, str]:
    """Return (file, insertion offset, insertion text) for one stale site."""
    path: Path = site["path"]
    source = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    terminator = _closing_offset(tokens, source, site["widen"])
    offset, text = _insertion(source, terminator)
    return path, offset, text


def _stage(sites: list[dict[str, Any]]) -> tuple[dict[Path, dict[str, int]], list[str]]:
    """Compute every edit and re-parse each result; return (edits, rejected labels)."""
    unique: dict[tuple[str, int, int], dict[str, Any]] = {}
    for site in sites:
        node = site["widen"]
        unique[(str(site["path"]), node.lineno, node.col_offset)] = site
    staged: dict[Path, dict[str, int]] = {}
    rejected: list[str] = []
    by_file: dict[Path, list[tuple[int, str]]] = {}
    for site in unique.values():
        path, offset, text = plan(site)
        edits = by_file.setdefault(path, [])
        if (offset, text) not in edits:
            edits.append((offset, text))
    for path, edits in sorted(by_file.items()):
        source = path.read_text(encoding="utf-8")
        edited = source
        for offset, text in sorted(set(edits), reverse=True):
            edited = f"{edited[:offset]}{text}{edited[offset:]}"
        try:
            ast.parse(edited)
        except SyntaxError as exc:
            rejected.append(f"{_label(path)} line={exc.lineno} {exc.msg}")
            continue
        staged[path] = {
            "insertions": len(set(edits)),
            "before_bytes": len(source),
            "after_bytes": len(edited),
            "text": edited,
        }
    return staged, rejected


def repair_files(
    sites: list[dict[str, Any]], *, apply: bool
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Widen every stale site; write only if all staged files re-parse."""
    staged, rejected = _stage(sites)
    growth = {
        _label(path): {key: value for key, value in stats.items() if key != "text"}
        for path, stats in staged.items()
    }
    if apply:
        for path, stats in staged.items():
            path.write_text(stats["text"], encoding="utf-8")
    return growth, rejected


def _measure(tests_dir: Path) -> dict[str, Any]:
    modules = census._module_index(REPO / "opendata")
    seams, visible, module_visible = census.product_seams(REPO / "opendata", modules)
    return census.scan_tests(tests_dir, seams, visible, module_visible)


def _exploded_source(stale: bool) -> str:
    params = "    url,\n    params,\n    timeout,\n" if stale else (
        "    url,\n    params,\n    timeout,\n    **kwargs,\n"
    )
    return (
        f"def fake(\n{params}):\n    return 200, 'ok'\n\n\n"
        "def test_case(monkeypatch):\n"
        '    monkeypatch.setattr(\n        "opendata.data.providers.fred.models._client'
        '._http_get", fake)\n'
    )


def _seed_source(shape: str) -> str:
    return _exploded_source(True) if shape == "exploded" else census._arm_source(shape, True)


REPAIR_SHAPES = (*census.ARM_SHAPES, "exploded")


class _Recorder:
    """Stand in for ``monkeypatch`` and keep the callable each site installs."""

    def __init__(self) -> None:
        self.installed: list[object] = []

    def setattr(self, target: object, attr: object, value: object = None) -> None:
        self.installed.append(attr if value is None else value)


def _installed(shape: str, source: str) -> object:
    """Return the callable the seed's patch site installs, not a name looked up."""
    namespace: dict[str, Any] = {}
    exec(compile(source, f"<seed-{shape}>", "exec"), namespace)  # noqa: S102 (arm harness)
    recorder = _Recorder()
    namespace["test_case"](recorder)
    if not recorder.installed:
        raise AssertionError(f"seed {shape} installed nothing")
    return recorder.installed[-1]


def _call_seed(shape: str, source: str) -> str:
    """Call the installed fake the way the product calls a seam."""
    target = _installed(shape, source)
    if shape == "reexport":
        return repr(target("query", timeout=None, transport=None))
    return repr(target("https://example.test", {"a": "b"}, 1.0, transport=None))


def _typeerror(source: str, shape: str) -> bool:
    try:
        _call_seed(shape, source)
    except TypeError:
        return True
    return False


def _bites(files: dict[str, Path], shapes: tuple[str, ...]) -> int:
    return sum(_typeerror(files[shape].read_text(encoding="utf-8"), shape) for shape in shapes)


def _arm() -> int:
    with tempfile.TemporaryDirectory(prefix="fake-repair-arm-") as tmp:
        tests = Path(tmp)
        files: dict[str, Path] = {}
        for shape in REPAIR_SHAPES:
            path = tests.joinpath(f"test_zz_{shape}.py")
            path.write_text(_seed_source(shape), encoding="utf-8")
            files[shape] = path
        before = _measure(tests)
        pre = _bites(files, REPAIR_SHAPES)
        exploded_before = files["exploded"].read_text(encoding="utf-8")
        growth, rejected = repair_files(before["stale"], apply=True)
        after = _measure(tests)
        post = _bites(files, REPAIR_SHAPES)
        exploded_after = files["exploded"].read_text(encoding="utf-8")
    re_parsed = True
    try:
        ast.parse(exploded_after)
    except SyntaxError:
        re_parsed = False
    exploded_widened = exploded_after.count(PARAM) == 1 and exploded_after != exploded_before
    bites = (
        len(before["stale"]) == len(REPAIR_SHAPES)
        and pre == len(REPAIR_SHAPES)
        and after["stale"] == []
        and post == 0
        and len(growth) == len(REPAIR_SHAPES)
        and not rejected
        and re_parsed
        and exploded_widened
    )
    print(
        f"arm: seeds={len(REPAIR_SHAPES)} flagged={len(before['stale'])} "
        f"pre_typeerrors={pre}"
    )
    print(
        f"arm: post_stale={len(after['stale'])} post_typeerrors={post} "
        f"files={len(growth)} rejected={len(rejected)}"
    )
    print(f"arm: exploded_widened={exploded_widened} -> {'PASS' if bites else 'FAIL'}")
    return 0 if bites else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="run the falsification arm only")
    parser.add_argument("--apply", action="store_true", help="write the planned insertions")
    args = parser.parse_args(argv)
    if args.arm:
        return _arm()

    stale = _measure(REPO / "tests")["stale"]
    growth, rejected = repair_files(stale, apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    insertions = sum(stats["insertions"] for stats in growth.values())
    print(
        f"face: stale_sites={len(stale)} files={len(growth)} insertions={insertions} "
        f"rejected={len(rejected)} mode={mode}"
    )
    for name, stats in sorted(growth.items()):
        print(
            f"edit: {name} insertions={stats['insertions']} "
            f"bytes={stats['before_bytes']}->{stats['after_bytes']}"
        )
    for label in rejected:
        print(f"rejected: {label}")
    if rejected:
        print("face: nothing was written; every stale site must plan cleanly")
        return 1
    if args.apply:
        recheck = _measure(REPO / "tests")
        print(f"recheck: stale_sites={len(recheck['stale'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
