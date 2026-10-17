"""Re-issue the wave-19 shortlist with the column that wave-19's predicate did not have.

``docs/evidence/C124/.../next_wave_predicate.py`` ranked the residue on three booleans --
``bound`` / ``row_model`` / ``witness`` -- and its ``SHARD_ONLY_ROWS`` line was read as "these two
cost one shard each and nothing else". Both rows in that shortlist turned out to need something
else. ``fred::SeniorLoanOfficerSurvey`` refused every phase before reading a fixture because the
runner granted 3 request sends while its fetcher preflights a 6-leg fan-out; ``sec::InsiderTrading``
needs still more (ticker directory + feed pages + an index/XML pair per filing). The predicate was
not wrong about the three booleans it measured -- it simply could not see a fourth gate, so its
"cheapest" reading was unfalsifiable in exactly the dimension that mattered.

This version keeps the three booleans and adds the carrier grant as a fourth, derived from the
runner's own ``_carrier_sends`` rather than re-implemented here:

``grant``
    how many request sends the on-disk fixture can answer, as the runner computes it. ``NO_FIXTURE``
    means the row was never produced, so its grant is not a quantity that exists yet; ``3`` means a
    non-``client_seam`` carrier (the engine transport answers any number of legs from one document).

The row the predicate is calibrated on is the one this wave landed: the positive control must read
``grant=6`` for ``fred::SeniorLoanOfficerSurvey``. If the instrument cannot see the grant it just
used, its buckets are worthless again.

The ``PREFLIGHT`` column names the fetchers that would care: a bound fetcher whose module reads
the request budget (``authorize_request`` /
``_preflight_request_allowance`` / ``ensure_request_capacity``) refuses before transport when the
grant is short, so for those rows the grant is load-bearing. For the others it is a number the
fetcher never consults. The first version of this probe keyed that list on rows that already had a
carrier, which on this residue printed an honest but useless ``PREFLIGHT_RESIDUE=0`` -- every
remaining row is exactly the row with no carrier yet. It is now keyed on the fetcher alone.

Run it from the repository root::

    /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
        docs/evidence/C125/sloos-witness-20261017/probes/next_wave_predicate.py

It exits 0 unless its own positive control fails; it is a census, not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the repo's own shard loader
import iter2_claims_probe_runner as runner  # noqa: E402  # grant + fixture readers, not copies

from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.providers.catalog import register_provider  # noqa: E402
from opendata.data.registry import ProviderRegistry  # noqa: E402

DEFAULT_REPORT: Final = "docs/evidence/C101/judge/claims.merged.report.json"
DEFAULT_EVIDENCE_ROOT: Final = "docs/evidence/C101"
POSITIVE_CONTROL_IDENTITY: Final = "fred::SeniorLoanOfficerSurvey"
CONTROL_GRANT: Final = 6
#: Names a fetcher consults the request budget through; module-level, so an import alias is enough.
PREFLIGHT_MARKERS: Final = (
    "authorize_request",
    "_preflight_request_allowance",
    "ensure_request_capacity",
)


def _witness_models(provider: str) -> set[str] | str:
    """Ask the loader which rows one provider has a *validated* witness for (C124 lesson kept)."""
    try:
        config = witnesses.provider_config(provider)
    except KeyError:
        return "NO_WITNESS_MODULE"
    return set(config.get("models") or {})


def _fetcher_info(provider: str, model: str) -> tuple[str, bool]:
    """Resolve the bound fetcher and report its row_model name plus whether it preflights."""
    registry = ProviderRegistry()
    register_provider(provider, registry)
    try:
        fetcher: Any = registry.resolve_model(provider, model)
    except Exception as exc:
        return f"UNRESOLVED:{type(exc).__name__}", False
    row_model = getattr(fetcher, "row_model", None)
    name = (
        str(row_model.__name__)
        if isinstance(row_model, type) and issubclass(row_model, ContractModel)
        else "ABSENT"
    )
    module = sys.modules.get(type(fetcher).__module__)
    source = getattr(module, "__file__", None)
    preflight = False
    if source and Path(source).exists():
        text = Path(source).read_text(encoding="utf-8")
        preflight = any(marker in text for marker in PREFLIGHT_MARKERS)
    return name, preflight


def _grant_for(provider: str, model: str, evidence_root: Path) -> int | str:
    """Return the runner's own derived grant for this row's on-disk carrier."""
    fixture = evidence_root / "fixtures" / provider / f"{model}.json"
    if not fixture.exists():
        return "NO_FIXTURE"
    try:
        entry = dict(witnesses.model_entry(provider, model))
    except KeyError:
        return "NO_ENTRY"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    return runner._carrier_sends(entry, document)


def main(argv: list[str] | None = None) -> int:
    """Cross-tab the judged residue on bound/row_model/witness plus the carrier grant."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--evidence-root", default=DEFAULT_EVIDENCE_ROOT)
    args = parser.parse_args(argv)
    evidence_root = REPO_ROOT / args.evidence_root

    document: dict[str, Any] = json.loads((REPO_ROOT / args.report).read_text(encoding="utf-8"))
    models: list[dict[str, Any]] = document["models"]
    accepted = [r for r in models if r["status"] == "OFFLINE_ACCEPTED"]
    residue = [r for r in models if r["status"] != "OFFLINE_ACCEPTED"]

    control = next((r for r in models if r["model_identity"] == POSITIVE_CONTROL_IDENTITY), None)
    if control is None:
        print(f"positive_control_absent={POSITIVE_CONTROL_IDENTITY}")
        return 1
    control_grant = _grant_for(control["provider"], str(control["upstream_model"]), evidence_root)
    print(
        f"POSITIVE_CONTROL {POSITIVE_CONTROL_IDENTITY} status={control['status']} "
        f"grant={control_grant} expected_grant={CONTROL_GRANT} "
        "-> the instrument must see the grant this wave ran on"
    )
    if control_grant != CONTROL_GRANT:
        print("INSTRUMENT_DEAD: the control row's grant is not what the carrier answers")
        return 1

    buckets: Counter[str] = Counter()
    grant_hist: Counter[str] = Counter()
    per_provider: dict[str, Counter[str]] = {}
    shard_only: list[str] = []
    preflight_residue: list[str] = []
    witness_by_provider: dict[str, set[str] | str] = {}
    for row in residue:
        provider = str(row["provider"])
        model = str(row["upstream_model"])
        if provider not in witness_by_provider:
            witness_by_provider[provider] = _witness_models(provider)
        known = witness_by_provider[provider]
        has_witness = isinstance(known, set) and model in known
        row_model, preflight = _fetcher_info(provider, model)
        bound = not row_model.startswith("UNRESOLVED")
        has_row = bound and row_model != "ABSENT"
        grant = _grant_for(provider, model, evidence_root) if has_witness else "NO_WITNESS"
        bucket = (
            f"bound={bound} row_model={has_row} witness={has_witness} "
            f"preflight={preflight} grant={grant}"
        )
        buckets[bucket] += 1
        grant_hist[str(grant)] += 1
        per_provider.setdefault(provider, Counter())[bucket] += 1
        if bound and has_row and not has_witness:
            shard_only.append(f"{provider}::{model}")
        if bound and preflight:
            preflight_residue.append(f"{provider}::{model}@grant={grant}@row_model={row_model}")

    print(f"identity_count={len(models)} accepted={len(accepted)} residue={len(residue)}")
    print("truth_table bound/row_model/witness/preflight/grant -> n (each False is one edit):")
    for bucket, n in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {bucket:78s} {n}")
    print(f"grant_histogram={dict(sorted(grant_hist.items()))}")
    print(f"SHARD_ONLY_ROWS={len(shard_only)} {shard_only}")
    print(
        f"PREFLIGHT_RESIDUE={len(preflight_residue)} (these fetchers consult the request budget, "
        "so their carrier must answer every leg or the runner refuses before transport):"
    )
    for item in preflight_residue:
        print(f"  {item}")
    print("per_provider_residue:")
    for provider in sorted(per_provider, key=lambda p: -sum(per_provider[p].values())):
        total = sum(per_provider[provider].values())
        detail = " ".join(f"[{k}]={v}" for k, v in sorted(per_provider[provider].items()))
        print(f"  {provider:22s} residue={total:2d} {detail}")
    print(f"NEXT_WAVE_BUCKETS_OK={sum(buckets.values()) == len(residue)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
