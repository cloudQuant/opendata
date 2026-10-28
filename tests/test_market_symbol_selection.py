"""Behavior tests for the reserved exact market/symbol selection profile."""

from __future__ import annotations

import warnings
from typing import Literal

import pytest
from pydantic import ValidationError

from opendata.data.market_symbol_selection import (
    MARKET_SYMBOL_FILTER_FIELDS,
    MAX_MARKET_LENGTH,
    MAX_SYMBOL_LENGTH,
    MarketSymbolSelection,
    MarketSymbolSelectionError,
    parse_market_symbol_selection,
    selection_profile,
    validate_selector_pair,
)
from opendata.data.platform_contracts import (
    DatasetContract,
    FieldContract,
    SourceIdentity,
    canonical_sha256,
)

RecordKind = Literal["time_series", "snapshot", "event", "table"]
TemporalSemantics = Literal["business_date", "event_time", "observation_time", "none"]
PitPolicy = Literal["supported", "required", "unsupported"]
SelectorFieldType = Literal["string", "integer"]
Frequency = Literal["1d", "1w"]
MARKET = "CN-SH"
SYMBOL = "SSE:600519"


def _contract(
    *,
    filter_fields: tuple[str, ...] = MARKET_SYMBOL_FILTER_FIELDS,
    record_kind: RecordKind = "time_series",
    temporal_semantics: TemporalSemantics = "business_date",
    time_field: str | None = "trade_date",
    pit_policy: PitPolicy = "unsupported",
    market_nullable: bool = False,
    symbol_nullable: bool = False,
    market_type: SelectorFieldType = "string",
    symbol_type: SelectorFieldType = "string",
    business_key: tuple[str, ...] = ("market", "symbol", "trade_date"),
    frequency: Frequency = "1d",
) -> DatasetContract:
    """Build a valid daily-series contract for profile-boundary tests."""
    fields = [
        FieldContract(name="market", data_type=market_type, nullable=market_nullable),
        FieldContract(name="symbol", data_type=symbol_type, nullable=symbol_nullable),
        FieldContract(name="trade_date", data_type="date"),
        FieldContract(name="close", data_type="number"),
    ]
    if filter_fields == ("instrument_id", "capture_id"):
        fields.extend(
            (
                FieldContract(name="instrument_id", data_type="string"),
                FieldContract(name="capture_id", data_type="string"),
            )
        )
    ordering = tuple(dict.fromkeys(("trade_date", *business_key)))
    return DatasetContract(
        dataset_id="cn-equity-stock-daily",
        schema_version="1.0.0",
        semantic_version="stock-daily-raw-v1",
        asset_class="equity",
        frequency=frequency,
        record_kind=record_kind,
        fields=tuple(fields),
        business_key=business_key,
        ordering=ordering,
        filter_fields=filter_fields,
        time_field=time_field,
        temporal_semantics=temporal_semantics,
        source=SourceIdentity(source_id="akshare.daily", source="akshare"),
        adjustment_basis="raw",
        timezone="Asia/Shanghai",
        pit_policy=pit_policy,
    )


def test_exact_profile_preserves_contract_and_accepts_non_daily_frequency() -> None:
    contract = _contract(frequency="1w")
    before_document = contract.model_dump(mode="json")
    before_hash = canonical_sha256(contract)

    assert selection_profile(contract) == "market_symbol_v1"

    assert contract.model_dump(mode="json") == before_document
    assert canonical_sha256(contract) == before_hash


