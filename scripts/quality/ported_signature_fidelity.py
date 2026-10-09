#!/usr/bin/env python3
"""AC2-04's 「实际函数名/签名/语义保真」 leg as a recomputed AST comparison, not an assertion.

``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/验收文档.md`` §2 row AC2-04 demands that the
ported tree keep upstream's *actual function names, signatures and semantics*. Until now the
archive only carried byte equality of a deterministic replay
(``port_report.recorded_replay_status``) and per-node rewrite counts, and the acceptance case
declared the signature clause as an unmet leg. Byte equality proves the codemod was run; it does
not name which def changed, so the clause stayed prose.

This tool names it. For every ``.py`` record in the vendor tree's ``upstream.lock`` it parses the
pristine upstream file at the pinned checkout and the ported file on disk, and compares
-- as multisets -- one key per ``ast.FunctionDef`` / ``ast.AsyncFunctionDef`` / ``ast.ClassDef`` at
every depth: ``(enclosing def/class chain, node kind, name, ast.unparse(node.args))``. The enclosing
chain is in the key so a method and a same-named module-level function cannot cancel each other out,
and the node kind is in the key so a class and a zero-arg function cannot either. Nested defs and
class bodies are collected, so a renamed inner helper is a difference.

What it refuses to do is report a clean reading it did not measure:

* the pristine checkout is resolved the way ``scripts/quality/port_scope_inventory.py`` resolves it
  (same ``PORTED_ROOT`` / ``DEFAULT_UPSTREAM_REPO`` / ``UpstreamLock`` / ``verify_upstream`` from
  ``scripts/codemod/port_module.py``); a missing clone, a HEAD that is not the locked commit or a
  dirty checkout prints ``checkout_present:false`` / ``checkout_commit_matches_lock:false`` and the
  process exits non-zero, because an unjoinable population otherwise reads as zero differences;
* a row whose pristine file is absent, whose record is malformed, or whose bytes will not parse is
  counted as not-compared and forces a non-zero exit; ``rows_compared`` is only ever the number of
  rows actually parsed on both sides;
* the root facade is *not* special-cased by path name here -- it is one locked ``.py`` row like any
  other, and whatever it differs by is reported for the judge to weigh.

Output is exactly one JSON object as the last line of stdout (diagnostics go to stderr), carrying
counters. ``manual_edits_true_rows`` is read from the lock so a caller can ask the sharper question
-- which differing rows did the lock already register as manual deviations -- and the answer,
``differing_and_not_flagged_manual``, is a list of paths plus its total, not a sentence.

Usage::

    python scripts/quality/ported_signature_fidelity.py
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess  # nosec B404  # literal argv, shell disabled
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Resolution reuse, not invention: these are the four symbols
# ``scripts/quality/port_scope_inventory.py`` imports (its ``build_inventory`` calls
# ``verify_upstream(upstream_repo, lock)`` before digesting a single pristine file), so this tool
# joins the same population the port-scope archive joins.
from scripts.codemod.port_module import (  # noqa: E402
    DEFAULT_UPSTREAM_REPO,
    PORTED_ROOT,
    UpstreamLock,
    verify_upstream,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The def/class node kinds whose name and parameter list carry the fidelity claim.
DEF_NODES: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
#: One class has no ``args`` to unparse; its signature is the empty string.
_NO_PARAMETERS: Final = ""
#: Locked records are joined by suffix, exactly as ``port_scope_inventory._is_python`` joins them.
_PYTHON_SUFFIX: Final = ".py"
#: Printed list ceilings; the totals are printed next to them so a cap never hides a population.
LIST_CAP: Final = 20
#: How many not-compared paths the stderr notes name before switching to a count.
NOTE_CAP: Final = 5
EXIT_OK: Final = 0
EXIT_INCOMPLETE: Final = 2
Key = tuple[str, str, str, str]


@dataclass(frozen=True)
class Checkout:
    """Where the pristine sources were read from, and whether that place was admissible."""

    present: bool
    commit: str
    lock_commit: str
    matches_lock: bool
    verified: bool

    @property
    def admissible(self) -> bool:
        """Whether pristine bytes may be trusted at all -- the anti-silent-zero gate."""
        return self.present and self.matches_lock and self.verified

    def to_json(self) -> dict[str, object]:
        """Render the checkout availability counters the acceptance plane reads."""
        return {
            "checkout_present": self.present,
            "checkout_commit": self.commit,
            "lock_commit": self.lock_commit,
            "checkout_commit_matches_lock": self.matches_lock,
            "checkout_verified_by_port_module": self.verified,
        }


def _git_head(repo: Path) -> str:
    """Return the checkout's HEAD commit, or the empty string when git cannot answer.

    Same literal argv ``scripts/codemod/port_module._git`` runs for ``verify_upstream``; the only
    difference is that a missing clone is reported instead of raised, because the instrument has to
    print ``checkout_present:false`` rather than die on it.
    """
    completed = subprocess.run(  # noqa: S603  # nosec B603 B607  # literal argv, shell disabled
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def resolve_checkout(repo: Path, lock: UpstreamLock) -> Checkout:
    """Resolve and admit the pinned upstream checkout, reusing ``port_module.verify_upstream``.

    Fails loud rather than empty: a directory that is not there, a HEAD that is not the locked
    commit, or a dirty tree all make the population unjoinable, and every one of them has to reach
    the exit code instead of showing up as ``rows_compared: 0, differing_total: 0``.
    """
    present = repo.is_dir()
    commit = _git_head(repo) if present else ""
    matches = present and bool(commit) and commit == lock.commit
    verified = False
    if matches:
        try:
            verify_upstream(repo, lock)
            verified = True
        except (RuntimeError, OSError) as error:
            print(f"note: upstream verify_upstream refused {repo}: {error}", file=sys.stderr)
    return Checkout(present, commit, lock.commit, matches, verified)


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Index every node by its parent so a def/class chain can be walked upward."""
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _position(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    """Return the dotted chain of enclosing def/class names, outermost first, '' at module level."""
    chain: list[str] = []
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, DEF_NODES):
            chain.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(chain))


