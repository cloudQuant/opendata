#!/usr/bin/env python
"""Price the D-option batch (yfinance's 12 provider-local landable rows) BEFORE it is landed.

``landing_set_blast_radius.py`` (the C136 face) prices residue rows by *class*: it says the 12 rows
sit behind no tree-wide premise, and it reports ``local provider=yfinance accepted_rows=0
closure_union=0``.  That closure is derived from accepted rows, and yfinance has none, so the 0 is
**vacuous** -- it measures nothing about the files a future landing must edit.  This probe replaces
that zero with a measurement over the thing that does exist: the provider's own files on disk.

For every path in the population below it reads the current blast radius from the same pin census
the face uses (``accepted_claims`` x ``step6._pin_slots``), so the answer is "how many of the 291
accepted rows would go stale if this file changed", not a guess from the path's name.

Population (per provider, working tree, ``.py`` only)
    ``opendata/data/providers/<prov>/**``  +  ``opendata/data/models/<prov>*.py``
    +  ``scripts/quality/iter2_witnesses/<prov>.py``  +  ``…/iter2_witnesses/_parts/<prov>/**``

Arms (all seven required)
``A_population_non_empty``  both providers glob a non-empty file set, with the counts printed --
    every later zero has a denominator.
``B_pin_reconciliation``    per provider, (globbed paths with radius>0) == (pin-census keys living
    under this provider's prefixes).  A wrong glob silently reads 0 pinned; this is the arm that
    would catch it.
``C_local_target_is_free``  yfinance: max radius == 0 and pinned paths == 0.
``D_control_is_not_free``   fmp: max radius == 69 (the face's ``max_partial_rows``) and pinned
    paths > 0 -- the same computation, on a provider that does have accepted rows, returns non-zero,
    so C's zero is a measurement and not a dead predicate.
``E_engine_price_list``     every pinned path under ``opendata/data/providers/_engine/`` and its
    radius; the batch stays free only while it stays inside the local population, so the price of
    stepping outside is printed next to it.
``F_row_level_membership``  the 12 rows: identities printed, all ``provider == yfinance``, all
    ``witness_module`` absent, action labels only ``add_code_carrying_guard`` and
    ``retarget_fetcher_output`` -- the face's class is backed row-by-row, not by a count.
``G_generation_untouched``  identity set and accepted count equal the face's generation (this probe
    writes nothing).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import landing_set_blast_radius as sib  # noqa: E402  re-uses the face's judge + step-6 wiring

REPO_ROOT = sib.REPO_ROOT
LOCAL_PREFIXES = (
    "opendata/data/providers/{p}/",
    "opendata/data/models/{p}",
    "scripts/quality/iter2_witnesses/{p}.py",
    "scripts/quality/iter2_witnesses/_parts/{p}/",
)
ENGINE_REL = "opendata/data/providers/_engine"
FMP_MAX_EXPECTED = 69
LANDABLE_EXPECTED = 12


def _local_paths(provider: str) -> list[str]:
    """Glob this provider's own files from disk; missing directories simply contribute nothing."""
    out: set[str] = set()
    pkg = REPO_ROOT / "opendata/data/providers" / provider
    out |= {str(p.relative_to(REPO_ROOT)) for p in sorted(pkg.rglob("*.py")) if p.is_file()}
    models = REPO_ROOT / "opendata/data/models"
    out |= {
        str(p.relative_to(REPO_ROOT)) for p in sorted(models.glob(f"{provider}*.py")) if p.is_file()
    }
    wit = REPO_ROOT / "scripts/quality/iter2_witnesses"
    if (wit / f"{provider}.py").is_file():
        out.add(str((wit / f"{provider}.py").relative_to(REPO_ROOT)))
    parts = wit / "_parts" / provider
    out |= {str(p.relative_to(REPO_ROOT)) for p in sorted(parts.rglob("*.py")) if p.is_file()}
    return sorted(out)


def _prefixes(provider: str) -> tuple[str, ...]:
    return tuple(t.format(p=provider) for t in LOCAL_PREFIXES)


def _pinned_under(slot_rows: dict[str, set[str]], provider: str) -> set[str]:
    """Keys of the pin census that live under this provider's own prefixes."""
    prefixes = _prefixes(provider)
    return {p for p in slot_rows if p.startswith(prefixes)}


