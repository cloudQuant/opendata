"""Behavioral tests for exact in-memory Fuyao financial response decoding.

The compact payloads below encode the field names and envelope shape recorded
by the C10/C18 THS financial endpoint measurements. They are contract-shape
examples, not archived live response bytes; the repository does not contain a
replayable raw THS financial body.
"""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from opendata.data.providers.ths.endpoints import financial_statement_items
from opendata.data.providers.ths.models.financial_observation_raw import (
    MAX_BODY_BYTES,
    MAX_FINANCIAL_ROWS,
    MAX_JSON_NUMBER_TOKEN_CHARS,
    FinancialObservationDecodeError,
    decode_financial_observation_body,
)

_OBSERVED_AT = datetime(2025, 4, 18, 2, 30, tzinfo=timezone.utc)
_PERIOD_END_MS = 1735574400000
_DISCLOSURE_MS = 1744819200000
_UNIT_MAPPING_VERSION_DECLARED = "shape-fixture-units-v1"
_UNIT_MAPPING_SHA256_DECLARED = hashlib.sha256(b"caller declaration for unit fixture").hexdigest()


def _native_units(statement_type: str = "income") -> dict[str, str]:
    """Provide explicit test declarations matching the measured source units."""
    return {
        item: "CNY/share" if item == "basic_eps" else "CNY"
        for item in financial_statement_items(statement_type)
    }


def _row(**overrides: object) -> dict[str, object]:
    """Build one wide row with the measured THS financial source field names."""
    row: dict[str, object] = {
        "thscode": "600519.SH",
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": _PERIOD_END_MS,
        "report_date_ms": _DISCLOSURE_MS,
        "currency": "CNY",
        **dict.fromkeys(financial_statement_items("income"), 0),
    }
    row["net_profit"] = 86228150000
    row.update(overrides)
    return row


def _body(rows: list[dict[str, object]], *, timestamp: object = _PERIOD_END_MS) -> bytes:
    """Encode the success envelope used by the measured Fuyao endpoint."""
    return _response_body(item=rows, timestamp=timestamp)


