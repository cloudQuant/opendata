"""Rev-pinned reachability face: what the remaining provider waves are actually gated on.

The iteration-2 objective is 32 providers / 350 models. This face reads that roster and three
in-carrier facts about every provider -- (a) does the census cover its rows, (b) can the engine
declare them at all, (c) is the source registered and packaged in this repo -- and publishes the
gate each provider is stuck on. Every number is recomputed from the shipped tree at one git rev, so
a concurrent worker editing the worktree cannot move the reading.

Run from the repo root (the rev is optional, HEAD by default):

    python /path/to/this/file.py [rev]

Faces:
  R1 denominator  roster models == censused rows + named per-provider gap
  R2 ceiling      rows whose needs hold an engine_work=False label, recomputed independently of
                  the artifact's stored rows_outside_engine list and compared to it
  R3 packaging    roster providers with a package directory at the rev vs without
  R3b work-bucket census rows split between packaged and no-package providers, so a wave estimate
                  can be read off the same measurement instead of typed from prose
  R4 rights       RIGHTS2 status cross-checked against a real in-tree directory row in the registry
  R4b rights      providers connected (package directory) while RIGHTS2 still says NOT_REGISTERED
  R5 dispatch     per provider: rows / expressible / engine-reachable / package / rights -> gate

Judges (J1 identity, J2 cross-check, J3 no stale rights status, J4 no connect-before-register,
J5 the two buckets partition the census) must be satisfiable AND breakable. Arms A1-A7 tamper one
INPUT each and require the named reading to move; an arm that cannot move the verdict makes the face
a decoration, so the script exits non-zero when any arm fails to differ.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import pathlib
import re
import shutil
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
import tempfile
from typing import Any

PLAN_DIR_REL = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
ROSTER_REL = f"{PLAN_DIR_REL}/Provider覆盖清单.md"
RIGHTS2_REL = f"{PLAN_DIR_REL}/前置条件与决策清单.md"
REGISTRY_REL = "docs/data-rights-registry.md"
ARTIFACT_REL = f"{PLAN_DIR_REL}/capability-roadmap.json"
CENSUS_MODULE_REL = "scripts/quality/model_capability_census.py"
PROVIDERS_ROOT_REL = "opendata/data/providers"

FAILURES: list[str] = []  # the instrument is broken: an arm could not move a reading
FINDINGS: list[str] = []  # the tree violates a rule: publishable, and names the act that closes it


def _git(repo: pathlib.Path, *args: str) -> bytes:
    """Run a read-only git command with a fixed argv and no shell."""
    return subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _extract_to(
    repo: pathlib.Path, rev: str, paths: tuple[str, ...], work: pathlib.Path
) -> pathlib.Path:
    """Materialise one rev's copies of ``paths`` so every face reads committed bytes, not the tree.

    ``WORK`` is the one accepted non-rev: it copies the current worktree instead of ``git archive``,
    so a fix that has not been committed yet can still be re-measured. A WORK reading is a snapshot
    and can move under a concurrent writer; a named rev cannot.
    """
    root = work / rev
    root.mkdir(parents=True, exist_ok=True)
    if rev.upper() in {"WORK", "WORKTREE"}:
        for rel in paths:
            src = repo / rel
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        return root
    pipe = subprocess.Popen(  # nosec B603, B607  # noqa: S603
        ["git", "-C", str(repo), "archive", rev, *paths],  # noqa: S607
        stdout=subprocess.PIPE,
    )
    if pipe.stdout is None:
        raise SystemExit("git archive gave no stdout")
    subprocess.run(  # nosec B603, B607  # noqa: S603
        ["tar", "-x", "-C", str(root)],  # noqa: S607
        stdin=pipe.stdout,
        check=True,
    )
    rc = pipe.wait()
    if rc != 0:
        raise SystemExit(f"git archive {rev} failed rc={rc}")
    return root


def _read(path: pathlib.Path) -> str:
    """Read a materialised file as UTF-8 text."""
    return path.read_text(encoding="utf-8")


def parse_roster(path: pathlib.Path) -> dict[str, int]:
    """The fixed roster: provider -> model entries, from the 6-cell model table only."""
    roster: dict[str, int] = {}
    for line in _read(path).splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) != 8:
            continue
        name, models, verified = cells[1], cells[2], cells[3]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            continue
        if not re.fullmatch(r"\d+", models) or not re.fullmatch(r"\d+ / \d+", verified):
            continue
        roster[name] = int(models)
    return roster


def parse_rights2(path: pathlib.Path) -> dict[str, str]:
    """RIGHTS2-* task rows: provider -> declared review status (parentheticals dropped)."""
    out: dict[str, str] = {}
    for line in _read(path).splitlines():
        m = re.match(r"^\| RIGHTS2-([a-z0-9_]+) \| ([a-z0-9_]+) \| .*? \| ([A-Z_]+)", line)
        if m:
            out[m.group(2)] = m.group(3)
    return out


def registry_dir_providers(path: pathlib.Path) -> set[str]:
    """Providers the registry gives a numbered row naming THIS repo's package directory."""
    found: set[str] = set()
    for line in _read(path).splitlines():
        if not re.match(r"^\| \d+ \|", line):
            continue
        found.update(re.findall(r"opendata/data/providers/([a-z0-9_]+)", line))
    return found


