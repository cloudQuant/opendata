"""Branch-level offline tests for the reviewed provider storage codecs (C85).

Positive cases round-trip through ``encode_model_storage_row`` and
``decode_model_storage_row``. Every fail-closed guard is driven with the exact
value shape a storage driver, a strict revalidation, or a drifting contract
would hand back, and is asserted on both the exception type and the message.
No provider, network, or warehouse access.
"""

from __future__ import annotations

import importlib
import json
import re
import typing
from datetime import date, datetime

import pytest

from opendata.data.models import (
    BlsFootnote,
    BlsObservation,
    EquityHistorical,
    SeriesObservation,
)
from opendata.data.models.base import ContractModel
from opendata.pipeline import provider_model_codec as codec

NON_FINITE = (float("nan"), float("inf"), float("-inf"))
_FRED_CONTEXT = '["lin",1,null,"avg"]'
_VOLUME_SHAPE_ERROR = "stored equity volume must be JSON text, an object, or null"


def _fred_row(**changes: object) -> SeriesObservation:
    values: dict[str, object] = {
        "series_id": "CPIAUCSL",
        "date": date(2024, 2, 1),
        "value": 3.1,
        "realtime_start": date(2024, 1, 1),
        "realtime_end": date(2024, 12, 31),
        "transform_units": "lin",
        "output_type": 1,
        "requested_frequency": None,
        "requested_aggregation_method": "avg",
    }
    values.update(changes)
    return SeriesObservation.model_validate(values)


def _bls_row(**changes: object) -> BlsObservation:
    values: dict[str, object] = {
        "series_id": "LNS14000000",
        "year": 2025,
        "period": "M13",
        "period_name": "Annual average",
        "value": 4.1,
        "footnotes": (BlsFootnote(code="P", text="Preliminary"),),
        "latest": None,
        "preliminary": False,
        "api_version": "v2",
    }
    values.update(changes)
    return BlsObservation.model_validate(values)


def _equity_row(**changes: object) -> EquityHistorical:
    values: dict[str, object] = {
        "symbol": "AAPL",
        "date": date(2026, 1, 2),
        "open": 100.0,
        "high": 103.0,
        "low": 99.5,
        "close": 102.0,
        "volume": 1000,
        "change": 1.0,
        "change_percent": 0.01,
        "vwap": 101.5,
        "query_window_scope": "explicit",
    }
    values.update(changes)
    return EquityHistorical.model_validate(values)


def _fred_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "series_id": "CPIAUCSL",
        "date": date(2024, 2, 1),
        "value": None,
        "realtime_start": date(2024, 1, 1),
        "realtime_end": date(2024, 12, 31),
        "transform_units": "lin",
        "output_type": 1,
        "requested_frequency": None,
        "requested_aggregation_method": "avg",
    }
    values.update(changes)
    return values


def _bls_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "series_id": "LNS14000000",
        "year": 2025,
        "period": "M13",
        "period_name": "Annual average",
        "value": 4.1,
        "footnotes": (),
        "latest": None,
        "preliminary": False,
        "api_version": "v2",
    }
    values.update(changes)
    return values


def _equity_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "symbol": "AAPL",
        "date": date(2026, 1, 2),
        "open": 100.0,
        "high": 103.0,
        "low": 99.5,
        "close": 102.0,
        "volume": 1000,
        "change": 1.0,
        "change_percent": 0.01,
        "vwap": 101.5,
        "currency": None,
        "currency_semantics": "source_unverified",
        "volume_unit": None,
        "volume_unit_semantics": "source_unverified",
        "query_window_scope": "explicit",
        "window_boundary_semantics": "source_unverified",
        "provider_default_window_semantics": "source_unverified",
        "close_adjustment_semantics": "split_adjusted_per_source_faq",
        "adj_close_provided": False,
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("domain", [None, 7, 4.5, True, ["fred_series"], b"fred_series"])
def test_storage_domain_must_be_a_string(domain: object) -> None:
    with pytest.raises(ValueError, match="storage domain must be a string"):
        codec.physical_model_key(domain)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="storage domain must be a string"):
        codec.encode_model_storage_row(domain, _fred_row())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="storage domain must be a string"):
        codec.decode_model_storage_row(domain, {})  # type: ignore[arg-type]


