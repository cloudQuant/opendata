"""Offline FMP historical storage codec coverage."""

from __future__ import annotations

import json
import math
from datetime import date, datetime

import pytest
from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions, escape_item

from opendata.data.domains import require_domain_semantics
from opendata.data.models import EquityHistorical
from opendata.pipeline import provider_model_codec as codec


def _row(volume: int | float | None = None) -> EquityHistorical:
    return EquityHistorical.model_validate(
        {
            "symbol": "AAPL",
            "date": date(2026, 1, 2),
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.0,
            "volume": volume,
            "query_window_scope": "provider_default_unknown",
        }
    )


def _encoded(volume: int | float | None = None) -> dict[str, object]:
    return codec.encode_model_storage_row("equity_historical", _row(volume))


def _decoded(record: dict[str, object]) -> EquityHistorical:
    decoded = codec.decode_model_storage_row("equity_historical", record)
    assert type(decoded) is EquityHistorical
    return decoded


@pytest.mark.parametrize(
    "volume",
    [
        None,
        0,
        -1,
        2**53 + 1,
        2**64 + 1,
        10**300,
        0.0,
        -0.0,
        float.fromhex("0x0.0000000000001p-1022"),
        1.0e308,
    ],
    ids=[
        "null",
        "zero-int",
        "negative-int",
        "above-2pow53",
        "above-2pow64",
        "large-int",
        "zero-float",
        "negative-zero-float",
        "subnormal-float",
        "large-finite-float",
    ],
)
def test_volume_roundtrip_preserves_exact_numeric_kind_and_value(
    volume: int | float | None,
) -> None:
    row = _row(volume)
    encoded = codec.encode_model_storage_row("equity_historical", row)

    if volume is None:
        assert encoded["volume"] is None
    else:
        assert type(encoded["volume"]) is str
        payload = json.loads(encoded["volume"])
        assert type(payload["kind"]) is str
        assert type(payload["value"]) is str
        if type(volume) is int:
            assert payload == {"kind": "int", "value": str(volume)}
        else:
            assert type(volume) is float
            assert payload == {"kind": "float", "value": volume.hex()}

    decoded = _decoded(encoded)
    assert decoded.volume == volume
    assert type(decoded.volume) is type(volume)
    if type(volume) is float and volume == 0.0:
        assert type(decoded.volume) is float
        assert math.copysign(1.0, decoded.volume) == math.copysign(1.0, volume)
    assert decoded.query_window_scope == "provider_default_unknown"
    assert decoded.close_adjustment_semantics == "split_adjusted_per_source_faq"
    assert decoded.adj_close_provided is False


@pytest.mark.parametrize(
    "volume",
    [2**64 + 1, 10**300, -0.0, float.fromhex("0x0.0000000000001p-1022")],
    ids=["large-int", "very-large-int", "negative-zero-float", "subnormal-float"],
)
def test_volume_json_text_binds_with_real_pymysql_converter(
    volume: int | float,
) -> None:
    record = _encoded(volume)
    encoded = record["volume"]
    assert type(encoded) is str

    escaped = escape_item(encoded, "utf8mb4", conversions)
    assert escaped.startswith("'") and escaped.endswith("'")
    payload = json.loads(encoded)
    assert type(payload) is dict
    assert set(payload) == {"kind", "value"}
    assert type(payload["kind"]) is str
    assert type(payload["value"]) is str

    decoded = _decoded(record)
    assert type(decoded.volume) is type(volume)
    assert decoded.volume == volume
    if type(volume) is float and volume == 0.0:
        assert type(decoded.volume) is float
        assert math.copysign(1.0, decoded.volume) == -1.0


def test_null_volume_remains_a_sql_null_binding() -> None:
    record = _encoded(None)
    assert record["volume"] is None
    assert escape_item(record["volume"], "utf8mb4", conversions) == "NULL"


def test_json_text_order_whitespace_and_native_dict_shapes_decode() -> None:
    integer_record = _encoded()
    integer_record["volume"] = '{ "value" : "18446744073709551617", "kind" : "int" }'
    decoded_int = _decoded(integer_record)
    assert decoded_int.volume == 2**64 + 1
    assert type(decoded_int.volume) is int

    float_value = float.fromhex("-0x0.0000000000001p-1022")
    float_record = _encoded()
    float_record["volume"] = {"value": float_value.hex(), "kind": "float"}
    decoded_float = _decoded(float_record)
    assert decoded_float.volume == float_value
    assert type(decoded_float.volume) is float


