"""Fail-closed DDL typing for strict and structured contract annotations."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

import pytest
from pydantic import StrictInt, StrictStr

import opendata.pipeline.ddl as ddl
from opendata.data.models.economic_series import SeriesCatalogItem, SeriesObservation
from opendata.data.models.equity_price import EquityQuote
from opendata.data.models.period_series import BlsCatalogItem, BlsObservation
from opendata.pipeline.ddl import Column, contract_columns, dwd_table_ddl


def test_sql_type_unwraps_annotated_strict_and_homogeneous_literals() -> None:
    assert ddl._sql_type(Annotated[StrictInt, "warehouse"], "count") == "bigint"
    assert ddl._sql_type(Annotated[StrictStr | None, "warehouse"], "symbol") == "varchar(64)"
    assert ddl._is_optional(Annotated[StrictInt | None, "warehouse"]) is True
    assert ddl._sql_type(Literal["daily", "monthly"], "period") == "varchar(255)"
    assert ddl._sql_type(Literal[1, 2], "output_type") == "bigint"
    assert ddl._sql_type(Literal[True, False], "enabled") == "tinyint(1)"


def test_contract_columns_map_real_bls_and_fred_structures(monkeypatch: pytest.MonkeyPatch) -> None:
    contracts = {
        "bls_catalog": BlsCatalogItem,
        "bls_observation": BlsObservation,
        "fred_catalog": SeriesCatalogItem,
        "fred_observation": SeriesObservation,
    }
    monkeypatch.setattr(ddl, "contract_model", lambda domain: contracts[domain])

    bls_catalog = {column.name: column for column in contract_columns("bls_catalog")}
    bls_observation = {column.name: column for column in contract_columns("bls_observation")}
    fred_catalog = {column.name: column for column in contract_columns("fred_catalog")}
    fred_observation = {column.name: column for column in contract_columns("fred_observation")}

    assert bls_catalog["dimensions"] == Column("dimensions", "json", nullable=False)
    assert bls_catalog["source_metadata"] == Column("source_metadata", "json", nullable=False)
    assert bls_observation["footnotes"] == Column("footnotes", "json", nullable=False)
    assert fred_catalog["popularity"] == Column("popularity", "bigint", nullable=True)
    assert fred_observation["output_type"] == Column("output_type", "bigint", nullable=False)
    assert fred_observation["requested_frequency"] == Column(
        "requested_frequency", "varchar(255)", nullable=True
    )


def test_existing_primitive_domain_ddl_is_unchanged() -> None:
    ddl_text = dwd_table_ddl(
        "stock_daily",
        key=("symbol", "trade_date"),
        partition_key="trade_date",
        start_year=2025,
        years=1,
    )

    assert hashlib.sha256(ddl_text.encode()).hexdigest() == (
        "368e2646946b5b1428f4527899f1e96e014fab6d45aac20f1fbd2f99b76a2f9e"
    )


def test_nested_json_types_require_typed_string_keys_and_supported_values() -> None:
    from opendata.data.models.period_series import BlsFootnote

    annotation = dict[StrictStr, tuple[BlsFootnote, ...]]

    assert ddl._sql_type(annotation, "metadata") == "json"


@pytest.mark.parametrize(
    "annotation",
    [
        int | float | None,
        type(None),
        object,
        Any,
        Literal["text", 1],
        Literal[True, 1],
        dict[int, str],
        dict[Any, str],
    ],
)
def test_unsupported_or_ambiguous_types_fail_closed(annotation: object) -> None:
    with pytest.raises(ValueError, match="unsupported contract field type"):
        ddl._sql_type(annotation, "payload")


def test_fmp_quote_contract_remains_unmappable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddl, "contract_model", lambda _domain: EquityQuote)

    with pytest.raises(ValueError, match="unsupported contract field type"):
        contract_columns("fmp_quote")


def test_fmp_mixed_volume_union_remains_unmappable() -> None:
    annotation = EquityQuote.model_fields["volume"].annotation

    with pytest.raises(ValueError, match="unsupported contract field type"):
        ddl._sql_type(annotation, "volume")
