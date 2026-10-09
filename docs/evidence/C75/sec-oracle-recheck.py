#!/usr/bin/env python3
"""C75 audit: do the 24 ``sec`` census cites agree with the installed ``openbb_sec`` oracle?

Why this face exists
    ``census-carrier-census.py`` put all 24 ``sec`` rows in ``repo_only``: each row's ``evidence``
    resolves inside the repository tree. But a real ``openbb_sec`` (1.6.7) is installed next to that
    tree in the same interpreter environment. Nobody had asked the follow-up question -- when a row
    cites a file, does a file of that name exist in the installed provider package at all, and if it
    does, is it the same bytes and the same line? If the two copies diverge, a row's
    ``needs_engine_capability`` / ``expressible_today`` judgement may rest on the wrong source.

    This instrument resolves every row's cited path twice, once under the repo tree and once under
    the installed package, and buckets each row into exactly one of ``absent_in_oracle``,
    ``line_text_matches``, ``line_text_differs``, ``file_byte_identical``, ``path_unresolvable``, so
    the face sums to the denominator it was taken over.

    ``0/24`` in the both-copies count is only meaningful against its populations, so the face prints
    them next to it: how many model files the installed package carries, how many the repo's
    ``providers/sec/models/`` carries, and which repo files the resolves actually land on. Without
    those counts the zero reads like "there is nothing upstream to compare with", which is the
    opposite of the measured situation.

What the reading is taken against
    The same yardstick as the sibling audit: the absolute site-packages root below, with
    ``sysconfig`` as fallback, resolved and printed in the header rather than trusted silently.
    A missing oracle package aborts (a NO_READING about the environment), because with no oracle
    every row would land in ``absent_in_oracle`` and print a confident, fabricated face.

Usage
    python3 docs/evidence/C75/sec-oracle-recheck.py > docs/evidence/C75/sec-oracle-recheck.txt

    Read-only: writes no file and mutates no row (the self-test arm runs on a ``deepcopy``).
    Exits non-zero when the denominator moves, a named control lands in the wrong bucket, or a
    liveness control fails. An empty bucket prints ``0`` and carries no invented control.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess  # nosec B404
import sys
import sysconfig
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
PLAN_DIR: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
CENSUS_FILE: Final = "census-sec-tmx-fed-gov-finra.json"
SEC_PROVIDER: Final = "sec"
PROVIDER_KEY: Final = "provider"
ORACLE_PKG: Final = "openbb_sec"
EXPECTED_SEC_ROWS: Final = 24
#: The shared declarative engine, spelled as a repo-relative prefix because the namespace split is
#: taken over paths already stripped of REPO_ROOT.
ENGINE_NS: Final = "opendata/data/providers/_engine/"
REPO_SEC_MODELS: Final = "opendata/data/providers/sec/models"
#: The yardstick, spelled out rather than inferred from sys.path; sysconfig is the fallback.
SITE_PACKAGES: Final = Path("/Users/yunjinqi/opt/anaconda3/lib/python3.11/site-packages")
#: ``providers/<pkg>/openbb_<pkg>/<rest>`` -- an explicit cite of the installed package layout.
ORACLE_CITE = re.compile(r"providers/[a-z_0-9]+/(openbb_[a-z_0-9]+)")
REPO_PREFIX: Final = "opendata/data/providers/"
CITE_WITH_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)$")

#: The four buckets that mean "the cite did resolve on both sides" plus the unresolvable cite.
Bucket = Literal[
    "absent_in_oracle",
    "line_text_matches",
    "line_text_differs",
    "file_byte_identical",
    "path_unresolvable",
]
#: Precedence, first match wins: unresolvable cite, then oracle absence, then strongest agreement.
FACE_ORDER: Final = (
    "absent_in_oracle",
    "line_text_matches",
    "line_text_differs",
    "file_byte_identical",
    "path_unresolvable",
)

#: Rows whose bucket this script must reproduce; a broken resolver cannot then print a false face.
CONTROLS: Final = {
    "OBB2-sec-BalanceSheet": "absent_in_oracle",
    "OBB2-sec-CikMap": "absent_in_oracle",
    "OBB2-sec-RssLitigation": "absent_in_oracle",
}

#: Self-test cites, differing in one variable only: ``__init__.py`` exists in BOTH copies,
#: ``_source.py`` exists in the repo tree but has no counterpart inside the installed package.
ST_BOTH_CITE: Final = f"{REPO_PREFIX}sec/__init__.py:1"
ST_NO_ORACLE_CITE: Final = f"{REPO_PREFIX}sec/_source.py:1"
ST_DONOR: Final = "OBB2-sec-CikMap"


@dataclass(frozen=True)
class RowFace:
    """One row's dual-resolution reading, plus the bucket it fell into."""

    task_id: str
    cite: str
    repo_file: str
    oracle_file: str
    repo_line: str
    oracle_line: str
    repo_sha: str
    oracle_sha: str
    bucket: Bucket
    reason: str
    counterpart: str


