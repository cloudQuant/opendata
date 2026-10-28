"""Contract tests for the versioned prepared financial capture JSON codec."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

import opendata.pipeline.financial_observation_store as store
import opendata.pipeline.financial_prepared_codec as codec
from opendata.pipeline.financial_observation_store import PreparedCapture
from opendata.pipeline.financial_prepared_codec import (
    FinancialPreparedCaptureCodecError,
    decode_prepared_capture,
    encode_prepared_capture,
)


def _prepared_capture(
    *,
    observation_count: int = 2,
    links_per_observation: int = 1,
    quarantine_count: int = 1,
    source_metadata_json: str | None = None,
) -> PreparedCapture:
    """Create a bounded in-memory DTO that satisfies the store validator."""
    capture_id = "a" * 32
    observed_at = datetime(2025, 4, 1, 1, 2, 3, 456789)
    metadata_json = source_metadata_json or json.dumps(
        {"currency": "CNY", "issuer": "贵州茅台"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    capture_values: dict[str, object] = {
        "capture_id": capture_id,
        "provider_id": "ths",
        "dataset_id": "ths_financial_statement",
        "operation_id": "income_annual",
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "observed_at_utc": observed_at,
        "captured_at_utc": datetime(2025, 4, 1, 1, 2, 4, 1),
        "source_request_id": "provider-request-001",
        "http_status": 200,
        "request_summary_sha256": "b" * 64,
        "raw_object_sha256": "c" * 64,
        "raw_object_size_bytes": 1024,
        "raw_object_key": f"cc/{'c' * 64}.blob",
        "raw_manifest_sha256": "d" * 64,
        "raw_manifest_size_bytes": 2048,
        "raw_manifest_key": f"dd/{'d' * 64}.json",
        "retention_class": "restricted",
        "source_history_limit": "unknown",
        "native_unit_mapping_version_declared": "operator-units-v1",
        "native_unit_mapping_sha256_declared": "e" * 64,
        "unit_mapping_declaration_status": "operator_declared",
        "decode_status": "decoded",
    }
    observations: list[tuple[str, dict[str, object]]] = []
    links: dict[str, tuple[dict[str, object], ...]] = {}
    for index in range(observation_count):
        observation_id = f"{index + 1:064x}"
        pointer = f"/data/item/{index // 16}/item_{index}"
        observations.append(
            (
                observation_id,
                {
                    "observation_id": observation_id,
                    "natural_key_sha256": "f" * 64,
                    "content_sha256": "1" * 64,
                    "source_id": "ths_fuyao",
                    "instrument_id": "600519.SH",
                    "statement_type": "income",
                    "periodicity": "annual",
                    "report_period": date(2024, 12, 31),
                    "source_disclosure_date": date(2025, 3, 31),
                    "observed_at_utc": observed_at,
                    "item_code": f"item_{index}",
                    "value_decimal_text": (
                        "12345678901234567890.123456789012345678" if index == 0 else None
                    ),
                    "native_unit": "CNY/股·原值",
                    "source_metadata_json": metadata_json,
                    "observation_policy": "current_as_observed_only",
                },
            )
        )
        links[observation_id] = tuple(
            {
                "link_id": f"{index * links_per_observation + link_index + 1:064x}",
                "capture_id": capture_id,
                "value_pointer": (
                    pointer if links_per_observation == 1 else f"{pointer}/value_{link_index}"
                ),
                "observation_id": observation_id,
                "row_pointer": f"/data/item/{index // 16}",
            }
            for link_index in range(links_per_observation)
        )
    rows = store._PreparedRows(
        capture=capture_values,
        capture_id=capture_id,
        observations=tuple(observations),
        links_by_observation=links,
        quarantine=tuple(
            {
                "quarantine_id": f"{index + 2:064x}",
                "capture_id": capture_id,
                "row_pointer": f"/data/item/quarantine_{index}",
                "disclosure_pointer": None,
                "reason": "disclosure_date_missing",
            }
            for index in range(quarantine_count)
        ),
    )
    return store._freeze_prepared_rows(rows)


def _canonical_json(document: object) -> bytes:
    """Serialize a deliberately modified document in canonical test form."""
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unsafe_capture_copy(prepared: PreparedCapture, **changes: object) -> PreparedCapture:
    """Copy a frozen DTO while replacing selected tuple fields for tests."""
    copied = object.__new__(PreparedCapture)
    for name in PreparedCapture.__dataclass_fields__:
        object.__setattr__(copied, name, changes.get(name, getattr(prepared, name)))
    return copied


def _change_row_scalar(
    document: dict[str, object],
    *,
    section: str,
    row_index: int,
    column_name: str,
    value: object,
) -> None:
    """Change one tagged scalar in a parsed envelope for fail-closed tests."""
    rows = document[section]
    if section == "capture_values":
        row = rows
    else:
        assert type(rows) is list
        entry = rows[row_index]
        assert type(entry) is list
        row = entry[1] if section == "observation_values" else entry
    _change_encoded_row_scalar(row, column_name, value)


def _change_encoded_row_scalar(row: object, column_name: str, value: object) -> None:
    """Change a scalar in the order-preserving array-of-pairs row encoding."""
    assert type(row) is list
    for entry in row:
        assert type(entry) is list and len(entry) == 2
        if entry[0] == column_name:
            entry[1] = value
            return
    raise AssertionError("test fixture column was not found")


def test_round_trip_preserves_exact_dates_values_units_and_utc_microseconds() -> None:
    """Preserve SQL scalars and their exact source-native string content."""
    prepared = _prepared_capture()

    payload = encode_prepared_capture(prepared)
    decoded = decode_prepared_capture(payload)
    rows = store._materialize_prepared_capture(decoded)

    assert decoded == prepared
    assert encode_prepared_capture(decoded) == payload
    assert rows.capture["observed_at_utc"] == datetime(2025, 4, 1, 1, 2, 3, 456789)
    observation = rows.observations[0][1]
    assert observation["report_period"] == date(2024, 12, 31)
    assert observation["source_disclosure_date"] == date(2025, 3, 31)
    assert observation["value_decimal_text"] == "12345678901234567890.123456789012345678"
    assert observation["native_unit"] == "CNY/股·原值"
    assert observation["source_metadata_json"] == json.dumps(
        {"currency": "CNY", "issuer": "贵州茅台"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert rows.quarantine[0]["disclosure_pointer"] is None
    document = json.loads(payload)
    assert document["codec"] == "opendata.financial_prepared_capture"
    assert document["version"] == 1
    assert payload == _canonical_json(document)


def test_round_trip_preserves_shuffled_valid_tuple_order() -> None:
    """Preserve store-valid row, observation, link, and quarantine ordering."""
    prepared = _prepared_capture(
        observation_count=3,
        links_per_observation=2,
        quarantine_count=2,
    )
    shuffled = _unsafe_capture_copy(
        prepared,
        _capture_values=tuple(reversed(prepared._capture_values)),
        _observation_values=tuple(
            (observation_id, tuple(reversed(row)))
            for observation_id, row in reversed(prepared._observation_values)
        ),
        _links_by_observation=tuple(
            (
                observation_id,
                tuple(tuple(reversed(row)) for row in reversed(rows)),
            )
            for observation_id, rows in reversed(prepared._links_by_observation)
        ),
        _quarantine_values=tuple(
            tuple(reversed(row)) for row in reversed(prepared._quarantine_values)
        ),
    )

    payload = encode_prepared_capture(shuffled)

    assert decode_prepared_capture(payload) == shuffled


def test_large_store_valid_capture_round_trips_within_structural_budget() -> None:
    """Exercise the store's maximum observation/link cardinalities in memory."""
    prepared = _prepared_capture(observation_count=1024)

    payload = encode_prepared_capture(prepared)

    assert len(payload) <= codec._MAX_CODEC_BYTES
    assert decode_prepared_capture(payload) == prepared