def _response_body(
    *,
    item: object,
    code: object = 0,
    message: object = "success",
    request_id: object = "shape-only-request",
    timestamp: object = _PERIOD_END_MS,
) -> bytes:
    """Build an envelope with selected untrusted response fields for boundary tests."""
    return json.dumps(
        {
            "code": code,
            "message": message,
            "request_id": request_id,
            "data": {"timestamp": timestamp, "item": item},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode(
    body: bytes,
    *,
    observed_at: datetime = _OBSERVED_AT,
    expected_instrument_id: str = "600519.SH",
    statement_type: str = "income",
    periodicity: str = "annual",
    unit_mapping_version_declared: str = _UNIT_MAPPING_VERSION_DECLARED,
    unit_mapping_sha256_declared: str = _UNIT_MAPPING_SHA256_DECLARED,
    native_units: dict[str, str] | None = None,
):
    """Call the public decoder with caller-supplied request context."""
    return decode_financial_observation_body(
        body,
        expected_instrument_id=expected_instrument_id,
        statement_type=statement_type,
        periodicity=periodicity,
        observed_at=observed_at,
        native_units=_native_units() if native_units is None else native_units,
        native_unit_mapping_version_declared=unit_mapping_version_declared,
        native_unit_mapping_sha256_declared=unit_mapping_sha256_declared,
    )


def test_measured_wide_row_emits_exact_items_and_source_pointers() -> None:
    """Map one real-shape wide row into exact source-native item observations."""
    raw_body = _body([_row()]).replace(
        b'"net_profit":86228150000',
        b'"net_profit":86228150000.123456789012345678',
    )

    decoded = _decode(raw_body)
    net_profit = next(
        item for item in decoded.observations if item.observation.item_code == "net_profit"
    )

    assert len(decoded.observations) == len(financial_statement_items("income"))
    assert net_profit.observation.instrument_id == "600519.SH"
    assert net_profit.observation.report_period.isoformat() == "2024-12-31"
    assert net_profit.observation.source_disclosure_date.isoformat() == "2025-04-17"
    assert net_profit.observation.observed_at == _OBSERVED_AT
    assert net_profit.observation.value == Decimal("86228150000.123456789012345678")
    assert net_profit.observation.native_unit == "CNY"
    assert net_profit.observation.source_metadata_json.startswith('{"currency":"CNY"')
    assert net_profit.body_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert net_profit.row_pointer == "/data/item/0"
    assert net_profit.value_pointer == "/data/item/0/net_profit"
    assert decoded.source_request_id == "shape-only-request"
    assert decoded.native_unit_mapping_version_declared == _UNIT_MAPPING_VERSION_DECLARED
    assert decoded.native_unit_mapping_sha256_declared == _UNIT_MAPPING_SHA256_DECLARED
    assert decoded.quarantined_rows == ()


def test_scientific_notation_financial_number_remains_an_exact_decimal() -> None:
    """Parse an exponent token directly to Decimal without binary float conversion."""
    raw_body = _body([_row()]).replace(b'"net_profit":86228150000', b'"net_profit":8.622815e10')

    decoded = _decode(raw_body)
    net_profit = next(
        item for item in decoded.observations if item.observation.item_code == "net_profit"
    )

    assert net_profit.observation.value == Decimal("8.622815e10")
    assert type(net_profit.observation.value) is Decimal


def test_decimal_with_excess_source_precision_is_rejected_by_the_domain_contract() -> None:
    """Keep parser acceptance separate from the domain's bounded Decimal contract."""
    raw_body = _body([_row()]).replace(b'"net_profit":86228150000', b'"net_profit":' + b"9" * 39)

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(raw_body)

    assert raised.value.code == "observation_invalid"
    assert raised.value.pointer == "/data/item/0/net_profit"


def test_null_source_value_is_preserved_as_null_observation() -> None:
    """Distinguish a source null from zero and keep its raw field pointer."""
    decoded = _decode(_body([_row(net_profit=None)]))
    net_profit = next(
        item for item in decoded.observations if item.observation.item_code == "net_profit"
    )

    assert net_profit.observation.value is None
    assert net_profit.value_pointer == "/data/item/0/net_profit"


def test_missing_disclosure_date_quarantines_one_row_and_keeps_later_rows() -> None:
    """Retain the undated row reference while emitting the independently valid row."""
    undated = _row(
        fiscal_year=2023,
        period_end_ms=1703952000000,
        thscode="600519.SH",
        ticker="600519",
    )
    undated.pop("report_date_ms")
    valid = _row(thscode="600519.SH", ticker="600519")

    decoded = _decode(_body([undated, valid]))

    assert len(decoded.observations) == len(financial_statement_items("income"))
    assert {item.observation.instrument_id for item in decoded.observations} == {"600519.SH"}
    assert len(decoded.quarantined_rows) == 1
    assert decoded.quarantined_rows[0].row_pointer == "/data/item/0"
    assert decoded.quarantined_rows[0].disclosure_pointer is None
    assert decoded.quarantined_rows[0].body_sha256 == decoded.body_sha256
    assert decoded.quarantined_rows[0].reason == "disclosure_date_missing"


@pytest.mark.parametrize(
    ("disclosure", "reason"),
    [
        ("invalid", "disclosure_date_invalid"),
        (None, "disclosure_date_invalid"),
        (1735488000000, "disclosure_date_invalid"),
        (_DISCLOSURE_MS + 3_600_000, "disclosure_date_invalid"),
        (1767110400000, "disclosure_date_after_observed_at"),
    ],
)
def test_invalid_disclosure_values_quarantine_without_fabricating_dates(
    disclosure: object, reason: str
) -> None:
    """Keep malformed or not-yet-observed disclosure dates out of PIT rows."""
    decoded = _decode(_body([_row(report_date_ms=disclosure)]))

    assert decoded.observations == ()
    assert decoded.quarantined_rows[0].row_pointer == "/data/item/0"
    assert decoded.quarantined_rows[0].disclosure_pointer == "/data/item/0/report_date_ms"
    assert decoded.quarantined_rows[0].reason == reason


def test_malformed_fiscal_period_is_not_reclassified_as_disclosure_quarantine() -> None:
    """Fail the entire decode when report identity cannot be reconciled exactly."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(fiscal_period="Q3")]))

    assert raised.value.code == "row_period_invalid"
    assert raised.value.pointer == "/data/item/0"


@pytest.mark.parametrize(
    ("context", "expected_code"),
    [
        ({"statement_type": "unknown"}, "context_invalid"),
        ({"periodicity": "monthly"}, "context_invalid"),
        ({"expected_instrument_id": "600519"}, "context_invalid"),
    ],
)
def test_caller_context_rejects_unsupported_statement_market_identity_or_period(
    context: dict[str, str], expected_code: str
) -> None:
    """Reject invalid caller selectors before interpreting source rows."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row()]), **context)

    assert raised.value.code == expected_code


def test_source_disclosure_column_mapping_must_match_the_measured_row_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop before decoding if a local mapping silently renames the THS disclosure field."""

    class DriftedFinancialMapping:
        def item_columns(self, statement_type: str) -> tuple[str, ...]:
            return financial_statement_items(statement_type)

        def row_column(self, _field: str) -> str:
            return "announce_date"

    monkeypatch.setattr(
        "opendata.data.providers.ths.models.financial_observation_raw.require_pivot",
        lambda *_args: DriftedFinancialMapping(),
    )

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row()]))

    assert raised.value.code == "schema_drift"


def test_empty_or_wrong_type_observation_time_is_rejected() -> None:
    """Do not replace missing caller observation time with source dates or now."""
    with pytest.raises(FinancialObservationDecodeError) as empty_body:
        _decode(b"")
    assert empty_body.value.code == "body_empty"

    with pytest.raises(FinancialObservationDecodeError) as missing_time:
        _decode(_body([_row()]), observed_at=None)
    assert missing_time.value.code == "observed_at_invalid"


def test_envelope_timestamp_must_match_latest_row_report_period() -> None:
    """Enforce the measured Fuyao envelope timestamp meaning for financial windows."""
    body = _body([_row()]).replace(b'"timestamp":1735574400000', b'"timestamp":1735660800000')

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "envelope_invalid"
    assert raised.value.pointer == "/data/timestamp"


def test_identity_mismatch_fails_without_echoing_source_values() -> None:
    """Reject ticker/thscode disagreement with a stable, content-free error."""
    body = _body([_row(ticker="600520")])

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "row_identity_invalid"
    assert "600520" not in str(raised.value)
    assert "request" not in str(raised.value)


def test_response_cannot_substitute_another_requested_instrument() -> None:
    """Bind every returned row to the issuer requested by the caller."""
    body = _body([_row(thscode="000001.SZ", ticker="000001")])

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "row_identity_invalid"
    assert raised.value.pointer == "/data/item/0"


def test_source_request_id_rejects_unpaired_unicode_surrogates() -> None:
    """Reject JSON-escaped request IDs that cannot be encoded for UTF-8 storage."""
    body = _body([_row()]).replace(b'"shape-only-request"', b'"\\ud800"')

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "envelope_invalid"
    assert raised.value.pointer == "/request_id"


def test_unit_mapping_declaration_rejects_unpaired_surrogates() -> None:
    """Reject unpersistable caller version text without exposing it in the error."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(
            _body([_row()]),
            unit_mapping_version_declared=chr(0xD800),
        )

    assert raised.value.code == "unit_mapping_declaration_invalid"
    assert "D800" not in str(raised.value)


@pytest.mark.parametrize("currency", ["USD", "CNY ", "cny", "", " ", None, 1])
def test_currency_must_match_the_measured_a_share_cny_contract(currency: object) -> None:
    """Reject non-CNY values rather than treating arbitrary currency as metadata."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(currency=currency)]))

    assert raised.value.code == "row_currency_invalid"
    assert raised.value.pointer == "/data/item/0/currency"
    if isinstance(currency, str) and currency.strip():
        assert currency not in str(raised.value)


def test_duplicate_json_keys_are_rejected_before_envelope_selection() -> None:
    """Do not allow ambiguous duplicate root or row keys to choose a value."""
    body = _body([_row()]).replace(b'"code":0', b'"code":0,"code":0', 1)

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "json_duplicate_key"
    assert str(raised.value) == "json_duplicate_key"


def test_duplicate_json_keys_inside_rows_are_also_rejected() -> None:
    """Apply duplicate-key protection to source records, not only the envelope."""
    body = _body([_row()]).replace(
        b'"net_profit":86228150000',
        b'"net_profit":86228150000,"net_profit":7',
    )

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "json_duplicate_key"


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonfinite_json_numbers_are_rejected(token: bytes) -> None:
    """Reject Python JSON extensions instead of turning them into numeric values."""
    body = _body([_row()]).replace(b'"net_profit":86228150000', b'"net_profit":' + token)

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "json_nonfinite_number"
    assert token.decode("ascii") not in str(raised.value)


def test_malformed_json_and_overlong_fractional_token_fail_closed() -> None:
    """Bound both JSON syntax handling and Decimal token length before conversion."""
    with pytest.raises(FinancialObservationDecodeError) as malformed:
        _decode(b"{")
    assert malformed.value.code == "json_invalid"

    long_fraction = b"1." + b"2" * MAX_JSON_NUMBER_TOKEN_CHARS
    body = _body([_row()]).replace(b'"net_profit":86228150000', b'"net_profit":' + long_fraction)
    with pytest.raises(FinancialObservationDecodeError) as oversized_token:
        _decode(body)
    assert oversized_token.value.code == "json_number_too_long"


@pytest.mark.parametrize(
    ("body", "expected_code", "pointer"),
    [
        (b"[]", "envelope_invalid", None),
        (_response_body(item=[_row()], code="0"), "envelope_invalid", None),
        (_response_body(item=[_row()], code=7), "upstream_error", None),
        (_response_body(item=[_row()], message=7), "envelope_invalid", None),
        (_response_body(item=[_row()], request_id=None), "envelope_invalid", None),
        (
            _response_body(item=[_row()], timestamp="2024-12-31"),
            "envelope_invalid",
            "/data/timestamp",
        ),
        (_response_body(item={"not": "a row list"}), "envelope_invalid", "/data/item"),
        (_response_body(item=[None]), "row_invalid", "/data/item/0"),
    ],
)
def test_envelope_and_row_shape_failures_keep_stable_categories(
    body: bytes, expected_code: str, pointer: str | None
) -> None:
    """Reject malformed source contracts without echoing provider payload values."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == expected_code
    assert raised.value.pointer == pointer


def test_oversized_body_is_rejected_before_json_parsing() -> None:
    """Bound memory and parser work before decoding any supplied bytes."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(b" " * (MAX_BODY_BYTES + 1))

    assert raised.value.code == "body_too_large"


def test_number_tokens_are_bounded_before_decimal_construction() -> None:
    """Reject a huge exact token even though Decimal itself supports large exponents."""
    long_number = b"1" + b"0" * 128
    body = _body([_row()]).replace(b'"net_profit":86228150000', b'"net_profit":' + long_number)

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "json_number_too_long"


def test_body_requires_exact_bytes_and_utf8() -> None:
    """Accept only caller-owned UTF-8 bytes, never paths or parsed objects."""
    with pytest.raises(FinancialObservationDecodeError, match="body_type_invalid"):
        decode_financial_observation_body(
            bytearray(_body([_row()])),
            expected_instrument_id="600519.SH",
            statement_type="income",
            periodicity="annual",
            observed_at=_OBSERVED_AT,
            native_units=_native_units(),
            native_unit_mapping_version_declared=_UNIT_MAPPING_VERSION_DECLARED,
            native_unit_mapping_sha256_declared=_UNIT_MAPPING_SHA256_DECLARED,
        )
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(b"\xff")
    assert raised.value.code == "body_encoding_invalid"


def test_unit_declarations_must_cover_exactly_the_mapped_items() -> None:
    """Require the caller to supply a unit for every source item before emitting rows."""
    incomplete_units = _native_units()
    incomplete_units.pop("basic_eps")

    with pytest.raises(FinancialObservationDecodeError) as raised:
        decode_financial_observation_body(
            _body([_row()]),
            expected_instrument_id="600519.SH",
            statement_type="income",
            periodicity="annual",
            observed_at=_OBSERVED_AT,
            native_units=incomplete_units,
            native_unit_mapping_version_declared=_UNIT_MAPPING_VERSION_DECLARED,
            native_unit_mapping_sha256_declared=_UNIT_MAPPING_SHA256_DECLARED,
        )

    assert raised.value.code == "native_units_invalid"


def test_native_unit_rejects_unpaired_surrogates() -> None:
    """Reject unpersistable unit labels supplied by the caller."""
    units = _native_units()
    units["net_profit"] = chr(0xD800)

    with pytest.raises(FinancialObservationDecodeError) as raised:
        decode_financial_observation_body(
            _body([_row()]),
            expected_instrument_id="600519.SH",
            statement_type="income",
            periodicity="annual",
            observed_at=_OBSERVED_AT,
            native_units=units,
            native_unit_mapping_version_declared=_UNIT_MAPPING_VERSION_DECLARED,
            native_unit_mapping_sha256_declared=_UNIT_MAPPING_SHA256_DECLARED,
        )

    assert raised.value.code == "native_units_invalid"


@pytest.mark.parametrize("bad_unit", [None, ""])
def test_native_unit_values_must_be_nonblank_strings(bad_unit: object) -> None:
    """Reject missing or empty unit labels without inferring a fallback."""
    units = _native_units()
    units["net_profit"] = bad_unit  # type: ignore[assignment]

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row()]), native_units=units)

    assert raised.value.code == "native_units_invalid"


def test_native_unit_mapping_must_be_a_usable_mapping() -> None:
    """Reject non-mapping values and mapping implementations that fail on read."""
    with pytest.raises(FinancialObservationDecodeError) as wrong_type:
        decode_financial_observation_body(
            _body([_row()]),
            expected_instrument_id="600519.SH",
            statement_type="income",
            periodicity="annual",
            observed_at=_OBSERVED_AT,
            native_units=None,  # type: ignore[arg-type]
            native_unit_mapping_version_declared=_UNIT_MAPPING_VERSION_DECLARED,
            native_unit_mapping_sha256_declared=_UNIT_MAPPING_SHA256_DECLARED,
        )
    assert wrong_type.value.code == "native_units_invalid"

    class BrokenItems(dict[str, str]):
        def items(self):
            raise RuntimeError("unit map unavailable")

    with pytest.raises(FinancialObservationDecodeError) as broken_mapping:
        _decode(_body([_row()]), native_units=BrokenItems(_native_units()))
    assert broken_mapping.value.code == "native_units_invalid"


@pytest.mark.parametrize(
    ("version", "digest"),
    [("", _UNIT_MAPPING_SHA256_DECLARED), ("fixture-v1", "not-a-sha256")],
)
def test_unit_mapping_declarations_must_have_explicit_bounded_syntax(
    version: str, digest: str
) -> None:
    """Require explicit declarations but do not claim their provenance was verified."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(
            _body([_row()]),
            unit_mapping_version_declared=version,
            unit_mapping_sha256_declared=digest,
        )

    assert raised.value.code == "unit_mapping_declaration_invalid"


