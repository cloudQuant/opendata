"""Project the next wave off the judged residue instead of off a provider count.

Plane 1 reports 94 rows that are not ``OFFLINE_ACCEPTED``. Each of those needs two independent
things before the judge can accept it: the bound fetcher must publish a typed ``row_model`` (the
gate at ``iter2_claims_probe_runner.py:400-402`` refuses the ``implementation`` phase without one),
and a witness shard must exist so the five phases have a fixture to replay. This probe re-derives
both facts from the repo's own registration path -- ``register_provider`` + ``ProviderRegistry
.resolve_model``, the same calls the runner makes -- and cross-tabs them against the judge report,
so the next wave is chosen from four measured buckets rather than from a guess about "the 16
providers that are left".

Each residue row is graded on three independent booleans, because each False is exactly one edit
of a different kind:

``bound``
    ``ProviderRegistry.resolve_model(provider, model)`` returns a fetcher. False means the
    inventory names a model this repository does not bind at all -- real implementation work, and
    it must not be counted into a "just declare ``row_model``" pool.
``row_model``
    the bound fetcher publishes a typed ``ContractModel`` subclass. False means the runner gate at
    ``iter2_claims_probe_runner.py:400-402`` refuses the row before any fixture is consulted.
``witness``
    ``iter2_claims_fixtures.provider_config(provider)["models"]`` carries an entry for the model,
    which is what a producer run needs. False means a shard must be authored.

The ``SHARD_ONLY_ROWS`` line is the shortlist where all three booleans except ``witness`` already
hold: those rows cost one shard each and nothing else.

A text scan of the shard files is NOT a substitute for the witness face. The first version of this
probe scanned ``_parts`` sources for quoted model keys and printed ``sec::InsiderTrading`` and
``fred::SeniorLoanOfficerSurvey`` as shard-resident while their ``claims.json`` recorded ``no
offline witness configured for this model row`` -- two false positives in a nine-name pool. The
bucket vocabulary above is read from the loader instead.

Run it from the repository root::

    /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
        docs/evidence/C124/famafrench-witness-20261017/probes/next_wave_predicate.py

It always exits 0 unless its own positive control fails: it is a census, not a gate. The control is
one row the judge already accepts, and both of its faces must read as present -- if the instrument
cannot see a known-good row, every bucket it prints is worthless.
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

import iter2_claims_fixtures as witnesses  # noqa: E402  # the repo's own shard loader, not a re-implementation

from opendata.data.models.base import ContractModel  # noqa: E402
from opendata.data.providers.catalog import register_provider  # noqa: E402
from opendata.data.registry import ProviderRegistry  # noqa: E402

DEFAULT_REPORT: Final = "docs/evidence/C101/judge/claims.merged.report.json"
POSITIVE_CONTROL_IDENTITY: Final = "famafrench::FamaFrenchBreakpoints"


def _witness_models(provider: str) -> set[str] | str:
    """Ask the fixtures loader itself which rows one provider has a validated witness for.

    ``provider_config`` imports the provider module, merges its ``_parts`` shards and runs
    ``_validate_entry`` on every row, so membership here means "a producer run would find a
    configured witness", not "this model name appears somewhere in a shard's text". The earlier
    text scan of this very probe printed ``sec::InsiderTrading`` as shard-resident while
    ``claims.json`` records ``no offline witness configured for this model row`` -- a false
    ``READY``, which is exactly the reading this function must not be able to produce.
    """
    try:
        config = witnesses.provider_config(provider)
    except KeyError:
        return "NO_WITNESS_MODULE"
    models = config.get("models") or {}
    return set(models)


def _row_model_for(provider: str, model: str) -> str:
    registry = ProviderRegistry()
    register_provider(provider, registry)
    try:
        fetcher: Any = registry.resolve_model(provider, model)
    except Exception as exc:  # a resolution failure is a bucket, not a crash
        return f"UNRESOLVED:{type(exc).__name__}"
    row_model = getattr(fetcher, "row_model", None)
    if not (isinstance(row_model, type) and issubclass(row_model, ContractModel)):
        return "ABSENT"
    return str(row_model.__name__)


def main(argv: list[str] | None = None) -> int:
    """Cross-tab the judged residue against row_model presence and shard residency."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    document: dict[str, Any] = json.loads((REPO_ROOT / args.report).read_text(encoding="utf-8"))
    models: list[dict[str, Any]] = document["models"]
    accepted = [r for r in models if r["status"] == "OFFLINE_ACCEPTED"]
    residue = [r for r in models if r["status"] != "OFFLINE_ACCEPTED"]
    per_provider_witness: dict[str, set[str] | str] = {}

    control = next((r for r in accepted if r["model_identity"] == POSITIVE_CONTROL_IDENTITY), None)
    if control is None:
        print(f"positive_control_absent={POSITIVE_CONTROL_IDENTITY}")
        return 1
    control_row = _row_model_for(control["provider"], control["upstream_model"])
    control_witness = _witness_models(control["provider"])
    control_model = str(control["upstream_model"])
    control_shard = isinstance(control_witness, set) and control_model in control_witness
    print(
        f"POSITIVE_CONTROL {POSITIVE_CONTROL_IDENTITY} row_model={control_row} "
        f"witness_configured={control_shard} "
        "-> both faces must be True for the instrument to be live"
    )
    if control_row == "ABSENT" or not control_shard:
        print("INSTRUMENT_DEAD: the accepted control row does not read as both-declared")
        return 1

    buckets: Counter[str] = Counter()
    per_provider: dict[str, Counter[str]] = {}
    shard_only: list[str] = []
    for row in residue:
        provider = str(row["provider"])
        model = str(row["upstream_model"])
        if provider not in per_provider_witness:
            per_provider_witness[provider] = _witness_models(provider)
        witness = per_provider_witness[provider]
        has_witness = isinstance(witness, set) and model in witness
        row_model = _row_model_for(provider, model)
        bound = not row_model.startswith("UNRESOLVED")
        # An unbound row has no fetcher to read a declaration off, so row_model is False-by-
        # unmeasurability. Printing True here would claim a declaration the probe never read.
        has_row = bound and row_model != "ABSENT"
        bucket = f"bound={bound} row_model={has_row} witness={has_witness}"
        buckets[bucket] += 1
        per_provider.setdefault(provider, Counter())[bucket] += 1
        if bound and has_row and not has_witness:
            shard_only.append(f"{provider}::{model}")

    print(f"identity_count={len(models)} accepted={len(accepted)} residue={len(residue)}")
    print("truth_table bound/row_model/witness -> n (each False is exactly one edit):")
    for bucket, n in sorted(buckets.items(), key=lambda kv: kv[1] * -1):
        print(f"  {bucket:52s} {n}")
    print(f"SHARD_ONLY_ROWS={len(shard_only)} {shard_only}")
    print("per_provider_residue:")
    for provider in sorted(per_provider, key=lambda p: -sum(per_provider[p].values())):
        total = sum(per_provider[provider].values())
        detail = " ".join(f"[{k}]={v}" for k, v in sorted(per_provider[provider].items()))
        witness = per_provider_witness[provider]
        face = f"witness_rows={len(witness)}" if isinstance(witness, set) else str(witness)
        print(f"  {provider:22s} residue={total:2d} {face:16s} {detail}")
    print(f"NEXT_WAVE_BUCKETS_OK={sum(buckets.values()) == len(residue)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