def main(argv: list[str] | None = None) -> int:
    """Read the pin census, price both populations, print the seven-arm face."""
    del argv
    claims_path = REPO_ROOT / sib.CLAIMS_REL
    doc = json.loads(claims_path.read_text(encoding="utf-8"))
    claims = [c for c in doc.get("claims", []) if isinstance(c, dict)]
    judge = sib.judge
    inventory = judge.load_inventory(REPO_ROOT / judge.DEFAULT_INVENTORY)
    report = judge.build_report(
        inventory,
        doc,
        claims_sha256=judge.sha256_bytes(claims_path.read_bytes()),
        claims_path=sib.CLAIMS_REL,
    )
    accepted = [m for m in report["models"] if m["status"] == "OFFLINE_ACCEPTED"]
    accepted_ids = {(m["provider"], m["upstream_model"]) for m in accepted}
    accepted_claims = [
        c for c in claims if (c.get("provider"), c.get("upstream_model")) in accepted_ids
    ]
    slot_rows: dict[str, set[str]] = defaultdict(set)
    for claim in accepted_claims:
        rid = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        for _group, _index, entry in sib.step6._pin_slots(claim):  # imported, not re-typed
            slot_rows[str(entry["path"])].add(rid)
    tree = len(accepted)
    print(
        f"population: accepted={tree} accepted_claims={len(accepted_claims)} "
        f"pinned_paths={len(slot_rows)} identity_set="
        f"{inventory['identity_set_sha256'][:16]}"
    )

    radii: dict[str, dict[str, int]] = {}
    for provider in ("yfinance", "fmp"):
        paths = _local_paths(provider)
        radius = {p: len(slot_rows.get(p, ())) for p in paths}
        radii[provider] = radius
        pinned = {p: n for p, n in radius.items() if n > 0}
        mx = max(radius.values()) if radius else -1
        print(
            f"  provider={provider} files_globbed={len(paths)} pinned={len(pinned)} "
            f"max_radius={mx} sum_radius={sum(radius.values())} "
            f"top={sorted(pinned.items(), key=lambda kv: (-kv[1], kv[0]))[:3]}"
        )

    arm_a = all(radii[p] for p in radii)
    print(
        f"A_population_non_empty yfinance={len(radii['yfinance'])} fmp={len(radii['fmp'])} "
        f"PASS={arm_a}"
    )

    recon: dict[str, list[str]] = {}
    for provider in ("yfinance", "fmp"):
        census_side = _pinned_under(slot_rows, provider)
        glob_side = {p for p, n in radii[provider].items() if n > 0}
        asym = sorted(census_side ^ glob_side)
        if asym:
            recon[provider] = asym
    arm_b = not recon
    print(f"B_pin_reconciliation mismatched={recon} PASS={arm_b}")

    yf_pinned = [p for p, n in radii["yfinance"].items() if n > 0]
    yf_max = max(radii["yfinance"].values())
    arm_c = yf_pinned == [] and yf_max == 0
    print(
        f"C_local_target_is_free provider=yfinance pinned={len(yf_pinned)} "
        f"max={yf_max} PASS={arm_c}"
    )

    fmp_max = max(radii["fmp"].values())
    fmp_pinned = [p for p, n in radii["fmp"].items() if n > 0]
    arm_d = fmp_max == FMP_MAX_EXPECTED and len(fmp_pinned) > 0
    print(
        f"D_control_is_not_free provider=fmp pinned={len(fmp_pinned)} max={fmp_max} "
        f"expected_max={FMP_MAX_EXPECTED} "
        f"expected_max_from_face=G_radius:max_partial_rows PASS={arm_d}"
    )

    engine = {p: len(r) for p, r in slot_rows.items() if p.startswith(f"{ENGINE_REL}/")}
    eng_max = max(engine.values()) if engine else 0
    arm_e = bool(engine) and eng_max > 0
    print(f"E_engine_price_list paths={len(engine)} max={eng_max} PASS={arm_e}")
    for p, n in sorted(engine.items(), key=lambda kv: (-kv[1], kv[0]))[:6]:
        print(f"  engine path={p} accepted_rows_pinning={n}")

    rows = sib._census_rows()
    landable = [
        r for r in rows if r["action"] in ("add_code_carrying_guard", "retarget_fetcher_output")
    ]
    provs = sorted({sib._provider_of(r["identity"]) for r in landable})
    no_module = [r for r in landable if r.get("witness_module") not in ("True", "1", "yes")]
    labels: dict[str, int] = defaultdict(int)
    for r in landable:
        labels[r["action"]] += 1
    arm_f = (
        len(landable) == LANDABLE_EXPECTED
        and provs == ["yfinance"]
        and len(no_module) == len(landable)
        and set(labels) == {"add_code_carrying_guard", "retarget_fetcher_output"}
    )
    print(
        f"F_row_level_membership rows={len(landable)} expected={LANDABLE_EXPECTED} "
        f"providers={provs} without_witness_module={len(no_module)} labels={dict(labels)} "
        f"PASS={arm_f}"
    )
    for r in sorted(landable, key=lambda x: x["identity"]):
        print(
            f"  row {r['identity']} status={r['status']} bucket={r['bucket']} "
            f"verdict={r['verdict']} action={r['action']}"
        )

    arm_g = (
        tree == 291
        and len(accepted_claims) == 1455
        and inventory["identity_set_sha256"].startswith("5168e1e723999312")
    )
    print(f"G_generation_untouched accepted={tree} claims={len(accepted_claims)} PASS={arm_g}")

    arms: list[tuple[str, bool]] = [
        ("A", arm_a),
        ("B", arm_b),
        ("C", arm_c),
        ("D", arm_d),
        ("E", arm_e),
        ("F", arm_f),
        ("G", arm_g),
    ]
    passed = sum(1 for _n, ok in arms if ok)
    failed = [n for n, ok in arms if not ok]
    verdict = passed == len(arms)
    print(
        f"landing_target_radius_face arms={len(arms)} arms_passed={passed} "
        f"arms_failed={len(failed)} failed={failed} PASS={verdict}"
    )
    # The exit reading is derived from the verdict, never typed: a red face must not sign 0.
    rc = 0 if verdict else 1
    print(f"landing_target_radius_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