@pytest.mark.parametrize("failure", [LookupError, ValueError, TypeError])
@pytest.mark.parametrize("registry_symbol", ["require_domain_semantics", "contract_model"])
def test_incomplete_domain_declaration_fails_closed(
    monkeypatch: pytest.MonkeyPatch, registry_symbol: str, failure: type[Exception]
) -> None:
    assert codec.physical_model_key("fred_series")[-1] == "_request_context"

    def raiser(_domain: str) -> object:
        raise failure("registry entry is gone")

    monkeypatch.setattr(codec, registry_symbol, raiser)
    with pytest.raises(ValueError, match=re.escape("storage domain is not fully declared")):
        codec.physical_model_key("bls_series")
    with pytest.raises(ValueError, match=re.escape("storage domain is not fully declared")):
        codec.encode_model_storage_row("bls_series", _bls_row())


@pytest.mark.parametrize(
    "physical_key",
    ["series_id,date", 7, {"series_id": 1}, ("series_id", None), ("series_id", 3, True)],
)
def test_reviewed_physical_key_configuration_is_validated(
    monkeypatch: pytest.MonkeyPatch, physical_key: object
) -> None:
    assert codec.physical_model_key("fred_series") == (
        "series_id",
        "date",
        "realtime_start",
        "realtime_end",
        "_request_context",
    )
    drifted = dict(codec._REVIEWED_DOMAINS["fred_series"])
    drifted["physical_key"] = physical_key
    monkeypatch.setitem(codec._REVIEWED_DOMAINS, "fred_series", drifted)

    with pytest.raises(
        ValueError, match=re.escape("reviewed physical key configuration is invalid")
    ):
        codec.physical_model_key("fred_series")


@pytest.mark.parametrize("values", [_fred_values(), _fred_values(value=1.25)])
@pytest.mark.parametrize("extra", [{}, {"output_type": 4}, {"requested_frequency": "w"}])
def test_fred_storage_types_accept_reviewed_shapes(values: dict, extra: dict) -> None:
    assert codec._validate_storage_python_types("fred_series", {**values, **extra}) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"output_type": "1"}, "FRED output_type must be an integer literal"),
        ({"output_type": 1.0}, "FRED output_type must be an integer literal"),
        ({"output_type": True}, "FRED output_type must be an integer literal"),
        ({"output_type": None}, "FRED output_type must be an integer literal"),
        ({"value": 1}, "FRED observation value must be a finite float or null"),
        ({"value": "3.1"}, "FRED observation value must be a finite float or null"),
        ({"value": NON_FINITE[0]}, "FRED observation value must be a finite float or null"),
        ({"value": NON_FINITE[1]}, "FRED observation value must be a finite float or null"),
        ({"date": datetime(2024, 2, 1, 9)}, "FRED date fields must be plain dates"),
        ({"date": "2024-02-01"}, "FRED date fields must be plain dates"),
        ({"date": None}, "FRED date fields must be plain dates"),
        ({"realtime_start": "2024-01-01"}, "FRED date fields must be plain dates"),
        ({"realtime_end": datetime(2024, 12, 31)}, "FRED date fields must be plain dates"),
    ],
)
def test_fred_storage_types_reject_ambiguous_values(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        codec._validate_storage_python_types("fred_series", _fred_values(**changes))


@pytest.mark.parametrize("value", [4.1, -0.5, 0.0, 2**53 + 1.0])
def test_bls_storage_types_accept_finite_floats(value: float) -> None:
    assert codec._validate_storage_python_types("bls_series", _bls_values(value=value)) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"year": "2025"}, "BLS year must be an integer"),
        ({"year": 2025.0}, "BLS year must be an integer"),
        ({"year": True}, "BLS year must be an integer"),
        ({"year": None}, "BLS year must be an integer"),
        ({"value": "4.1"}, "BLS value must be a finite float"),
        ({"value": 4}, "BLS value must be a finite float"),
        ({"value": None}, "BLS value must be a finite float"),
        ({"value": NON_FINITE[0]}, "BLS value must be a finite float"),
        ({"value": NON_FINITE[2]}, "BLS value must be a finite float"),
    ],
)
def test_bls_storage_types_reject_ambiguous_values(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        codec._validate_storage_python_types("bls_series", _bls_values(**changes))


@pytest.mark.parametrize("volume", [None, 1000, 2**53 + 1, 1.5])
@pytest.mark.parametrize("optional", [None, 0.25])
def test_equity_storage_types_accept_reviewed_shapes(volume: object, optional: object) -> None:
    values = _equity_values(volume=volume, change=optional, change_percent=optional, vwap=optional)
    assert codec._validate_storage_python_types("equity_historical", values) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"symbol": 123}, "equity symbol must be a string"),
        ({"symbol": None}, "equity symbol must be a string"),
        ({"symbol": ["AAPL"]}, "equity symbol must be a string"),
        ({"date": "2026-01-02"}, "equity historical date must be a plain date"),
        ({"date": datetime(2026, 1, 2)}, "equity historical date must be a plain date"),
        ({"open": "100.0"}, "equity price fields must be finite floats"),
        ({"high": 103}, "equity price fields must be finite floats"),
        ({"low": NON_FINITE[0]}, "equity price fields must be finite floats"),
        ({"close": None}, "equity price fields must be finite floats"),
        ({"change": "1.0"}, "optional equity price fields must be finite floats or null"),
        ({"change": NON_FINITE[1]}, "optional equity price fields must be finite floats or null"),
        ({"change_percent": 1}, "optional equity price fields must be finite floats or null"),
        ({"vwap": NON_FINITE[0]}, "optional equity price fields must be finite floats or null"),
        ({"volume": "1000"}, "equity volume must be an exact integer, float, or null"),
        ({"volume": True}, "equity volume must be an exact integer, float, or null"),
        ({"volume": NON_FINITE[0]}, "equity volume float must be finite"),
        ({"volume": NON_FINITE[1]}, "equity volume float must be finite"),
        ({"currency": "USD"}, "equity currency and volume unit remain unverified"),
        ({"volume_unit": "SHARES"}, "equity currency and volume unit remain unverified"),
        ({"adj_close_provided": True}, "equity adjusted close flag must remain false"),
        ({"adj_close_provided": 0}, "equity adjusted close flag must remain false"),
        ({"adj_close_provided": None}, "equity adjusted close flag must remain false"),
    ],
)
def test_equity_storage_types_reject_ambiguous_values(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        codec._validate_storage_python_types("equity_historical", _equity_values(**changes))


@pytest.mark.parametrize("domain", ["fred_search", "stock_daily", "anything"])
def test_unreviewed_domains_are_left_to_the_domain_guard(domain: str) -> None:
    assert codec._validate_storage_python_types(domain, {"value": object()}) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([1, None, "lin"], '[1,null,"lin"]'),
        ({"code": "P", "text": None}, '{"code":"P","text":null}'),
        ({}, "{}"),
        ([], "[]"),
        (True, "true"),
        ({"code": "\u4e2d"}, '{"code":"\\u4e2d"}'),
    ],
)
def test_canonical_json_is_ascii_separators_and_finite(value: object, expected: str) -> None:
    assert codec._canonical_json(value) == expected