def test_json_preflight_ignores_structural_characters_inside_escaped_strings() -> None:
    """Do not count quoted brackets, commas, or escaped quotes as structure."""
    quoted = json.dumps('text ] } , [ { quote=" escaped=\\ \\"', ensure_ascii=False)
    document = f"[{quoted}]"

    codec._preflight_json_structure(document)


def test_json_preflight_accepts_exact_container_and_total_entry_limits() -> None:
    """Accept the exact configured structural budgets before the JSON parser."""
    nested_at_depth_limit = "[" * codec._MAX_JSON_NESTING + "null" + "]" * (codec._MAX_JSON_NESTING)
    codec._preflight_json_structure(nested_at_depth_limit)

    container_at_limit = "[" + ",".join(["null"] * codec._MAX_JSON_CONTAINER_ENTRIES) + "]"
    codec._preflight_json_structure(container_at_limit)

    item_counts = [codec._MAX_JSON_CONTAINER_ENTRIES] * 68 + [667]
    nested_at_limit = (
        "[" + ",".join("[" + ",".join(["null"] * count) + "]" for count in item_counts) + "]"
    )
    assert sum(item_counts) + len(item_counts) == codec._MAX_JSON_TOTAL_ENTRIES
    codec._preflight_json_structure(nested_at_limit)


