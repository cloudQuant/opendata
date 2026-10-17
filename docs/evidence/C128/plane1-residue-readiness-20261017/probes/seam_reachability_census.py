"""Classify every provider's transport seam usage, so reachability is a census and not a spot check.

:mod:`seam_surface_match.py` answers the seam question per provider but takes its arms from the
command line. Before spending a wave on rows, the question is which providers the *get-only* fake
can reach at all. Answering that by hand for 32 providers is where a hardcoded membership list
creeps in, so this driver enumerates the provider packages from disk and reuses the shipped
collector, the shipped fake surface and the shipped judge-report reader -- it imports all three,
it does not restate any of them.

``seam_surface_match`` globs ``models/*.py`` because that is where the witness kind patches. A
provider could call the shared client somewhere else in its package, so each row prints a second,
whole-package verb set. The narrower one is what the seam mechanism sees; the wider one is what
rules out "the seam is used, just not under models/".

Five buckets partition the 350 identities: SEAM_REACHABLE (the get-only fake answers every verb),
BLOCKED_ON_RUNNER_SURFACE (a verb outside the fake's surface -- widening it is a full re-sign),
SEAM_USED_OUTSIDE_MODELS, SEAM_NEVER_USED (the provider owns its transport, so client_seam is the
wrong witness kind), and NO_PACKAGE_ON_DISK (the pinned inventory names the provider but the
repository has no such package, so no witness mechanism applies at all).

Prints one row per unit plus two face lines: the bucket census with its row coverage of the report,
and the residual-per-bucket table with the ``accepted + residual == 350`` equation. Exits 0
unconditionally -- this is a measurement of the population, not a judge over it.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seam_surface_match as seam  # the shipped collector + fake + report reader

PROVIDERS_DIR = seam.REPO_ROOT / "opendata" / "data" / "providers"
WITNESS_DIR = seam.REPO_ROOT / "scripts" / "quality" / "iter2_witnesses"


def _witness_module_shape(provider: str) -> tuple[bool, bool]:
    """Whether the provider ships a witness module, and whether it declares the client seam.

    Two shipped mechanisms land plane-1 rows: the ``client_seam`` kind (which is what the verb
    columns above describe) and the engine's own ``transport`` leg served by
    ``FixedResponseTransport``. The kind is declared as the ``"shared_http_client"`` string
    constant, top-level in some modules and nested per entry in others, so it is detected by
    literal rather than by variable name. A provider with no client seam is therefore not blocked
    -- it needs the other mechanism, the same way tiingo and econdb were accepted.
    """
    path = WITNESS_DIR / f"{provider}.py"
    if not path.is_file():
        return False, False
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    declares_seam = any(
        isinstance(node, ast.Constant) and node.value == "shared_http_client"
        for node in ast.walk(tree)
    )
    return True, declares_seam


def _package_verbs(provider: str) -> set[str]:
    """The same collection, applied to every module in the provider package."""
    verbs: set[str] = set()
    for module_path in sorted((PROVIDERS_DIR / provider).rglob("*.py")):
        verbs |= seam._verbs_in(module_path)
    return verbs


def main() -> int:
    """Print one row per provider package on disk plus the bucket census."""
    surface = seam._fake_surface()
    report = seam._load_report()
    identity_set = report["inventory"]["expected_identity_set_sha256"][:16]
    judged = seam._accepted_by_provider(report)
    packages = sorted(p.name for p in PROVIDERS_DIR.iterdir() if (p / "__init__.py").is_file())

    buckets: Counter[str] = Counter()
    residual_by_bucket: Counter[str] = Counter()
    residual_by_mechanism: Counter[str] = Counter()
    witness_modules = 0
    client_seam_modules = 0
    accepted_total = 0
    rows_seen = 0
    for pkg in packages:
        models_verbs = seam._transport_verbs(pkg)
        package_verbs = _package_verbs(pkg)
        missing = sorted(models_verbs - surface)
        has_witness, has_client_seam = _witness_module_shape(pkg)
        witness_modules += int(has_witness)
        client_seam_modules += int(has_client_seam)
        if not models_verbs:
            bucket = "SEAM_NEVER_USED" if not package_verbs else "SEAM_USED_OUTSIDE_MODELS"
        elif missing:
            bucket = "BLOCKED_ON_RUNNER_SURFACE"
        else:
            bucket = "SEAM_REACHABLE"
        mechanism = {
            "SEAM_REACHABLE": "client_seam",
            "SEAM_USED_OUTSIDE_MODELS": "client_seam_other_module",
            "BLOCKED_ON_RUNNER_SURFACE": "runner_surface_widening",
            "SEAM_NEVER_USED": "engine_transport",
        }[bucket]
        buckets[bucket] += 1
        statuses = judged.get(pkg, {})
        total = sum(statuses.values())
        accepted = statuses.get("OFFLINE_ACCEPTED", 0)
        rows_seen += total
        accepted_total += accepted
        residual_by_bucket[bucket] += total - accepted
        residual_by_mechanism[mechanism] += total - accepted
        print(
            f"seam_census_provider={pkg} bucket={bucket} mechanism={mechanism} "
            f"models_verbs={sorted(models_verbs)} package_verbs={sorted(package_verbs)} "
            f"witness_module={has_witness} client_seam={has_client_seam} "
            f"rows={total} accepted={accepted} residual={total - accepted}"
        )

    # Inventory names with no package on disk are their own bucket: their rows cannot be reached by
    # any witness mechanism until the provider exists, and leaving them out of the partition is how
    # a census ends up describing part of the inventory while claiming to describe all of it.
    for pkg in sorted(set(judged) - set(packages)):
        statuses = judged[pkg]
        total = sum(statuses.values())
        accepted = statuses.get("OFFLINE_ACCEPTED", 0)
        buckets["NO_PACKAGE_ON_DISK"] += 1
        residual_by_bucket["NO_PACKAGE_ON_DISK"] += total - accepted
        residual_by_mechanism["new_provider_package"] += total - accepted
        rows_seen += total
        accepted_total += accepted
        print(
            f"seam_census_provider={pkg} bucket=NO_PACKAGE_ON_DISK mechanism=new_provider_package "
            f"models_verbs=NA package_verbs=NA witness_module=False client_seam=False "
            f"rows={total} accepted={accepted} residual={total - accepted}"
        )

    disk_only = sorted(set(packages) - set(judged))
    report_rows = len(report["models"])
    residual_total = sum(residual_by_bucket.values())
    print(
        f"seam_census_units={len(packages) + len(set(judged) - set(packages))} "
        f"buckets={dict(sorted(buckets.items()))} "
        f"witness_modules={witness_modules} client_seam_modules={client_seam_modules} "
        f"census_rows={rows_seen} report_rows={report_rows} "
        f"rows_covered={report_rows == rows_seen} disk_only={disk_only}"
    )
    print(
        f"seam_census_residual_by_bucket={dict(sorted(residual_by_bucket.items()))} "
        f"residual_by_mechanism={dict(sorted(residual_by_mechanism.items()))} "
        f"residual_total={residual_total} residual_by_mechanism_sum="
        f"{sum(residual_by_mechanism.values())} accepted_total={accepted_total} "
        f"accepted_plus_residual={accepted_total + residual_total} report_rows={report_rows} "
        f"identity_set={identity_set}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