def signature_keys(text: str) -> Counter[Key]:
    """Collect ``(position, kind, name, parameters)`` for every def/class at every depth.

    Raises:
        SyntaxError: When the bytes are not parseable python, so the caller records the row as
            not-compared instead of as an equal one.
    """
    tree = ast.parse(text)
    parents = _parent_map(tree)
    found: Counter[Key] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, DEF_NODES):
            continue
        parameters = (
            ast.unparse(node.args)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            else _NO_PARAMETERS
        )
        found[(_position(node, parents), type(node).__name__, node.name, parameters)] += 1
    return found


def _read_text(path: Path) -> str:
    """Read UTF-8 source, letting the caller see the failure type."""
    return path.read_text(encoding="utf-8")


def audit_lock(*, ported_root: Path, upstream_repo: Path, checkout: Checkout) -> dict[str, Any]:
    """Compare pristine and ported def/class multisets for every locked ``.py`` record.

    A row that cannot be joined or parsed lands in a not-compared bucket and never in
    ``rows_equal``; ``rows_compared`` is the number of rows actually parsed on both sides, so a
    checkout that is not there yields a small ``rows_compared``, not a clean ``differing_total``.
    """
    lock = UpstreamLock.load(ported_root / "upstream.lock")
    python_rows = sorted(
        str(row["path"]) for row in lock.files.values() if str(row["path"]).endswith(_PYTHON_SUFFIX)
    )
    compared = 0
    equal: list[str] = []
    differing: list[str] = []
    unregistered: list[str] = []
    parse_errors: list[str] = []
    missing: list[str] = []
    for path in python_rows:
        record = lock.files[path]
        upstream_path = str(record.get("upstream_path") or "")
        pristine = upstream_repo / PurePosixPath(upstream_path) if upstream_path else None
        ported = ported_root / PurePosixPath(path)
        if pristine is None or not pristine.is_file() or not ported.is_file():
            missing.append(path)
            continue
        try:
            upstream_keys = signature_keys(_read_text(pristine))
            ported_keys = signature_keys(_read_text(ported))
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError) as error:
            print(f"note: {path} could not be parsed on both sides ({error})", file=sys.stderr)
            parse_errors.append(path)
            continue
        compared += 1
        if upstream_keys == ported_keys:
            equal.append(path)
            continue
        differing.append(path)
        if record.get("manual_edits") is not True:
            unregistered.append(path)
    for label, paths in (("missing", missing), ("parse error", parse_errors)):
        if paths:
            head = ", ".join(paths[:NOTE_CAP])
            rest = f" (+{len(paths) - NOTE_CAP} more)" if len(paths) > NOTE_CAP else ""
            print(
                f"note: {len(paths)} locked row(s) not compared, {label}: {head}{rest}",
                file=sys.stderr,
            )
    for path in unregistered:
        print(
            f"note: {path} differs in def/class signatures and the lock flags no manual edit",
            file=sys.stderr,
        )
    return {
        "rows_compared": compared,
        "python_records": len(python_rows),
        "lock_file_records": len(lock.files),
        "rows_equal": len(equal),
        "differing_total": len(differing),
        "differing_paths": differing[:LIST_CAP],
        "differing_and_not_flagged_manual_total": len(unregistered),
        "differing_and_not_flagged_manual": unregistered[:LIST_CAP],
        "manual_edits_true_rows": sum(
            1 for path in python_rows if lock.files[path].get("manual_edits") is True
        ),
        "parse_errors_total": len(parse_errors),
        "parse_errors": parse_errors[:LIST_CAP],
        "missing_total": len(missing),
        "missing_paths": missing[:LIST_CAP],
        **checkout.to_json(),
    }


