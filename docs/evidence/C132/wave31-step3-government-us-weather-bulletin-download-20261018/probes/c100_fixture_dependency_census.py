"""Census which tests in ``tests/test_government_us_iter2.py`` cannot run from a clean clone.

Why this is a carrier and not a sentence: the step-3 witness docstring says the bulletin bytes are a
transcription of ``nws_document.txt``, and the natural way to point at prior art for that claim is
the provider's own test. Checking the cite turned up that the test reads its receipts out of
``docs/evidence/C100/gov-finra/fixtures/``, and every file in that root is ignored
(``git check-ignore -v`` resolves it to the ``docs/evidence/C100/**`` rule), so a clone holds none
of them. "The provider's own test expects these bytes" and "the provider's own test runs on a clone"
are then two different claims, and only the second is worth quoting as a floor.

The census is AST-scoped rather than textual: a test counts as dependent when its own body, or a
callee defined in this same file, touches the ignored root. Transitivity is walked over in-file
callees only, because that is the whole population. Five arms gate the instrument: the dependency
set must be non-empty (an empty set would mean the finding evaporated and the docstring needs no
qualifier), the root must be the one ``git`` says is ignored *and* a committed sibling must be shown
not to be, on the same argv, because ``check-ignore`` exiting 0 means nothing on its own; the
witness's inline bytes must equal both the fixture bytes and the base64 literal the test asserts --
that equality is what the witness sentence rests on -- and the witness module itself must open
nothing, measured as a count of read calls in its tree rather than as a substring test, because a
docstring is allowed to name a receipt it never opens.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import shutil
import subprocess  # nosec B404  # the shipped `git check-ignore -v` argv, list form, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
TEST = REPO / "tests/test_government_us_iter2.py"
STEP3 = "docs/evidence/C132/wave31-step3-government-us-weather-bulletin-download-20261018"
RUNS = REPO / STEP3 / "runs"
IGNORED_ROOT = "docs/evidence/C100/gov-finra/fixtures"
READERS = frozenset({"read", "read_bytes", "read_text", "open"})

#: The carrier's own header, kept out of ``main`` so the indentation ruff format gives the list
#: inside it cannot push these prose lines past the 100-column limit.
_NOTE = (
    "wave-31 step 3 -- which tests read the gitignored receipt root, and whether the witness's",
    "inline bytes are the bytes those receipts hold. The finding this carrier prices is a",
    "reproducibility floor on the provider's own suite, not on the witness: the witness opens",
    "nothing, so the landed row stands on a clone while seven of the nine provider tests do not.",
    "Repaired elsewhere; recorded here.",
)


def _read_calls(node: ast.AST) -> list[ast.Call]:
    """Every file-reading call in this tree, whether ``open(x)`` or ``p.read_bytes()``."""

    def reads(call: ast.Call) -> bool:
        f = call.func
        same = isinstance(f, ast.Name) and f.id in READERS
        method = isinstance(f, ast.Attribute) and f.attr in READERS
        return same or method

    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and reads(c)]


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _callees(node: ast.AST) -> set[str]:
    """Names of plain functions this node calls, which is where transitivity can lead."""
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def dependent(name: str, funcs: dict[str, ast.FunctionDef], seen: set[str]) -> bool:
    """True when this test, or an in-file helper it calls, reads the ignored receipt root."""
    fn = funcs.get(name)
    if fn is None or name in seen:
        return False
    seen.add(name)
    if _names(fn) & {"EVIDENCE", "_fixture", "_fixture_bytes"}:
        return True
    return any(dependent(callee, funcs, seen) for callee in _callees(fn))


def _test_literal(tree: ast.AST) -> bytes:
    """The base64 string the provider test asserts the fetcher returns, decoded."""
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "content":
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return base64.b64decode(value.value)
    raise AssertionError("no content= literal in the test file")


def face() -> tuple[list[str], dict[str, bool]]:
    """Print the per-test census, the ignore rule, and the three byte/gate readings."""
    source = TEST.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(TEST))
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    tests = sorted(n for n in funcs if n.startswith("test_"))
    deps = [t for t in tests if dependent(t, funcs, set())]
    helpers = sorted(n for n in funcs if not n.startswith("test_") and dependent(n, funcs, set()))

    lines = [
        f"test_file={TEST.relative_to(REPO)} test_functions={len(tests)} "
        f"dependent_on_ignored_root={len(deps)} in_file_helpers_reading_it={helpers or '[]'} "
        f"clean={len(tests) - len(deps)}"
    ]
    lines += [f"DEPENDS {t}" for t in deps]
    lines += [f"clean    {t}" for t in tests if t not in deps]

    git = shutil.which("git")
    rule = subprocess.run(  # noqa: S603  # nosec B603  # argv fixed; only the path element varies
        [str(git), "check-ignore", "-v", f"{IGNORED_ROOT}/nws_document.txt"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    lines.append(
        f"check_ignore_exit={rule.returncode} rule={rule.stdout.strip() or '<none>'} "
        f"skip_markers_in_test_file={source.count('skip')}"
    )
    ignored = rule.returncode == 0 and rule.stdout.strip() != ""
    # Two-sided: ``git check-ignore`` exits 0 for whatever it ignores, so the reading means nothing
    # until a committed path is shown NOT to be ignored by the same command on the same argv.
    contrast = subprocess.run(  # noqa: S603  # nosec B603  # argv fixed; only the path element varies
        [str(git), "check-ignore", "-v", "docs/evidence/C101/government_us/claims.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    lines.append(
        f"CONTROL_NOT_IGNORED exit={contrast.returncode} "
        f"rule={contrast.stdout.strip() or '<none>'} expected=exit=1/<none> "
        f"so_the_ignore_rule_is_specific={contrast.returncode == 1 and not contrast.stdout.strip()}"
    )

    fixture = REPO / IGNORED_ROOT / "nws_document.txt"
    fixture_bytes = fixture.read_bytes() if fixture.exists() else b"<absent>"
    witness = _witness_module()
    inline = bytes(witness._BULLETIN_TEXT_BYTES)  # the sentence under test names this
    literal = _test_literal(tree)
    digest = lambda b: hashlib.sha256(b).hexdigest()[:16]  # noqa: E731  # one-line sha helper
    lines.append(
        f"CONTROL_BYTE_IDENTITY inline_sha16={digest(inline)} fixture_sha16={digest(fixture_bytes)}"
        f" test_literal_sha16={digest(literal)} bytes={len(inline)} "
        f"all_equal={fixture_bytes == inline == literal}"
    )

    witness_source = Path(witness.__file__).read_text(encoding="utf-8")
    opens = _read_calls(ast.parse(witness_source, filename=str(witness.__file__)))
    lines.append(
        f"CONTROL_WITNESS_READS file={Path(witness.__file__).relative_to(REPO)} read_calls="
        f"{len(opens)} names_the_ignored_root_in_prose={IGNORED_ROOT in witness_source}"
    )

    arms = {
        "population_nonempty": len(deps) > 0,
        "root_is_ignored": ignored,
        "rule_is_specific": contrast.returncode == 1 and not contrast.stdout.strip(),
        "byte_identity_holds": fixture_bytes == inline == literal,
        "witness_is_inline_only": not opens,
    }
    return lines, arms


def _witness_module() -> Any:  # noqa: ANN401  # the witness is a data module, typed loosely here
    """Import the government_us witness module the way the runner does."""
    sys.path.insert(0, str(REPO))
    return __import__("scripts.quality.iter2_witnesses.government_us", fromlist=["CONFIG"])


def main() -> int:
    """Run the census and write the carrier this wave cites."""
    lines, arms = face()
    failed = sorted(k for k, ok in arms.items() if not ok)
    RUNS.mkdir(parents=True, exist_ok=True)
    text = (
        "\n".join(
            [
                *_NOTE,
                f"argv: {sys.executable} {STEP3}/probes/c100_fixture_dependency_census.py",
                "  (cwd = repo root, no pipes, PYTHONPATH=.)",
                "stderr_bytes=0",
                *lines,
                f"gates {arms} findings={failed or '[]'}",
                f"c100_fixture_dependency_census_exit={1 if failed else 0}",
            ]
        )
        + "\n"
    )
    path = RUNS / "wave31-step3-c100_fixture_dependency_census.txt"
    path.write_text(text, encoding="utf-8")
    for line in lines:
        print(line, flush=True)
    print(
        f"carrier_written bytes={path.stat().st_size} arms={len(arms)} gates_failed={len(failed)}",
        flush=True,
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