@pytest.mark.parametrize(
    "value", [date(2024, 1, 1), object(), NON_FINITE[0], NON_FINITE[1], NON_FINITE[2]]
)
def test_canonical_json_rejects_values_a_driver_cannot_bind(value: object) -> None:
    with pytest.raises(ValueError, match=re.escape("not serializable finite JSON")):
        codec._canonical_json(value)


def test_canonical_json_rejects_a_self_referencing_value() -> None:
    circular: list[object] = []
    circular.append(circular)
    with pytest.raises(ValueError, match=re.escape("not serializable finite JSON")):
        codec._canonical_json(circular)


def test_canonical_json_requires_a_string_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codec.json, "dumps", lambda *args, **kwargs: 42)
    with pytest.raises(ValueError, match="storage JSON value is not serializable\\Z"):
        codec._canonical_json({"code": "P"})


def test_fred_request_context_covers_only_the_four_context_fields() -> None:
    values = _fred_values()
    assert codec._fred_request_context(values) == _FRED_CONTEXT
    assert codec._fred_request_context({**values, "date": date(2030, 1, 1)}) == _FRED_CONTEXT
    assert codec._fred_request_context({**values, "output_type": 2}) != _FRED_CONTEXT
    assert codec._fred_request_context({**values, "transform_units": "log"}) == (
        '["log",1,null,"avg"]'
    )


@pytest.mark.parametrize("output_type", ["1", 1.0, True, None, [1]])
def test_fred_request_context_requires_an_integer_literal(output_type: object) -> None:
    with pytest.raises(ValueError, match=re.escape("FRED output_type must be an integer literal")):
        codec._fred_request_context(_fred_values(output_type=output_type))


