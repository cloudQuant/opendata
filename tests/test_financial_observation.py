"""Behavioral contract tests for source-native A-share financial observations."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from opendata.data.financial_observation import FinancialStatementObservation


def _observation(**overrides: object) -> FinancialStatementObservation:
    """Build one valid observation and apply scenario-specific field changes."""
    values: dict[str, object] = {
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "report_period": date(2024, 12, 31),
        "source_disclosure_date": date(2025, 3, 31),
        "observed_at": datetime(2025, 4, 1, 2, 30, tzinfo=timezone.utc),
        "source_id": "ths_fuyao",
        "item_code": "net_profit",
        "value": Decimal("1234567890.1200"),
        "native_unit": "CNY",
        "source_metadata": (("ticker", "600519.SH"), ("currency", "CNY")),
    }
    values.update(overrides)
    return FinancialStatementObservation(**values)


@pytest.mark.parametrize("instrument_id", ["600519.SH", "000001.SZ", "430047.BJ"])
def test_instrument_id_requires_exchange_qualified_six_digit_a_share(
    instrument_id: str,
) -> None:
    """Accept each supported exchange suffix in canonical instrument identity."""
    assert _observation(instrument_id=instrument_id).instrument_id == instrument_id


@pytest.mark.parametrize(
    "instrument_id",
    ["600519", "600519.BJX", "000001.SZ ", "abc123.SH", "12345.SH", "123456.CSI"],
)
def test_instrument_id_rejects_bare_or_non_a_share_forms(instrument_id: str) -> None:
    """Reject identifiers whose exchange or six-digit code cannot be established."""
    with pytest.raises(ValueError, match="instrument_id"):
        _observation(instrument_id=instrument_id)


def test_instrument_id_rejects_non_string_input() -> None:
    """Fail closed when an instrument identifier has no string representation contract."""
    with pytest.raises(ValueError, match="instrument_id"):
        _observation(instrument_id=None)


def test_same_code_on_different_exchanges_has_distinct_natural_key() -> None:
    """Keep market qualification inside the natural key to avoid code collisions."""
    shanghai = _observation(instrument_id="000001.SH")
    shenzhen = _observation(instrument_id="000001.SZ")

    assert shanghai.natural_key != shenzhen.natural_key
    assert shanghai.observation_id != shenzhen.observation_id


@pytest.mark.parametrize("statement_type", ["income", "balance", "cashflow"])
def test_report_type_accepts_only_canonical_statement_names(statement_type: str) -> None:
    """Retain the provider's three declared report types without guessing aliases."""
    assert _observation(statement_type=statement_type).statement_type == statement_type


@pytest.mark.parametrize("statement_type", ["profit", "balance_sheet", "", None])
def test_report_type_rejects_unknown_values(statement_type: object) -> None:
    """Reject report types that do not match the provider's declared contract."""
    with pytest.raises(ValueError, match="statement_type"):
        _observation(statement_type=statement_type)


@pytest.mark.parametrize("periodicity", ["annual", "quarterly"])
def test_periodicity_accepts_annual_and_quarterly(periodicity: str) -> None:
    """Represent annual and quarterly source query cadences distinctly."""
    report_period = date(2024, 12, 31)
    observation = _observation(periodicity=periodicity, report_period=report_period)
    assert observation.periodicity == periodicity


@pytest.mark.parametrize("periodicity", ["monthly", None])
def test_periodicity_rejects_undeclared_cadence(periodicity: object) -> None:
    """Reject unsupported cadence before using it to interpret fiscal dates."""
    with pytest.raises(ValueError, match="periodicity"):
        _observation(periodicity=periodicity)


@pytest.mark.parametrize(
    ("periodicity", "report_period"),
    [
        ("annual", date(2024, 9, 30)),
        ("quarterly", date(2024, 12, 30)),
        ("quarterly", datetime(2024, 12, 31, tzinfo=timezone.utc)),
    ],
)
def test_reporting_period_must_be_exact_calendar_period_end(
    periodicity: str, report_period: date | datetime
) -> None:
    """Reject mid-period dates and datetimes rather than truncating or rounding."""
    with pytest.raises((TypeError, ValueError), match="report_period"):
        _observation(periodicity=periodicity, report_period=report_period)


