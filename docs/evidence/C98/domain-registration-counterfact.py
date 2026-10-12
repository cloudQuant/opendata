#!/usr/bin/env python3
"""C98 counterfact probe: is the domain registration load-bearing, or does the plane pass anyway?

The round registered three engine domains in ``domains.yaml`` and bound a reviewed contract to each
(``federal_reserve_federal_funds_rate``, ``federal_reserve_sofr``, ``intrinio_insider_trading``).
Two claims need falsifiers, not assertions:

1. ``build_row_model``'s contract branch is a real judge. If a declaration could add or drop a
   column against its domain's contract without a refusal, the shipped contract classes would be
   decoration and the green suite would prove nothing.
2. The map plane's registered-domain guard is live on exactly the axis this round filled. If an
   unregistered domain were admitted silently, the 76-red cascade this round started from would
   have been an artifact of something else.

Every arm prints its own expectation and whether the reading matched, so a false arm is visible in
the transcript rather than hidden behind an empty stdout. Nothing here writes to the repo: the
tampers are ``dataclasses.replace`` copies of the shipped specs, never the specs themselves.

Run: PYTHONPATH=. python docs/evidence/C98/domain-registration-counterfact.py
"""

from __future__ import annotations

import sys
from dataclasses import replace

from opendata.data import openbb_map
from opendata.data.domains import contract_model, load_domains
from opendata.data.providers import register_providers
from opendata.data.providers._engine.http_json import build_row_model
from opendata.data.providers.federal_reserve.specs import FEDERAL_FUNDS_RATE, SOFR
from opendata.data.providers.intrinio.specs import INSIDER_TRADING
from opendata.data.registry import get_registry

ROWS = (
    ("federal_reserve::FederalFundsRate", FEDERAL_FUNDS_RATE, "federal_reserve_federal_funds_rate"),
    ("federal_reserve::SOFR", SOFR, "federal_reserve_sofr"),
    ("intrinio::InsiderTrading", INSIDER_TRADING, "intrinio_insider_trading"),
)
#: A domain this repo genuinely does not register (``federal_reserve/specs.py::DECLARED_GAPS``
#: prices it), used so the synthesized-branch arm is a real absence rather than a typo.
UNREGISTERED_DOMAIN = "overnight_bank_funding_rate"

failures: list[str] = []


def report(arm: str, expected: str, actual: str, ok: bool) -> None:
    """Print one arm's expectation beside its reading, and record a miss."""
    verdict = "PASS" if ok else "FAIL"
    print(f"[{verdict}] {arm}\n        expected: {expected}\n        actual:   {actual}")
    if not ok:
        failures.append(arm)


def map_guard_face(domain: str, model: str) -> str:
    """Return the map plane's reading for one domain: an accepted spec or the refusal text."""
    try:
        domain_spec = openbb_map._require_domain_spec(domain, model, openbb_map.OPENBB_MAP_PATH)
    except openbb_map.OpenBBMapError as exc:
        return f"OpenBBMapError: {exc}"
    return (
        f"accepted; contract={domain_spec.contract} semantics_declared="
        f"{domain_spec.semantics_declared} storage_mode={domain_spec.storage_mode}"
    )