@pytest.mark.parametrize(
    ("volume", "kind", "expected"),
    [
        (None, None, None),
        (0, "int", '{"kind":"int","value":"0"}'),
        (1000, "int", '{"kind":"int","value":"1000"}'),
        (2**53 + 1, "int", '{"kind":"int","value":"9007199254740993"}'),
        (-1000, "int", '{"kind":"int","value":"-1000"}'),
        (1.5, "float", '{"kind":"float","value":"0x1.8000000000000p+0"}'),
        (0.1, "float", '{"kind":"float","value":"0x1.999999999999ap-4"}'),
    ],
)
def test_equity_volume_encoding_is_tagged_and_exact(
    volume: object, kind: str | None, expected: str | None
) -> None:
    encoded = codec._encode_equity_volume(volume)
    assert encoded == expected
    assert codec._decode_equity_volume(encoded) == volume
    if expected is None:
        assert kind is None
        return
    payload = json.loads(expected)
    assert payload["kind"] == kind
    assert payload["value"] == (str(volume) if kind == "int" else float(volume).hex())
    assert json.loads(encoded)["value"] == payload["value"]


@pytest.mark.parametrize(
    "volume",
    [True, False, "1000", [1000], {"value": 1000}, NON_FINITE[0], NON_FINITE[1], NON_FINITE[2]],
)
def test_equity_volume_encoding_rejects_inexact_kinds(volume: object) -> None:
    message = "equity volume must be an exact finite integer, float, or null"
    with pytest.raises(ValueError, match=re.escape(message)):
        codec._encode_equity_volume(volume)


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        (None, None),
        (1000, '{"kind":"int","value":"1000"}'),
        (9007199254740993, '{"kind":"int","value":"9007199254740993"}'),
        (0.5, '{"kind":"float","value":"0x1.0000000000000p-1"}'),
    ],
)
def test_equity_row_roundtrip_preserves_the_exact_volume(volume: object, expected: str) -> None:
    row = _equity_row(volume=volume)
    encoded = codec.encode_model_storage_row("equity_historical", row)

    assert encoded["volume"] == expected
    assert encoded["currency"] is None and encoded["volume_unit"] is None
    assert encoded["adj_close_provided"] is False
    decoded = codec.decode_model_storage_row("equity_historical", encoded)
    assert decoded == row
    assert type(decoded.volume) is type(volume)
    assert decoded.volume == volume


