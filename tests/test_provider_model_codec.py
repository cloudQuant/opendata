"""Offline contract tests for the reviewed provider storage codecs."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions

from opendata.data.domains import contract_model, load_domains, require_domain_semantics
from opendata.data.models import BlsFootnote, BlsObservation, SeriesObservation
from opendata.pipeline import provider_model_codec as codec


def _fred_row(
    *,
    realtime_start: date = date(2024, 1, 1),
    realtime_end: date = date(2024, 12, 31),
    transform_units: str = "lin",
    output_type: int = 1,
    requested_frequency: str | None = None,
    requested_aggregation_method: str = "avg",
    value: float | None = None,
) -> SeriesObservation:
    return SeriesObservation(
        series_id="CPIAUCSL",
        date=date(2024, 2, 1),
        value=value,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
        transform_units=transform_units,  # type: ignore[arg-type]
        output_type=output_type,  # type: ignore[arg-type]
        requested_frequency=requested_frequency,  # type: ignore[arg-type]
        requested_aggregation_method=requested_aggregation_method,  # type: ignore[arg-type]
    )


def _bls_row(*, footnotes: tuple[BlsFootnote, ...] = (), period: str = "M13") -> BlsObservation:
    return BlsObservation(
        series_id="LNS14000000",
        year=2025,
        period=period,
        period_name="Annual average",
        value=4.1,
        footnotes=footnotes,
        latest=None,
        preliminary=False,
        api_version="v2",
    )


@pytest.fixture(scope="module")
def production_specs():
    """Use the actual reviewed production registry for positive cases."""
    specs = load_domains()
    assert specs["fred_series"].semantics_declared is True
    assert specs["bls_series"].semantics_declared is True
    assert contract_model("fred_series") is SeriesObservation
    assert contract_model("bls_series") is BlsObservation
    return specs


def test_production_domain_specs_define_exact_physical_keys(production_specs):
    assert production_specs["fred_series"] is require_domain_semantics("fred_series")
    assert production_specs["bls_series"] is require_domain_semantics("bls_series")
    assert codec.physical_model_key("fred_series") == (
        "series_id",
        "date",
        "realtime_start",
        "realtime_end",
        "_request_context",
    )
    assert codec.physical_model_key("bls_series") == ("series_id", "year", "period")


def test_fred_revision_and_request_context_keys_are_reversible(production_specs):
    rows = (
        _fred_row(),
        _fred_row(
            transform_units="chg",
            output_type=2,
            requested_frequency="m",
            requested_aggregation_method="sum",
        ),
        _fred_row(
            transform_units="pch",
            output_type=3,
            requested_frequency="q",
            requested_aggregation_method="eop",
        ),
        _fred_row(
            transform_units="log",
            output_type=4,
            requested_frequency="a",
        ),
        _fred_row(realtime_start=date(2024, 3, 1), realtime_end=date(2024, 12, 31)),
    )
    encoded = [codec.encode_model_storage_row("fred_series", row) for row in rows]

    assert len(
        {
            (
                item["series_id"],
                item["date"],
                item["realtime_start"],
                item["realtime_end"],
                item["_request_context"],
            )
            for item in encoded
        }
    ) == len(rows)
    assert encoded[0]["value"] is None
    assert encoded[0]["_request_context"] == '["lin",1,null,"avg"]'
    assert all(
        codec.decode_model_storage_row("fred_series", record) == source
        for source, record in zip(rows, encoded, strict=True)
    )


def test_fred_context_uses_canonical_ascii_json(production_specs):
    row = _fred_row(transform_units="pc1", requested_frequency=None)
    encoded = codec.encode_model_storage_row("fred_series", row)
    assert encoded["_request_context"] == json.dumps(
        ["pc1", 1, None, "avg"], ensure_ascii=True, separators=(",", ":")
    )


def test_bls_m13_footnotes_json_text_and_native_driver_shapes(production_specs):
    empty = _bls_row(footnotes=())
    noted = _bls_row(
        footnotes=(BlsFootnote(code="P", text="Preliminary"),),
        period="M13",
    )
    empty_record = codec.encode_model_storage_row("bls_series", empty)
    noted_record = codec.encode_model_storage_row("bls_series", noted)

    assert empty_record["period"] == "M13"
    assert empty_record["footnotes"] == "[]"
    assert noted_record["footnotes"] == '[{"code":"P","text":"Preliminary"}]'
    assert codec.decode_model_storage_row("bls_series", empty_record) == empty
    assert codec.decode_model_storage_row("bls_series", noted_record) == noted

    native_record = dict(noted_record)
    native_record["footnotes"] = [{"code": "P", "text": "Preliminary"}]
    assert codec.decode_model_storage_row("bls_series", native_record) == noted

    empty_object_record = dict(empty_record)
    empty_object_record["footnotes"] = "[{}]"
    decoded_empty_object = codec.decode_model_storage_row("bls_series", empty_object_record)
    assert decoded_empty_object.footnotes == (BlsFootnote(code=None, text=None),)


def test_bls_tiny_booleans_accept_current_pymysql_converter_shapes(production_specs):
    tiny_converter = conversions[FIELD_TYPE.TINY]
    converted_zero = tiny_converter(b"0")
    converted_one = tiny_converter(b"1")
    assert type(converted_zero) is int and converted_zero == 0
    assert type(converted_one) is int and converted_one == 1

    nullable_latest = codec.encode_model_storage_row("bls_series", _bls_row(footnotes=()))
    nullable_latest["latest"] = None
    nullable_latest["preliminary"] = converted_zero
    decoded_nullable = codec.decode_model_storage_row("bls_series", nullable_latest)
    assert decoded_nullable.latest is None
    assert type(decoded_nullable.preliminary) is bool
    assert decoded_nullable.preliminary is False

    converted_record = codec.encode_model_storage_row("bls_series", _bls_row())
    converted_record["latest"] = converted_one
    converted_record["preliminary"] = converted_zero
    decoded = codec.decode_model_storage_row("bls_series", converted_record)
    assert type(decoded.latest) is bool
    assert decoded.latest is True
    assert type(decoded.preliminary) is bool
    assert decoded.preliminary is False


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("latest", 2),
        ("latest", -1),
        ("latest", 0.0),
        ("latest", 1.0),
        ("latest", "1"),
        ("preliminary", 2),
        ("preliminary", -1),
        ("preliminary", 0.0),
        ("preliminary", 1.0),
        ("preliminary", "0"),
        ("preliminary", None),
    ],
)
def test_bls_tiny_booleans_reject_noncanonical_shapes(
    production_specs, field_name: str, invalid_value: object
):
    record = codec.encode_model_storage_row("bls_series", _bls_row())
    record[field_name] = invalid_value
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("bls_series", record)


def test_bls_encode_still_requires_central_boolean_contract(production_specs):
    values = _bls_row().model_dump(mode="python")
    invalid = BlsObservation.model_construct(**{**values, "latest": 1})
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("bls_series", invalid)


def test_allowed_audit_fields_are_stripped_without_changing_public_time(production_specs):
    row = _fred_row()
    record = codec.encode_model_storage_row("fred_series", row)
    record.update(
        {
            "_source": "fred",
            "_fetched_at": "2026-10-08T00:00:00Z",
            "_batch_id": "batch-1",
            "source": "fred",
            "_merged_at": "2026-10-08T00:00:00Z",
            "_diff_flag": False,
            "_as_of": "2026-10-08",
        }
    )

    decoded = codec.decode_model_storage_row("fred_series", record)
    assert decoded == row
    assert "_fetched_at" not in decoded.model_dump()
    assert decoded.date == row.date


@pytest.mark.parametrize("domain", ["fred_search", "bls_search", "unknown"])
def test_unsupported_or_unreviewed_domains_fail_closed(production_specs, domain: str):
    with pytest.raises(ValueError):
        codec.physical_model_key(domain)


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("contract", "BlsObservation"),
        ("temporal_kind", "snapshot"),
        ("time_field", "realtime_start"),
        ("natural_key", ("series_id", "date")),
        ("filter_dims", ("series_id",)),
        ("storage_mode", "append"),
        ("permissions", ("query",)),
    ],
)
def test_domain_semantic_drift_is_rejected(
    production_specs, monkeypatch: pytest.MonkeyPatch, field_name: str, changed_value: object
):
    actual_spec = require_domain_semantics("fred_series")
    drifted = actual_spec.model_copy(update={field_name: changed_value})
    monkeypatch.setattr(codec, "require_domain_semantics", lambda domain: drifted)

    with pytest.raises(ValueError):
        codec.encode_model_storage_row("fred_series", _fred_row())


def test_exact_central_contract_type_is_required(production_specs):
    class DerivedSeriesObservation(SeriesObservation):
        pass

    derived = DerivedSeriesObservation(**_fred_row().model_dump())
    with pytest.raises(ValueError, match="exact reviewed central contract"):
        codec.encode_model_storage_row("fred_series", derived)
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("fred_series", _bls_row())


def test_model_construct_and_model_copy_cannot_bypass_validation(production_specs):
    valid_dump = _fred_row().model_dump(mode="python")
    constructed = SeriesObservation.model_construct(**{**valid_dump, "output_type": True})
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("fred_series", constructed)

    copied = _fred_row().model_copy(update={"value": float("nan")})
    with pytest.raises(ValueError):
        codec.encode_model_storage_row("fred_series", copied)

    copied_with_extra = _fred_row().model_copy(update={"unreviewed": "field"})
    with pytest.raises(ValueError, match="unknown contract fields"):
        codec.encode_model_storage_row("fred_series", copied_with_extra)


@pytest.mark.parametrize(
    "footnotes",
    [
        '[{"code":"A","code":"B"}]',
        '[{"code":"A","text":NaN}]',
        '["not-a-footnote"]',
        '[{"unexpected":"field"}]',
        '[{"code":1}]',
    ],
)
def test_bad_bls_json_or_footnote_shapes_are_rejected(production_specs, footnotes: str):
    record = codec.encode_model_storage_row("bls_series", _bls_row())
    record["footnotes"] = footnotes
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("bls_series", record)


def test_bls_native_driver_shape_must_be_a_strict_json_array(production_specs):
    record = codec.encode_model_storage_row("bls_series", _bls_row())
    record["footnotes"] = ({"code": None, "text": None},)
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("bls_series", record)

    record["footnotes"] = [{"code": None, "text": None, "extra": "x"}]
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("bls_series", record)


def test_decode_rejects_unknown_storage_columns(production_specs):
    record = codec.encode_model_storage_row("bls_series", _bls_row())
    record["unreviewed_column"] = "value"
    with pytest.raises(ValueError, match="unknown fields"):
        codec.decode_model_storage_row("bls_series", record)


@pytest.mark.parametrize(
    "context",
    [
        '["log",2,"m","sum"]',
        '["lin",true,null,"avg"]',
        '[ "lin",1,null,"avg"]',
        "not-json",
    ],
)
def test_fred_context_must_be_canonical_and_match_public_fields(production_specs, context: str):
    record = codec.encode_model_storage_row("fred_series", _fred_row())
    record["_request_context"] = context
    with pytest.raises(ValueError, match="request context"):
        codec.decode_model_storage_row("fred_series", record)


def test_fred_context_is_required_and_bls_does_not_accept_it(production_specs):
    fred = codec.encode_model_storage_row("fred_series", _fred_row())
    del fred["_request_context"]
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("fred_series", fred)

    bls = codec.encode_model_storage_row("bls_series", _bls_row())
    bls["_request_context"] = "[]"
    with pytest.raises(ValueError, match="unknown fields"):
        codec.decode_model_storage_row("bls_series", bls)


def test_decode_revalidates_public_contract_fields_and_rejects_bad_mapping(production_specs):
    record = codec.encode_model_storage_row("fred_series", _fred_row())
    record["output_type"] = True
    with pytest.raises(ValueError):
        codec.decode_model_storage_row("fred_series", record)

    with pytest.raises(ValueError):
        codec.decode_model_storage_row("fred_series", object())  # type: ignore[arg-type]