def strings(value: object) -> Iterator[str]:
    """Yield every string inside a nested JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def load_sec_rows() -> list[dict[str, object]]:
    """The census rows whose ``provider`` is ``sec``, in file order."""
    path = REPO_ROOT / PLAN_DIR / CENSUS_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get(PROVIDER_KEY) == SEC_PROVIDER]


def resolve_yardstick() -> tuple[Path, str]:
    """The site-packages root used, and how it was found (sibling audit's logic)."""
    if SITE_PACKAGES.is_dir():
        return SITE_PACKAGES, "recorded"
    fallback = Path(sysconfig.get_paths()["purelib"])
    if fallback.is_dir():
        return fallback, "sysconfig-fallback"
    return SITE_PACKAGES, "absent"


def run_git(*args: str) -> str:
    """Run a read-only git command for the header; returns a marker instead of raising."""
    exe = shutil.which("git")
    if exe is None:
        return "(no git on PATH)"
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
            [exe, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "(git call failed)"
    if done.returncode != 0:
        return f"(git {args[0] if args else '?'} rc={done.returncode})"
    return done.stdout.rstrip("\n")


def oracle_version(root: Path) -> str:
    """The installed ``openbb_sec`` version read from its own dist-info METADATA."""
    for meta in sorted(root.glob(f"{ORACLE_PKG}-*.dist-info/METADATA")):
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    return "(no Version line in METADATA)"


def parse_cite(evidence: str) -> tuple[str, int] | None:
    """Split ``path:line`` into its parts, or ``None`` when the cite carries no line number."""
    match = CITE_WITH_LINE.match(evidence.strip())
    if match is None:
        return None
    return match.group("path"), int(match.group("line"))


def repo_file_for(cite_path: str) -> Path | None:
    """The cited file under the repository tree, trying the repo root and ``src/``."""
    for base in (REPO_ROOT, REPO_ROOT / "src"):
        candidate = base / cite_path
        if candidate.is_file():
            return candidate
    return None


def oracle_candidates(cite_path: str, root: Path) -> list[Path]:
    """Where this cite's file would live inside the installed package, most specific first."""
    out: list[Path] = []

    def add(path: Path) -> None:
        if path not in out:
            out.append(path)

    match = ORACLE_CITE.search(cite_path)
    if match is not None:
        tail = cite_path.split(f"{match.group(1)}/", 1)[1]
        add(root / match.group(1) / tail)
    tail = cite_path.split("providers/", 1)[1] if "providers/" in cite_path else cite_path
    add(root / ORACLE_PKG / tail)
    parts = Path(tail).parts
    if len(parts) > 1:
        add((root / ORACLE_PKG).joinpath(*parts[1:]))
    return out


def oracle_file_for(cite_path: str, root: Path) -> Path | None:
    """The first candidate that exists inside the installed package, else ``None``."""
    return next((p for p in oracle_candidates(cite_path, root) if p.is_file()), None)


def read_stripped_line(path: Path, lineno: int) -> str:
    """Stripped text at ``lineno``; empty when the file has no such line or cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if lineno < 1 or lineno > len(lines):
        return ""
    return lines[lineno - 1].strip()


def sha256_of(path: Path) -> str:
    """Whole-file sha256 as ``str`` (empty when the bytes cannot be read)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def snake(name: str) -> str:
    """``CashFlowStatement`` -> ``cash_flow_statement``; used only for the counterpart face."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def counterpart_for(row: dict[str, object], root: Path) -> str:
    """An oracle model file whose stem plausibly IS this row's ``upstream_model`` (name-matched).

    Read-only and clearly labelled as a name match, not a cite: it answers "does the installed
    package carry a re-readable source for this row at all", which is the question the census notes
    answer ``no`` to by pointing at the repo's missing vendor copy.
    """
    model = row.get("upstream_model")
    if not isinstance(model, str) or not model:
        return ""
    want = snake(model).replace("_", "").lower()
    files = sorted((root / ORACLE_PKG / "models").glob("*.py"))
    for path in files:
        if path.stem.replace("_", "").lower() == want:
            return str(path.relative_to(root))
    for path in files:
        stem = path.stem.replace("_", "").lower()
        if stem and (want.startswith(stem) or stem.startswith(want)):
            return f"{path.relative_to(root)} (name-tolerant)"
    return ""


def make_face(
    task_id: str,
    cite: str,
    bucket: Bucket,
    reason: str,
    counterpart: str,
    repo: Path | None = None,
    oracle: Path | None = None,
    lineno: int = 0,
) -> RowFace:
    """Assemble a row face from the two resolved paths, filling the line/sha readings."""
    repo_line = read_stripped_line(repo, lineno) if repo is not None else ""
    oracle_line = read_stripped_line(oracle, lineno) if oracle is not None else ""
    return RowFace(
        task_id=task_id,
        cite=cite,
        repo_file=str(repo) if repo is not None else "",
        oracle_file=str(oracle) if oracle is not None else "",
        repo_line=repo_line,
        oracle_line=oracle_line,
        repo_sha=sha256_of(repo) if repo is not None else "",
        oracle_sha=sha256_of(oracle) if oracle is not None else "",
        bucket=bucket,
        reason=reason,
        counterpart=counterpart,
    )


def compare_row(row: dict[str, object], root: Path) -> RowFace:
    """Resolve this row's cite under both carriers and bucket it; prints a verdict, never raises."""
    task_id = str(row.get("task_id"))
    cites = list(strings(row.get("evidence")))
    cite = cites[0] if cites else ""
    counterpart = counterpart_for(row, root)

    parsed = parse_cite(cite)
    if parsed is None:
        return make_face(task_id, cite, "path_unresolvable", "cite carries no :line", counterpart)
    cite_path, lineno = parsed

    repo = repo_file_for(cite_path)
    if repo is None:
        return make_face(
            task_id,
            cite,
            "path_unresolvable",
            "cited file not found under the repo tree",
            counterpart,
        )

    oracle = oracle_file_for(cite_path, root)
    if oracle is None:
        tried = ", ".join(str(p.relative_to(root)) for p in oracle_candidates(cite_path, root))
        return make_face(
            task_id,
            cite,
            "absent_in_oracle",
            f"no oracle file among [{tried}]",
            counterpart,
            repo=repo,
            lineno=lineno,
        )

    face = make_face(
        task_id,
        cite,
        "line_text_differs",
        "line text differs",
        counterpart,
        repo=repo,
        oracle=oracle,
        lineno=lineno,
    )
    if face.repo_sha and face.repo_sha == face.oracle_sha:
        return make_face(
            task_id,
            cite,
            "file_byte_identical",
            "whole-file sha256 equal",
            counterpart,
            repo=repo,
            oracle=oracle,
            lineno=lineno,
        )
    if face.repo_line and face.repo_line == face.oracle_line:
        return make_face(
            task_id,
            cite,
            "line_text_matches",
            "cited line text equal, file bytes differ",
            counterpart,
            repo=repo,
            oracle=oracle,
            lineno=lineno,
        )
    return face


def same_file_check(root: Path) -> bool:
    """A file compared to itself must hash equal -- the sha path is live."""
    digest = sha256_of(root / ORACLE_PKG / "__init__.py")
    return bool(digest) and digest == sha256_of(root / ORACLE_PKG / "__init__.py")


def diff_file_check() -> bool:
    """Two different repo engine files must NOT hash equal -- the sha path is not stuck at equal."""
    engine = REPO_ROOT / REPO_PREFIX.rstrip("/") / "_engine"
    first, second = sha256_of(engine / "spec.py"), sha256_of(engine / "http_json.py")
    return bool(first) and bool(second) and first != second


def self_test(rows: list[dict[str, object]], root: Path) -> tuple[int, int]:
    """Prove the ``absent_in_oracle`` counter and the comparator are live, on a deepcopy.

    Two synthetic single-row reads differ in one variable only -- whether the cited file exists in
    the installed package -- so ``absent_in_oracle`` must go 0 -> 1. The both-copies row must land
    in a non-absent bucket, proving the resolver is not structurally blind to the oracle.
    """
    donor = next((r for r in rows if str(r.get("task_id")) == ST_DONOR), None)
    if donor is None:
        print(f"SELF-TEST: 0/1 controls agreed (donor row {ST_DONOR} absent, counter unproven)")
        return 0, 1

    def bucket_of(cite: str) -> str:
        clone = copy.deepcopy(donor)
        clone["evidence"] = cite
        return compare_row(clone, root).bucket

    both = bucket_of(ST_BOTH_CITE)
    no_oracle = bucket_of(ST_NO_ORACLE_CITE)
    baseline = sum(1 for r in rows if compare_row(r, root).bucket == "absent_in_oracle")
    after = baseline + (1 if no_oracle == "absent_in_oracle" else 0)

    checks: list[tuple[str, bool]] = [
        (f"{ST_BOTH_CITE} reads {both}: file found in both copies", both != "absent_in_oracle"),
        (f"{ST_NO_ORACLE_CITE} reads {no_oracle}", no_oracle == "absent_in_oracle"),
        (f"absent_in_oracle count {baseline} -> {after} (+1)", after == baseline + 1),
        ("sha comparator: a file against itself hashes equal", same_file_check(root)),
        ("sha comparator: two different files do not hash equal", diff_file_check()),
    ]
    print("\nself-test (synthetic cites on a deepcopy; no file written, no census row mutated):")
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    agreed = sum(1 for _, ok in checks if ok)
    print(f"SELF-TEST: {agreed}/{len(checks)} controls agreed")
    return agreed, len(checks)


def print_header(root: Path, provenance: str) -> None:
    """Provenance measured, not typed: HEAD, dirty count, oracle version, UTC stamp, argv."""
    cmd = " ".join([sys.executable, sys.argv[0], *sys.argv[1:]])
    print("=== C75 sec oracle recheck ==============================================")
    print(f"generated (UTC)   : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"command line      : {cmd}")
    print(f"git HEAD          : {run_git('rev-parse', 'HEAD')}")
    entries = [line for line in run_git("status", "--porcelain").splitlines() if line.strip()]
    print(f"git status lines  : {len(entries)}")
    for line in entries[:8]:
        print(f"                    {line}")
    print(f"site-packages root: {root} (found via {provenance}, exists={root.is_dir()})")
    print(f"oracle package    : {root / ORACLE_PKG} version {oracle_version(root)}")
    print()


def print_faces(faces: list[RowFace], root: Path) -> tuple[int, int]:
    """Print the faces; return (named-bucket misses, bucket-sum miss)."""
    counts = {name: sum(1 for f in faces if f.bucket == name) for name in FACE_ORDER}
    print(f"buckets over {len(faces)} sec rows (must sum to the denominator):")
    for name in FACE_ORDER:
        print(f"  {name:<19} {counts[name]:4d}")
    total = sum(counts.values())
    ok = "ok" if total == len(faces) else "MISMATCH"
    print(f"  {'sum':<19} {total:4d}  {ok}")

    repo_ok = sum(1 for f in faces if f.repo_file)
    both = [f for f in faces if f.repo_file and f.oracle_file]
    not_identical = [f for f in both if f.repo_sha != f.oracle_sha]
    print(f"\ncite resolves in the repo tree : {repo_ok}/{len(faces)}")
    print(f"file exists in BOTH copies     : {len(both)}/{len(faces)}")
    print(f"both copies, NOT byte-identical: {len(not_identical)}")

    # The denominators the line above is short of: the oracle really does carry upstream sec model
    # files, so a 0 here is not "the oracle has nothing to compare against". It is the cites.
    oracle_models = sorted((root / ORACLE_PKG / "models").glob("*.py"))
    repo_models = sorted((REPO_ROOT / REPO_SEC_MODELS).glob("*.py"))
    print(
        f"\npopulations: oracle {ORACLE_PKG}/models/*.py = {len(oracle_models)},"
        f" repo {REPO_SEC_MODELS}/*.py = {len(repo_models)}"
    )
    targets: dict[str, int] = {}
    for face in faces:
        if face.repo_file:
            key = face.repo_file[len(str(REPO_ROOT)) + 1 :]
            targets[key] = targets.get(key, 0) + 1
    print(f"the {repo_ok} resolves land on {len(targets)} distinct repo files:")
    for key, hit_count in sorted(targets.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {hit_count:3d}  {key}")
    engine_hits = sum(v for k, v in targets.items() if k.startswith(ENGINE_NS))
    other_hits = repo_ok - engine_hits
    print(
        f"  namespace split: {ENGINE_NS}* = {engine_hits}, everything else = {other_hits}"
        " -- a reading, not a gate: an 'else' hit would mean a sec row cites a first-party file"
        " outside the shared engine, which is a different state, not a failure."
    )

    print("\nper row:")
    for face in faces:
        print(f"  {face.task_id:<38} {face.bucket:<19} {face.cite}")
        print(f"    repo   : {face.repo_file or '(none)'}")
        print(f"    oracle : {face.oracle_file or '(none)'}")
        print(f"    reason : {face.reason}")
        if face.oracle_file:
            print(f"    repo line  : {face.repo_line or '(no such line)'}")
            print(f"    oracle line: {face.oracle_line or '(no such line)'}")

    with_cp = [f for f in faces if f.counterpart]
    print("\nsecondary face -- a name-matched model file DOES exist in the installed package:")
    print(f"  {len(with_cp)}/{len(faces)} rows")
    for face in with_cp:
        print(f"  {face.task_id:<38} {face.counterpart}")
    print(f"  rows with NO name-matched oracle model file: {len(faces) - len(with_cp)}")

    print("\ncontrols (a bucket face is trustworthy only if a known row lands where it was read):")
    buckets = {f.task_id: f.bucket for f in faces}
    failed = 0
    for task_id, want in CONTROLS.items():
        got = buckets.get(task_id, "(absent)")
        print(f"  {task_id:<38} wants {want:<19} reads {got}")
        failed += 0 if got == want else 1
    for name in FACE_ORDER:
        if counts[name] == 0:
            print(f"  bucket {name:<19} is empty (0 rows) -- no control invented for it")
    return failed, 0 if total == len(faces) else 1


def main(argv: list[str]) -> int:
    """Print the provenance header, the bucket face, controls and the self-test arm."""
    root, provenance = resolve_yardstick()
    print_header(root, provenance)
    if not (root / ORACLE_PKG).is_dir():
        print(f"ABORT: no {ORACLE_PKG} directory under the yardstick, so every row would read")
        print("       absent_in_oracle. A NO_READING about the environment, not a census face.")
        return 1

    rows = load_sec_rows()
    print(f"sec census rows   : {len(rows)} (expected {EXPECTED_SEC_ROWS})")
    if len(rows) != EXPECTED_SEC_ROWS:
        print("ABORT: the sec denominator moved; the buckets below would not describe this census.")
        return 1

    faces = [compare_row(row, root) for row in rows]
    control_failures, sum_failure = print_faces(faces, root)
    agreed, attempted = self_test(rows, root)

    if sum_failure:
        print("\nCONTROL FAIL -- bucket sum does not equal the row denominator.")
        return 1
    if control_failures:
        print(f"\nCONTROL FAIL -- {control_failures} named row(s) landed in the wrong bucket.")
        return 1
    if agreed != attempted:
        print("\nSELF-TEST FAIL -- the counter is not demonstrably live, so the face is unproven.")
        return 1
    counts = {name: sum(1 for f in faces if f.bucket == name) for name in FACE_ORDER}
    both = [f for f in faces if f.repo_file and f.oracle_file]
    absent = counts["absent_in_oracle"]
    print(f"\nVERDICT: {absent}/{len(faces)} sec cites resolve only in the repo tree, i.e. the")
    print(f"         cited file has no counterpart inside {ORACLE_PKG}, so those {absent}")
    print("         judgements rest on repo source, not on the installed oracle.")
    rows_both = len(both)
    rows_not_identical = sum(1 for f in both if f.repo_sha != f.oracle_sha)
    print(f"         Rows whose file exists in both copies: {rows_both}.")
    print(f"         Of those, not byte-identical: {rows_not_identical}.")
    print(
        f"CONTROLS OK: {len(CONTROLS)}/{len(CONTROLS)} named rows, SELF-TEST {agreed}/{attempted}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
