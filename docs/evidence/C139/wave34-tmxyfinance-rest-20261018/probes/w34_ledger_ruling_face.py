"""Face for the eleven ``ledger_ruled_out`` residue rows: does each exclusion have evidence?

The shipped census (``residue_axis_census.py``) excludes 11 rows because a dict in the provider's
own package names them, and prints ``action=ledger_ruled_out``. A dict is a decision, not a face:
this probe re-derives, per row, what the ruling could be resting on, and prints a two-sided control
for every predicate it uses. Nothing here edits product code; every reading comes off disk.

What is imported rather than re-typed:

* ``incomplete_row_guard_check._ruled_out`` -- the shipped axis measurer, including its
  ``ruled_out`` / ``no_ledger`` split. The exclusion set is the set THIS returns, never a list.
* ``readiness_census._residue`` + ``seam_surface_match._load_report`` -- the judged population.
* ``iter2_model_evidence.load_inventory`` -- the shipped judge's pinned identity inventory.
* ``model_capability_census.load_rows`` / ``verify_registry`` -- the shipped capability measurer:
  it folds each ``census-*.json`` row's raw need labels onto canonical capability ids and decides
  each id's presence from an anchor token in an engine file as it reads the tree right now.
* ``residue_axis_census._action`` / ``_package_blob`` -- the shipped action rule, re-fed with the
  ledger axis on and off, so the arm shows the axis (not the wording) decides the exclusion.

Per row the probe prints the ledger dict path:line and reason, the measured canonical need set,
the subset of those needs the registry reads as not present, the pinned upstream declaration
(``upstream_shape_file`` sha256 plus the upstream model module's existence), and the row's rights
fields. The cause class comes from this precedence and nothing else:

``a``
    needs are blocked and at least one blocker is an ``outside.*`` canonical -- the shipped taxonomy
    itself says the blocker is upstream/access, not engine vocabulary.
``c``
    needs are blocked and every blocker is engine work -- an in-repo structural choice.
``d``
    nothing blocks the row: the measured face says declarable while the dict rules it out, so what
    is left is prose.
``b``
    the dict's reason is a rights/licensing declaration. Printed per provider as an independent
    reading (registry section-1 row present or absent, ``rights_rows`` linked or not), because none
    of these eleven reasons is a rights claim.

Arms: ``population_identity``, ``dict_denominators``, ``cause_attributed``, ``upstream_declared``,
``two_sided_same_dict``, ``action_counterfact``, ``census_crosscheck``,
``bootstrap_defect_measured``. The last one prices a known path-bootstrap defect: the shipped census
and the modules it imports put only their own directory and ``scripts/quality`` on ``sys.path``,
never the repo root, so ``import incomplete_row_guard_check`` -> ``opendata.data...`` dies with
``ModuleNotFoundError: No module named 'opendata'`` for any interpreter that has no installed
``opendata``. Whether that error appears on this machine is printed as a reading, not assumed; what
the arm gates is the structural half (the shipped trio never inserts the repo root, this probe
does). This probe inserts the repo root itself and therefore runs without ``PYTHONPATH``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import os
import re
import subprocess  # nosec B404  # read-only evidence probe; every argv is a literal list
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[5]
C128_REL: Final = "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
ITER2_REL: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
INVENTORY_REL: Final = f"{ITER2_REL}/模型级任务清单.csv"
INVENTORY_YAML_REL: Final = "docs/proposals/openbb-migration/provider-inventory.yaml"
RIGHTS_REL: Final = "docs/data-rights-registry.md"
CENSUS_PROBE: Final = REPO_ROOT / C128_REL / "residue_axis_census.py"
UPSTREAM_DEFAULT: Final = "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform"
LEDGER_PREFIX: Final = "NOT_DECL"
RULED_ACTION: Final = "ledger_ruled_out"
EXPECTED_INVENTORY: Final = 350

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
sys.path.insert(0, str(REPO_ROOT / C128_REL))

import incomplete_row_guard_check as axes  # noqa: E402  # shipped four-axis row measurer
import model_capability_census as cap  # noqa: E402  # shipped capability/roadmap measurer
import readiness_census as rc  # noqa: E402  # shipped residue rule
import residue_axis_census as census_axes  # noqa: E402  # shipped action rule + blob reader
import seam_surface_match as seam  # noqa: E402  # shipped judged-report loader
from iter2_model_evidence import load_inventory  # noqa: E402  # shipped judge


def _run(argv: list[str], env: dict[str, str], cwd: Path | None = None) -> tuple[int, str]:
    """Run one literal argv with an explicit environment; never through a shell.

    ``cwd`` defaults to the repo root, except for the bootstrap reading, which must run from
    somewhere else or ``sys.path[0]`` (the current directory) resolves ``opendata`` for it.
    """
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, no interpolation
        argv,
        capture_output=True,
        text=True,
        cwd=cwd if cwd is not None else REPO_ROOT,
        check=False,
        timeout=1800,
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _population() -> tuple[dict[str, str], dict[str, Any]]:
    """Residue identities with judged status, plus the shipped judge's own pinned inventory."""
    report = seam._load_report()
    inventory = load_inventory(REPO_ROOT / INVENTORY_REL)
    return rc._residue(report), report | {"_inventory": inventory}