@pytest.mark.parametrize(
    ("volume", "message"),
    [
        (1000, _VOLUME_SHAPE_ERROR),
        (1.5, _VOLUME_SHAPE_ERROR),
        (True, _VOLUME_SHAPE_ERROR),
        (b'{"kind":"int","value":"1"}', _VOLUME_SHAPE_ERROR),
        (["kind", "value"], _VOLUME_SHAPE_ERROR),
        ("not-json", "stored equity volume JSON is invalid"),
        ('[{"kind":"int","value":"1"}]', "stored equity volume must be a JSON object"),
        ('{"kind":"int","value":"1","extra":true}', "unknown or missing fields"),
        ('{"kind":"int"}', "unknown or missing fields"),
        ('{"kind":1,"value":"1"}', "tag and value must be strings"),
        ('{"kind":"int","value":1}', "tag and value must be strings"),
        ('{"kind":"int","kind":"float","value":"1"}', "stored equity volume JSON is invalid"),
        ('{"kind":"int","value":NaN}', "stored equity volume JSON is invalid"),
        ('{"kind":"int","value":"abc"}', "stored equity integer volume is invalid"),
        ('{"kind":"int","value":"01"}', "not canonical decimal text"),
        ('{"kind":"int","value":"1_0"}', "not canonical decimal text"),
        ('{"kind":"float","value":"zz"}', "invalid hexadecimal text"),
        ('{"kind":"float","value":"0x1.8p+0"}', "not canonical finite hexadecimal text"),
        ('{"kind":"float","value":"Infinity"}', "not canonical finite hexadecimal text"),
        ('{"kind":"decimal","value":"1"}', "unknown numeric kind"),
    ],
)
def test_equity_volume_decoding_rejects_noncanonical_storage_shapes(
    volume: object, message: str
) -> None:
    record = codec.encode_model_storage_row("equity_historical", _equity_row())
    record["volume"] = volume
    with pytest.raises(ValueError, match=re.escape(message)):
        codec.decode_model_storage_row("equity_historical", record)


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        ({"kind": "int", "value": "10"}, 10),
        ({"kind": "float", "value": "0x1.8000000000000p+0"}, 1.5),
        ('{"kind":"int","value":"10"}', 10),
    ],
)
def test_equity_volume_decoding_accepts_text_and_native_objects(
    volume: object, expected: object
) -> None:
    assert codec._decode_equity_volume(volume) == expected


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        (1, "adj_close_provided must be exact false or TINY zero"),
        (True, "adj_close_provided must be exact false or TINY zero"),
        (None, "adj_close_provided must be exact false or TINY zero"),
        ("0", "adj_close_provided must be exact false or TINY zero"),
        (0.0, "adj_close_provided must be exact false or TINY zero"),
    ],
)
def test_equity_adjusted_close_flag_accepts_only_false_or_tiny_zero(
    flag: object, message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        codec._decode_equity_adjusted_close_flag(flag)

    record = codec.encode_model_storage_row("equity_historical", _equity_row())
    record["adj_close_provided"] = flag
    with pytest.raises(ValueError, match=re.escape(message)):
        codec.decode_model_storage_row("equity_historical", record)


@pytest.mark.parametrize("flag", [False, 0])
def test_equity_adjusted_close_flag_tiny_zero_decodes_to_false(flag: object) -> None:
    assert codec._decode_equity_adjusted_close_flag(flag) is False
    record = codec.encode_model_storage_row("equity_historical", _equity_row())
    record["adj_close_provided"] = flag
    assert codec.decode_model_storage_row("equity_historical", record) == _equity_row()


@pytest.mark.parametrize("missing", ["symbol", "close", "adj_close_provided", "volume"])
def test_equity_decode_requires_every_contract_field(missing: str) -> None:
    record = codec.encode_model_storage_row("equity_historical", _equity_row())
    del record[missing]
    with pytest.raises(
        ValueError, match=re.escape("equity historical storage row requires every contract field")
    ):
        codec.decode_model_storage_row("equity_historical", record)


@pytest.mark.parametrize(
    ("footnotes", "message"),
    [
        ([], "BLS footnotes must be a validated tuple"),
        ([{"code": "P", "text": None}], "BLS footnotes must be a validated tuple"),
        (("P",), "BLS footnotes must use the central footnote contract"),
        ((None,), "BLS footnotes must use the central footnote contract"),
        (
            ({"code": "P", "text": None, "extra": "x"},),
            "BLS footnote contains unknown fields",
        ),
        (({"code": 1, "text": None},), "BLS footnote is invalid"),
        (({"code": None, "text": 2},), "BLS footnote is invalid"),
        (({"code": ["P"], "text": None},), "BLS footnote is invalid"),
    ],
)
def test_bls_footnote_encode_guards(
    monkeypatch: pytest.MonkeyPatch, footnotes: object, message: str
) -> None:
    monkeypatch.setattr(
        codec,
        "_strict_row_values",
        lambda domain, row, model: _bls_values(footnotes=footnotes),
    )
    with pytest.raises(ValueError, match=re.escape(message)):
        codec.encode_model_storage_row("bls_series", _bls_row())


def test_bls_footnote_encode_dumps_validated_tuple_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codec,
        "_strict_row_values",
        lambda domain, row, model: _bls_values(footnotes=({"code": "P", "text": None},)),
    )

    encoded = codec.encode_model_storage_row("bls_series", _bls_row())
    assert encoded["footnotes"] == '[{"code":"P","text":null}]'
    assert encoded["series_id"] == "LNS14000000"
    assert encoded["value"] == 4.1


def test_bls_footnote_encode_keeps_only_reviewed_footnote_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codec,
        "_strict_row_values",
        lambda domain, row, model: _bls_values(footnotes=(BlsFootnote(code="P", text="N"),)),
    )

    with pytest.raises(ValueError, match=re.escape("central footnote contract")):
        codec.encode_model_storage_row("bls_series", _bls_row())


@pytest.mark.parametrize(
    "dumped",
    [
        {"unexpected": True},
        ["not", "a", "mapping"],
        {**_fred_values(), "extra_field": "x"},
        {key: value for key, value in _fred_values().items() if key != "series_id"},
    ],
)
def test_untrusted_first_dump_is_rejected(monkeypatch: pytest.MonkeyPatch, dumped: object) -> None:
    monkeypatch.setattr(SeriesObservation, "model_dump", lambda self, **kwargs: dumped)
    with pytest.raises(ValueError, match=re.escape("invalid fred_series contract row")):
        codec.encode_model_storage_row("fred_series", _fred_row())