def test_unit_mapping_declaration_rejects_oversized_version_and_noncanonical_hash() -> None:
    """Bound caller declarations without claiming that their source was verified."""
    with pytest.raises(FinancialObservationDecodeError) as long_version:
        _decode(_body([_row()]), unit_mapping_version_declared="v" * 129)
    assert long_version.value.code == "unit_mapping_declaration_invalid"

    with pytest.raises(FinancialObservationDecodeError) as uppercase_hash:
        _decode(_body([_row()]), unit_mapping_sha256_declared="A" * 64)
    assert uppercase_hash.value.code == "unit_mapping_declaration_invalid"


def test_observation_time_is_mandatory_caller_context() -> None:
    """Require an aware retrieval time instead of substituting now or disclosure day."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        decode_financial_observation_body(
            _body([_row()]),
            expected_instrument_id="600519.SH",
            statement_type="income",
            periodicity="annual",
            observed_at=datetime(2025, 4, 18, 2, 30),
            native_units=_native_units(),
            native_unit_mapping_version_declared=_UNIT_MAPPING_VERSION_DECLARED,
            native_unit_mapping_sha256_declared=_UNIT_MAPPING_SHA256_DECLARED,
        )

    assert raised.value.code == "observed_at_invalid"


def test_duplicate_business_observation_keys_fail_closed() -> None:
    """Reject repeated rows rather than manufacture duplicate value observations."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(), _row()]))

    assert raised.value.code == "duplicate_observation_key"
    assert raised.value.pointer == "/data/item/1/operating_income"


