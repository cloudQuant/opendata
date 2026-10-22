#!/usr/bin/env python3
"""A2 zero-tolerance gate (quality spec §1 and §7).

The A1 legacy layer carries debt that is managed by ``quality-ratchet``
(only-down). A2 — code that is **new or modified** — has no snapshot: every
violation blocks.

This script finds the A2 Python files and runs the full self-developed standard
on them:

* ``ruff check`` (full rule set) and ``ruff format --check``
* ``mypy`` with the project's strict settings
* ``bandit``

A2 is defined relative to the recorded baseline commit
(``docs/quality/baseline.json``, written when the A0 baseline commit is created):
the A2 set is everything added or modified since then, plus untracked files.
Set ``A2_BASE_REF`` in CI to the target branch so a pull request is checked
against its merge base instead. The A2 base is the *later* of that merge base
and the recorded baseline, so a PR whose target predates the baseline cannot
sweep frozen A1 files into the A2 set.

The gate fails closed: if a configured baseline cannot be resolved (shallow
clone, stale ``A2_BASE_REF``), it errors instead of reporting "nothing changed".

Before the baseline commit exists, A1 and A2 are indistinguishable, so the gate
reports that state explicitly instead of failing on the whole tree.

The ported tree, the two ``alembic`` migration envs and the frontend are out of
scope: they have their own layers (B / D / E in the quality spec) - and "out of
scope" means their *top-level directory*, never a path segment. C35 found this
list matched any segment, so ``opendata/data/providers/akshare/`` (15
first-party modules, named after the vendored tree that used to sit at the root)
was silently dropped from the A2 set and the whole package went unchecked.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

# calls use GIT(which) or sys.executable as argv[0], list argv, shell never enabled.
import subprocess  # nosec B404  # 3 runs: GIT(which)/sys.executable lists, no shell
import sys
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality.immutable_evidence import (  # noqa: E402
    partition_static_files,
    scan_snapshot_bytes,
    validate_manifest,
)
from scripts.quality.source_layout import PORTED, classify_path  # noqa: E402

BASELINE_PATH = "docs/quality/baseline.json"

#: Top-level trees the A2 layer does not own. Matched against the first path
#: segment only - see the module docstring for what segment matching cost.
#: C45: ``alembic`` and ``alembic_data`` were dropped from this set. They were
#: justified as "tool-generated migration env, not first-party application
#: code", but the modules under ``versions/`` are hand-written Python that
#: merely *import* their DDL text from ``opendata/pipeline/ddl`` - nothing
#: regenerates them, so excluding the roots hid 10 touched first-party files
#: from every static plane at once (``a2-check``, the ratchet, ruff's walk and
#: mypy's walk). Only the ``script.py.mako`` templates are generated artifacts,
#: and they are not ``.py``.
EXCLUDED_ROOT_DIRS = frozenset({"akshare", "frontend"})

#: Owner ruling 2026-10-22 (release readiness, option C): A2's static population is product code.
#: ``docs/evidence/**`` holds one-shot instruments and archived inputs from ~50 waves; those are
#: inputs to the *evidence* judges, not A2 static targets. The size of what this filter hides is
#: printed on every run, so the hidden set cannot widen silently, and a trust/schema failure on any
#: path outside these prefixes still fails the gate.
ARCHIVE_PREFIXES = ("docs/evidence/",)


def archive_census() -> tuple[int, int]:
    """Count tracked and on-disk .py files inside the archived prefixes (the hidden set)."""
    listed = _git("ls-files", "--", *ARCHIVE_PREFIXES)
    tracked = [n for n in listed.splitlines() if n.endswith(".py")]
    on_disk = [n for n in tracked if (REPO_ROOT / n).is_file()]
    return len(tracked), len(on_disk)


# Resolved once so the subprocess call never uses a partial executable path.
GIT = shutil.which("git")


class _MypyTargetResult(NamedTuple):
    """Preserve the exact process result for one explicit mypy target."""

    path: str
    returncode: int
    stdout: str
    stderr: str


def _git(*args: str) -> str:
    """Run git and return its stdout, or "" when git is unavailable or fails."""
    if GIT is None:
        return ""
    result = subprocess.run(  # noqa: S603  # nosec B603  # GIT=which(git); revs as argv items
        [GIT, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def baseline_commit() -> str | None:
    """Return the recorded baseline commit SHA, if one has been established."""
    path = REPO_ROOT / BASELINE_PATH
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    commit = raw.get("commit")
    return commit if isinstance(commit, str) and commit.strip() else None


def _is_a2_candidate(name: str) -> bool:
    if not name.endswith(".py"):
        return False
    parts = Path(name).parts
    if parts and parts[0] in EXCLUDED_ROOT_DIRS:
        return False
    if name.startswith(ARCHIVE_PREFIXES):
        return False
    if classify_path(name) == PORTED:
        return False
    return (REPO_ROOT / name).is_file()


def changed_files(base: str | None) -> list[str]:
    """Return A2 Python files as repo-relative POSIX paths, sorted.

    The comparison is against the *working tree* rather than ``base..HEAD``, so
    uncommitted local edits are gated too. Comparing two commits would silently
    pass during development and only fire after a commit — the wrong feedback
    loop for a pre-commit-style gate.
    """
    names: set[str] = set()
    revision = base or "HEAD"
    names.update(_git("diff", "--name-only", "--diff-filter=ACMR", revision).splitlines())
    names.update(_git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted({name.strip() for name in names if _is_a2_candidate(name.strip())})


class BaselineError(RuntimeError):
    """Raised when the A2 baseline cannot be resolved.

    The gate must fail closed: a shallow clone (``fetch-depth: 1``) or a stale
    ``A2_BASE_REF`` would otherwise make the check silently pass by reporting
    "no A2 files changed".
    """


def _resolve_ref(ref: str) -> str | None:
    """Return ``ref`` or ``origin/<ref>``, whichever resolves to a commit."""
    for candidate in (ref, f"origin/{ref}"):
        resolved = _git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").strip()
        if resolved:
            return candidate
    return None


def _baseline_is_ahead(merge_base: str, baseline: str) -> bool:
    """True when the A0 baseline is a descendant of ``merge_base``.

    A merge base *older* than the A0 baseline (e.g. a pull request targeting a
    branch that predates the baseline) would drag pre-baseline A1 files into the
    A2 set and apply zero tolerance to legacy debt. The later of the two wins.
    """
    return _git("rev-list", "--count", f"{merge_base}..{baseline}").strip() not in ("", "0")


def _checked_baseline() -> str | None:
    """Return the recorded baseline commit, failing closed if it is unreachable."""
    baseline = baseline_commit()
    if baseline is None:
        return None
    if not _git("rev-parse", "--verify", "--quiet", f"{baseline}^{{commit}}").strip():
        raise BaselineError(
            f"baseline commit {baseline} is not present in this clone; "
            "check out with full history (actions/checkout with fetch-depth: 0)."
        )
    return baseline


def resolve_files(explicit: list[str] | None) -> list[str] | None:
    """Resolve the A2 file list.

    The A2 base is the later of the A0 baseline commit (``docs/quality/baseline.json``)
    and, when ``A2_BASE_REF`` is set, the merge base with that ref. The earlier
    one would sweep already-frozen A1 files into the A2 set.

    Returns:
        The A2 file paths, or ``None`` when no baseline has been recorded yet
        (i.e. before milestone A0.11, when A1 and A2 are indistinguishable).

    Raises:
        BaselineError: When a baseline is configured but cannot be resolved.
    """
    if explicit is not None:
        return [name for name in explicit if _is_a2_candidate(name)]

    baseline = _checked_baseline()

    env_base = os.environ.get("A2_BASE_REF", "").strip()
    if not env_base:
        return None if baseline is None else changed_files(baseline)

    resolved = _resolve_ref(env_base)
    if resolved is None:
        raise BaselineError(
            f"A2_BASE_REF={env_base!r} does not resolve to a commit; "
            "fetch it first (actions/checkout with fetch-depth: 0)."
        )
    merge_base = _git("merge-base", resolved, "HEAD").strip()
    if not merge_base:
        raise BaselineError(f"no merge base between {resolved} and HEAD")
    if baseline is not None and _baseline_is_ahead(merge_base, baseline):
        return changed_files(baseline)
    return changed_files(merge_base)


def _run(args: list[str]) -> tuple[bool, str]:
    result = subprocess.run(  # noqa: S603  # nosec B603  # sys.executable -m ruff|mypy, file argv
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def _run_streams(args: list[str]) -> tuple[int, str, str]:
    """Run one tool and keep its stdout and stderr apart."""
    result = subprocess.run(  # noqa: S603  # nosec B603  # sys.executable -m bandit or mypy argv
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return result.returncode, (result.stdout or ""), (result.stderr or "")


def _mypy_target_chunks(targets: list[str], workers: int) -> list[tuple[int, tuple[str, ...]]]:
    """Split sorted targets into stable, balanced chunks for persistent workers."""
    if type(workers) is not int or workers < 1:
        raise ValueError("mypy workers must be a positive integer")
    if not targets:
        return []

    worker_count = min(workers, len(targets))
    base_size, extra = divmod(len(targets), worker_count)
    chunks: list[tuple[int, tuple[str, ...]]] = []
    start = 0
    for worker_index in range(worker_count):
        size = base_size + (1 if worker_index < extra else 0)
        end = start + size
        chunks.append((worker_index, tuple(targets[start:end])))
        start = end
    return chunks


def _mypy_worker_cache_dir(cache_parent: Path, worker_index: int) -> Path:
    """Return a persistent per-invocation cache path private to one worker."""
    return cache_parent / f"worker-{worker_index:03d}"


def _run_mypy_target(path: str, cache_dir: Path | None = None) -> _MypyTargetResult:
    """Run one mypy root and retain failures raised before a process result exists."""
    try:
        args = [
            sys.executable,
            "-m",
            "mypy",
            "--no-color-output",
            "--no-error-summary",
            "--follow-imports=silent",
        ]
        if cache_dir is not None:
            args.extend(("--cache-dir", str(cache_dir)))
        args.append(path)
        returncode, stdout, stderr = _run_streams(args)
    except Exception as error:
        return _MypyTargetResult(
            path,
            127,
            "",
            f"mypy target invocation failed ({type(error).__name__})\n",
        )
    return _MypyTargetResult(path, returncode, stdout, stderr)


def _run_mypy_chunk(
    cache_parent: Path, worker_index: int, paths: tuple[str, ...]
) -> list[_MypyTargetResult]:
    """Run a fixed chunk serially so one worker exclusively owns its cache."""
    cache_dir = _mypy_worker_cache_dir(cache_parent, worker_index)
    return [_run_mypy_target(path, cache_dir) for path in paths]


def _submit_mypy_chunk(
    executor: ThreadPoolExecutor,
    cache_parent: Path,
    worker_index: int,
    paths: tuple[str, ...],
) -> tuple[Future[list[_MypyTargetResult]] | None, str | None]:
    """Submit a chunk and turn executor errors into explicit target failures."""
    try:
        return executor.submit(_run_mypy_chunk, cache_parent, worker_index, paths), None
    except Exception as error:
        return None, f"mypy worker scheduling failed ({type(error).__name__})\n"


def _report_mypy_parallel_failure(stage: str, error: Exception) -> None:
    """Write a safe aggregate failure without altering individual tool streams."""
    print(f"mypy {stage} failed ({type(error).__name__})", file=sys.stderr)


def _run_mypy_targets(files: list[str], workers: int = 1) -> tuple[bool, list[_MypyTargetResult]]:
    """Run isolated mypy roots and retain every target's full process result.

    A single invocation cannot safely accept unrelated same-basename modules from
    evidence directories. Isolating explicit roots avoids module-name collisions;
    sorting and exact-path de-duplication make execution deterministic without
    dropping any unique A2 target. Parallel execution assigns contiguous, balanced
    chunks to fixed workers, each with a private cache reused by its chunk. Each
    parallel invocation owns a temporary cache parent. A single worker retains
    the legacy serial command and default cache behavior.
    """
    targets = sorted(set(files))
    chunks = _mypy_target_chunks(targets, workers)
    if not targets:
        return True, []
    if workers == 1:
        results = [_run_mypy_target(path) for path in targets]
        return all(result.returncode == 0 for result in results), results

    results_by_path: dict[str, _MypyTargetResult] = {}
    aggregate_failure = False
    try:
        with tempfile.TemporaryDirectory(prefix="opendata-a2-mypy-") as temp_dir:
            cache_parent = Path(temp_dir)
            try:
                executor = ThreadPoolExecutor(max_workers=len(chunks))
            except Exception as error:
                for path in targets:
                    results_by_path[path] = _MypyTargetResult(
                        path,
                        127,
                        "",
                        f"mypy worker pool initialization failed ({type(error).__name__})\n",
                    )
            else:
                futures: dict[int, Future[list[_MypyTargetResult]]] = {}
                try:
                    with executor:
                        for worker_index, paths in chunks:
                            future, error_text = _submit_mypy_chunk(
                                executor, cache_parent, worker_index, paths
                            )
                            if future is None:
                                for path in paths:
                                    results_by_path[path] = _MypyTargetResult(
                                        path,
                                        127,
                                        "",
                                        error_text or "mypy worker scheduling failed\n",
                                    )
                            else:
                                futures[worker_index] = future

                        for worker_index, paths in chunks:
                            future = futures.get(worker_index)
                            if future is None:
                                continue
                            try:
                                for result in future.result():
                                    results_by_path[result.path] = result
                            except Exception as error:
                                for path in paths:
                                    results_by_path.setdefault(
                                        path,
                                        _MypyTargetResult(
                                            path,
                                            127,
                                            "",
                                            f"mypy worker execution failed "
                                            f"({type(error).__name__})\n",
                                        ),
                                    )
                except Exception as error:
                    aggregate_failure = True
                    _report_mypy_parallel_failure("worker pool lifecycle", error)
    except Exception as error:
        aggregate_failure = True
        _report_mypy_parallel_failure("temporary cache lifecycle", error)
        for path in targets:
            results_by_path.setdefault(
                path,
                _MypyTargetResult(
                    path,
                    127,
                    "",
                    f"mypy parallel dispatch failed ({type(error).__name__})\n",
                ),
            )

    for path in targets:
        results_by_path.setdefault(
            path,
            _MypyTargetResult(
                path,
                127,
                "",
                "mypy worker produced no result for this target\n",
            ),
        )
    results = [results_by_path[path] for path in targets]
    passed = not aggregate_failure and all(result.returncode == 0 for result in results)
    return passed, results


def _mypy_results_text(results: list[_MypyTargetResult]) -> str:
    """Render every mypy target, exact return code, and captured output streams."""
    lines: list[str] = []
    for result in results:
        lines.append(
            f"  {result.path}: exit={result.returncode} "
            f"stdout_chars={len(result.stdout)} stderr_chars={len(result.stderr)}"
        )
        if result.stdout:
            lines.extend(("    stdout:", result.stdout))
        if result.stderr:
            lines.extend(("    stderr:", result.stderr))
    return "\n".join(lines)


def _bandit(files: list[str]) -> tuple[bool, str]:
    """Read bandit's JSON on stdout; output that cannot be read is a failure, never a pass.

    The first version concatenated stdout and stderr before parsing. bandit writes its
    ``nosec encountered, but no failed test`` and ``Test in comment`` warnings to stderr, and this
    repo's A2 set carries both kinds, so a warning made the parse throw and the handler answered
    ``True``. The gate printed ``ok bandit`` on every run in which bandit said anything at all --
    a verdict produced by not reading the findings rather than by reading an empty list. Fail
    closed now: stdout alone is parsed, and a non-list ``results`` or unparseable stdout reports
    what it saw instead of passing.
    """
    code, stdout, stderr = _run_streams(
        [sys.executable, "-m", "bandit", "-c", "bandit.yaml", "-f", "json", "-q", *files]
    )
    if code != 0:
        return False, (stdout + stderr).strip()
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as error:
        return False, (
            f"bandit exited {code} but its stdout is not JSON ({error}); "
            f"stdout tail={stdout[-400:]!r} stderr tail={stderr[-400:]!r}"
        )
    results = payload.get("results")
    if not isinstance(results, list):
        return False, f"bandit JSON carries no results list; keys={sorted(payload)}"
    if results:
        return False, "\n".join(
            f"  {item.get('filename')}:{item.get('line_number')} "
            f"{item.get('test_id')} {item.get('issue_text')}"
            for item in results
        )
    return True, ""


def _drop_reason(name: str) -> str:
    """Say why an explicitly requested path is not an A2 candidate."""
    if not name.endswith(".py"):
        return "not a .py file"
    first = name.split("/", 1)[0]
    if first in EXCLUDED_ROOT_DIRS:
        return f"root tree {first}/ is outside the A2 layer"
    if name.startswith(ARCHIVE_PREFIXES):
        return "archived evidence corpus is outside the A2 layer (owner ruling 2026-10-22)"
    if classify_path(name) == PORTED:
        return "nested vendor package is outside the A2 layer"
    if not (REPO_ROOT / name).is_file():
        return "not present in the working tree"
    return "not an A2 candidate"


def run(explicit: list[str] | None = None, mypy_workers: int = 1) -> int:
    """Run the A2 zero-tolerance gate and report the result."""
    try:
        files = resolve_files(explicit)
    except BaselineError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if files is None:
        print(
            "NOTE: A2 gating is inactive until the baseline commit is recorded in "
            f"{BASELINE_PATH} (created at milestone A0.11).\n"
            "      Before that commit the whole tree is untracked, so A1 and A2 "
            "cannot be told apart; A1 debt is already frozen by quality-ratchet."
        )
        return 0

    # Keep the complete resolver output visible. Immutable evidence is a
    # post-resolution execution partition only; it never changes A2 discovery.
    evidence = validate_manifest(REPO_ROOT)
    partition_error = ""
    try:
        tool_files, excluded_files = partition_static_files(files, evidence)
    except ValueError as exc:
        partition_error = str(exc)
        tool_files = files
        excluded_files = []
    secret_scan = scan_snapshot_bytes(REPO_ROOT, evidence)

    if explicit:
        # C45: `--files` used to answer "OK: no A2 files changed" when every
        # requested path had been filtered out, which reads exactly like a pass
        # for the files the operator just asked about.
        checked = set(files)
        dropped = [name for name in explicit if name not in checked]
        if dropped:
            print(f"NOT CHECKED (explicitly requested, {len(dropped)} path(s)):")
            for name in dropped:
                print(f"  {name}  -  {_drop_reason(name)}")
            print()

    if not files:
        if explicit:
            print(
                f"NOTHING CHECKED: 0 of {len(explicit)} requested paths are A2 "
                "candidates (see NOT CHECKED above)."
            )
        else:
            print("OK: no A2 files changed (nothing to check).")
    else:
        print(f"A2 files (complete resolver): {len(files)}")
        for name in files:
            print(f"  {name}")
        print()

    tracked, on_disk = archive_census()
    print(
        f"A2 archive scope (owner ruling 2026-10-22): {ARCHIVE_PREFIXES[0]}** -> "
        f"{tracked} tracked .py file(s), {on_disk} present, excluded from static tools."
    )
    print(
        f"Static-tool execution partition: {len(tool_files)} file(s), "
        f"{len(excluded_files)} verified immutable snapshot(s) excluded."
    )
    if excluded_files:
        for name in excluded_files:
            print(f"  immutable evidence (static remediation only): {name}")
    if evidence.historical_ruff_count:
        print(
            "Historical Ruff findings: "
            f"{evidence.historical_ruff_count} (NOT GREEN; historical evidence only)."
        )
    if evidence.errors:
        print("BLOCKED immutable evidence validation:")
        for error in evidence.errors:
            print(f"  {error}")
    for path_name, reasons in evidence.blocked_details:
        print(f"BLOCKED candidate {path_name}:")
        for reason in reasons:
            print(f"  {reason}")
    if partition_error:
        print(f"BLOCKED static partition: {partition_error}")
    failures: list[str] = []
    # Owner ruling 2026-10-22: the archived evidence corpus is out of A2's static scope. The
    # decision is computed once and printed once -- a log line that says FAIL while the gate
    # passes would be worse than either verdict, because the log is what a reviewer re-reads.
    blocked_paths = [name for name, _ in evidence.blocked_details]
    archive_blocked = [n for n in blocked_paths if n.startswith(ARCHIVE_PREFIXES)]
    outside_blocked = sorted(set(blocked_paths) - set(archive_blocked))
    error_paths = [
        token.strip(" :;,")
        for message in evidence.errors
        for token in message.split()
        if "/" in token and token.rstrip(" :;,").endswith((".py", ".json"))
    ]
    outside_errors = sorted(p for p in error_paths if not p.startswith(ARCHIVE_PREFIXES))
    unnamed_errors = len(error_paths) < len(evidence.errors)
    # Fail closed: an error that names no path, or names a path outside the archive, is not
    # something this ruling covers.
    errors_in_scope = unnamed_errors or bool(outside_errors)
    immutable_fail = (
        bool(partition_error)
        or bool(outside_blocked)
        or (bool(evidence.errors) and errors_in_scope)
    )
    if (archive_blocked or evidence.errors) and not immutable_fail:
        print(
            f"ok  immutable evidence: {len(archive_blocked)} blocked candidate(s) and "
            f"{len(evidence.errors)} manifest error(s) all name the archived corpus, which A2 "
            "does not statically check (owner ruling 2026-10-22)"
        )
    if immutable_fail:
        print("FAIL immutable evidence")
        if outside_blocked:
            print(f"  {len(outside_blocked)} blocked candidate(s) outside the archive prefixes:")
            for name in outside_blocked[:10]:
                print(f"  {name}")
        if outside_errors:
            print(f"  {len(outside_errors)} manifest error path(s) outside the archive prefixes:")
            for name in outside_errors[:5]:
                print(f"  {name}")
        if unnamed_errors and evidence.errors:
            print(f"  {len(evidence.errors)} manifest error(s) do not all name a path:")
            for message in evidence.errors[:5]:
                print(f"  {message}")
        if partition_error:
            print(f"  static partition failed: {partition_error}")
        failures.append("immutable evidence")
    scan_msg = " ".join(secret_scan.errors)
    scan_scoped_out = (
        not secret_scan.ok
        and "no trusted snapshot paths" in scan_msg
        and not immutable_fail
        and not outside_blocked
    )
    if secret_scan.ok:
        print(f"ok  pinned Gitleaks file scan ({len(secret_scan.scanned_paths)} snapshot file(s))")
    elif scan_scoped_out:
        print(
            "ok  pinned Gitleaks file scan skipped: every snapshot path is in the archived corpus, "
            "which this checkout does not carry (owner ruling 2026-10-22)"
        )
    else:
        print("FAIL pinned Gitleaks file scan")
        for error in secret_scan.errors:
            print(f"  {error}")
        failures.append("immutable evidence secret scan")

    if tool_files:
        ok, output = _run([sys.executable, "-m", "ruff", "check", *tool_files])
        print(f"{'ok  ' if ok else 'FAIL'} ruff check")
        if not ok:
            failures.append("ruff check")
            print(output)

        ok, output = _run([sys.executable, "-m", "ruff", "format", "--check", *tool_files])
        print(f"{'ok  ' if ok else 'FAIL'} ruff format --check")
        if not ok:
            failures.append("ruff format")
            print(output)

        # mypy and bandit follow the spec's layer definitions: test code is C layer,
        # where mypy is gradual (not enforced) and bandit's assert rule is exempt.
        typed_files = [name for name in tool_files if not name.startswith("tests/")]

        if typed_files:
            ok, mypy_results = _run_mypy_targets(typed_files, workers=mypy_workers)
            mypy_label = "ok  " if ok else "FAIL "
            print(f"{mypy_label}mypy ({len(mypy_results)} unique target(s))")
            rendered_mypy_results = _mypy_results_text(mypy_results)
            if rendered_mypy_results:
                print("mypy target results:")
                print(rendered_mypy_results)
            if not ok:
                failures.append("mypy")

            ok, output = _bandit(typed_files)
            print(f"{'ok  ' if ok else 'FAIL'} bandit")
            if not ok:
                failures.append("bandit")
                print(output)
        else:
            print("skip mypy / bandit (no non-test A2 files)")
    else:
        print("skip static tools (all resolved A2 files are verified immutable evidence)")

    if failures:
        print(f"\nFAIL: A2 gate failed on: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nOK: A2 files meet the full A2 standard (ruff + format + mypy + bandit).")
    return 0


def _positive_mypy_workers(value: str) -> int:
    """Parse a positive worker count for the A2 command-line option."""
    try:
        workers = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("mypy workers must be a positive integer") from error
    if workers < 1:
        raise argparse.ArgumentTypeError("mypy workers must be a positive integer")
    return workers


def main(argv: list[str] | None = None) -> int:
    """Run the A2 gate from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="check exactly these files instead of the computed A2 set",
    )
    parser.add_argument(
        "--mypy-workers",
        type=_positive_mypy_workers,
        default=1,
        help="run isolated mypy targets with this many persistent workers (default: 1)",
    )
    args = parser.parse_args(argv)
    return run(args.files, mypy_workers=args.mypy_workers)


if __name__ == "__main__":
    raise SystemExit(main())