def test_untrusted_revalidation_dump_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _fred_row().model_dump(mode="python", by_alias=False)
    calls: list[str | None] = []

    def flaky_dump(self: SeriesObservation, **kwargs: object) -> object:
        calls.append(kwargs.get("mode") if isinstance(kwargs.get("mode"), str) else None)
        return good if len(calls) == 1 else ["not", "a", "mapping"]

    monkeypatch.setattr(SeriesObservation, "model_dump", flaky_dump)
    with pytest.raises(ValueError, match=re.escape("invalid fred_series contract row")):
        codec.encode_model_storage_row("fred_series", _fred_row())

    assert calls == ["python", "python"]


@pytest.mark.parametrize(
    ("footnotes", "message"),
    [
        ('{"code":"P"}', "stored JSON value must be an array"),
        ('"[]"', "stored JSON value must be an array"),
        ("17", "stored JSON value must be an array"),
        ('{"a":[1],"b":[2]}', "stored JSON value must be an array"),
        ("not-json", "stored JSON array is invalid"),
        ("[NaN]", "stored JSON array is invalid"),
        ('[{"code":"A","code":"B"}]', "stored JSON array is invalid"),
    ],
)
def test_bls_footnotes_json_array_text_is_strict(footnotes: str, message: str) -> None:
    record = codec.encode_model_storage_row("bls_series", _bls_row())
    record["footnotes"] = footnotes
    with pytest.raises(ValueError, match=re.escape(message)):
        codec.decode_model_storage_row("bls_series", record)


@pytest.mark.parametrize(
    ("footnotes", "expected"),
    [
        ("[]", ()),
        ('[{"code":"P","text":null}]', (BlsFootnote(code="P", text=None),)),
        ("[{}]", (BlsFootnote(code=None, text=None),)),
        ('[{"code":null,"text":"Note"}]', (BlsFootnote(code=None, text="Note"),)),
    ],
)
def test_bls_footnotes_json_array_text_decodes(footnotes: str, expected: tuple) -> None:
    row = _bls_row(footnotes=expected)
    record = codec.encode_model_storage_row("bls_series", _bls_row(footnotes=()))
    record["footnotes"] = footnotes

    decoded = codec.decode_model_storage_row("bls_series", record)
    assert decoded.footnotes == expected
    assert decoded == row
    assert type(decoded) is BlsObservation


@pytest.mark.parametrize(
    ("changes", "expected_context"),
    [
        ({"requested_frequency": "m"}, '["lin",1,"m","avg"]'),
        ({"transform_units": "log"}, '["log",1,null,"avg"]'),
        ({"output_type": 4}, '["lin",4,null,"avg"]'),
        ({"requested_aggregation_method": "sum"}, '["lin",1,null,"sum"]'),
        ({"value": None}, _FRED_CONTEXT),
    ],
)
def test_fred_row_roundtrip_revalidates_the_request_context(
    changes: dict, expected_context: str
) -> None:
    row = _fred_row(**changes)
    encoded = codec.encode_model_storage_row("fred_series", row)

    assert encoded["_request_context"] == expected_context
    assert codec.decode_model_storage_row("fred_series", encoded) == row

    tampered = {**encoded, "transform_units": "pc1"}
    with pytest.raises(ValueError, match=re.escape("request context does not match")):
        codec.decode_model_storage_row("fred_series", tampered)


def test_bls_row_roundtrip_keeps_footnotes_and_tiny_booleans() -> None:
    row = _bls_row(footnotes=(BlsFootnote(code="P", text="Preliminary"), BlsFootnote()))
    encoded = codec.encode_model_storage_row("bls_series", row)

    assert encoded["footnotes"] == '[{"code":"P","text":"Preliminary"},{"code":null,"text":null}]'
    assert encoded["year"] == 2025
    decoded = codec.decode_model_storage_row("bls_series", encoded)
    assert decoded == row
    assert decoded.footnotes == row.footnotes


def test_annotation_only_contract_import_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not hasattr(codec, "ContractModel")
    monkeypatch.setattr(typing, "TYPE_CHECKING", True)

    reloaded = importlib.reload(codec)
    assert reloaded.ContractModel is ContractModel
    monkeypatch.undo()
    restored = importlib.reload(codec)
    assert restored.physical_model_key("equity_historical") == ("symbol", "date")