def package_dirs(repo: pathlib.Path, rev: str | None) -> set[str]:
    """Provider package directories, either from the worktree (``rev is None``) or from a rev."""
    if rev is None:
        base = repo / PROVIDERS_ROOT_REL
        return {p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", "__"))}
    out = _git(repo, "ls-tree", "-d", "--name-only", rev, "--", f"{PROVIDERS_ROOT_REL}/").decode()
    return {
        pathlib.PurePosixPath(line).name
        for line in out.splitlines()
        if line and not pathlib.PurePosixPath(line).name.startswith(("_", "__"))
    }


@dataclasses.dataclass(frozen=True)
class Reading:
    """Every count this face publishes, from one set of inputs."""

    rows: int
    expressible: int
    outside_rows: int
    engine_reachable: int
    roster_providers: int
    roster_models: int
    gap_rows: dict[str, int]
    packaged: list[str]
    unpackaged: list[str]
    stale_rights: list[str]
    connect_before_register: list[str]
    rights_of_stale: dict[str, str]
    per_provider: dict[str, dict[str, Any]]
    outside_provider_split: dict[str, int]
    rows_in_unpackaged: int
    reachable_in_unpackaged: int
    rows_in_packaged: int
    census_providers_not_in_roster: list[str]


def measure(
    rows: list[Any],
    roster: dict[str, int],
    rights2: dict[str, str],
    registry_dirs: set[str],
    packages: set[str],
    artifact_outside_ids: set[str],
    outside_caps: set[str],
) -> Reading:
    """Recompute every reading; an arm's job is to change exactly one of the six inputs."""
    by_provider: dict[str, dict[str, Any]] = {}
    outside_ids: set[str] = set()
    for r in rows:
        cell = by_provider.setdefault(r.provider, {"rows": 0, "expressible": 0, "reachable": 0})
        cell["rows"] += 1
        if r.expressible_today:
            cell["expressible"] += 1
        if r.needs & outside_caps:
            outside_ids.add(r.task_id)
        else:
            cell["reachable"] += 1
    expressible = sum(1 for r in rows if r.expressible_today)
    stale = sorted(p for p in roster if rights2.get(p) == "NOT_REGISTERED" and p in registry_dirs)
    gap = {p: n - by_provider.get(p, {"rows": 0})["rows"] for p, n in roster.items()}
    gap = {p: n for p, n in gap.items() if n}
    packaged = sorted(p for p in roster if p in packages)
    unpackaged = sorted(p for p in roster if p not in packages)
    rows_unp = sum(by_provider.get(p, {"rows": 0})["rows"] for p in unpackaged)
    reach_unp = sum(by_provider.get(p, {"reachable": 0})["reachable"] for p in unpackaged)
    rows_pkg = sum(by_provider.get(p, {"rows": 0})["rows"] for p in packaged)
    not_in_roster = sorted(p for p in by_provider if p not in roster)
    connected_before_registered = sorted(
        p for p in roster if p in packages and rights2.get(p) == "NOT_REGISTERED"
    )
    split: dict[str, int] = collections.Counter()
    for r in rows:
        if r.task_id in outside_ids:
            split[r.provider] += 1
    return Reading(
        rows=len(rows),
        expressible=expressible,
        outside_rows=len(outside_ids),
        engine_reachable=len(rows) - len(outside_ids),
        roster_providers=len(roster),
        roster_models=sum(roster.values()),
        gap_rows=gap,
        packaged=packaged,
        unpackaged=unpackaged,
        stale_rights=stale,
        connect_before_register=connected_before_registered,
        rights_of_stale={p: f"RIGHTS2={rights2.get(p)} registry_dir_row=yes" for p in stale},
        per_provider=by_provider,
        outside_provider_split=dict(sorted(split.items(), key=lambda kv: -kv[1])),
        rows_in_unpackaged=rows_unp,
        reachable_in_unpackaged=reach_unp,
        rows_in_packaged=rows_pkg,
        census_providers_not_in_roster=not_in_roster,
    )


def _artifact_outside_ids(path: pathlib.Path) -> set[str]:
    """The outside-engine task ids the roadmap artifact stores."""
    payload = json.loads(_read(path))
    return {row["task_id"] for row in payload["roadmap"]["rows_outside_engine"]}


def print_faces(
    base: Reading, artifact_ids: set[str], rev: str, rights2: dict[str, str]
) -> tuple[bool, bool, bool, bool, bool]:
    """Print R1-R5 for one reading and return the five judge verdicts."""
    tag = (
        "worktree snapshot, may move under a concurrent writer"
        if rev.upper() in {"WORK", "WORKTREE"}
        else "committed bytes at this rev"
    )
    print(f"== rev {rev} ({tag}) ==")
    j1 = base.roster_models - base.rows == sum(base.gap_rows.values())
    print(
        f"R1 denominator: providers={base.roster_providers} roster_models={base.roster_models} "
        f"censused_rows={base.rows} gap={base.roster_models - base.rows} "
        f"named_gap_providers={base.gap_rows} identity={j1}"
    )
    j2 = len(artifact_ids) == base.outside_rows
    print(
        f"R2 ceiling: engine_reachable={base.engine_reachable} outside_engine={base.outside_rows} "
        f"artifact_rows_outside_engine={len(artifact_ids)} expressible_today={base.expressible} "
        f"agree={j2}"
    )
    print(f"   outside split by provider: {base.outside_provider_split}")
    print(
        f"R3 packaging: packaged={len(base.packaged)}/{base.roster_providers} {base.packaged} "
        f"unpackaged={len(base.unpackaged)}"
    )
    j5 = base.rows_in_packaged + base.rows_in_unpackaged == base.rows
    print(
        f"R3b work-bucket: providers_without_package={len(base.unpackaged)} "
        f"rows_in_unpackaged={base.rows_in_unpackaged} "
        f"engine_reachable_in_unpackaged={base.reachable_in_unpackaged} "
        f"rows_in_packaged={base.rows_in_packaged} "
        f"roster_missing_census_providers={base.census_providers_not_in_roster} "
        f"partition_of_census={j5}"
    )
    j3 = not base.stale_rights
    print(
        f"R4 rights: stale_status_rows={len(base.stale_rights)} {base.rights_of_stale} "
        f"consistent={j3}"
    )
    j4 = not base.connect_before_register
    print(
        f"R4b connect-before-register: packaged_while_NOT_REGISTERED="
        f"{len(base.connect_before_register)} {base.connect_before_register} "
        f"clean={j4} -- 前置条件与决策清单 §1 requires 接入前逐产品三项结论"
    )
    print("R5 dispatch table (rows/expr/reachable -> gate):")
    for p in sorted(base.per_provider, key=lambda x: (-base.per_provider[x]["rows"], x)):
        c = base.per_provider[p]
        rights = {
            "NOT_REGISTERED": "NO-ROW",
            "EXISTING_SOURCE_REVIEW_MODEL_PENDING": "v1.2",
            "SOURCE_ROW_REGISTERED_MODEL_PENDING": "v1.5",
        }.get(rights2.get(p, "?"), rights2.get(p, "?")[:12])
        print(
            f"  {p:19s} rows={c['rows']:3d} expr={c['expressible']:2d} reach={c['reachable']:3d} "
            f"pkg={'Y' if p in base.packaged else '.'} rights={rights:12s}"
        )
    return j1, j2, j3, j4, j5


def arm(
    label: str,
    base: Reading,
    field: str,
    expected_delta: int,
    tampered: Reading,
) -> None:
    """Require the tampered input to move exactly this field by exactly this much."""
    before = getattr(base, field)
    after = getattr(tampered, field)
    if isinstance(before, list):
        before_n, after_n = len(before), len(after)
    else:
        before_n, after_n = before, after
    delta = after_n - before_n
    moved = delta == expected_delta
    print(
        f"{label}: {field} {before_n} -> {after_n} (delta {delta:+d}, "
        f"required {expected_delta:+d}) -> {'MOVED' if moved else 'DID NOT MOVE'}"
    )
    if not moved:
        FAILURES.append(f"{label} must move {field} by {expected_delta:+d}, got {delta:+d}")


def main() -> int:
    """Measure one rev, print every face, then prove each judge can still fire."""
    rev = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    if rev.startswith("-"):
        raise SystemExit(f"rev must be a name, not an option: {rev!r}")
    repo = pathlib.Path(_git(pathlib.Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    work = pathlib.Path(tempfile.mkdtemp(prefix="c96_reachability_"))
    base_dir = _extract_to(
        repo,
        rev,
        (PLAN_DIR_REL, CENSUS_MODULE_REL, REGISTRY_REL),
        work,
    )
    sys.path.insert(0, str(base_dir))
    from scripts.quality import model_capability_census as mcc

    plan = base_dir / PLAN_DIR_REL
    rows = mcc.load_rows(plan)
    outside_caps = {e.capability for e in mcc.REGISTRY if not e.engine_work}
    roster = parse_roster(base_dir / ROSTER_REL)
    rights2 = parse_rights2(base_dir / RIGHTS2_REL)
    registry_dirs = registry_dir_providers(base_dir / REGISTRY_REL)
    packages = package_dirs(repo, None if rev.upper() in {"WORK", "WORKTREE"} else rev)
    artifact_ids = _artifact_outside_ids(base_dir / ARTIFACT_REL)

    base = measure(rows, roster, rights2, registry_dirs, packages, artifact_ids, outside_caps)
    j1, j2, j3, j4, j5 = print_faces(base, artifact_ids, rev, rights2)
    if not j1:
        FAILURES.append("R1 identity roster_models - rows == sum(named gaps) is false")
    if not j2:
        FAILURES.append("R2 recomputed outside-engine count disagrees with the stored artifact")
    if not j3:
        FINDINGS.append(
            f"R4 stale rights status: {base.rights_of_stale} -- the decision list and the "
            "registry disagree about a source that now has a numbered in-tree row"
        )
    if not j4:
        FINDINGS.append(
            f"R4b connected while NOT_REGISTERED: {base.connect_before_register} -- each has a "
            "package directory in this repo but no source-specific row in "
            "docs/data-rights-registry.md; closing it needs that row quoted from the source's own "
            "terms, which cannot be written offline"
        )

    if base.census_providers_not_in_roster:
        FINDINGS.append(
            f"R3b census rows from providers the roster does not name: "
            f"{base.census_providers_not_in_roster} -- the partition then cannot close, and the "
            "roster is the authoritative denominator per 前置条件与决策清单"
        )

    if j5 == bool(base.census_providers_not_in_roster):
        FAILURES.append(
            f"R3b self-consistency: partition_of_census={j5} but the named roster/census "
            f"difference is {base.census_providers_not_in_roster} -- one of the two readings "
            "is mis-computed"
        )

    print("\n-- counterfact arms (each tampers one input) --")
    one_outside = next(r for r in rows if len(r.needs & outside_caps) == 1 and r.provider == "fred")
    stripped = [
        dataclasses.replace(r, needs=frozenset(r.needs - outside_caps))
        if r.task_id == one_outside.task_id
        else r
        for r in rows
    ]
    arm(
        f"A1 strip the only outside label off {one_outside.task_id}",
        base,
        "outside_rows",
        -1,
        measure(stripped, roster, rights2, registry_dirs, packages, artifact_ids, outside_caps),
    )
    reachable_row = next(
        r for r in rows if r.provider == "intrinio" and not (r.needs & outside_caps)
    )
    forged = [
        dataclasses.replace(r, needs=frozenset(r.needs | {"outside.sdk"}))
        if r.task_id == reachable_row.task_id
        else r
        for r in rows
    ]
    tampered = measure(forged, roster, rights2, registry_dirs, packages, artifact_ids, outside_caps)
    arm(
        f"A2 forge outside.sdk onto {reachable_row.task_id}",
        base,
        "outside_rows",
        +1,
        tampered,
    )
    if tampered.engine_reachable != base.engine_reachable - 1:
        FAILURES.append(
            "A2 engine_reachable must fall by 1 with the forged outside label, got "
            f"{base.engine_reachable} -> {tampered.engine_reachable}"
        )
    print(
        f"A2 engine_reachable {base.engine_reachable} -> {tampered.engine_reachable} "
        "(required -1) -> MOVED"
    )

    roster_minus_fmp = {p: n for p, n in roster.items() if p != "fmp"}
    arm(
        "A3 drop fmp from the roster table",
        base,
        "roster_models",
        -roster.get("fmp", 0),
        measure(
            rows, roster_minus_fmp, rights2, registry_dirs, packages, artifact_ids, outside_caps
        ),
    )
    packages_minus_bls = {p for p in packages if p != "bls"}
    tampered4 = measure(
        rows, roster, rights2, registry_dirs, packages_minus_bls, artifact_ids, outside_caps
    )
    arm(
        "A4 pretend bls has no package directory",
        base,
        "packaged",
        -1,
        tampered4,
    )
    bls_rows = base.per_provider.get("bls", {}).get("rows", 0)
    arm(
        f"A4b bls's {bls_rows} census rows move into the no-package bucket",
        base,
        "rows_in_unpackaged",
        +bls_rows,
        tampered4,
    )
    dirs_forged = registry_dirs | {"sec"}
    arm(
        "A5 forge a numbered registry row for sec while RIGHTS2 still says NOT_REGISTERED",
        base,
        "stale_rights",
        +1,
        measure(rows, roster, rights2, dirs_forged, packages, artifact_ids, outside_caps),
    )
    if base.stale_rights:
        dropped = {p for p in registry_dirs if p != base.stale_rights[0]}
        arm(
            f"A5n delete {base.stale_rights[0]}'s numbered registry row",
            base,
            "stale_rights",
            -1,
            measure(rows, roster, rights2, dropped, packages, artifact_ids, outside_caps),
        )
    else:
        print("A5n skipped: stale_rights is already empty at this rev, A5 proves the other side")
    packages_minus_sec = {p for p in packages if p != "sec"}
    arm(
        "A7 remove sec's package directory (R4b reads the package+status pair, not either alone)",
        base,
        "connect_before_register",
        -1,
        measure(
            rows, roster, rights2, registry_dirs, packages_minus_sec, artifact_ids, outside_caps
        ),
    )
    ids_minus_one = set(artifact_ids)
    ids_minus_one.discard(next(iter(sorted(artifact_ids))))
    _, agree, _ = judge_quiet(
        rows, roster, rights2, dirs_forged, packages, ids_minus_one, outside_caps
    )
    print(
        f"A6 artifact stores one row fewer: recomputed={base.outside_rows} "
        f"tampered_artifact={len(ids_minus_one)} cross_check_agrees={agree} "
        f"-> {'MOVED' if not agree else 'DID NOT MOVE'}"
    )
    if agree:
        FAILURES.append("A6 the R2 cross-check must go red when the artifact list shrinks by one")

    print(f"\nFINDINGS (rules this tree violates): {len(FINDINGS)}")
    for f in FINDINGS:
        print("  -", f)
    print(f"INSTRUMENT FAILURES (an arm could not move its reading): {len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    if FAILURES:
        print("VERDICT: INSTRUMENT BROKEN")
        return 2
    if FINDINGS:
        print("VERDICT: RED WITH NAMED BLOCKERS")
        return 1
    print("VERDICT: PASS")
    return 0


def judge_quiet(
    rows: list[Any],
    roster: dict[str, int],
    rights2: dict[str, str],
    registry_dirs: set[str],
    packages: set[str],
    artifact_ids: set[str],
    outside_caps: set[str],
) -> tuple[bool, bool, bool]:
    """Re-judge R1/R2/R4 for a tampered input without printing a second face."""
    got = measure(rows, roster, rights2, registry_dirs, packages, artifact_ids, outside_caps)
    return (
        got.roster_models - got.rows == sum(got.gap_rows.values()),
        len(artifact_ids) == got.outside_rows,
        not got.stale_rights,
    )


if __name__ == "__main__":
    sys.exit(main())
