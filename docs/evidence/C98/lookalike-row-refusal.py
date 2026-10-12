#!/usr/bin/env python3
"""C98 refusal probe: does the query service accept the contract row and refuse the look-alike?

``build_row_model``'s docstring carries the claim this round's registration is built on: a domain
with declared semantics publishes the reviewed contract because ``provider_model_query`` accepts a
row only when ``type(row)`` is exactly that class, and a synthesized look-alike "produces rows those
services refuse ... at query time". Until C98 that sentence was only argued, never shown, and three
engine rows sat on the look-alike side of it.

Both sides run here on the same record text, so the only variable is the class the row was built
from:

* positive control -- a row of the shipped contract class serializes, and the payload carries every
  declared field;
* refusal arm -- a row of ``build_row_model``'s synthesized ``<Model>Row`` for the same declaration
  is refused by the same call.

The synthesized class is produced by pointing a copy of the declaration at a domain this repo does
not register, which is exactly the pre-C98 shape, and no shipped spec or model is mutated.

Run: PYTHONPATH=. python docs/evidence/C98/lookalike-row-refusal.py
"""

from __future__ import annotations

import sys
from dataclasses import replace

from opendata.data.domains import contract_model
from opendata.data.providers import register_providers
from opendata.data.providers._engine.http_json import build_row_model
from opendata.data.providers.federal_reserve.specs import FEDERAL_FUNDS_RATE, SOFR
from opendata.data.providers.intrinio.specs import INSIDER_TRADING
from opendata.data.registry import get_registry
from opendata.services.provider_model_query import (
    ProviderModelQueryOutputError,
    _serialize_contract_row,
)

#: A domain with no DomainSpec behind it: the branch that produced these rows before C98.
UNREGISTERED_DOMAIN = "overnight_bank_funding_rate"

RECORDS = (
    (
        "federal_reserve::FederalFundsRate",
        "federal_reserve_federal_funds_rate",
        FEDERAL_FUNDS_RATE,
        {"date": "2026-10-06", "rate": 4.083},
    ),
    (
        "federal_reserve::SOFR",
        "federal_reserve_sofr",
        SOFR,
        {"date": "2026-10-06", "rate": "4.05"},
    ),
    (
        "intrinio::InsiderTrading",
        "intrinio_insider_trading",
        INSIDER_TRADING,
        {
            "symbol": "AAPL",
            "company_cik": "0000320193",
            "company_name": "Apple Inc.",
            "filing_date": "2026-10-02",
            "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/x.xml",
            "owner_cik": "0001214983",
            "owner_name": "COOK TIMOTHY D",
            "securities_transacted": 0,
            "derivative_transaction": False,
        },
    ),
)

failures: list[str] = []


def report(arm: str, expected: str, actual: str, ok: bool) -> None:
    """Print one arm's expectation beside its reading, and record a miss."""
    verdict = "PASS" if ok else "FAIL"
    print(f"[{verdict}] {arm}\n        expected: {expected}\n        actual:   {actual}")
    if not ok:
        failures.append(arm)


def serialize(row: object, contract_type: type) -> str:
    """Return the serializer's face: the payload keys, or the refusal class name."""
    try:
        payload = _serialize_contract_row(row, contract_type)
    except ProviderModelQueryOutputError:
        return "ProviderModelQueryOutputError"
    return f"accepted, {len(payload)} keys"


def main() -> int:
    """Run both sides of the serializer's identity check for each registered row."""
    register_providers(get_registry())

    for label, domain, spec, record in RECORDS:
        contract_type = contract_model(domain)
        lookalike_type = build_row_model(replace(spec, domain=UNREGISTERED_DOMAIN))

        contract_row = contract_type.model_validate(record)
        contract_face = serialize(contract_row, contract_type)
        report(
            f"accept/{label}",
            f"a row of the reviewed contract {contract_type.__name__} serializes with all "
            f"{len(contract_type.model_fields)} declared fields",
            contract_face,
            contract_face == f"accepted, {len(contract_type.model_fields)} keys",
        )

        lookalike_row = lookalike_type.model_validate(record)
        lookalike_face = serialize(lookalike_row, contract_type)
        report(
            f"refuse/{label}",
            f"the same text built as {lookalike_type.__name__} is refused by the serializer",
            lookalike_face,
            lookalike_face == "ProviderModelQueryOutputError",
        )

        report(
            f"identity/{label}",
            "the look-alike and the contract are different classes over the same field names",
            f"lookalike={lookalike_type.__name__} fields_equal="
            f"{set(lookalike_type.model_fields) == set(contract_type.model_fields)}",
            lookalike_type is not contract_type
            and set(lookalike_type.model_fields) == set(contract_type.model_fields),
        )

    if failures:
        print(f"\nVERDICT: FAIL arms={len(failures)} {failures}")
        return 1
    print("\nVERDICT: PASS -- the contract branch is what makes these rows readable at query time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