def main() -> int:
    """Run every arm and print the verdict the exit code repeats."""
    register_providers(get_registry())
    specs = load_domains()

    # Arm 0 (ship): a declared domain publishes its own contract class, field-for-field.
    for label, spec, domain in ROWS:
        row_model = build_row_model(spec)
        contract = contract_model(domain)
        report(
            f"ship/{label}",
            f"build_row_model returns the reviewed contract {contract.__name__}",
            f"{row_model.__name__}, is_contract={row_model is contract}, "
            f"fields={len(row_model.model_fields)}, columns={len(spec.columns)}",
            row_model is contract and domain in specs,
        )

    # Arm 1 (DROP): a declaration may not shed a column its domain's contract publishes.
    for label, spec, _domain in ROWS:
        dropped = spec.columns[-1].name
        tampered = replace(spec, columns=spec.columns[:-1])
        try:
            published = build_row_model(tampered)
        except ValueError as exc:
            report(
                f"drop/{label} -> {dropped}",
                "ValueError naming the dropped column",
                f"{type(exc).__name__}: {exc}",
                dropped in str(exc),
            )
        else:
            report(
                f"drop/{label} -> {dropped}",
                "ValueError naming the dropped column",
                f"no refusal; published {published.__name__}",
                False,
            )

    # Arm 2 (ADD): FEDERAL_FUNDS_RATE's 13 columns bound to federal_reserve_sofr, whose reviewed
    # contract carries 7. The difference is pure add -- six names the contract does not have.
    sofr_contract = contract_model("federal_reserve_sofr")
    added = {c.name for c in FEDERAL_FUNDS_RATE.columns} - set(sofr_contract.model_fields)
    try:
        published = build_row_model(replace(FEDERAL_FUNDS_RATE, domain="federal_reserve_sofr"))
    except ValueError as exc:
        named = {name for name in added if name in str(exc)}
        report(
            "add/FederalFundsRate->federal_reserve_sofr",
            f"ValueError naming all {len(added)} columns absent from {sofr_contract.__name__}",
            f"{type(exc).__name__}: {exc} | named {len(named)}/{len(added)}",
            named == added,
        )
    else:
        report(
            "add/FederalFundsRate->federal_reserve_sofr",
            f"ValueError naming all {len(added)} columns absent from {sofr_contract.__name__}",
            f"no refusal; published {published.__name__}",
            False,
        )

    # Arm 3 (ABSENCE, the pre-C98 shape): with no declared domain behind it the same declaration
    # falls to the synthesized look-alike branch, which is what the registration replaced.
    shipped_contract = contract_model("intrinio_insider_trading")
    lookalike = build_row_model(replace(INSIDER_TRADING, domain=UNREGISTERED_DOMAIN))
    contract_absent = False
    contract_face = ""
    try:
        contract_model(UNREGISTERED_DOMAIN)
    except LookupError as exc:
        contract_absent = True
        contract_face = f"{type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
    report(
        f"absence/{UNREGISTERED_DOMAIN}",
        "synthesized InsiderTradingRow, not the shipped contract, and no reviewed contract class "
        "behind the domain",
        f"row_model={lookalike.__name__} is_contract={lookalike is shipped_contract} "
        f"({shipped_contract.__name__}); contract_model(domain) refused={contract_absent} "
        f"({contract_face})",
        lookalike.__name__ == "InsiderTradingRow"
        and lookalike is not shipped_contract
        and contract_absent,
    )

    # Arm 4 (MAP GUARD, both sides): the plane that turned 76 tests red admits a registered domain
    # and refuses an unregistered one, on the same call.
    unregistered_face = map_guard_face(UNREGISTERED_DOMAIN, "OvernightBankFundingRate")
    refused = unregistered_face.startswith("OpenBBMapError:")
    report(
        "map-guard/unregistered",
        "OpenBBMapError naming the unknown domain",
        unregistered_face
        if refused
        else f"NO REFUSAL -- the guard is not live: {unregistered_face}",
        refused and UNREGISTERED_DOMAIN in unregistered_face,
    )
    for _label, _spec, domain in ROWS:
        accepted = map_guard_face(domain, _spec.model)
        report(
            f"map-guard/registered/{domain}",
            f"no error, and the spec's contract is {contract_model(domain).__name__}",
            accepted,
            accepted.startswith("accepted; ")
            and f"contract={contract_model(domain).__name__} " in accepted,
        )

    # Arm 5 (DENOMINATOR): the whole map plane recomputes against the 58 registered capabilities.
    entries = openbb_map.load_openbb_map()
    ours = [o for entry in entries for o in entry.ours]
    print(
        f"[INFO] map entries={len(entries)} ours_rows={len(ours)} "
        f"covered_pairs={len(openbb_map.covered_capabilities())} "
        f"capabilities={len(get_registry().capabilities())} "
        f"descriptors={len(get_registry().list_model_descriptors())} "
        f"domains={len(specs)}"
    )

    if failures:
        print(f"\nVERDICT: FAIL arms={len(failures)} {failures}")
        return 1
    print("\nVERDICT: PASS -- every falsifier falsified; the registration is load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