def test_missing_mapped_source_item_fails_with_its_pointer() -> None:
    """Require every registered statement item in each wide source row."""
    row = _row()
    del row["net_profit"]

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([row]))

    assert raised.value.code == "row_field_missing"
    assert raised.value.pointer == "/data/item/0/net_profit"


def test_missing_required_row_metadata_has_a_field_pointer() -> None:
    """Reject an incomplete wide row at the absent required metadata field."""
    row = _row()
    del row["currency"]

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([row]))

    assert raised.value.code == "row_field_missing"
    assert raised.value.pointer == "/data/item/0/currency"


def test_row_field_and_scalar_limits_fail_before_mapping() -> None:
    """Bound row shape work and reject non-scalar source fields before melting."""
    oversized = _row()
    oversized.update({f"unmapped_{index}": index for index in range(256)})
    with pytest.raises(FinancialObservationDecodeError) as too_many_fields:
        _decode(_body([oversized]))
    assert too_many_fields.value.code == "row_invalid"

    with pytest.raises(FinancialObservationDecodeError) as nested_value:
        _decode(_body([_row(net_profit=[])]))
    assert nested_value.value.code == "row_invalid"


def test_boolean_financial_value_is_not_accepted_as_numeric() -> None:
    """Reject JSON booleans even though Python bool subclasses int."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(net_profit=True)]))

    assert raised.value.code == "row_value_invalid"
    assert raised.value.pointer == "/data/item/0/net_profit"


@pytest.mark.parametrize(
    ("overrides", "periodicity"),
    [
        ({"period": "quarterly"}, "annual"),
        ({"fiscal_year": "2024"}, "annual"),
        ({"fiscal_year": 10000}, "annual"),
        ({"fiscal_period": "H1"}, "annual"),
        ({"period_end_ms": "1735574400000"}, "annual"),
        ({"period_end_ms": 1703952000000}, "annual"),
    ],
)
def test_inconsistent_fiscal_identity_or_period_timestamp_fails_closed(
    overrides: dict[str, object], periodicity: str
) -> None:
    """Reconcile cadence, fiscal labels and the source's period-end timestamp."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(**overrides)]), periodicity=periodicity)

    assert raised.value.code == "row_period_invalid"