@pytest.mark.parametrize(
    "payload",
    [
        '{"kind":"int","value":"-0"}',
        '{"kind":"int","value":"+1"}',
        '{"kind":"int","value":"01"}',
        '{"kind":"int","value":" 1"}',
        '{"kind":"int","value":""}',
        '{"kind":"decimal","value":"1"}',
        '{"kind":"int","value":1}',
        '{"kind":"int"}',
        '{"kind":"int","value":"1","extra":false}',
        '{"kind":"int","kind":"float","value":"1"}',
        '{"kind":"int","value":"1","extra":NaN}',
        '[{"kind":"int","value":"1"}]',
        "not JSON",
    ],
)
def test_invalid_integer_volume_json_is_rejected(payload: str) -> None:
    record = _encoded()
    record["volume"] = payload
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("equity_historical", record)


@pytest.mark.parametrize(
    "payload",
    [
        '{"kind":"float","value":"0x1p+0"}',
        '{"kind":"float","value":"0X1.0000000000000P+0"}',
        '{"kind":"float","value":"nan"}',
        '{"kind":"float","value":"inf"}',
        '{"kind":"float","value":"-inf"}',
        '{"kind":"float","value":"not-hex"}',
    ],
)
def test_invalid_float_volume_hex_is_rejected(payload: str) -> None:
    record = _encoded()
    record["volume"] = payload
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("equity_historical", record)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("currency", "USD"),
        ("volume_unit", "shares"),
        ("adj_close_provided", True),
        ("adj_close_provided", 1),
        ("adj_close_provided", 0.0),
        ("unreviewed_column", "unexpected"),
    ],
)
def test_unverified_units_adjusted_close_and_unknown_columns_are_rejected(
    field_name: str,
    invalid_value: object,
) -> None:
    record = _encoded()
    record[field_name] = invalid_value
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("equity_historical", record)


def test_pymysql_tiny_zero_is_the_only_integer_adjusted_close_shape() -> None:
    converted_zero = conversions[FIELD_TYPE.TINY](b"0")
    assert type(converted_zero) is int and converted_zero == 0
    record = _encoded()
    record["adj_close_provided"] = converted_zero
    decoded = _decoded(record)
    assert decoded.adj_close_provided is False

    record = _encoded()
    record["adj_close_provided"] = False
    assert _decoded(record).adj_close_provided is False


@pytest.mark.parametrize("volume", [True, float("nan"), float("inf"), float("-inf")])
def test_invalid_model_construct_volume_is_rejected(volume: object) -> None:
    malformed = _row().model_copy(update={"volume": volume})
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("equity_historical", malformed)


def test_datetime_missing_and_unknown_constructed_fields_are_rejected() -> None:
    missing = EquityHistorical.model_construct(
        symbol="AAPL",
        open=100.0,
        high=103.0,
        low=99.5,
        close=102.0,
        query_window_scope="explicit",
    )
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("equity_historical", missing)

    malformed_date = _row().model_copy(update={"date": datetime(2026, 1, 2)})
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("equity_historical", malformed_date)

    extra = EquityHistorical.model_construct(**_row().model_dump(mode="python"))
    extra.__dict__["unreviewed_field"] = "unexpected"
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("equity_historical", extra)


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("contract", "EquityQuote"),
        ("temporal_kind", "snapshot"),
        ("time_field", "provider_default_window_semantics"),
        ("natural_key", ("symbol",)),
        ("filter_dims", ("symbol",)),
        ("storage_mode", "transient"),
        ("permissions", ("query",)),
    ],
)
def test_equity_historical_semantic_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    changed_value: object,
) -> None:
    drifted = require_domain_semantics("equity_historical").model_copy(
        update={field_name: changed_value}
    )
    monkeypatch.setattr(codec, "require_domain_semantics", lambda _domain: drifted)

    with pytest.raises(ValueError):
        codec.physical_model_key("equity_historical")


def test_equity_quote_is_transient_and_not_accepted_by_storage_codec() -> None:
    with pytest.raises(ValueError, match="does not support this domain"):
        codec.physical_model_key("equity_quote")
    with pytest.raises(ValueError, match="does not support this domain"):
        codec.encode_model_storage_row("equity_quote", object())  # type: ignore[arg-type]