def test_disclosure_date_is_required_source_date_and_never_inferred() -> None:
    """Require a date-only source disclosure field, without deriving it from period end."""
    with pytest.raises(TypeError, match="source_disclosure_date"):
        _observation(source_disclosure_date=datetime(2025, 3, 31, tzinfo=timezone.utc))
    with pytest.raises(TypeError, match="source_disclosure_date"):
        _observation(source_disclosure_date=None)
    with pytest.raises(ValueError, match="cannot precede report_period"):
        _observation(source_disclosure_date=date(2024, 12, 30))


def test_disclosure_date_cannot_be_a_future_business_date() -> None:
    """Do not let a future disclosure date masquerade as already observed data."""
    with pytest.raises(ValueError, match="later than observed_at"):
        _observation(
            source_disclosure_date=date(2025, 4, 2),
            observed_at=datetime(2025, 4, 1, 14, 30, tzinfo=timezone.utc),
        )


def test_observation_time_must_be_aware_and_is_normalized_to_utc() -> None:
    """Keep retrieval time distinct from source disclosure date and normalize its zone."""
    local_time = datetime(2025, 4, 1, 10, 30, tzinfo=timezone(timedelta(hours=8)))
    observation = _observation(observed_at=local_time)

    assert observation.observed_at == datetime(2025, 4, 1, 2, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="include a timezone"):
        _observation(observed_at=datetime(2025, 4, 1, 2, 30))


def test_observation_time_rejects_non_datetime_input() -> None:
    """Reject a date-only source field instead of treating it as retrieval time."""
    with pytest.raises(TypeError, match="observed_at must be a datetime"):
        _observation(observed_at=date(2025, 4, 1))


@pytest.mark.parametrize("source_id", ["THS", None])
def test_source_identity_rejects_noncanonical_or_missing_id(source_id: object) -> None:
    """Require a stable provider identifier instead of inferring one from metadata."""
    with pytest.raises(ValueError, match="source_id"):
        _observation(source_id=source_id)


def test_identical_content_reobserved_later_has_same_digest_and_identity() -> None:
    """Avoid manufacturing a restatement merely because collection ran again."""
    first = _observation()
    later = _observation(observed_at=datetime(2025, 4, 2, 2, 30, tzinfo=timezone.utc))

    assert first.observed_at != later.observed_at
    assert first.content_sha256 == later.content_sha256
    assert first.observation_id == later.observation_id


def test_changed_reported_content_is_a_distinct_observation_under_same_key() -> None:
    """Retain a changed value as a new observation while preserving report identity."""
    original = _observation()
    restated = _observation(value=Decimal("1234567890.13"))

    assert original.natural_key == restated.natural_key
    assert original.content_sha256 != restated.content_sha256
    assert original.observation_id != restated.observation_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1234567890.1200"), Decimal("1234567890.12")),
        ("-0.000000000000000001", Decimal("-0.000000000000000001")),
        (42, Decimal(42)),
        (None, None),
    ],
)
def test_numeric_values_are_exact_and_canonical(value: object, expected: Decimal | None) -> None:
    """Accept exact decimal inputs and preserve null as distinct from numeric zero."""
    assert _observation(value=value).value == expected


def test_integer_trailing_zeroes_change_content_digest_and_observation_id() -> None:
    """Preserve integer magnitude when canonicalizing values for identity hashes."""
    observations = tuple(_observation(value=value) for value in (1, 10, 100))

    assert len({item.content_sha256 for item in observations}) == 3
    assert len({item.observation_id for item in observations}) == 3


def test_numeric_source_metadata_preserves_integer_zero_negative_and_fraction_values() -> None:
    """Serialize metadata numerics without dropping integer zeroes or signs."""
    observation = _observation(
        source_metadata=(
            ("one", 1),
            ("ten", 10),
            ("hundred", 100),
            ("zero", 0),
            ("negative_ten", -10),
            ("negative_fraction", Decimal("-1.2500")),
        )
    )

    assert observation.source_metadata_json == (
        '{"hundred":100,"negative_fraction":-1.25,"negative_ten":-10,"one":1,"ten":10,"zero":0}'
    )