def test_json_preflight_rejects_entry_and_nesting_overflow_before_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject excessive structure in one scan without parsing or echoing input."""
    container_over_limit = "[" + ",".join(["null"] * (codec._MAX_JSON_CONTAINER_ENTRIES + 1)) + "]"
    with pytest.raises(FinancialPreparedCaptureCodecError, match="container"):
        codec._preflight_json_structure(container_over_limit)

    nested_over_limit = (
        "[" * (codec._MAX_JSON_NESTING + 1) + "null" + "]" * (codec._MAX_JSON_NESTING + 1)
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="nesting"):
        codec._preflight_json_structure(nested_over_limit)

    item_counts = [codec._MAX_JSON_CONTAINER_ENTRIES] * 68 + [668]
    oversized_document = (
        "["
        + ",".join(
            "["
            + ",".join(
                [json.dumps("private-sentinel")] + ["null"] * (count - 1)
                if index == 0
                else ["null"] * count
            )
            + "]"
            for index, count in enumerate(item_counts)
        )
        + "]"
    )
    assert sum(item_counts) + len(item_counts) == codec._MAX_JSON_TOTAL_ENTRIES + 1

    def fail_if_json_parser_runs(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("JSON parser must not run after structural overflow")

    monkeypatch.setattr(codec.json, "loads", fail_if_json_parser_runs)
    with pytest.raises(FinancialPreparedCaptureCodecError, match="total entry") as error:
        decode_prepared_capture(oversized_document.encode("utf-8"))
    assert "private-sentinel" not in str(error.value)


def test_canonical_encoder_accepts_exact_byte_limit_and_rejects_one_over() -> None:
    """Enforce the byte boundary without building an unbounded JSON buffer."""
    empty_document = codec._canonical_json_bytes({"value": ""})
    exact_value = "x" * (codec._MAX_CODEC_BYTES - len(empty_document))
    exact_payload = codec._canonical_json_bytes({"value": exact_value})
    assert len(exact_payload) == codec._MAX_CODEC_BYTES
    del exact_payload, exact_value
    over_limit_value = "x" * (codec._MAX_CODEC_BYTES - len(empty_document) + 1)

    with pytest.raises(FinancialPreparedCaptureCodecError, match="16 MiB"):
        codec._canonical_json_bytes({"value": over_limit_value})


@pytest.mark.parametrize(
    "invalid_payload",
    [None, bytearray(b"{}"), memoryview(b"{}"), b"", b"\xff"],
)
def test_decoder_rejects_non_bytes_empty_and_invalid_utf8(invalid_payload: object) -> None:
    """Reject payloads outside the exact byte input contract."""
    with pytest.raises(FinancialPreparedCaptureCodecError):
        decode_prepared_capture(invalid_payload)


def test_decoder_rejects_payload_over_the_runner_byte_limit() -> None:
    """Reject an oversized byte string before attempting JSON parsing."""
    with pytest.raises(FinancialPreparedCaptureCodecError, match="size"):
        decode_prepared_capture(b" " * (16 * 1024 * 1024 + 1))


def test_encoder_rejects_a_store_valid_capture_over_the_runner_byte_limit() -> None:
    """Fail closed when repeated large source metadata exceeds the runner cap."""
    large_metadata_json = json.dumps(
        {"quoted": '"' * 32_760},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(large_metadata_json.encode("utf-8")) <= 65_536
    prepared = _prepared_capture(
        observation_count=512,
        source_metadata_json=large_metadata_json,
    )

    with pytest.raises(FinancialPreparedCaptureCodecError, match="16 MiB"):
        encode_prepared_capture(prepared)


def test_encoder_requires_the_exact_store_dto_and_store_valid_values() -> None:
    """Reject subclasses and malformed row scalars before serialization."""

    class PreparedCaptureChild(PreparedCapture):
        """Untrusted subtype used to verify exact DTO identity enforcement."""

    prepared = _prepared_capture()
    child = object.__new__(PreparedCaptureChild)
    for name in PreparedCapture.__dataclass_fields__:
        object.__setattr__(child, name, getattr(prepared, name))
    with pytest.raises(FinancialPreparedCaptureCodecError, match="exact type"):
        encode_prepared_capture(child)

    bad_capture_values = tuple(
        (name, True if name == "http_status" else value) for name, value in prepared._capture_values
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="store validation"):
        encode_prepared_capture(_unsafe_capture_copy(prepared, _capture_values=bad_capture_values))


def test_decoder_rejects_duplicate_keys_unknown_fields_and_unknown_versions() -> None:
    """Reject ambiguous JSON objects and schema evolution without a version."""
    payload = encode_prepared_capture(_prepared_capture())
    duplicate_codec = b'{"codec":"opendata.financial_prepared_capture",' + payload[1:]
    with pytest.raises(FinancialPreparedCaptureCodecError, match="duplicate JSON key"):
        decode_prepared_capture(duplicate_codec)

    document = json.loads(payload)
    document["credential"] = "unrecognized"
    with pytest.raises(FinancialPreparedCaptureCodecError, match="unknown or missing fields"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(payload)
    document["version"] = 2
    with pytest.raises(FinancialPreparedCaptureCodecError, match="unsupported"):
        decode_prepared_capture(_canonical_json(document))


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (b'"version":1.0', "floats are unsupported"),
        (b'"version":NaN', "non-finite"),
        (b'"version":Infinity', "non-finite"),
        (b'"version":-Infinity', "non-finite"),
    ],
)
def test_decoder_rejects_float_and_nonfinite_json_tokens(replacement: bytes, message: str) -> None:
    """Reject floating tokens and JavaScript-style non-finite values."""
    payload = encode_prepared_capture(_prepared_capture())
    changed = payload.replace(b'"version":1', replacement, 1)

    with pytest.raises(FinancialPreparedCaptureCodecError, match=message):
        decode_prepared_capture(changed)


def test_decoder_rejects_noncanonical_whitespace_escape_and_integer_forms() -> None:
    """Require byte-for-byte canonical JSON, not merely parseable JSON."""
    payload = encode_prepared_capture(_prepared_capture())
    for changed in (payload + b" ", payload.replace(b"CNY", b"\\u0043NY", 1)):
        with pytest.raises(FinancialPreparedCaptureCodecError, match="canonical"):
            decode_prepared_capture(changed)

    document = json.loads(payload)
    document["sql_statement_ceiling"] = 10**21
    with pytest.raises(FinancialPreparedCaptureCodecError, match="integer token"):
        decode_prepared_capture(_canonical_json(document))


def test_decoder_rejects_unknown_scalar_tags_fields_and_boolean_integer() -> None:
    """Keep scalar discriminators closed and prevent bool-to-int coercion."""
    payload = encode_prepared_capture(_prepared_capture())
    document = json.loads(payload)

    _change_row_scalar(
        document,
        section="capture_values",
        row_index=0,
        column_name="http_status",
        value={"kind": "integer", "value": True},
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="tag or value type"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(payload)
    _change_row_scalar(
        document,
        section="capture_values",
        row_index=0,
        column_name="capture_id",
        value={"kind": "string", "value": "a" * 32, "extra": "unknown"},
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="unknown or missing fields"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(payload)
    _change_row_scalar(
        document,
        section="capture_values",
        row_index=0,
        column_name="provider_id",
        value={"kind": "unknown", "value": "ths"},
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="tag or value type"):
        decode_prepared_capture(_canonical_json(document))


@pytest.mark.parametrize(
    ("column", "kind", "value", "message"),
    [
        ("report_period", "date", "2024-1-1", "date scalar is invalid"),
        ("observed_at_utc", "datetime_utc", "2025-04-01T01:02:03.456789", "marker"),
        (
            "observed_at_utc",
            "datetime_utc",
            "2025-04-01T01:02:03.456789+00:00Z",
            "exact canonical UTC",
        ),
        (
            "observed_at_utc",
            "datetime_utc",
            "2025-04-01T01:02:03.45Z",
            "exact canonical UTC",
        ),
    ],
)
def test_decoder_rejects_noncanonical_date_and_utc_values(
    column: str, kind: str, value: str, message: str
) -> None:
    """Reject date and timestamp spellings that could normalize on parsing."""
    document = json.loads(encode_prepared_capture(_prepared_capture()))
    _change_row_scalar(
        document,
        section="observation_values",
        row_index=0,
        column_name=column,
        value={"kind": kind, "value": value},
    )

    with pytest.raises(FinancialPreparedCaptureCodecError, match=message):
        decode_prepared_capture(_canonical_json(document))


def test_decoder_delegates_cross_row_semantics_to_the_store_validator() -> None:
    """Reject inconsistent link references using the existing store contract."""
    document = json.loads(encode_prepared_capture(_prepared_capture()))
    link_row = document["links_by_observation"][0][1][0]
    _change_encoded_row_scalar(
        link_row,
        "capture_id",
        {"kind": "string", "value": "b" * 32},
    )

    with pytest.raises(FinancialPreparedCaptureCodecError, match="store validation"):
        decode_prepared_capture(_canonical_json(document))


def test_decoder_rejects_unknown_sql_columns() -> None:
    """Do not accept fields outside the existing fixed database tables."""
    document = json.loads(encode_prepared_capture(_prepared_capture()))
    document["capture_values"].append(["unknown_column", {"kind": "string", "value": "unexpected"}])

    with pytest.raises(FinancialPreparedCaptureCodecError, match="store validation"):
        decode_prepared_capture(_canonical_json(document))


def test_preflight_handles_root_strings_and_rejects_unbalanced_tokens() -> None:
    """Keep root scalar scanning safe and reject mismatched or incomplete JSON."""
    codec._preflight_json_structure('"root string"')
    for text in (
        "]",
        "[}",
        "[1",
    ):
        with pytest.raises(FinancialPreparedCaptureCodecError, match="structure"):
            codec._preflight_json_structure(text)


@pytest.mark.parametrize("payload", [b"{}{}", b"1,2"])
def test_decoder_sanitizes_json_parser_errors(payload: bytes) -> None:
    """Let the JSON parser reject grammar errors the structural scan permits."""
    with pytest.raises(FinancialPreparedCaptureCodecError, match="JSON is invalid") as error:
        decode_prepared_capture(payload)
    assert payload.decode("ascii") not in str(error.value)


def test_decoder_sanitizes_unexpected_internal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not expose unexpected materialization error details to callers."""
    payload = encode_prepared_capture(_prepared_capture())

    def fail_decode(_document: object) -> PreparedCapture:
        raise AttributeError("private-sentinel")

    monkeypatch.setattr(codec, "_decode_document", fail_decode)
    with pytest.raises(FinancialPreparedCaptureCodecError, match="store contract") as error:
        decode_prepared_capture(payload)
    assert "private-sentinel" not in str(error.value)


