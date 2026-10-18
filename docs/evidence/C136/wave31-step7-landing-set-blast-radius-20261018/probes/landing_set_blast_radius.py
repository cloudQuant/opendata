"""Price a landing: which files a residue action edits, and how many accepted rows that stales.

Why this exists as an instrument
--------------------------------
Step 6 measured one number -- ``domains.yaml`` stales ``291/291`` accepted rows -- and left the
complement unmeasured: "the other buckets land provider-locally" was an inference from the commit
subjects.  This probe measures it four ways and makes the ``§9`` batch choice arithmetical.

1. **Pin closure per provider.**  The producer writes each row's pins by role (see step 6), so the
   set of paths a landed row of provider ``P`` pins is an *observation* of what a future row of
   ``P`` will also pin.  Splitting that set into paths pinned by every accepted row (tree-wide
   premises) and paths pinned only inside ``P`` (local) turns "does this batch cost a re-sign" into
   a lookup.
2. **Git precedent.**  Every ``feat(witness)`` commit that moved plane 1 is re-read from the object
   database: rows landed (the ``A -> B/350`` delta in its own subject) against the tree-wide
   paths its *product* files touch.  A landing that touched a tree-wide premise is the counter-arm;
   if only one kind existed the reading would be one-sided.
3. **Tamper arms through the shipped predicate.**  ``_simulate``/``_walk_claims`` are imported
   from the step-6 module rather than re-typed: one local provider file and one tree-wide file get
   their recorded digests replaced in memory, and the predicate must stale the local subset (< the
   tree) and the whole tree respectively.  Without these two arms the closure census is a path
   count, not a measurement of staleness.
4. **Partial-radius exposure.**  Radius is not binary: a pinned path can be tree-wide (291 rows),
   *partial* (2..290) or singleton.  A landing that touches a partial file re-signs a strict
   subset -- cheaper than a tree-wide cycle but not free.  ``§9``'s option B is priced by the
   exposure union of the precedent's own local paths, so "provider-local" reads as a row count and
   not as an adjective.

Nothing under version control is edited.  The action map is not re-derived either: it is read back
from the shipped ``residue_axis_census.py`` stdout, so this probe and the wave's own instrument
cannot disagree about which row is in which bucket.

Arms (all seven required)
------------------------
``A_tree_wide_reconciles``   the tree-wide path set derived here equals the four paths step 6
                             published, as a set, and each is pinned by every accepted row.
``B_closure_populated``      every residue provider with at least one accepted row yields a
                             non-empty local closure, and every such closure excludes the
                             tree-wide set.
``C_precedent_two_sided``    the precedent walk has at least one tree-wide-free landing and at
                             least one landing that paid a tree-wide touch (a one-sided history
                             would not price anything), and the *newest* landing commit's ``after``
                             count equals the ``OFFLINE_ACCEPTED`` count this run's judge
                             derives.  The chain between those commits is deliberately not gated:
                             the population is restricted to ``feat(witness)``, so a plane-1 move
                             recorded by any other commit type shows up as a break, and breaks are
                             printed for the reader to see the filter's cost.
``D_local_tamper``           tampering one provider-local pinned path stales a strict subset:
                             ``0 < stale_rows < accepted_rows``, and that subset is exactly the
                             rows the closure census says pin it.
``E_tree_wide_tamper``       tampering ``domains.yaml`` stales every accepted row (re-measured here,
                             not quoted from step 6).
``F_action_priced``          every residue row is put in exactly one price class by the *fields the
                             shipped census measured about it* (seam bucket, ``fake_ok``,
                             ``name_in_package``, ``action``), never by a class set typed into this
                             file.  A class that names a tree-wide key path is only allowed if arm A
                             put that path in the tree-wide set, every class must be non-empty, a
                             harness-bound row whose fake actually answers is a contradiction, the
                             class sizes must sum to the residue, and every class prints the action
                             labels it is made of.  The last clause exists because of a defect in
                             the first version of this arm: its catch-all bucket was named
                             ``provider_local`` (rows=23) while silently holding the census's 11
                             ``ledger_ruled_out`` rows, so the face published
                             ``rows_landable_provider_local=23`` for 12 landable rows.
``G_partial_radius``         the price is not binary.  A pinned path's radius is "how many accepted
                             rows pin it", which splits the pinned paths into tree-wide (radius =
                             the tree), *partial* (1 < radius < tree) and singleton/orphan.  Editing
                             a partial file stales a strict subset: cheaper than a tree-wide cycle,
                             not free.  "Which files does a landing touch" is answered from the git
                             precedent looked up in that radius table, not from a path-name
                             heuristic.  Required readings: at least one partial file exists, at
                             least one landed commit touched one (a priced precedent), at least one
                             touched none (a free precedent), every residue provider's accepted-row
                             count is printed -- the zeros included, because a provider with zero
                             accepted rows is exactly what makes a class free -- and no provider's
                             own local closure reaches the whole tree.  The extreme exposures print
                             the files that produced them, because an aggregate radius with no named
                             path cannot be checked against a planned landing.  Each touched path is
                             also asked whether it existed before that landing (``ls-tree`` on the
                             parent), because today's radius counts the rows a commit landed
                             itself: the reading a batch decision needs is the *pre-existing*
                             exposure, and arm G requires both sides of it -- at least one landing
                             that re-signed nothing pre-existing and at least one that re-signed
                             something.

Exits non-zero when any arm disagrees with its own control.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404  # house style: fixed argv children, no shell
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

from scripts.quality import iter2_model_evidence as judge  # noqa: E402

STEP6_PROBES = (
    REPO_ROOT / "docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018/probes"
)
sys.path.insert(0, str(STEP6_PROBES))

import domains_pin_blast_radius as step6  # noqa: E402  the shipped simulate/walk pair

C128_PROBES = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
ACTION_CENSUS = C128_PROBES / "residue_axis_census.py"
CLAIMS_REL = "docs/evidence/C101/claims.merged.json"
STEP6_RUN = (
    "docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018"
    "/runs/domains_pin_blast_radius.txt"
)
TREE_WIDE_EXPECTED = {
    "scripts/quality/iter2_claims_fixtures.py",
    "scripts/quality/iter2_claims_probe_runner.py",
    "opendata/data/domains.yaml",
    "opendata/data/providers/catalog.py",
}
DOMAINS_REL = "opendata/data/domains.yaml"
DELTA_RE = re.compile(r"(\d+)\s*->\s*(\d+)\s*/\s*350")
TUPLE_RE = re.compile(r"\('([^']+)', (\d+), (\d+)\)")


def _git(*args: str) -> str:
    """Run one read-only git command and return stdout.

    ``core.quotepath=off`` is load-bearing: the default escapes a non-ASCII path into a quoted octal
    blob, so ``docs/迭代计划/…/开发验收记录.md`` comes back starting with ``"`` and slips past the
    ``docs/`` filter below.  That is the same defect the repo's own tracked-file census hit.
    """
    res = subprocess.run(  # noqa: S603  # nosec B603 B607  # fixed argv, no shell, repo cwd
        ["git", "-c", "core.quotepath=off", *args],  # noqa: S607  # nosec B607  # git from PATH
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed rc={res.returncode}: {res.stderr[:200]}")
    return res.stdout


def _provider_of(identity: str) -> str:
    return identity.split("::", 1)[0]


def _exists_at_parent(sha: str, path: str) -> bool:
    """Did ``path`` already exist before this landing?

    The verdict is read from ``ls-tree``'s stdout, not from an exit code.  ``cat-file -e`` was the
    first attempt and it answered **rc=128** ("exists on disk, but not in ``<sha>^``") for exactly
    the case this helper is asking about, so a ``rc != 0 means missing`` reading would have crashed
    the arm on the first newly created file.  ``ls-tree`` exits 0 either way and prints a line only
    when the object is in that tree.

    The distinction is the whole point of the reading: a landing that touched only files it created
    itself re-signed no pre-existing row, and without this test arm G could not tell that case apart
    from an expensive one.
    """
    res = subprocess.run(  # noqa: S603  # nosec B603 B607  # fixed argv, no shell, repo cwd
        ["git", "-c", "core.quotepath=off", "ls-tree", f"{sha}^", "--", path],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(f"git ls-tree rc={res.returncode}: {res.stderr[:160]}")
    return bool(res.stdout.strip())


def _snake(name: str) -> str:
    """``EtfHistorical`` -> ``etf_historical`` (the house key shape used by step 6's block)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return re.sub(r"_+", "_", s).lower()


def _pin_closure(
    accepted_claims: list[dict[str, Any]], tree_wide: set[str]
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Per-provider (local paths -> slots, tree-wide paths -> slots) from landed rows."""
    local: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    shared: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for claim in accepted_claims:
        provider = str(claim.get("provider"))
        for _group, _index, entry in step6._pin_slots(claim):  # imported, not re-typed
            path = str(entry["path"])
            if path in tree_wide:
                shared[provider][path] += 1
            else:
                local[provider][path] += 1
    return dict(local), dict(shared)


def _precedent_walk(tree_wide: set[str]) -> tuple[list[dict[str, Any]], int, int]:
    """Read every ``feat(witness)`` commit that claims a plane-1 delta, oldest first.

    Returns ``(per-commit faces, commits matching the prefix, commits with no parsable delta)``.
    """
    log = _git("log", "--format=%H%x1f%ct%x1f%s", "--no-merges")
    faces: list[dict[str, Any]] = []
    prefix_hits = 0
    no_delta = 0
    for line in log.splitlines():
        sha, _ct, subject = line.split("\x1f", 2)
        if not subject.startswith("feat(witness)"):
            continue
        prefix_hits += 1
        m = DELTA_RE.search(subject)
        if not m:
            no_delta += 1
            continue
        before, after = int(m.group(1)), int(m.group(2))
        name_only = _git("show", "--name-only", "--format=", sha)
        product = [p for p in name_only.splitlines() if p and not p.startswith("docs/")]
        wide = sorted(p for p in product if p in tree_wide)
        faces.append(
            {
                "sha": sha[:9],
                "ct": int(_ct),
                "delta": after - before,
                "before": before,
                "after": after,
                "product_files": len(product),
                "tree_wide_touches": wide,
                "local_paths": sorted(p for p in product if p not in tree_wide),
            }
        )
    faces.sort(key=lambda f: f["ct"])
    return faces, prefix_hits, no_delta


def _census_rows() -> list[dict[str, str]]:
    """Every ``row ...`` line of the shipped action census, fields and all.

    The fields are kept rather than dropped because arm F prices a row from what the shipped
    instrument *measured* about it (seam bucket, whether the fake answers, whether the model name is
    already in the package), not from a class set typed into this file.
    """
    res = subprocess.run(  # noqa: S603  # nosec B603  # this interpreter, one fixed argv child
        [sys.executable, str(ACTION_CENSUS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise SystemExit(f"action census rc={res.returncode}: {res.stderr[-400:]}")
    rows: list[dict[str, str]] = []
    for line in res.stdout.splitlines():
        if not line.startswith("row "):
            continue
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        fields["identity"] = line.split()[1]
        if "action" not in fields:
            raise SystemExit(f"census row line carries no action field: {line[:120]}")
        rows.append(fields)
    if not rows:
        raise SystemExit("action census printed no `row ...` lines: census face is unreadable")
    return rows


def main(argv: list[str] | None = None) -> int:
    """Build the closure, walk the precedents, tamper two files, print the price face."""
    ap = argparse.ArgumentParser(description="landing-set blast radius")
    ap.add_argument("--claims", default=CLAIMS_REL)
    ap.add_argument("--wide-lookback", type=int, default=8)
    ap.add_argument("--partial-lookback", type=int, default=6)
    ap.add_argument("--exposure-sample", type=int, default=3)
    args = ap.parse_args(argv)

    claims_path = REPO_ROOT / args.claims
    doc = json.loads(claims_path.read_text(encoding="utf-8"))
    claims = [c for c in doc.get("claims", []) if isinstance(c, dict)]
    inventory = judge.load_inventory(REPO_ROOT / judge.DEFAULT_INVENTORY)
    report = judge.build_report(
        inventory,
        doc,
        claims_sha256=judge.sha256_bytes(claims_path.read_bytes()),
        claims_path=args.claims,
    )
    summary = report["summary"]
    accepted = [m for m in report["models"] if m["status"] == "OFFLINE_ACCEPTED"]
    residue = [m for m in report["models"] if m["status"] != "OFFLINE_ACCEPTED"]
    accepted_ids = {(m["provider"], m["upstream_model"]) for m in accepted}
    accepted_claims = [
        c for c in claims if (c.get("provider"), c.get("upstream_model")) in accepted_ids
    ]
    print(
        f"population: inventory={summary['identity_count']} accepted={len(accepted)} "
        f"residue={len(residue)} status_counts={summary['status_counts']} "
        f"identity_set={inventory['identity_set_sha256'][:16]} "
        f"accepted_claims={len(accepted_claims)}"
    )

    slot_rows: dict[str, set[str]] = defaultdict(set)
    slot_count: dict[str, int] = defaultdict(int)
    for claim in accepted_claims:
        rid = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        for _group, _index, entry in step6._pin_slots(claim):  # imported, not re-typed
            slot_count[entry["path"]] += 1
            slot_rows[entry["path"]].add(rid)
    tree_wide = {p for p, rows in slot_rows.items() if len(rows) == len(accepted)}
    step6_line = next(
        (
            ln
            for ln in (REPO_ROOT / STEP6_RUN).read_text(encoding="utf-8").splitlines()
            if ln.startswith("tree_wide_paths(")
        ),
        "",
    )
    step6_set = {t[0] for t in TUPLE_RE.findall(step6_line)}
    arm_a = tree_wide == TREE_WIDE_EXPECTED == step6_set
    print(
        f"A_tree_wide derived={sorted(tree_wide)} expected={sorted(TREE_WIDE_EXPECTED)} "
        f"step6={sorted(step6_set)} step6_line_found={bool(step6_line)} PASS={arm_a}"
    )

    local, shared = _pin_closure(accepted_claims, tree_wide)
    residue_providers = sorted(
        {_provider_of(f"{m['provider']}::{m['upstream_model']}") for m in residue}
    )
    with_sibling = [p for p in residue_providers if local.get(p)]
    without = [p for p in residue_providers if not local.get(p)]
    leaks = {p: sorted(set(local[p]) & tree_wide) for p in local if set(local[p]) & tree_wide}
    arm_b = bool(with_sibling) and not leaks and bool(residue_providers)
    print(
        f"B_closure providers_residue={len(residue_providers)} "
        f"with_local_closure={len(with_sibling)} no_landed_sibling={without} "
        f"closure_leaks_tree_wide={leaks} PASS={arm_b}"
    )
    accepted_by_provider: dict[str, int] = defaultdict(int)
    for model in accepted:
        accepted_by_provider[model["provider"]] += 1
    for provider in with_sibling:
        radii = {p: len(slot_rows[p]) for p in local[provider]}
        top = sorted(radii.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        print(
            f"  closure provider={provider} "
            f"accepted_rows={accepted_by_provider[provider]} "
            f"local_paths={len(local[provider])} shared_paths={len(shared.get(provider, {}))} "
            f"max_local_rows={max(radii.values())} top={top}"
        )

    faces, prefix_hits, no_delta = _precedent_walk(tree_wide)
    free = [f for f in faces if not f["tree_wide_touches"]]
    priced = [f for f in faces if f["tree_wide_touches"]]
    rows_free = sum(f["delta"] for f in free if f["delta"] > 0)
    rows_priced = sum(f["delta"] for f in priced if f["delta"] > 0)
    # The trajectory is only contiguous over *all* commit types, and this population is
    # restricted to ``feat(witness)``, so breaks are expected and are printed rather than gated.
    # What can fail is the endpoint: the newest landing commit must hand its count to the judge
    # reading in this run.
    breaks = [
        (b["sha"], a["after"], b["before"])
        for a, b in zip(faces, faces[1:], strict=False)
        if a["after"] != b["before"]
    ]
    endpoint = faces[-1]["after"] if faces else -1
    arm_c = bool(free) and bool(priced) and endpoint == len(accepted)
    print(
        f"C_precedent feat_witness_commits={prefix_hits} no_delta_skipped={no_delta} "
        f"with_plane1_delta={len(faces)} tree_wide_free={len(free)} tree_wide_priced={len(priced)} "
        f"rows_landed_free={rows_free} rows_landed_priced={rows_priced} "
        f"rows_total={rows_free + rows_priced} newest_landing={faces and faces[-1]['sha']} "
        f"endpoint_after={endpoint} judge_accepted={len(accepted)} "
        f"chain_breaks_outside_population={len(breaks)} breaks={breaks} PASS={arm_c}"
    )
    for face in sorted(faces, key=lambda f: -f["delta"])[: args.wide_lookback]:
        print(
            f"  precedent sha={face['sha']} delta={face['delta']} "
            f"product_files={face['product_files']} tree_wide_touches={face['tree_wide_touches']}"
        )
    for face in priced:
        print(
            f"  priced sha={face['sha']} delta={face['delta']} wide={face['tree_wide_touches']} "
            f"local={face['local_paths'][:4]}"
        )

    census_rows = _census_rows()
    domains_doc = yaml.safe_load((REPO_ROOT / DOMAINS_REL).read_text(encoding="utf-8")) or {}
    domain_keys = set(domains_doc.get("domains") or {})
    by_action: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in census_rows:
        by_action[row["action"]].append(row)
    print(
        f"actions: labels={len(by_action)} rows={len(census_rows)} "
        f"{json.dumps({k: len(v) for k, v in sorted(by_action.items())}, sort_keys=True)}"
    )
    if len(census_rows) != len(residue):
        raise SystemExit(f"action census {len(census_rows)} != residue {len(residue)}")

    # --- arm D: one provider-local pinned path, tampered in memory.
    local_target = max(
        (p for p in slot_rows if p not in tree_wide and len(slot_rows[p]) > 1),
        key=lambda p: len(slot_rows[p]),
    )
    sim_local = judge.sha256_bytes(
        (REPO_ROOT / local_target).read_bytes() + b"# landing-set pricer local tamper marker\n"
    )
    d_rows, d_slots, _d_issues, _ = step6._walk_claims(accepted_claims, local_target, sim_local)
    expected_d = slot_rows[local_target]
    arm_d = 0 < len(d_rows) < len(accepted) and d_rows == expected_d
    print(
        f"D_local_tamper path={local_target} slots={d_slots} stale_rows={len(d_rows)} "
        f"expected_rows={len(expected_d)} tree={len(accepted)} sample={sorted(d_rows)[:3]} "
        f"PASS={arm_d}"
    )

    # --- arm E: the tree-wide file, re-measured rather than quoted.
    sim_wide = judge.sha256_bytes(
        (REPO_ROOT / DOMAINS_REL).read_bytes() + b"# landing-set pricer tree-wide marker\n"
    )
    e_rows, e_slots, _e_issues, _ = step6._walk_claims(accepted_claims, DOMAINS_REL, sim_wide)
    arm_e = len(e_rows) == len(accepted) and e_slots == slot_count[DOMAINS_REL]
    print(
        f"E_tree_wide_tamper path={DOMAINS_REL} slots={e_slots} "
        f"expected_slots={slot_count[DOMAINS_REL]} stale_rows={len(e_rows)} tree={len(accepted)} "
        f"PASS={arm_e}"
    )

    # --- arm F: price each row from the fields the shipped census measured about it.
    #
    # Two of the classes are keyed on a path that arm A proved is tree-wide, so the price is a
    # lookup rather than a claim; the third (a row whose model class is not in its package at all)
    # is reported as priced by step 6's own domains.yaml measurement, because the naming predicate
    # that would decide it here is only a precedent signal -- ``accepted_without_candidate_key``
    # prints how many landed rows would falsify it as a necessity.
    catalog_rel = "opendata/data/providers/catalog.py"
    runner_rel = "scripts/quality/iter2_claims_probe_runner.py"
    classes: dict[str, list[str]] = defaultdict(list)
    contradictions: list[str] = []
    for row in census_rows:
        ident = row["identity"]
        seam = row.get("seam", "")
        fake_ok = row.get("fake_ok", "")
        action = row["action"]
        if seam == "NO_PACKAGE_ON_DISK":
            classes["wide_catalog"].append(ident)
        elif action == "shard_entry_needs_harness":
            if fake_ok != "False":
                contradictions.append(f"{ident}: harness action but fake_ok={fake_ok}")
            classes["wide_harness"].append(ident)
        elif action == "implement_missing_model" and row.get("name_in_package") == "False":
            classes["new_model_key_step6"].append(ident)
        elif action == "ledger_ruled_out":
            classes["ledger_ruled_out"].append(ident)
        else:
            classes["provider_local_landable"].append(ident)
    class_paths = {
        "wide_catalog": catalog_rel,
        "wide_harness": runner_rel,
        "new_model_key_step6": DOMAINS_REL,
        "ledger_ruled_out": "",
        "provider_local_landable": "",
    }
    partition = sum(len(v) for v in classes.values())
    wide_unproven = sorted(c for c, p in class_paths.items() if p and p not in tree_wide)
    empty_classes = sorted(c for c, v in classes.items() if not v)
    arm_f = (
        partition == len(residue)
        and not contradictions
        and not wide_unproven
        and not empty_classes
        and bool(classes["provider_local_landable"])
    )
    accepted_no_key = sum(
        1 for m in accepted if f"{m['provider']}_{_snake(m['upstream_model'])}" not in domain_keys
    )
    print(
        f"F_pricing partition={partition} residue={len(residue)} wide_unproven={wide_unproven} "
        f"empty_classes={empty_classes} contradictions={contradictions} PASS={arm_f}"
    )
    action_of = {row["identity"]: row["action"] for row in census_rows}
    for label, rows in sorted(classes.items()):
        by_provider: dict[str, int] = defaultdict(int)
        by_action_label: dict[str, int] = defaultdict(int)
        for ident in rows:
            by_provider[_provider_of(ident)] += 1
            by_action_label[action_of[ident]] += 1
        top = sorted(by_provider.items(), key=lambda kv: (-kv[1], kv[0]))
        acts = dict(sorted(by_action_label.items(), key=lambda kv: (-kv[1], kv[0])))
        print(
            f"  price class={label} rows={len(rows)} key_path={class_paths[label] or 'none'} "
            f"in_tree_wide={(class_paths[label] in tree_wide) if class_paths[label] else 'n/a'} "
            f"actions={acts} providers={top}"
        )
    rows_wide = len(classes["wide_catalog"]) + len(classes["wide_harness"])
    print(
        f"domains_key_predicate provider_snake_key_absent_rows="
        f"{len(classes['new_model_key_step6'])} "
        f"accepted_counterexamples={accepted_no_key} "
        f"(so the predicate prices the tmx precedent of step 6, not a necessity for every row)"
    )
    print(
        f"price_face residue={len(residue)} rows_behind_a_tree_wide_premise={rows_wide} "
        f"rows_new_model_needing_key={len(classes['new_model_key_step6'])} "
        f"rows_landable_provider_local={len(classes['provider_local_landable'])} "
        f"rows_ledger_ruled_out={len(classes['ledger_ruled_out'])} "
        f"precedent_commits_free={len(free)} precedent_rows_free={rows_free} "
        f"precedent_commits_priced={len(priced)} precedent_rows_priced={rows_priced}"
    )

    # --- arm G: partial radius, priced from the precedent rather than from path names.
    tree = len(accepted)
    radius = {p: len(r) for p, r in slot_rows.items()}
    partial = {p: n for p, n in radius.items() if 1 < n < tree}
    singletons = sum(1 for n in radius.values() if n == 1)
    print(
        f"G_radius pinned_paths={len(radius)} tree_wide={len(tree_wide)} partial={len(partial)} "
        f"singleton={singletons} max_partial_rows={max(partial.values()) if partial else 0} "
        f"tree={tree}"
    )
    for p, n in sorted(partial.items(), key=lambda kv: (-kv[1], kv[0]))[: args.partial_lookback]:
        print(f"  partial path={p} accepted_rows_pinning={n}")
    exposures: list[tuple[str, int, int, int]] = []
    corrected_rows: list[int] = []
    all_new: list[str] = []
    pre_of: dict[str, list[str]] = {}
    ls_tree_calls = 0
    for face in faces:
        touched = [p for p in face["local_paths"] if p in partial]
        union: set[str] = set()
        pre_union: set[str] = set()
        pre_names: list[str] = []
        for p in touched:
            union |= slot_rows[p]
            ls_tree_calls += 1
            if _exists_at_parent(face["sha"], p):
                pre_union |= slot_rows[p]
                pre_names.append(p)
        exposures.append((face["sha"], face["delta"], len(touched), len(union)))
        corrected_rows.append(len(pre_union))
        pre_of[face["sha"]] = pre_names
        if touched and not pre_names:
            all_new.append(face["sha"])
    exposure_rows = [e[3] for e in exposures]
    free_precedent = sum(1 for e in exposures if e[2] == 0)
    local_over_tree: dict[str, int] = {}
    zero_acc: list[str] = []
    pos_acc: list[str] = []
    for provider in residue_providers:
        paths = list(local.get(provider, {}))
        union_p: set[str] = set()
        for p in paths:
            union_p |= slot_rows[p]
        (zero_acc if accepted_by_provider[provider] == 0 else pos_acc).append(provider)
        if len(union_p) >= tree:
            local_over_tree[provider] = len(union_p)
        print(
            f"  local provider={provider} accepted_rows={accepted_by_provider[provider]} "
            f"closure_paths={len(paths)} closure_union={len(union_p)} tree={tree}"
        )
    ordered = sorted(exposures, key=lambda e: e[3])
    by_sha = {f["sha"]: f for f in faces}
    for entry in ordered[: args.exposure_sample] + ordered[-args.exposure_sample :]:
        face = by_sha[entry[0]]
        files = [p for p in face["local_paths"] if p in partial]
        print(
            f"  exposure sha={entry[0]} delta={entry[1]} partial_paths={entry[2]} "
            f"union_rows={entry[3]} preexisting_paths={len(pre_of[entry[0]])} "
            f"preexisting={pre_of[entry[0]][:2]} wide={face['tree_wide_touches']} files={files[:2]}"
        )
    max_exposure = max(exposure_rows) if exposure_rows else tree
    min_exposure = min(exposure_rows) if exposure_rows else 0
    corrected_max = max(corrected_rows) if corrected_rows else 0
    corrected_min = min(corrected_rows) if corrected_rows else -1
    priced_zero = sum(1 for n in corrected_rows if n == 0)
    print(
        f"G_selfcreated landings_priced_zero_preexisting={priced_zero} of={len(corrected_rows)} "
        f"corrected_min={corrected_min} corrected_max={corrected_max} "
        f"all_local_paths_new={all_new} ls_tree_calls={ls_tree_calls} "
        f"local_paths_total={sum(len(f['local_paths']) for f in faces)}"
    )
    arm_g = (
        bool(partial)
        and singletons > 0
        and max_exposure < tree
        and min_exposure < max_exposure
        and bool(zero_acc)
        and bool(pos_acc)
        and bool(all_new)
        and corrected_min == 0
        and corrected_max > 0
        and not local_over_tree
    )
    print(
        f"G_precedent landings={len(exposures)} touching_partial="
        f"{sum(1 for e in exposures if e[2] > 0)} touching_none={free_precedent} "
        f"exposure_min={min_exposure} exposure_max={max_exposure} "
        f"corrected_min={corrected_min} corrected_max={corrected_max} "
        f"priced_zero={priced_zero} tree={tree} zero_accepted_providers={zero_acc} "
        f"positive_accepted_providers={len(pos_acc)} "
        f"local_closure_reaches_tree={local_over_tree} PASS={arm_g}"
    )

    passed = sum([arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g])
    print(
        f"landing_set_face arms=7 arms_passed={passed} arms_failed={7 - passed} "
        f"identity_set={inventory['identity_set_sha256'][:16]} PASS={passed == 7}"
    )
    print(f"landing_set_blast_radius_exit={0 if passed == 7 else 1}")
    return 0 if passed == 7 else 1


if __name__ == "__main__":
    raise SystemExit(main())