@pytest.mark.parametrize(
    "value",
    [
        True,
        1.25,
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        "NaN",
        "1e999",
        "1e" + "9" * 120,
        "0.0000000000000000001",
        "123456789012345678901234567890123456789",
        object(),
    ],
)
def test_numeric_values_reject_ambiguous_nonfinite_or_excess_precision(value: object) -> None:
    """Reject binary floats, nonfinite values, and values outside declared precision."""
    with pytest.raises((TypeError, ValueError), match="value"):
        _observation(value=value)


def test_native_unit_is_required_and_is_not_silently_normalized() -> None:
    """Preserve exact source unit labels instead of inferring a normalized unit."""
    observation = _observation(native_unit="元/股")
    assert observation.native_unit == "元/股"
    with pytest.raises(ValueError, match="native_unit"):
        _observation(native_unit=" CNY")
    with pytest.raises(TypeError, match="native_unit"):
        _observation(native_unit=None)


def test_source_metadata_is_immutable_scalar_data_and_hash_order_is_stable() -> None:
    """Retain provider fields and make the digest independent of map insertion order."""
    left = _observation(source_metadata=(("ticker", "600519.SH"), ("currency", "CNY")))
    right = _observation(source_metadata=(("currency", "CNY"), ("ticker", "600519.SH")))

    assert left.source_metadata != right.source_metadata
    assert left.source_metadata_json == right.source_metadata_json
    assert left.content_sha256 == right.content_sha256
    with pytest.raises(ValueError, match="unique"):
        _observation(source_metadata=(("currency", "CNY"), ("currency", "CNY")))
    with pytest.raises(TypeError, match="exact Decimal"):
        _observation(source_metadata=(("raw_value", 1.0),))
    with pytest.raises(TypeError, match="immutable tuple"):
        _observation(source_metadata={"currency": "CNY"})


def test_source_metadata_rejects_empty_and_malformed_pairs() -> None:
    """Require at least one well-formed source field for provenance."""
    with pytest.raises(ValueError, match="between 1 and 256 fields"):
        _observation(source_metadata=())
    with pytest.raises(TypeError, match="key/value tuples"):
        _observation(source_metadata=(("ticker",),))


def test_source_metadata_rejects_aggregate_payload_over_limit() -> None:
    """Reject many individually bounded source fields when their aggregate is oversized."""
    metadata = tuple((f"raw_{index}", "x" * 8192) for index in range(9))

    with pytest.raises(ValueError, match="65536-byte limit"):
        _observation(source_metadata=metadata)


def test_source_metadata_accepts_json_boolean_and_null_without_coercion() -> None:
    """Preserve source boolean and null metadata as JSON scalars."""
    observation = _observation(
        source_metadata=(("reported", True), ("missing", None), ("count", 0))
    )

    assert observation.source_metadata_json == '{"count":0,"missing":null,"reported":true}'


def test_null_value_has_distinct_content_from_true_zero() -> None:
    """Keep an absent source value distinguishable from a measured zero."""
    missing = _observation(value=None)
    zero = _observation(value=Decimal("0.00"))

    assert missing.value is None
    assert zero.value == Decimal(0)
    assert missing.content_sha256 != zero.content_sha256


def test_policy_explicitly_disclaims_unarchived_historical_pit() -> None:
    """State that latest-effective source values cannot imply historical revisions."""
    observation = _observation()

    assert observation.observation_policy == "current_as_observed_only"
    with pytest.raises(ValueError, match="historical PIT"):
        _observation(observation_policy="historical_point_in_time")


def test_metadata_is_bounded_and_rejects_control_keys() -> None:
    """Bound retained source evidence and reject control-bearing field names."""
    with pytest.raises(ValueError, match="8192-byte limit"):
        _observation(source_metadata=(("raw", "x" * 8193),))
    with pytest.raises(ValueError, match="control characters"):
        _observation(source_metadata=(("bad\nkey", "value"),))