@pytest.mark.parametrize(
    "filter_fields",
    [(), ("market",), ("symbol",), ("symbol", "market"), ("instrument_id", "capture_id")],
)
def test_nonreserved_filter_tuples_keep_the_dataset_profile(
    filter_fields: tuple[str, ...],
) -> None:
    contract = _contract(filter_fields=filter_fields)

    assert selection_profile(contract) == "dataset"
    assert (
        parse_market_symbol_selection(
            contract,
            market=None,
            symbol=None,
            required=True,
        )
        is None
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"record_kind": "table", "temporal_semantics": "none", "time_field": None},
        {"record_kind": "event"},
        {"temporal_semantics": "event_time"},
        {"time_field": None, "temporal_semantics": "none"},
        {"business_key": ("market", "symbol")},
        {
            "fields": (
                FieldContract(name="market", data_type="string"),
                FieldContract(name="symbol", data_type="string"),
                FieldContract(name="trade_date", data_type="datetime"),
                FieldContract(name="close", data_type="number"),
            )
        },
        {
            "fields": (
                FieldContract(name="market", data_type="integer"),
                *_contract().fields[1:],
            )
        },
        {
            "fields": (
                _contract().fields[0],
                FieldContract(name="symbol", data_type="integer"),
                *_contract().fields[2:],
            )
        },
        {
            "fields": (
                FieldContract(name="market", data_type="string", nullable=True),
                *_contract().fields[1:],
            )
        },
        {
            "fields": (
                _contract().fields[0],
                FieldContract(name="symbol", data_type="string", nullable=True),
                *_contract().fields[2:],
            )
        },
    ],
)
def test_model_copy_cannot_bypass_reserved_contract_shape(updates: dict[str, object]) -> None:
    malformed = _contract().model_copy(update=updates)

    with pytest.raises(MarketSymbolSelectionError) as raised:
        selection_profile(malformed)

    assert str(raised.value) in {
        "dataset contract is invalid",
        "reserved selection contract is invalid",
    }


def test_non_contract_and_contract_subclass_are_rejected() -> None:
    class DerivedContract(DatasetContract):
        pass

    contract = _contract()
    derived = DerivedContract.model_validate(contract.model_dump(mode="python"))

    for candidate in (object(), derived):
        with pytest.raises(MarketSymbolSelectionError) as raised:
            selection_profile(candidate)  # type: ignore[arg-type]  # Runtime guard is under test.
        assert str(raised.value) == "dataset contract is invalid"


def test_nested_mutation_that_invalidates_contract_fails_closed() -> None:
    contract = _contract()
    contract.field_units["undeclared-field"] = "share"

    with pytest.raises(MarketSymbolSelectionError) as raised:
        selection_profile(contract)

    assert str(raised.value) == "dataset contract is invalid"


@pytest.mark.parametrize("field_name", ["filter_fields", "fields", "business_key", "ordering"])
def test_model_copy_list_bypasses_are_rejected_before_profile_classification(
    field_name: str,
) -> None:
    contract = _contract()
    malformed = contract.model_copy(update={field_name: list(getattr(contract, field_name))})

    with pytest.raises(MarketSymbolSelectionError) as raised:
        selection_profile(malformed)

    assert str(raised.value) == "dataset contract is invalid"


def test_nested_invalid_field_fails_without_warning_or_value_echo() -> None:
    contract = _contract()
    sentinel = "untrusted-nullable-sentinel"
    invalid_field = contract.fields[1].model_copy(update={"nullable": sentinel})
    malformed = contract.model_copy(
        update={"fields": (contract.fields[0], invalid_field, *contract.fields[2:])}
    )

    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        with pytest.raises(MarketSymbolSelectionError) as raised:
            selection_profile(malformed)

    assert str(raised.value) == "dataset contract is invalid"
    assert sentinel not in str(raised.value)
    assert emitted == []


def test_selector_pair_accepts_exact_values_without_case_or_market_inference() -> None:
    selection = validate_selector_pair(market="market-x", symbol="任意:代码")

    assert selection == MarketSymbolSelection("market-x", "任意:代码")
    assert selection is not None
    assert selection.market == "market-x"
    assert selection.symbol == "任意:代码"
    assert validate_selector_pair(market=None, symbol=None) is None


def test_selector_pair_accepts_the_storage_field_length_limits() -> None:
    market = "M" * MAX_MARKET_LENGTH
    symbol = "S" * MAX_SYMBOL_LENGTH

    assert validate_selector_pair(market=market, symbol=symbol) == MarketSymbolSelection(
        market,
        symbol,
    )