def test_valid_quarter_and_annual_label_on_quarterly_request() -> None:
    """Accept a correctly stamped quarter and reject an FY row in quarterly mode."""
    quarter_row = _row(
        period="quarterly",
        fiscal_period="Q1",
        period_end_ms=1711814400000,
    )
    quarter = _decode(_body([quarter_row], timestamp=1711814400000), periodicity="quarterly")
    assert {item.observation.report_period.isoformat() for item in quarter.observations} == {
        "2024-03-31"
    }

    with pytest.raises(FinancialObservationDecodeError) as annual_row:
        _decode(
            _body([_row(period="quarterly")]),
            periodicity="quarterly",
        )
    assert annual_row.value.code == "row_period_invalid"


def test_data_envelope_requires_timestamp_and_item_keys() -> None:
    """Reject a data object missing one of the measured envelope members."""
    body = json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "shape-only-request",
            "data": {"item": [_row()]},
        }
    ).encode()

    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(body)

    assert raised.value.code == "envelope_invalid"


def test_row_count_and_envelope_shapes_are_bounded_and_strict() -> None:
    """Reject unsupported envelopes and response sizes with stable error categories."""
    too_many_rows = _body([_row() for _ in range(MAX_FINANCIAL_ROWS + 1)])
    with pytest.raises(FinancialObservationDecodeError) as row_count_error:
        _decode(too_many_rows)
    assert row_count_error.value.code == "rows_too_many"

    wrong_shape = json.dumps({"code": 0, "data": {"item": []}}).encode()
    with pytest.raises(FinancialObservationDecodeError) as envelope_error:
        _decode(wrong_shape)
    assert envelope_error.value.code == "envelope_invalid"

    with pytest.raises(FinancialObservationDecodeError) as empty_error:
        _decode(_body([]))
    assert empty_error.value.code == "rows_empty"
    assert empty_error.value.pointer == "/data/item"