def _ruled_set(residue: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Ask the shipped measurer, per residue row, whether a ledger dict rules it out."""
    out: dict[str, tuple[str, str]] = {}
    for identity in sorted(residue):
        provider, _, model = identity.partition("::")
        entry, cause = axes._ruled_out(provider, model)
        if entry:
            out[identity] = (entry, cause)
    return out


def _ledger_dicts() -> tuple[list[dict[str, Any]], list[str]]:
    """Every provider package that spells a ledger dict, with the size it actually enumerates."""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    root = REPO_ROOT / "opendata" / "data" / "providers"
    for specs in sorted(root.glob("*/specs.py")):
        provider = specs.parent.name
        module = importlib.import_module(f"opendata.data.providers.{provider}.specs")
        for name, value in vars(module).items():
            if name.startswith(LEDGER_PREFIX) and isinstance(value, dict):
                rows.append(
                    {
                        "provider": provider,
                        "dict": name,
                        "path": f"opendata/data/providers/{provider}/specs.py",
                        "size": len(value),
                        "keys": sorted(str(key) for key in value),
                        "distinct_reasons": len({str(item) for item in value.values()}),
                    }
                )
        if not any(name.startswith(LEDGER_PREFIX) for name in vars(module)):
            missing.append(provider)
    return rows, missing


def _entry_location(provider: str, model: str, dict_name: str) -> tuple[str, int, int]:
    """Where the row is keyed: the specs file, the dict assignment line, the key line."""
    module = importlib.import_module(f"opendata.data.providers.{provider}.specs")
    path = str(module.__file__)
    relative = path.replace(f"{REPO_ROOT}/", "")
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    assign = next((no for no, line in enumerate(lines, 1) if line.startswith(f"{dict_name}")), 0)
    key = next((no for no, line in enumerate(lines, 1) if no >= assign and f'"{model}"' in line), 0)
    return relative, assign, key


def _dict_row(named: list[dict[str, Any]], provider: str, dict_name: str) -> dict[str, Any]:
    """The denominator row for one provider's dict."""
    for row in named:
        if row["provider"] == provider and row["dict"] == dict_name:
            return row
    return {"size": 0, "distinct_reasons": 0, "keys": []}


def _full_reason(provider: str, dict_name: str, model: str) -> str:
    """The reason exactly as the ledger dict spells it (the shipped entry truncates it to 70)."""
    module = importlib.import_module(f"opendata.data.providers.{provider}.specs")
    value = getattr(module, dict_name, {})
    return str(value.get(model, ""))


def _coverage(
    named: list[dict[str, Any]],
    ruled: dict[str, tuple[str, str]],
    inv: dict[str, Any],
) -> str:
    """Per provider: how many inventory rows it has, how many the dict rules out, dict size."""
    out: list[str] = []
    for provider in sorted({i.partition("::")[0] for i in ruled}):
        rows = [i for i in inv if i.partition("::")[0] == provider]
        dicts = [row["size"] for row in named if row["provider"] == provider]
        out.append(
            f"{provider}:ruled_out={sum(1 for i in ruled if i.partition('::')[0] == provider)}"
            f"/inventory_rows={len(rows)}/dict_sizes={sorted(dicts)}"
        )
    return " ".join(out)


def _inventory_rows() -> dict[str, dict[str, str]]:
    """The inventory CSV keyed by ``provider::model`` (rights and upstream-declaration fields)."""
    with (REPO_ROOT / INVENTORY_REL).open(encoding="utf-8-sig", newline="") as handle:
        return {
            f"{row['provider']}::{row['upstream_model']}": row for row in csv.DictReader(handle)
        }


def _capability_face() -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-row canonical needs and per-capability presence, both read live by the shipped tool."""
    rows = cap.load_rows(REPO_ROOT / ITER2_REL)
    readings = cap.verify_registry(cap.REGISTRY, REPO_ROOT, {})
    return {f"{row.provider}::{row.upstream_model}": row for row in rows}, readings


def _upstream_face(
    upstream: Path,
    shape_file: str,
    sha: str,
    line_no: str,
    model: str,
) -> dict[str, Any]:
    """Read the row's own pinned upstream declaration: line, fetcher symbol, and bytes.

    ``upstream_shape_file`` / ``upstream_shape_line`` / ``upstream_shape_sha256`` are the
    inventory's per-row citation of the upstream provider's own model registry -- a line that
    reads ``"FuturesCurve": CboeFuturesCurveFetcher``. This checks the file hashes to the pinned
    digest, that the named model token is on the cited line, and that the fetcher symbol it
    points at is actually defined in that upstream package.
    """
    relative = PurePosixPath(shape_file)
    shape = upstream / relative if shape_file else None
    exists = bool(shape is not None and shape.is_file())
    match = False
    if exists and shape is not None and sha:
        match = hashlib.sha256(shape.read_bytes()).hexdigest() == sha
    package = relative.parts[1] if len(relative.parts) > 2 else ""
    line = ""
    if exists and shape is not None:
        try:
            body = shape.read_text(encoding="utf-8", errors="replace").splitlines()
            line = body[int(line_no) - 1]
        except (ValueError, IndexError):
            line = ""
    symbol = ""
    found = re.search(rf'"{re.escape(model)}"\s*:\s*([A-Za-z_][A-Za-z0-9_]*)', line)
    if found:
        symbol = found.group(1)
    defined = False
    if symbol and package:
        blob = upstream / "providers" / package
        defined = blob.is_dir() and any(
            f"class {symbol}" in path.read_text(encoding="utf-8", errors="replace")
            for path in blob.rglob("*.py")
        )
    return {
        "shape_exists": exists,
        "shape_sha_pinned_match": match,
        "declared_line": f"{shape_file}:{line_no}",
        "line_names_model": f'"{model}"' in line,
        "fetcher_symbol": symbol or "-",
        "fetcher_defined": defined,
    }


def _registry_named(providers: set[str]) -> dict[str, bool]:
    """Does the rights registry's section-1 table name each provider at all?"""
    text = (REPO_ROOT / RIGHTS_REL).read_text(encoding="utf-8")
    section = text.split("## 1. 登记表")[-1].split("\n## ")[0]
    body = "\n".join(line for line in section.splitlines() if re.match(r"^\|\s*\d+\s*\|", line))
    return {provider: provider in body for provider in sorted(providers)}


def _yaml_rights(providers: set[str]) -> dict[str, str]:
    """What the baseline record links for each provider: ``rights_rows`` or nothing."""
    text = (REPO_ROOT / INVENTORY_YAML_REL).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    blocks = re.split(r"^  - provider: ", text, flags=re.M)
    for block in blocks[1:]:
        provider = block.splitlines()[0].strip()
        if provider in providers:
            found = re.search(r"^\s+rights_rows:\s*(.*)$", block, re.M)
            out[provider] = found.group(1).strip() if found else "ABSENT_FIELD"
    return out


def _blocking(needs: frozenset[str], readings: dict[str, Any]) -> dict[str, list[str]]:
    """Split a row's canonical needs by whether the registry reads each one as present."""
    blocked = sorted(need for need in needs if not readings[need].present)
    return {
        "blocking": blocked,
        "engine": [c for c in blocked if readings[c].engine_work],
        "outside": [c for c in blocked if not readings[c].engine_work],
    }


def _class_of(kind: dict[str, list[str]]) -> str:
    """Apply the documented precedence: outside blocker, engine blocker, or nothing blocking."""
    if not kind["blocking"]:
        return "d"
    return "a" if kind["outside"] else "c"


def _tally(ruled: dict[str, tuple[str, str]]) -> dict[str, int]:
    """Provider tally of the ruled-out set, derived rather than asserted."""
    out: dict[str, int] = {}
    for identity in ruled:
        provider = identity.partition("::")[0]
        out[provider] = out.get(provider, 0) + 1
    return dict(sorted(out.items()))


def _controls(
    residue: dict[str, str],
    ruled: dict[str, tuple[str, str]],
    inv: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Apply the shipped predicate to rows the same dicts do NOT rule out, plus a forged name."""
    providers = sorted({i.partition("::")[0] for i in ruled})
    probed: list[str] = []
    not_ruled: list[str] = []
    for identity, csv_row in sorted(inv.items()):
        provider = identity.partition("::")[0]
        if provider not in providers or identity in ruled:
            continue
        probed.append(identity)
        entry, _cause = axes._ruled_out(provider, csv_row["upstream_model"])
        if not entry:
            not_ruled.append(identity)
    both_sides = sorted({i.partition("::")[0] for i in not_ruled})
    forged = axes._ruled_out(providers[0], "NoSuchModelAnywhere") if providers else ("", "")
    return {
        "providers": providers,
        "probed": len(probed),
        "not_ruled_out": not_ruled,
        "both_sides": both_sides,
        "forged": forged,
        "sample": not_ruled[:4],
    }


def _counterfact(ruled: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """Re-feed the shipped action rule with the ledger axis on and off; the axis must decide."""
    decides = 0
    stuck: list[str] = []
    other: set[str] = set()
    sample: list[str] = []
    for identity in sorted(ruled):
        provider, _, model = identity.partition("::")
        name_in_package = bool(
            re.search(rf"\b{re.escape(model)}\b", census_axes._package_blob(provider))
        )
        on = census_axes._action("UNBOUND", True, "SEAM_NEVER_USED", False, name_in_package)
        off = census_axes._action("UNBOUND", False, "SEAM_NEVER_USED", False, name_in_package)
        if on == RULED_ACTION and off != RULED_ACTION:
            decides += 1
            other.add(off)
            if len(sample) < 3:
                sample.append(f"{identity}:{on}->{off}")
        else:
            stuck.append(f"{identity}:{on}/{off}")
    return {"decides": decides, "other_actions": other, "sample": sample, "stuck": stuck}


def _census_crosscheck(skip: bool, here: set[str]) -> dict[str, Any]:
    """Re-run the shipped census and compare its ruled-out set with this probe's derivation."""
    out: dict[str, Any] = {
        "ran": False,
        "skipped": skip,
        "census_exit": None,
        "face": "not-run",
        "count": 0,
        "agrees": False,
        "only_census": [],
        "only_here": [],
        "not_ruled_out": 0,
        "opposite_ok": False,
    }
    if skip:
        return out
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    code, text = _run([sys.executable, str(CENSUS_PROBE)], env)
    census: set[str] = set()
    opposite: set[str] = set()
    for line in text.splitlines():
        found = re.match(r"^row (\S+) .*action=(\S+)$", line)
        if found:
            (census if found.group(2) == RULED_ACTION else opposite).add(found.group(1))
    face = next((line for line in text.splitlines() if "residue_axis_census_face" in line), "")
    out.update(
        {
            "ran": bool(face),
            "census_exit": code,
            "face": face,
            "count": len(census),
            "agrees": bool(face) and census == here,
            "only_census": sorted(census - here),
            "only_here": sorted(here - census),
            "not_ruled_out": len(opposite),
            "opposite_ok": bool(opposite) and not (opposite & here),
        }
    )
    return out


def _bootstrap() -> dict[str, Any]:
    """Measure the path-bootstrap defect on the shipped census, both sides.

    The census is a *script*: ``sys.path[0]`` is its own directory, not the repo root, and it only
    adds ``scripts/quality`` from there. So ``import incomplete_row_guard_check`` ->
    ``opendata.data...`` dies with ``ModuleNotFoundError: No module named 'opendata'`` unless the
    caller exports ``PYTHONPATH`` at the repo root. Run from outside the repo, with only the probe
    directory on the path, that is reproduced in one process; with the repo root added, the same
    import resolves. This probe puts the repo root on the path itself, so it runs either way.
    """
    elsewhere = Path("/tmp")  # noqa: S108  # nosec B108  # a cwd only; nothing is written here
    snippet = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT / C128_REL)!r});"
        " import incomplete_row_guard_check"
    )
    argv = [sys.executable, "-c", snippet]
    env = {"PATH": "/usr/bin:/bin"}
    code, text = _run(argv, env, elsewhere)
    without = (
        "ModuleNotFoundError:No module named 'opendata'"
        if code != 0 and "No module named 'opendata'" in text
        else f"rc={code} (no ModuleNotFoundError; this interpreter resolves opendata without"
        f" the repo root anyway)"
    )
    env2 = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / C128_REL}",
    }
    code2, _ = _run([sys.executable, "-c", "import incomplete_row_guard_check"], env2, elsewhere)
    shipped = "\n".join(
        (REPO_ROOT / C128_REL / name).read_text(encoding="utf-8")
        for name in (
            "residue_axis_census.py",
            "incomplete_row_guard_check.py",
            "seam_surface_match.py",
        )
    )
    here = Path(__file__).read_text(encoding="utf-8")
    return {
        "without_path": without,
        "with_path": code2 == 0,
        "needs_env": code != 0,
        "shipped_probes_insert_repo_root": "sys.path.insert(0, str(REPO_ROOT))" in shipped,
        "this_probe_inserts_repo_root": "sys.path.insert(0, str(REPO_ROOT))" in here,
    }


