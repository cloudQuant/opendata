"""Offline contracts for ECB YC and BPS observations.

Fixtures use the published YC and BPS series keys. The BPS quarterly case is
only a period/frequency shape check; it does not claim the modified key is a
series the ECB has published.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models.currency import SdmxGroupContext
from opendata.data.models.ecb_series import (
    EcbBalanceOfPaymentsObservation,
    EcbYieldCurveObservation,
)

YC_DIMENSION_FIELDS = (
    "frequency",
    "ref_area",
    "currency",
    "provider_fm",
    "instrument_fm",
    "provider_fm_id",
    "data_type_fm",
)
BPS_DIMENSION_FIELDS = (
    "frequency",
    "adjustment",
    "ref_area",
    "counterpart_area",
    "ref_sector",
    "counterpart_sector",
    "flow_stock_entry",
    "accounting_entry",
    "int_acc_item",
    "functional_cat",
    "instr_asset",
    "maturity",
    "unit_measure",
    "currency_denom",
    "valuation",
    "comp_method",
    "type_entity",
)
BPS_MONTHLY_KEY = "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"


def _yc_payload(**overrides: object) -> dict[str, object]:
    """Published YC series dimensions with a synthetic raw observation."""
    values: dict[str, object] = {
        "series_key": "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M",
        "frequency": "B",
        "ref_area": "U2",
        "currency": "EUR",
        "provider_fm": "4F",
        "instrument_fm": "G_N_A",
        "provider_fm_id": "SV_C_YM",
        "data_type_fm": "SR_3M",
        "date": "2026-10-08",
        "value": 3.123456,
        "source_value": "3.123456",
        "dataset_attributes": {"UNKNOWN_DATASET": "kept raw"},
        "series_attributes": {"UNIT": "Percent per annum", "COLLECTION": "E"},
        "observation_attributes": {"OBS_STATUS": "A"},
        "group_context": (
            SdmxGroupContext(
                group_type="YC_GROUP",
                key={"FREQ": "B", "REF_AREA": "U2"},
                attributes={"COMMENT": "source group text"},
            ),
        ),
    }
    return {**values, **overrides}


def _bps_payload(**overrides: object) -> dict[str, object]:
    """Published 17-dimensional BPS series key with a synthetic monthly row."""
    values: dict[str, object] = {
        "series_key": BPS_MONTHLY_KEY,
        "frequency": "M",
        "adjustment": "N",
        "ref_area": "I10",
        "counterpart_area": "W1",
        "ref_sector": "S121",
        "counterpart_sector": "S1",
        "flow_stock_entry": "T",
        "accounting_entry": "A",
        "int_acc_item": "FA",
        "functional_cat": "R",
        "instr_asset": "F",
        "maturity": "_Z",
        "unit_measure": "EUR",
        "currency_denom": "X1",
        "valuation": "_X",
        "comp_method": "N",
        "type_entity": "ALL",
        "period": "2014-09",
        "value": 1250.0,
        "source_value": "1250.00",
        "dataset_attributes": {"UNKNOWN_DATASET": "kept raw"},
        "series_attributes": {"UNIT_MULT": "6", "UNIT_MEASURE": "EUR"},
        "observation_attributes": {"OBS_STATUS": "A", "OBS_CONF": "F"},
        "group_context": (
            SdmxGroupContext(
                group_type="BPS_GROUP",
                key={"FREQ": "M", "REF_AREA": "I10"},
                attributes={"COMMENT": "opaque group context"},
            ),
        ),
    }
    return {**values, **overrides}


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
def test_official_dimension_examples_roundtrip_json_frame_and_nested_source_context(
    model: Any,
    payload: dict[str, object],
) -> None:
    row = model.model_validate(payload)

    frame = model.to_frame([row])
    restored = model.from_frame(frame)
    assert restored == [row]

    from_json = model.model_validate_json(row.model_dump_json())
    assert from_json == row
    assert restored[0].source_value == payload["source_value"]
    assert restored[0].dataset_attributes == payload["dataset_attributes"]
    assert restored[0].series_attributes == payload["series_attributes"]
    assert restored[0].observation_attributes == payload["observation_attributes"]
    assert restored[0].group_context == payload["group_context"]
    assert tuple(frame.columns) == tuple(model.model_fields)


def test_yield_curve_keeps_daily_date_as_date_and_rejects_frame_timestamp() -> None:
    row = EcbYieldCurveObservation.model_validate(_yc_payload(date=date(2026, 10, 8)))
    assert row.date == date(2026, 10, 8)
    assert type(row.date) is date
    assert EcbYieldCurveObservation.model_validate_json(row.model_dump_json()) == row

    frame = EcbYieldCurveObservation.to_frame([row])
    frame.at[0, "date"] = pd.Timestamp("2026-10-08T12:00:00", tz="UTC")
    with pytest.raises(ValueError, match="must not include a time"):
        EcbYieldCurveObservation.from_frame(frame)


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
def test_integer_values_are_normalized_to_float(
    model: Any,
    payload: dict[str, object],
) -> None:
    row = model.model_validate({**payload, "value": 7, "source_value": "7"})
    assert row.value == 7.0
    assert type(row.value) is float


@pytest.mark.parametrize(
    "invalid_date",
    [
        datetime(2026, 10, 8),
        datetime(2026, 10, 8, tzinfo=timezone.utc),
        "2026-1-08",
        "20261008",
        20261008,
    ],
)
def test_yield_curve_rejects_datetime_or_noncanonical_daily_date(invalid_date: object) -> None:
    with pytest.raises(ValidationError):
        EcbYieldCurveObservation.model_validate(_yc_payload(date=invalid_date))


def test_bps_quarter_period_is_kept_as_period_without_a_daily_date() -> None:
    quarterly_key = BPS_MONTHLY_KEY.replace("BPS.M.", "BPS.Q.", 1)
    row = EcbBalanceOfPaymentsObservation.model_validate(
        _bps_payload(series_key=quarterly_key, frequency="Q", period="2014-Q3")
    )
    assert row.period == "2014-Q3"
    assert row.frequency == "Q"
    assert "date" not in EcbBalanceOfPaymentsObservation.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EcbBalanceOfPaymentsObservation.model_validate(_bps_payload(date="2014-07-01"))


@pytest.mark.parametrize(
    ("model", "payload", "fields"),
    [
        (EcbYieldCurveObservation, _yc_payload(), YC_DIMENSION_FIELDS),
        (EcbBalanceOfPaymentsObservation, _bps_payload(), BPS_DIMENSION_FIELDS),
    ],
)
def test_every_product_dimension_is_required(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        incomplete = dict(payload)
        incomplete.pop(field)
        with pytest.raises(ValidationError):
            model.model_validate(incomplete)


@pytest.mark.parametrize(
    ("model", "payload", "wrong_key"),
    [
        (
            EcbYieldCurveObservation,
            _yc_payload(),
            "YC.B.U2.EUR.4F.SV_C_YM.G_N_A.SR_3M",
        ),
        (
            EcbBalanceOfPaymentsObservation,
            _bps_payload(),
            "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.X1.EUR._X.N.ALL",
        ),
    ],
)
def test_series_key_must_match_all_dimensions_in_official_order(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
    wrong_key: str,
) -> None:
    with pytest.raises(ValidationError, match="series_key"):
        model.model_validate({**payload, "series_key": wrong_key})


@pytest.mark.parametrize(
    ("model", "payload", "field", "value"),
    [
        (EcbYieldCurveObservation, _yc_payload(), "frequency", "D"),
        (EcbBalanceOfPaymentsObservation, _bps_payload(), "frequency", "A"),
        (EcbYieldCurveObservation, _yc_payload(), "currency", "eur"),
        (EcbYieldCurveObservation, _yc_payload(), "provider_fm", "4F.BAD"),
        (EcbBalanceOfPaymentsObservation, _bps_payload(), "ref_area", "I 10"),
        (EcbBalanceOfPaymentsObservation, _bps_payload(), "unit_measure", "X" * 33),
    ],
)
def test_frequency_and_dimension_tokens_are_strict(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: value})


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    [True, False, "1.2", Decimal("1.2"), float("nan"), float("inf")],
)
def test_value_requires_a_finite_non_boolean_python_number(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
    invalid_value: object,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "value": invalid_value})


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
@pytest.mark.parametrize("source_value", ["NaN", "Infinity", "1e9999", ".", "1,25", "1 2"])
def test_source_value_requires_finite_decimal_text(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
    source_value: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "source_value": source_value})


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
def test_value_and_raw_source_value_null_or_numeric_results_must_match(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
) -> None:
    for overrides in (
        {"value": None},
        {"source_value": None},
        {"value": 0.0},
        {"source_value": "2.000"},
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, **overrides})


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
def test_negative_zero_and_large_decimal_keep_raw_text_while_value_is_finite(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
) -> None:
    negative_zero = model.model_validate({**payload, "value": -0.0, "source_value": "-0"})
    assert negative_zero.source_value == "-0"
    assert negative_zero.value == 0.0
    assert negative_zero.value is not None
    assert math.copysign(1.0, negative_zero.value) == -1.0

    raw_large = "123456789012345678901234567890.12345678901234567890"
    converted = float(Decimal(raw_large))
    large = model.model_validate({**payload, "value": converted, "source_value": raw_large})
    assert large.value is not None
    assert math.isfinite(large.value)
    assert large.source_value == raw_large
    assert large.value == converted


def _validate_value_pair_via_entrypoint(
    model: Any,
    base_payload: dict[str, object],
    candidate: dict[str, object],
    entrypoint: str,
) -> Any:
    payload = {**base_payload, **candidate}
    if entrypoint == "model_validate":
        return model.model_validate(payload)
    if entrypoint == "model_validate_json":
        group_context = cast("tuple[SdmxGroupContext, ...]", payload["group_context"])
        json_payload = {
            **payload,
            "group_context": [context.model_dump(mode="json") for context in group_context],
        }
        return model.model_validate_json(json.dumps(json_payload))
    if entrypoint == "from_frame":
        valid_row = model.model_validate(base_payload)
        frame = model.to_frame([valid_row])
        frame.at[0, "value"] = candidate["value"]
        frame.at[0, "source_value"] = candidate["source_value"]
        return model.from_frame(frame)[0]
    raise AssertionError(f"unsupported validation entrypoint: {entrypoint}")


@pytest.mark.parametrize(
    "model,base_payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
@pytest.mark.parametrize("entrypoint", ["model_validate", "model_validate_json", "from_frame"])
@pytest.mark.parametrize(
    "candidate,expected_sign",
    [
        ({"value": -0.0, "source_value": "-0"}, -1.0),
        ({"value": 0.0, "source_value": "0"}, 1.0),
        ({"value": -0.0, "source_value": "-1e-9999"}, -1.0),
        ({"value": 0.0, "source_value": "1e-9999"}, 1.0),
    ],
)
def test_signed_zero_matches_decimal_float_conversion_at_every_entrypoint(
    model: Any,
    base_payload: dict[str, object],
    entrypoint: str,
    candidate: dict[str, object],
    expected_sign: float,
) -> None:
    row = _validate_value_pair_via_entrypoint(model, base_payload, candidate, entrypoint)
    assert row.source_value == candidate["source_value"]
    assert row.value == 0.0
    assert row.value is not None
    assert math.copysign(1.0, row.value) == expected_sign


@pytest.mark.parametrize(
    "model,base_payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
@pytest.mark.parametrize("entrypoint", ["model_validate", "model_validate_json", "from_frame"])
@pytest.mark.parametrize(
    "candidate",
    [
        {"value": 0.0, "source_value": "-0"},
        {"value": -0.0, "source_value": "0"},
        {"value": 0.0, "source_value": "-1e-9999"},
        {"value": -0.0, "source_value": "1e-9999"},
    ],
)
def test_signed_zero_mismatch_is_rejected_at_every_entrypoint(
    model: Any,
    base_payload: dict[str, object],
    entrypoint: str,
    candidate: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="value must equal the float conversion"):
        _validate_value_pair_via_entrypoint(model, base_payload, candidate, entrypoint)


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
@pytest.mark.parametrize(
    "field", ["dataset_attributes", "series_attributes", "observation_attributes"]
)
@pytest.mark.parametrize("invalid_map", [[], {"K": 1}, {1: "V"}])
def test_source_attributes_are_strict_string_maps(
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
    payload: dict[str, object],
    field: str,
    invalid_map: object,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, field: invalid_map})


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
def test_unknown_fields_are_rejected(model: Any, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unexpected": "value"})


@pytest.mark.parametrize(
    ("period", "frequency"),
    [
        ("2014-Q3", "M"),
        ("2014-00", "M"),
        ("2014-13", "M"),
        ("0000-01", "M"),
        ("2014-01", "Q"),
        ("2014-Q0", "Q"),
        ("2014-Q5", "Q"),
        ("0000-Q1", "Q"),
        ("10000-Q1", "Q"),
        ("2014Q1", "Q"),
        (201401, "M"),
    ],
)
def test_bps_period_shape_year_and_frequency_are_validated(
    period: object,
    frequency: str,
) -> None:
    key = BPS_MONTHLY_KEY if frequency == "M" else BPS_MONTHLY_KEY.replace("BPS.M.", "BPS.Q.", 1)
    with pytest.raises(ValidationError):
        EcbBalanceOfPaymentsObservation.model_validate(
            _bps_payload(series_key=key, frequency=frequency, period=period)
        )


@pytest.mark.parametrize(
    "model,payload",
    [
        (EcbYieldCurveObservation, _yc_payload()),
        (EcbBalanceOfPaymentsObservation, _bps_payload()),
    ],
)
def test_required_and_nullable_contract_fields_are_not_implicitly_defaulted(
    model: Any,
    payload: dict[str, object],
) -> None:
    for field in ("series_key", "value", "source_value"):
        incomplete = dict(payload)
        incomplete.pop(field)
        with pytest.raises(ValidationError):
            model.model_validate(incomplete)

    empty = dict(payload)
    empty.update(value=None, source_value=None)
    assert model.model_validate(empty).value is None