def test_source_pointer_and_body_digest_are_stable_for_each_line_item() -> None:
    """Bind every line to the exact caller-supplied body and JSON field path."""
    raw_body = _body([_row()])
    decoded = _decode(raw_body)

    assert decoded.body_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert {entry.body_sha256 for entry in decoded.observations} == {decoded.body_sha256}
    assert {entry.row_pointer for entry in decoded.observations} == {"/data/item/0"}
    assert {entry.value_pointer.rsplit("/", 1)[-1] for entry in decoded.observations} == set(
        financial_statement_items("income")
    )


@pytest.mark.parametrize("non_scalar", [[], {}, [1]])
def test_nested_row_values_are_rejected_instead_of_silently_dropped(
    non_scalar: object,
) -> None:
    """Reject shape drift in a row before metadata can be partially retained."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(unexpected=non_scalar)]))

    assert raised.value.code == "row_invalid"


def test_unknown_scalar_row_field_fails_closed_as_schema_drift() -> None:
    """Do not silently treat newly added scalar source columns as metadata."""
    with pytest.raises(FinancialObservationDecodeError) as raised:
        _decode(_body([_row(unmapped_financial_line=9.25)]))

    assert raised.value.code == "schema_drift"
    assert raised.value.pointer == "/data/item/0"