def _exit_code(payload: dict[str, Any]) -> int:
    """Non-zero unless the checkout was admissible and every locked ``.py`` row was compared.

    The three availability flags and the two not-compared buckets are checked here rather than
    inferred from an empty difference list: an unjoinable population prints zero differences, and
    zero differences printed from it is the one reading this tool must never emit as a pass.
    """
    joined = (
        bool(payload["checkout_present"])
        and bool(payload["checkout_commit_matches_lock"])
        and bool(payload["checkout_verified_by_port_module"])
    )
    complete = (
        joined
        and int(payload["python_records"]) > 0
        and int(payload["rows_compared"]) == int(payload["python_records"])
        and int(payload["parse_errors_total"]) == 0
        and int(payload["missing_total"]) == 0
    )
    return EXIT_OK if complete else EXIT_INCOMPLETE


def _unreadable_lock_payload(upstream_repo: Path) -> dict[str, Any]:
    """Print the whole counter set as unavailable when the lock itself cannot be read.

    Every not-compared bucket is stated as a counter so a caller never has to guess whether an
    absent reading means zero differences; the exit code stays non-zero regardless.
    """
    return {
        "rows_compared": 0,
        "python_records": 0,
        "lock_file_records": 0,
        "rows_equal": 0,
        "differing_total": 0,
        "differing_paths": [],
        "differing_and_not_flagged_manual_total": 0,
        "differing_and_not_flagged_manual": [],
        "manual_edits_true_rows": 0,
        "parse_errors_total": 0,
        "parse_errors": [],
        "missing_total": 1,
        "missing_paths": ["upstream.lock"],
        "exit_code": EXIT_INCOMPLETE,
        **Checkout(
            present=upstream_repo.is_dir(),
            commit="",
            lock_commit="",
            matches_lock=False,
            verified=False,
        ).to_json(),
    }


def main(argv: list[str] | None = None) -> int:
    """Print one JSON object on stdout and fail loud when the comparison did not run."""
    parser = argparse.ArgumentParser(
        description="Compare pristine upstream and ported def/class signatures per locked path."
    )
    parser.add_argument(
        "--ported-root",
        type=Path,
        default=PORTED_ROOT,
        help="vendored port tree (default: scripts.codemod.port_module.PORTED_ROOT)",
    )
    parser.add_argument(
        "--upstream-repo",
        type=Path,
        default=DEFAULT_UPSTREAM_REPO,
        help="pinned upstream clone (default: scripts.codemod.port_module.DEFAULT_UPSTREAM_REPO)",
    )
    args = parser.parse_args(argv)
    ported_root: Path = args.ported_root
    upstream_repo: Path = args.upstream_repo
    lock_path = ported_root / "upstream.lock"
    try:
        lock = UpstreamLock.load(lock_path)
    except (RuntimeError, OSError, ValueError) as error:
        print(f"FAIL: {lock_path} could not be read: {error}", file=sys.stderr)
        print(json.dumps(_unreadable_lock_payload(upstream_repo), ensure_ascii=False))
        return EXIT_INCOMPLETE
    checkout = resolve_checkout(upstream_repo, lock)
    if not checkout.admissible:
        print(
            f"FAIL: pristine sources unavailable (present={checkout.present} "
            f"head={checkout.commit or '-'} locked={checkout.lock_commit} "
            f"matches={checkout.matches_lock} verified={checkout.verified}); a zero-difference "
            "reading from this state would be a silent pass",
            file=sys.stderr,
        )
    payload = audit_lock(ported_root=ported_root, upstream_repo=upstream_repo, checkout=checkout)
    payload["exit_code"] = _exit_code(payload)
    print(json.dumps(payload, ensure_ascii=False))
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
