"""Re-audit the census plane at 238 rows and refresh the audit record's live fields.

C74/C75 audited three census files holding 150 rows against the 350-row ledger. C86 added twelve
providers' files (88 rows), so every number in ``census-audit.json`` that says "150" or "3 files" is
a claim about a tree that no longer exists, and the roadmap instrument's row-count guard is red.
This instrument re-measures the join the old audit performed -- task_id uniqueness, the
``(provider, upstream_model)`` match against the ledger, ``provider_groups`` against the row bodies,
the out-of-scope composition, the evidence-root face, and the hand-vs-registry expressibility
disagreement -- and writes the result into ``census-audit.json`` under ``c86_reaudit``,
archiving the values it replaces instead of overwriting them. The census is read through
:mod:`scripts/quality/model_capability_census` (``load_rows``, ``census_row_files``, the label map,
the greedy cover) rather than rebuilt, so the labels this audit accepts are exactly the labels the
roadmap accepts.

"New this round" is not taken from a constant: it is the difference between the file inventory this
run globs and the ``per_file`` list the audit record itself published last round, so the face cannot
silently age.

Every counter is printed with its denominator, and :func:`self_test` shows each one moving away from
its live reading in a copied tree, because a zero no input can produce proves nothing.

Usage::

    python docs/evidence/C86/census-merge-audit.py                 # read-only report
    python docs/evidence/C86/census-merge-audit.py --json          # the same face as JSON
    python docs/evidence/C86/census-merge-audit.py --write         # refresh census-audit.json
    python docs/evidence/C86/census-merge-audit.py --self-test     # five in-memory tampers
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PLAN_DIR: Final = REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
INSTRUMENT: Final = REPO_ROOT / "scripts/quality/model_capability_census.py"
LEDGER_CSV: Final = PLAN_DIR / "模型级任务清单.csv"
AUDIT_FILE: Final = PLAN_DIR / "census-audit.json"
UPSTREAM_REPO: Final = Path("/Users/yunjinqi/Documents/new_projects/OpenBB")
UPSTREAM_ROOT: Final = UPSTREAM_REPO / "openbb_platform"
EXPECTED_UPSTREAM_COMMIT: Final = "3e071fcc2cd9f891cac6040ae60296dba76dab46"
EVIDENCE_FILE_RE: Final = re.compile(r"[\w./\-]+\.(?:py|csv|json|md|txt)")
ARCHIVED_KEYS: Final = (
    "recomputed",
    "recomputed_total",
    "per_file",
    "ledger_total",
    "arith_verification",
    "out_of_scope_rows",
    "uncovered_providers",
    "evidence_root_audit",
    "upstream_root_ok",
    "upstream_commit_ok",
)


def _load_census_module() -> ModuleType:
    """Import the repo's own census instrument (frozen dataclasses need sys.modules registered)."""
    spec = importlib.util.spec_from_file_location("mcc", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {INSTRUMENT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcc"] = module
    spec.loader.exec_module(module)
    return module


mcc = _load_census_module()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_ledger() -> list[dict[str, str]]:
    """Read the 350-row provider x model ledger as dictionaries (BOM-aware, header-keyed)."""
    with LEDGER_CSV.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def carried_files() -> set[str]:
    """The file inventory the audit record published before this round -- the census's own memory.

    ``per_file`` is a dict keyed by file name in the C74/C75 record, so both a mapping and a bare
    list are accepted; an unreadable shape returns the empty set and says so through
    ``new_files_this_round`` rather than guessing.
    """
    payload = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
    listed = payload.get("per_file")
    if isinstance(listed, dict):
        return {str(name) for name in listed}
    if isinstance(listed, list):
        return {str(name) for name in listed}
    return set()


def _evidence_rel(row: dict[str, object]) -> str:
    """Return the first file path a row's ``evidence`` string names, or ``''`` if it names none."""
    match = EVIDENCE_FILE_RE.search(str(row.get("evidence", "")))
    return match.group(0) if match else ""


def _upstream_head() -> str:
    """Resolve the upstream checkout's HEAD from its git files, without execing anything.

    ``.git/HEAD`` names a ref; the object id lives either in ``.git/refs/heads/<ref>`` or in
    ``.git/packed-refs``. Reading them keeps this instrument's face free of a shell call, which
    would otherwise need a bandit exemption on an evidence script.
    """
    git = UPSTREAM_REPO / ".git"
    head_file = git / "HEAD"
    if not head_file.is_file():
        return ""
    text = head_file.read_text(encoding="utf-8").strip()
    if not text.startswith("ref: "):
        return text
    ref = text[len("ref: ") :]
    loose = git / ref
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip()
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith(("#", "^")):
                continue
            sha, _, name = line.strip().partition(" ")
            if name == ref:
                return sha
    return ""


def measure(census_dir: Path, ledger: list[dict[str, str]], carried: set[str]) -> dict[str, object]:
    """Compute every audit face from the census bodies, the ledger and the live registry."""
    rows = mcc.load_rows(census_dir)
    files = mcc.census_row_files(census_dir)
    bodies: dict[str, list[dict[str, object]]] = {}
    payloads: dict[str, dict[str, object]] = {}
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payloads[path.name] = payload
        bodies[path.name] = list(payload["rows"])

    owner: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for name, entries in bodies.items():
        for row in entries:
            task_id = str(row["task_id"])
            if task_id in owner:
                collisions.setdefault(task_id, [owner[task_id]]).append(name)
            else:
                owner[task_id] = name

    row_by_id = {
        str(row["task_id"]): (name, row) for name, entries in bodies.items() for row in entries
    }
    ledger_by_id = {entry["task_id"]: entry for entry in ledger}
    unknown_ids = sorted(set(owner) - set(ledger_by_id))
    mismatches: list[str] = []
    for task_id, (_name, row) in row_by_id.items():
        entry = ledger_by_id.get(task_id)
        if entry is None:
            continue
        if entry["provider"] != str(row["provider"]) or entry["upstream_model"] != str(
            row["upstream_model"]
        ):
            mismatches.append(
                f"{task_id}: ledger=({entry['provider']},{entry['upstream_model']}) "
                f"census=({row['provider']},{row['upstream_model']})"
            )

    shapes: dict[str, str] = {}
    group_mismatch: dict[str, object] = {}
    for name, entries in bodies.items():
        groups = payloads[name].get("provider_groups")
        derived = Counter(str(row["provider"]) for row in entries)
        if isinstance(groups, dict):
            shapes[name] = "dict"
            declared = {str(k): int(v) for k, v in groups.items()}
            if declared != dict(derived):
                group_mismatch[name] = {"declared": declared, "derived": dict(derived)}
        elif isinstance(groups, list):
            shapes[name] = "list"
            declared = sorted(str(g) for g in groups)
            if declared != sorted(derived):
                group_mismatch[name] = {"declared": declared, "derived": sorted(derived)}
        else:
            shapes[name] = type(groups).__name__
            group_mismatch[name] = "provider_groups is neither a dict of counts nor a name list"

    per_file: dict[str, object] = {}
    for name, entries in bodies.items():
        groups = payloads[name].get("provider_groups")
        per_file[name] = {
            "claim_shape": "dict" if isinstance(groups, dict) else type(groups).__name__,
            "claimed": (sum(int(v) for v in groups.values()) if isinstance(groups, dict) else None),
            "actual": len(entries),
            "providers": sorted({str(row["provider"]) for row in entries}),
        }

    ledger_by_provider: dict[str, set[str]] = {}
    for entry in ledger:
        ledger_by_provider.setdefault(entry["provider"], set()).add(entry["task_id"])
    census_by_provider: dict[str, set[str]] = {}
    for task_id, (_name, row) in row_by_id.items():
        census_by_provider.setdefault(str(row["provider"]), set()).add(task_id)
    per_provider = {
        provider: {
            "ledger": len(ledger_by_provider.get(provider, set())),
            "census": len(ids),
            "extra_task_ids": sorted(ids - ledger_by_provider.get(provider, set())),
            "missing_task_ids": sorted(ledger_by_provider.get(provider, set()) - ids),
        }
        for provider, ids in sorted(census_by_provider.items())
    }
    absent_from_census = {
        provider: len(ids)
        for provider, ids in sorted(ledger_by_provider.items())
        if provider not in census_by_provider
    }

    new_names = sorted(set(bodies) - carried)
    new_ids = {t for t, n in owner.items() if n in set(new_names)}
    provider_totals = Counter(str(row["provider"]) for _t, (_n, row) in row_by_id.items())
    out_rows = [entry for entry in ledger if entry["task_id"] not in owner]

    report = mcc.run(round_id="C86", root=REPO_ROOT, census_dir=census_dir)
    present = frozenset(c for c, r in report.readings.items() if r.present)
    ladder = report.roadmap
    hand_true = sorted(r.task_id for r in rows if r.expressible_today)
    derived_true = sorted(r.task_id for r in rows if r.needs <= present)
    disagreement = sorted(set(derived_true) ^ set(hand_true))

    resolve = Counter[str]()
    for _name, row in row_by_id.values():
        rel = _evidence_rel(row)
        if not rel:
            resolve["no_path_named"] += 1
        elif (UPSTREAM_ROOT / rel).is_file():
            resolve["upstream_root"] += 1
        elif (REPO_ROOT / rel).is_file():
            resolve["repo_root"] += 1
        else:
            resolve["unresolved"] += 1

    head = _upstream_head()

    return {
        "census_files": len(files),
        "census_file_names": sorted(bodies),
        "new_files_this_round": len(new_names),
        "new_file_names": new_names,
        "new_rows_this_round": len(new_ids),
        "new_row_providers": dict(
            sorted(Counter(str(row_by_id[t][1]["provider"]) for t in new_ids).items())
        ),
        "census_rows": len(rows),
        "unique_task_ids": len(owner),
        "ledger_rows": len(ledger),
        "ledger_upstream_commits": sorted({entry["upstream_commit"] for entry in ledger}),
        "upstream_head": head,
        "upstream_head_ok": head == EXPECTED_UPSTREAM_COMMIT,
        "in_census": len(set(owner) & set(ledger_by_id)),
        "out_of_scope_rows": len(out_rows),
        "out_of_scope_composition": dict(
            sorted(Counter(entry["implementation_task_status"] for entry in out_rows).items())
        ),
        "per_provider_row_counts": dict(sorted(provider_totals.items())),
        "providers_in_ledger": len(ledger_by_provider),
        "providers_with_census_rows": len(census_by_provider),
        "per_file_face": per_file,
        "per_provider_join": per_provider,
        "providers_absent_from_census": absent_from_census,
        "absent_provider_row_total": sum(absent_from_census.values()),
        "id_collisions": collisions,
        "unknown_task_ids": unknown_ids,
        "ledger_field_mismatches": sorted(mismatches),
        "provider_groups_shape_per_file": dict(sorted(shapes.items())),
        "files_publishing_counts": sum(1 for s in shapes.values() if s == "dict"),
        "provider_groups_mismatches": group_mismatch,
        "expressible_hand_true": hand_true,
        "expressible_derived_true": derived_true,
        "expressible_disagreement": disagreement,
        "expressible_disagreement_new_rows": sorted(set(disagreement) & new_ids),
        "evidence_root": dict(sorted(resolve.items())),
        "roadmap": {
            "rows_pending": ladder.rows_pending,
            "rows_expressible_today_hand": ladder.rows_expressible_today,
            "shipped_cover_rows": len(ladder.shipped_cover),
            "greedy_steps": len(ladder.build_order),
            "rows_unlocked_by_greedy_prefix": ladder.rows_unlocked_by_greedy_prefix,
            "rows_still_locked_after_engine_pool": len(ladder.rows_still_locked_after_engine_pool),
            "rows_outside_engine": len(ladder.rows_outside_engine),
            "rows_with_no_engine_work_possible": ladder.rows_with_no_engine_work_possible,
            "blocking_non_engine": list(ladder.blocking_non_engine),
            "first_five_steps": [
                [s.capability, s.rows_newly_unlocked, s.cumulative_rows_unlocked]
                for s in ladder.build_order[:5]
            ],
        },
        "instrument_expected_rows": mcc.EXPECTED_CENSUS_ROWS,
        "guard_fired": report.row_count_guard_fired,
        "census_files_digest": {n: _digest(census_dir / n)[:16] for n in sorted(bodies)},
    }


def _copy_tree() -> tuple[Path, Path]:
    """Copy the row-carrying census files into a temp tree so no arm touches the live bytes."""
    work = Path(tempfile.mkdtemp(prefix="census_audit_arm_"))
    for path in mcc.census_row_files(PLAN_DIR):
        (work / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (work / mcc.CENSUS_AUDIT_NAME).write_text(
        AUDIT_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return work, work


def _rewrite(target: Path, name: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = target / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def self_test(face: dict[str, object], ledger: list[dict[str, str]], carried: set[str]) -> int:
    """Show each counter away from its live reading; a counter no input can move proves nothing."""
    print("SELF-TEST (five tampers against copied trees; the live census is never written)")
    made: list[Path] = []
    arms = (
        ("drop the last census file", "census_files", lambda p, t: (t / p[-1]).unlink()),
        (
            "rename a task_id so the ledger join misses",
            "unknown_task_ids",
            lambda p, t: _rewrite(
                t, p[-1], lambda d: d["rows"][0].__setitem__("task_id", "OBB2-not-in-ledger-X")
            ),
        ),
        (
            "inflate a provider_groups count",
            "provider_groups_mismatches",
            lambda p, t: _rewrite(
                t,
                p[-1],
                lambda d: d.__setitem__(
                    "provider_groups",
                    {k: int(v) + 1 for k, v in d["provider_groups"].items()}
                    if isinstance(d.get("provider_groups"), dict)
                    else list(d["provider_groups"]) + ["phantom_provider"],
                ),
            ),
        ),
        (
            "swap a row's provider against the ledger",
            "ledger_field_mismatches",
            lambda p, t: _rewrite(
                t, p[-1], lambda d: d["rows"][0].__setitem__("provider", "not_the_ledger_provider")
            ),
        ),
        (
            "delete one row body",
            "census_rows",
            lambda p, t: _rewrite(t, p[-1], lambda d: d.__setitem__("rows", d["rows"][1:])),
        ),
    )
    names = [str(n) for n in face["census_file_names"]]
    failures = 0
    for label, key, mutate in arms:
        live = _count(face[key])
        work, target = _copy_tree()
        made.append(work)
        try:
            mutate(names, target)
            after_face = measure(target, ledger, carried)
            after = _count(after_face[key])
        except mcc.RoadmapError as exc:
            after = f"RoadmapError: {exc}"
        verdict = "PASS" if str(after) != str(live) else "FAIL"
        failures += verdict == "FAIL"
        print(f"  [{verdict}] {label}: {key} live={live} -> tampered={after}")
    for directory in made:
        shutil.rmtree(directory, ignore_errors=True)
    print(f"SELF_TEST arms={len(arms)} failures={failures} copied_trees={len(made)}")
    return 1 if failures else 0


def _count(value: object) -> object:
    if isinstance(value, (list, dict)):
        return len(value)
    return value


def _as_dict(value: object) -> dict[str, Any]:
    """Narrow a face value to a mapping for rendering; a non-mapping renders as empty."""
    return value if isinstance(value, dict) else {}


def render(face: dict[str, object], json_mode: bool) -> str:
    """Print each face with its denominator, or dump the raw face when asked for JSON."""
    if json_mode:
        return json.dumps(face, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    per_file = _as_dict(face["per_file_face"])
    joined = _as_dict(face["per_provider_join"])
    absent = _as_dict(face["providers_absent_from_census"])
    disagreeing = {
        name: (entry["claim_shape"], entry["claimed"], entry["actual"])
        for name, entry in sorted(per_file.items())
        if entry["claimed"] is not None and entry["claimed"] != entry["actual"]
    }
    extra_ids = sum(len(v["extra_task_ids"]) for v in joined.values())
    missing_ids = sum(len(v["missing_task_ids"]) for v in joined.values())
    return "\n".join(
        [
            "census merge audit -- C86",
            f"denominators: ledger rows={face['ledger_rows']} census rows={face['census_rows']} "
            f"census files={face['census_files']} unique task_ids={face['unique_task_ids']}",
            f"new this round: files={face['new_files_this_round']} "
            f"rows={face['new_rows_this_round']} "
            f"providers={len(face['new_row_providers'])}",
            f"join: in_census={face['in_census']} out_of_scope={face['out_of_scope_rows']} "
            f"id_collisions={len(face['id_collisions'])} "
            f"unknown_ids={len(face['unknown_task_ids'])} "
            f"ledger_field_mismatches={len(face['ledger_field_mismatches'])}",
            "out_of_scope composition: "
            f"{json.dumps(face['out_of_scope_composition'], sort_keys=True)}",
            "provider_groups: shape=dict on "
            f"{face['files_publishing_counts']} of {face['census_files']} files, "
            f"mismatches={len(face['provider_groups_mismatches'])}; "
            f"files whose published count disagrees from len(rows): {len(disagreeing)} "
            f"{json.dumps(disagreeing, sort_keys=True)}",
            f"provider join: ledger_providers={face['providers_in_ledger']} "
            f"with_census_rows={face['providers_with_census_rows']} "
            f"extra_ids_within_provider={extra_ids} missing_ids_within_provider={missing_ids}",
            f"providers with ledger rows but no census rows: {len(absent)} "
            f"holding {face['absent_provider_row_total']} rows "
            f"{json.dumps(absent, sort_keys=True)}",
            f"upstream: head={str(face['upstream_head'])[:12]} ok={face['upstream_head_ok']} "
            f"ledger_commits={json.dumps(face['ledger_upstream_commits'])}",
            f"evidence root: {json.dumps(face['evidence_root'], sort_keys=True)}",
            "expressibility (hand claim vs the registry this run read): "
            f"hand_true={len(face['expressible_hand_true'])} "
            f"derived_true={len(face['expressible_derived_true'])} "
            f"disagreement={len(face['expressible_disagreement'])} "
            f"new_rows_in_disagreement={len(face['expressible_disagreement_new_rows'])}",
            f"  disagreement ids: {json.dumps(face['expressible_disagreement'])}",
            f"roadmap: {json.dumps(face['roadmap'], sort_keys=True)}",
            f"guard: measured={face['census_rows']} EXPECTED_CENSUS_ROWS="
            f"{face['instrument_expected_rows']} fired={face['guard_fired']}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Measure, print, optionally self-test the arms, optionally refresh the audit record."""
    parser = argparse.ArgumentParser(description="C86 census merge audit")
    parser.add_argument(
        "--json", action="store_true", help="print the JSON face instead of the report"
    )
    parser.add_argument("--write", action="store_true", help="refresh census-audit.json")
    parser.add_argument("--self-test", action="store_true", help="run the five tamper arms")
    args = parser.parse_args(argv)

    ledger = read_ledger()
    carried = carried_files()
    face = measure(PLAN_DIR, ledger, carried)
    print(render(face, args.json))
    if args.self_test:
        return self_test(face, ledger, carried)
    if args.write:
        _apply(face, ledger, carried)
    return 0


def _apply(face: dict[str, object], ledger: list[dict[str, str]], carried: set[str]) -> None:
    before = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
    archived = {key: before[key] for key in ARCHIVED_KEYS if key in before}
    digest_before = _digest(AUDIT_FILE)[:16]
    payload = dict(before)
    payload["c86_reaudit"] = {
        "audited_by": "C86 census merge audit",
        "instrument": "docs/evidence/C86/census-merge-audit.py",
        "scope": (
            "Re-measured at C86 over "
            f"{face['census_files']} census files carrying {face['census_rows']} rows and the "
            f"{face['ledger_rows']}-row 模型级任务清单.csv. Refreshed by this run: recomputed, "
            "recomputed_total, per_file, ledger_total, arith_verification, out_of_scope_rows, "
            "evidence_root_audit, upstream_root_ok, upstream_commit_ok. Carried unread from "
            "C74/C75 and NOT re-measured here: engine_audit, registration_check, spot_checks, "
            "c74_reaudit, uncovered_providers. The "
            f"{face['new_rows_this_round']} new rows therefore rest on the label-map guard plus "
            "this ledger join, not on a per-row oracle re-read -- that re-read is the next "
            "round's job and is what AC-10|03-style provider review would certify."
        ),
        "previous_values_before_c86": archived,
        "face": face,
    }
    payload["recomputed"] = face["per_provider_row_counts"]
    payload["recomputed_total"] = face["census_rows"]
    payload["per_file"] = face["per_file_face"]
    payload["ledger_total"] = face["ledger_rows"]
    payload["out_of_scope_rows"] = face["out_of_scope_composition"]
    payload["upstream_root_ok"] = bool(face["upstream_head_ok"])
    payload["upstream_commit_ok"] = bool(face["upstream_head_ok"])
    payload["evidence_root_audit"] = face["evidence_root"]
    payload["arith_verification"] = (
        f"{face['census_files']} files carrying {face['census_rows']} rows "
        f"({face['new_files_this_round']} new files / {face['new_rows_this_round']} new rows this "
        f"round on top of the C74/C75 inventory {sorted(carried)}); unique task_ids="
        f"{face['unique_task_ids']}; id collisions={len(face['id_collisions'])}; task_ids absent "
        f"from the ledger={len(face['unknown_task_ids'])}; provider/model mismatches against the "
        f"ledger={len(face['ledger_field_mismatches'])}; provider_groups mismatches="
        f"{len(face['provider_groups_mismatches'])}"
    )
    AUDIT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"WROTE {AUDIT_FILE.name} sha256[:16] {digest_before} -> {_digest(AUDIT_FILE)[:16]} "
        f"(archived_before_c86_keys={sorted(archived)})"
    )


if __name__ == "__main__":
    sys.exit(main())