@pytest.mark.parametrize(
    ("market", "symbol", "message"),
    [
        (None, SYMBOL, "market and symbol are required together"),
        (MARKET, None, "market and symbol are required together"),
        ("", SYMBOL, "market is invalid"),
        ("   ", SYMBOL, "market is invalid"),
        (" CN-SH", SYMBOL, "market is invalid"),
        ("CN-SH ", SYMBOL, "market is invalid"),
        ("CN-SH*", SYMBOL, "market is invalid"),
        ("CN-SH?", SYMBOL, "market is invalid"),
        ("CN-SH%", SYMBOL, "market is invalid"),
        ("CN-SH_", SYMBOL, "market is invalid"),
        ("CN-SH[0]", SYMBOL, "market is invalid"),
        ("CN-SH\x1f", SYMBOL, "market is invalid"),
        ("M" * (MAX_MARKET_LENGTH + 1), SYMBOL, "market is invalid"),
        (MARKET, "", "symbol is invalid"),
        (MARKET, "\u00a0", "symbol is invalid"),
        (MARKET, " SSE:600519", "symbol is invalid"),
        (MARKET, "SSE:600519 ", "symbol is invalid"),
        (MARKET, "SSE:600519*", "symbol is invalid"),
        (MARKET, "SSE:600519\u200b", "symbol is invalid"),
        (MARKET, "S" * (MAX_SYMBOL_LENGTH + 1), "symbol is invalid"),
    ],
)
def test_selector_pair_rejects_partial_padded_wildcard_or_control_values(
    market: object | None,
    symbol: object | None,
    message: str,
) -> None:
    with pytest.raises(MarketSymbolSelectionError) as raised:
        validate_selector_pair(market=market, symbol=symbol)

    assert str(raised.value) == message
    assert "SSE:600519" not in str(raised.value)


def test_selector_pair_rejects_non_strings_subclasses_and_boolean_values() -> None:
    class StringSubclass(str):
        pass

    for market, symbol in (
        (StringSubclass(MARKET), SYMBOL),
        (MARKET, StringSubclass(SYMBOL)),
        (True, SYMBOL),
        (MARKET, False),
        (7, SYMBOL),
        (MARKET, 600519),
    ):
        with pytest.raises(MarketSymbolSelectionError):
            validate_selector_pair(market=market, symbol=symbol)


def test_parse_requires_both_values_for_reserved_profile_and_rejects_sina_mix() -> None:
    reserved = _contract()
    sina_profile = _contract(filter_fields=("instrument_id", "capture_id"))

    assert (
        parse_market_symbol_selection(
            reserved,
            market=None,
            symbol=None,
            required=False,
        )
        is None
    )
    with pytest.raises(MarketSymbolSelectionError):
        parse_market_symbol_selection(
            reserved,
            market=None,
            symbol=None,
            required=True,
        )
    with pytest.raises(MarketSymbolSelectionError):
        parse_market_symbol_selection(
            reserved,
            market=MARKET,
            symbol=None,
            required=False,
        )
    with pytest.raises(MarketSymbolSelectionError):
        parse_market_symbol_selection(
            sina_profile,
            market=MARKET,
            symbol=SYMBOL,
            required=False,
        )
    assert (
        parse_market_symbol_selection(
            sina_profile,
            market=None,
            symbol=None,
            required=True,
            instrument_id="SSE:600000",
            capture_id="a" * 64,
        )
        is None
    )


@pytest.mark.parametrize(
    ("instrument_id", "capture_id"),
    [("SSE:600000", None), (None, "a" * 64)],
)
def test_parse_rejects_sina_selector_mixed_into_market_symbol_profile(
    instrument_id: str | None,
    capture_id: str | None,
) -> None:
    with pytest.raises(MarketSymbolSelectionError) as raised:
        parse_market_symbol_selection(
            _contract(),
            market=MARKET,
            symbol=SYMBOL,
            required=True,
            instrument_id=instrument_id,
            capture_id=capture_id,
        )

    assert str(raised.value) == "selection profiles are mutually exclusive"


def test_parse_requires_a_boolean_requirement_flag() -> None:
    with pytest.raises(MarketSymbolSelectionError) as raised:
        parse_market_symbol_selection(
            _contract(),
            market=MARKET,
            symbol=SYMBOL,
            required=1,  # type: ignore[arg-type]  # Exact bool check is under test.
        )

    assert str(raised.value) == "selection requirement is invalid"


def test_reserved_contract_constructor_rejects_nullable_business_key_selectors() -> None:
    with pytest.raises(ValidationError):
        _contract(market_nullable=True)
    with pytest.raises(ValidationError):
        _contract(symbol_nullable=True)