def test_decoder_rejects_unknown_codec_and_invalid_envelope_field_types() -> None:
    """Keep the codec identifier and scalar envelope fields closed."""
    document = json.loads(encode_prepared_capture(_prepared_capture()))
    document["codec"] = "unknown.codec"
    with pytest.raises(FinancialPreparedCaptureCodecError, match="identifier is unknown"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(encode_prepared_capture(_prepared_capture()))
    document["capture_id"] = 17
    with pytest.raises(FinancialPreparedCaptureCodecError, match="field types are invalid"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(encode_prepared_capture(_prepared_capture()))
    document["sql_statement_ceiling"] = True
    with pytest.raises(FinancialPreparedCaptureCodecError, match="field types are invalid"):
        decode_prepared_capture(_canonical_json(document))


@pytest.mark.parametrize(
    ("section", "replacement", "message"),
    [
        ("observation_values", {}, "prepared observation rows are invalid"),
        ("observation_values", [[17, []]], "prepared observation entry is invalid"),
        ("links_by_observation", {}, "prepared link groups are invalid"),
        ("links_by_observation", [["observation", {}]], "prepared link group is invalid"),
        ("quarantine_values", {}, "prepared SQL rows are invalid"),
        ("capture_values", {}, "prepared SQL row is invalid"),
        ("capture_values", [["column"]], "prepared SQL row entry is invalid"),
    ],
)
def test_decoder_rejects_invalid_collection_and_row_shapes(
    section: str, replacement: object, message: str
) -> None:
    """Reject malformed DTO collection shapes before store materialization."""
    document = json.loads(encode_prepared_capture(_prepared_capture()))
    document[section] = replacement

    with pytest.raises(FinancialPreparedCaptureCodecError, match=message):
        decode_prepared_capture(_canonical_json(document))


def test_decoder_rejects_duplicate_row_columns_and_malformed_scalar_tags() -> None:
    """Reject duplicate SQL columns and malformed null/scalar tag shapes."""
    document = json.loads(encode_prepared_capture(_prepared_capture()))
    document["capture_values"].append(document["capture_values"][0])
    with pytest.raises(FinancialPreparedCaptureCodecError, match="duplicate column"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(encode_prepared_capture(_prepared_capture()))
    _change_row_scalar(
        document,
        section="capture_values",
        row_index=0,
        column_name="provider_id",
        value="not-a-tag",
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="scalar tag is invalid"):
        decode_prepared_capture(_canonical_json(document))

    document = json.loads(encode_prepared_capture(_prepared_capture()))
    _change_row_scalar(
        document,
        section="observation_values",
        row_index=0,
        column_name="value_decimal_text",
        value={"kind": "null", "value": None},
    )
    with pytest.raises(FinancialPreparedCaptureCodecError, match="null scalar has unknown fields"):
        decode_prepared_capture(_canonical_json(document))


def test_encoder_rejects_unsupported_datetime_and_scalar_exact_types() -> None:
    """Keep private scalar encoding strict even when called outside the DTO path."""
    with pytest.raises(FinancialPreparedCaptureCodecError, match="naive UTC"):
        codec._encode_scalar(datetime(2025, 1, 1, tzinfo=timezone.utc))

    with pytest.raises(FinancialPreparedCaptureCodecError, match="unsupported exact type"):
        codec._encode_scalar(object())


def test_decoder_rejects_noncanonical_date_and_unparseable_utc_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject parser-normalized dates and timestamps that cannot be parsed."""

    class NormalizingDateParser:
        """Model a permissive date parser to exercise canonical spelling checks."""

        @staticmethod
        def fromisoformat(_value: str) -> date:
            return date(2024, 1, 1)

    with monkeypatch.context() as patcher:
        patcher.setattr(codec, "date", NormalizingDateParser)
        with pytest.raises(
            FinancialPreparedCaptureCodecError, match="date scalar is not canonical"
        ):
            codec._decode_scalar({"kind": "date", "value": "noncanonical"})

    document = json.loads(encode_prepared_capture(_prepared_capture()))
    _change_row_scalar(
        document,
        section="observation_values",
        row_index=0,
        column_name="observed_at_utc",
        value={"kind": "datetime_utc", "value": "not-a-datetimeZ"},
    )
    with pytest.raises(
        FinancialPreparedCaptureCodecError, match="prepared UTC datetime is invalid"
    ):
        decode_prepared_capture(_canonical_json(document))


def test_canonical_encoder_rejects_non_utf8_string_values() -> None:
    """Fail closed on Python strings that cannot be encoded as strict UTF-8."""
    with pytest.raises(FinancialPreparedCaptureCodecError, match="canonical JSON"):
        codec._canonical_json_bytes({"value": chr(0xD800)})