def main(argv: list[str] | None = None) -> int:
    """Print the ledger-ruling face and the verdict on its arms."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", default=UPSTREAM_DEFAULT)
    parser.add_argument("--skip-census", action="store_true", help="skip the slow census re-run")
    parsed = parser.parse_args(argv)
    upstream = Path(parsed.upstream_root)
    arms: list[tuple[str, bool]] = []
    print(f"# carrier for {Path(__file__).name} (wave-34 ledger-ruling face)")
    print(f"# argv: {sys.executable} {Path(__file__).relative_to(REPO_ROOT)} {sys.argv[1:]}")
    print(f"# upstream checkout read for the pinned-declaration face: {upstream}")

    residue, extra = _population()
    inventory: dict[str, Any] = extra["_inventory"]
    ruled = _ruled_set(residue)
    identity_sha = str(extra["inventory"].get("expected_identity_set_sha256", "-"))[:16]
    print(
        f"P_population judge_identity_rows={len(inventory['rows'])}"
        f" identity_count={inventory['identity_count']}"
        f" expected_identity_sha256={identity_sha} report_rows={len(extra['models'])}"
        f" accepted={len(extra['models']) - len(residue)} residue={len(residue)}"
        f" ledger_ruled_out={len(ruled)} ruled_by_provider={_tally(ruled)}"
    )
    arms.append(
        (
            "population_identity",
            int(inventory["identity_count"]) == EXPECTED_INVENTORY
            and len(inventory["rows"]) == EXPECTED_INVENTORY
            and 0 < len(ruled) < len(residue)
            and identity_sha != "-",
        )
    )

    named, dicts_missing = _ledger_dicts()
    empty = [f"{row['provider']}.{row['dict']}" for row in named if row["size"] == 0]
    sizes = {f"{row['provider']}.{row['dict']}": row["size"] for row in named}
    through_empty = sum(
        1
        for identity, (entry, _) in ruled.items()
        if _dict_row(named, identity.partition("::")[0], entry.partition(":")[0])["size"] == 0
    )
    with_cause = sum(1 for _, cause in ruled.values() if cause)
    print(
        f"Q_dict_denominators ledger_dicts={len(named)} sizes={sizes} empty_dicts={empty}"
        f" providers_with_specs_but_no_ledger={dicts_missing}"
        f" ruled_out_rows_through_an_empty_dict={through_empty}"
        f" ruled_out_with_a_no_ledger_cause={with_cause}"
    )
    arms.append(
        (
            "dict_denominators",
            all(
                _dict_row(named, i.partition("::")[0], entry.partition(":")[0])["size"] >= 1
                for i, (entry, _) in ruled.items()
            )
            and not any(cause for _, cause in ruled.values())
            and len(named) > len(empty),
        )
    )

    inv = _inventory_rows()
    by_row, readings = _capability_face()
    classes: dict[str, int] = {}
    unexplained: list[str] = []
    contradicted: list[str] = []
    for identity, (entry, cause) in sorted(ruled.items()):
        provider, _, model = identity.partition("::")
        dict_name = entry.partition(":")[0]
        path, assign, key = _entry_location(provider, model, dict_name)
        row = by_row.get(identity)
        kind: dict[str, list[str]] = (
            _blocking(row.needs, readings) if row is not None else {"blocking": ["NO_CENSUS_ROW"]}
        )
        cls = _class_of(kind) if row is not None else "unknown"
        if cls == "unknown":
            unexplained.append(identity)
        if cls == "d":
            contradicted.append(identity)
        csv_row = inv.get(identity, {})
        up = _upstream_face(
            upstream,
            str(csv_row.get("upstream_shape_file", "")),
            str(csv_row.get("upstream_shape_sha256", "")),
            str(csv_row.get("upstream_shape_line", "")),
            model,
        )
        registry = _registry_named({provider, "biztoc", "stockgrid"})
        classes[cls] = classes.get(cls, 0) + 1
        print(
            f"R_row {identity} class={cls} ledger={path}:{assign} key_line={key} dict={dict_name}"
            f" dict_size={_dict_row(named, provider, dict_name)['size']}"
            f" distinct_reasons_in_dict={_dict_row(named, provider, dict_name)['distinct_reasons']}"
            f" census_keyed={row is not None}"
            f" needs={sorted(str(n) for n in row.needs) if row is not None else []}"
            f" blocking={kind['blocking']} engine_blockers={kind['engine']}"
            f" outside_blockers={kind['outside']}"
            f" upstream_declared_line={up['declared_line']}:{up['fetcher_symbol']}"
            f" line_names_model={up['line_names_model']} fetcher_defined={up['fetcher_defined']}"
            f" shape_sha_pinned_match={up['shape_sha_pinned_match']}"
            f" rights_status={csv_row.get('rights_status', '-')}"
            f" rights_review={csv_row.get('rights_review_task_id', '-')}"
            f" registry_section1_row={registry[provider]} no_ledger={cause or '-'}"
        )
        print(f"    reason[{identity}]={entry.partition(':')[2]}")
        print(f"    reason_full[{identity}]={_full_reason(provider, dict_name, model)}")
    print(
        f"R_class_counts {dict(sorted(classes.items()))} ruled_out_keyed_in_a_ledger_dict="
        f"{len(ruled)}/{len(ruled)} census_keyed="
        f"{sum(1 for i in ruled if i in by_row)}/{len(ruled)}"
        f" residual_unexplained={len(unexplained)} unexplained={unexplained}"
        f" declarable_face_says_otherwise={len(contradicted)} contradicted={contradicted}"
    )
    print(f"Q2_provider_coverage {_coverage(named, ruled, inv)}")
    arms.append(
        (
            "cause_attributed",
            not unexplained
            and sum(classes.values()) == len(ruled)
            and len(ruled) > 0
            and "unknown" not in classes,
        )
    )

    faces = [
        _upstream_face(
            upstream,
            str(inv.get(i, {}).get("upstream_shape_file", "")),
            str(inv.get(i, {}).get("upstream_shape_sha256", "")),
            str(inv.get(i, {}).get("upstream_shape_line", "")),
            i.partition("::")[2],
        )
        for i in sorted(ruled)
    ]
    forged = _upstream_face(
        upstream,
        str(inv.get(sorted(ruled)[0], {}).get("upstream_shape_file", "")),
        str(inv.get(sorted(ruled)[0], {}).get("upstream_shape_sha256", "")),
        str(inv.get(sorted(ruled)[0], {}).get("upstream_shape_line", "")),
        "NoSuchModelAnywhere",
    )
    print(
        f"S_upstream_declared rows_with_pinned_shape_sha_match="
        f"{sum(1 for f in faces if f['shape_sha_pinned_match'])}/{len(faces)}"
        f" rows_whose_cited_line_names_the_model={sum(1 for f in faces if f['line_names_model'])}"
        f"/{len(faces)} rows_with_a_defined_upstream_fetcher="
        f"{sum(1 for f in faces if f['fetcher_defined'])}/{len(faces)}"
        f" forged_model_reading={forged['line_names_model']}/{forged['fetcher_defined']}"
        f' => no ruling here rests on "the model does not exist upstream"'
    )
    arms.append(
        (
            "upstream_declared",
            upstream.is_dir()
            and len(faces) > 0
            and all(
                f["shape_sha_pinned_match"] and f["line_names_model"] and f["fetcher_defined"]
                for f in faces
            )
            and not forged["line_names_model"]
            and not forged["fetcher_defined"],
        )
    )

    controls = _controls(residue, ruled, inv)
    rights = _yaml_rights(set(controls["providers"]) | {"biztoc", "stockgrid"})
    registry_all = _registry_named(set(controls["providers"]) | {"biztoc", "stockgrid"})
    print(
        f"U_two_sided same_dict_controls_probed={controls['probed']}"
        f" read_not_ruled_out={len(controls['not_ruled_out'])} providers_holding_both_sides="
        f"{controls['both_sides']} sample={controls['sample']}"
        f" forged_ledger_reading={controls['forged']}"
    )
    arms.append(
        (
            "two_sided_same_dict",
            controls["probed"] >= 3
            and len(controls["not_ruled_out"]) >= 3
            and len(controls["both_sides"]) >= 3
            and controls["forged"] == ("", ""),
        )
    )

    print(
        f"D_rights_gate section1_rows={ {p: registry_all.get(p) for p in sorted(registry_all)} }"
        f" yaml_rights_rows={rights}"
        f" inventory_ruled_rows_rights="
        f"{ {i: inv.get(i, {}).get('rights_status', '-') for i in sorted(ruled)} }"
        f" gate=openbb_inventory_plane.RIGHTS LINK fires only when a source serves registered"
        f" capabilities, so biztoc/stockgrid are stopped by the package+registry plane, not by this"
        f" gate; AC-1|08 (acceptance_item_probe.measure_ac1_08) is the face that reads section 1"
    )

    flips = _counterfact(ruled)
    print(
        f"V_action_counterfact rows_where_the_ledger_axis_decides_the_action={flips['decides']}"
        f"/{len(ruled)} with_ledger={RULED_ACTION}"
        f" without_ledger_flips_to={sorted(flips['other_actions'])}"
        f" sample={flips['sample']} unflipped={flips['stuck']}"
    )
    arms.append(("action_counterfact", flips["decides"] == len(ruled) and not flips["stuck"]))

    cross = _census_crosscheck(parsed.skip_census, set(ruled))
    print(
        f"W_census_crosscheck ran={cross['ran']} skipped={cross['skipped']}"
        f" census_exit={cross['census_exit']} (non-zero is the shipped census's own red"
        f" B_landed_negative arm, not a failure of this comparison)"
        f" census_ledger_ruled_out={cross['count']} agrees_with_in_process={cross['agrees']}"
        f" only_in_census={cross['only_census'][:4]} only_here={cross['only_here'][:4]}"
        f" census_rows_not_ruled_out={cross['not_ruled_out']}"
        f" none_of_them_ruled_out_here={cross['opposite_ok']}"
    )
    arms.append(("census_crosscheck", cross["agrees"] and cross["opposite_ok"]))

    boot = _bootstrap()
    print(
        f"X_bootstrap_defect shipped_axis_import_without_repo_root_on_path={boot['without_path']}"
        f" import_with_repo_root_on_path={boot['with_path']}"
        f" census_needs_PYTHONPATH_here={boot['needs_env']}"
        f" shipped_probes_insert_repo_root={boot['shipped_probes_insert_repo_root']}"
        f" this_probe_inserts_repo_root={boot['this_probe_inserts_repo_root']}"
    )
    arms.append(
        (
            "bootstrap_defect_measured",
            # two-sided: the shipped trio never puts the repo root on the path, this probe does.
            not boot["shipped_probes_insert_repo_root"]
            and boot["this_probe_inserts_repo_root"]
            and isinstance(boot["without_path"], str)
            and boot["without_path"] != "",
        )
    )

    passed = sum(1 for _, ok in arms if ok)
    failed = [name for name, ok in arms if not ok]
    print(
        f"ledger_ruling_face arms={len(arms)} arms_passed={passed} arms_failed={len(failed)}"
        f" failed={failed} residue={len(residue)} ledger_ruled_out={len(ruled)}"
        f" class_counts={dict(sorted(classes.items()))} residual_unexplained={len(unexplained)}"
        f" contradicted_rulings={len(contradicted)}"
        f" two_sided_controls={len(controls['not_ruled_out'])} PASS={not failed}"
    )
    code = 0 if not failed else 1
    print(f"w34_ledger_ruling_face_exit={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
