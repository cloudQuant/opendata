"""Synthetic contracts for the reversible Sina LRB publication boundary."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import cast

import pytest

from opendata.data.providers.sina import lrb_statement_publication as publication
from opendata.data.providers.sina.lrb_statement_parser import (
    SinaLrbRawArray,
    SinaLrbRawObject,
    SinaLrbRawReport,
    SinaLrbRawResponse,
    SinaLrbRawValue,
    parse_sina_lrb_response,
)
from opendata.data.providers.sina.lrb_statement_process import (
    SINA_LRB_PROCESS_OPERATION_ID,
    sina_lrb_request_summary_sha256,
)
from opendata.data.providers.sina.lrb_statement_publication import (
    SINA_LRB_DATASET_ID,
    SINA_LRB_RESPONSE_ENCODING,
    SinaLrbPublicationError,
    bind_sina_lrb_raw_provenance,
    build_sina_lrb_observation_contract,
    reconstruct_sina_lrb_source_document,
    serialize_sina_lrb_response,
)
from opendata.pipeline.raw_evidence import RawResponseManifest, RawResponseSource
from opendata.pipeline.raw_evidence_reader import RawEvidenceReadResult
from opendata.pipeline.run_service import NormalizedBatch, NormalizedRecord

_CAPTURE_ID = "a" * 64
_RETRIEVED_AT = datetime(2026, 10, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)
_RAW_SOURCE = RawResponseSource(
    provider_id="sina",
    dataset_id="sina_lrb_report_observations",
    operation_id=SINA_LRB_PROCESS_OPERATION_ID,
)


def _raw_evidence(
    body: bytes,
    *,
    manifest_updates: dict[str, object] | None = None,
    manifest_sha256: str = "b" * 64,
) -> RawEvidenceReadResult:
    """Construct a synthetic reader result for isolated binder boundary checks."""
    manifest = RawResponseManifest(
        source=_RAW_SOURCE,
        request_summary_sha256=sina_lrb_request_summary_sha256("600519.SH"),
        captured_at_utc=_RETRIEVED_AT + timedelta(seconds=1),
        http_status=200,
        response_sha256=hashlib.sha256(body).hexdigest(),
        response_size_bytes=len(body),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    if manifest_updates:
        manifest = manifest.model_copy(update=manifest_updates)
    return RawEvidenceReadResult(
        body=body,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )


def _body() -> bytes:
    """Return a fixture with independent ordering, extensions, null, and decimals."""
    return (
        b'{"result":{"status":{"code":0,"message":"ok"},"data":'
        b'{"report_count":"2","report_date":['
        b'{"date_value":"20231231","label":"FY","nullable":null},'
        b'{"date_value":"20240630","ratio":1.2300e+02,"negative_zero":-0.00}],'
        b'"report_list":{"20240630":{"data":[{"item_title":"revenue",'
        b'"item_value":1.2300e+02,"empty":null,"signed_zero":-0.00,'
        b'"wire_negative_zero":-0}],"extra":true},'
        b'"20231231":{"data":[{"item_title":"cost","item_value":-4.50}],'
        b'"unknown_field":{"nested":["x",null]}}},"extension":{"decimal":1e-5}}},'
        b'"top_extension":[false,"retained"]}'
    )


def _batch(body: bytes | None = None):
    """Serialize the synthetic response with fixed capture metadata."""
    return serialize_sina_lrb_response(
        _body() if body is None else body,
        instrument_id="600519.SH",
        retrieved_at_utc=_RETRIEVED_AT,
        capture_id=_CAPTURE_ID,
    )


def test_raw_provenance_binds_verified_manifest_body_request_and_run() -> None:
    body = _body()
    batch = _batch(body)
    response_sha256 = hashlib.sha256(body).hexdigest()
    request_digest = sina_lrb_request_summary_sha256("600519.SH")
    evidence = _raw_evidence(body)

    bound = bind_sina_lrb_raw_provenance(
        batch,
        evidence=evidence,
        instrument_id="600519.SH",
        run_key=_CAPTURE_ID,
        source_access_ref="source-auth:synthetic-grant-1",
        raw_retention_ref="raw-retention:synthetic-grant-1",
        retrieved_at_utc=_RETRIEVED_AT,
    )

    assert batch.provenance["raw_response_persisted"] is False
    assert bound.provenance["raw_response_persisted"] is True
    assert bound.provenance["run_key"] == _CAPTURE_ID
    assert bound.provenance["raw_manifest_sha256"] == evidence.manifest_sha256
    assert bound.provenance["request_summary_sha256"] == request_digest
    assert bound.provenance["response_sha256"] == response_sha256
    assert bound.provenance["source_access_ref"] == "source-auth:synthetic-grant-1"
    assert bound.provenance["raw_retention_ref"] == "raw-retention:synthetic-grant-1"
    assert all(
        row.payload["capture_id"] == _CAPTURE_ID
        and row.payload["response_sha256"] == response_sha256
        for row in bound.records
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"manifest_updates": {"request_summary_sha256": "c" * 64}},
        {"manifest_updates": {"response_sha256": "d" * 64}},
        {"manifest_updates": {"response_size_bytes": len(_body()) + 1}},
        {"manifest_updates": {"http_status": 201}},
        {
            "manifest_updates": {
                "source": RawResponseSource(
                    provider_id="other",
                    dataset_id="sina_lrb_report_observations",
                    operation_id=SINA_LRB_PROCESS_OPERATION_ID,
                )
            }
        },
        {"manifest_sha256": "invalid"},
        {"evidence_body": b"tampered body"},
        {"run_key": "e" * 64},
        {"instrument_id": "000001.SZ"},
        {"source_access_ref": "source-auth:sk-example-0123456789abcdef"},
        {"manifest_updates": {"captured_at_utc": _RETRIEVED_AT - timedelta(seconds=1)}},
        {"retrieved_at_utc": _RETRIEVED_AT + timedelta(seconds=1)},
    ],
)
def test_raw_provenance_rejects_hash_identity_and_time_mismatches(
    changes: dict[str, object],
) -> None:
    body = _body()
    manifest_updates = changes.get("manifest_updates", {})
    assert isinstance(manifest_updates, dict)
    manifest_sha256 = changes.get("manifest_sha256", "b" * 64)
    assert isinstance(manifest_sha256, str)
    evidence = _raw_evidence(
        body,
        manifest_updates=manifest_updates,
        manifest_sha256=manifest_sha256,
    )
    if "evidence_body" in changes:
        assert type(changes["evidence_body"]) is bytes
        evidence = replace(evidence, body=changes["evidence_body"])
    arguments: dict[str, object] = {
        "evidence": evidence,
        "instrument_id": "600519.SH",
        "run_key": _CAPTURE_ID,
        "source_access_ref": "source-auth:synthetic-grant-1",
        "raw_retention_ref": "raw-retention:synthetic-grant-1",
        "retrieved_at_utc": _RETRIEVED_AT,
        **{
            key: value
            for key, value in changes.items()
            if key not in {"manifest_updates", "manifest_sha256", "evidence_body"}
        },
    }

    with pytest.raises(SinaLrbPublicationError) as raised:
        bind_sina_lrb_raw_provenance(_batch(body), **arguments)  # type: ignore[arg-type]

    assert raised.value.code == "raw_evidence_mismatch"


def _payloads() -> list[dict[str, object]]:
    """Return mutable deep copies of one valid full capture."""
    return [deepcopy(record.payload) for record in _batch().records]


def _raw_report(key: str, ordinal: int = 0) -> SinaLrbRawReport:
    """Build the minimum parser DTO needed by the isolated association guard."""
    fields = SinaLrbRawObject(fields=(("data", SinaLrbRawArray(items=())),))
    return SinaLrbRawReport(
        report_list_key=key,
        source_ordinal=ordinal,
        source_fields=fields,
        rows=(),
    )


def _date_entry(value: object) -> SinaLrbRawObject:
    """Build one raw date object for admission-boundary tests."""
    return SinaLrbRawObject(fields=(("date_value", cast("SinaLrbRawValue", value)),))


def _node_table(nodes: list[dict[str, object]], root_id: int = 0) -> dict[str, object]:
    """Create one node table using the publication's frozen encoding label."""
    return {
        "encoding": SINA_LRB_RESPONSE_ENCODING,
        "root_id": root_id,
        "nodes": nodes,
    }


def _normalized_batch(payloads: list[dict[str, object]]) -> NormalizedBatch:
    """Create a small in-memory batch for focused preflight-boundary tests."""
    records = tuple(
        NormalizedRecord(
            business_key=f"row-{index}",
            payload=payload,
            observed_at=_RETRIEVED_AT,
        )
        for index, payload in enumerate(payloads)
    )
    return NormalizedBatch(
        records=records,
        schema_version="source-native-report-v1",
        semantic_version="sina-lrb-source-native-v1",
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={},
        provenance={},
    )


def test_lrb_contract_declares_native_capture_selector_and_atemporal_semantics() -> None:
    contract = build_sina_lrb_observation_contract()

    assert contract.dataset_id == SINA_LRB_DATASET_ID
    assert contract.schema_version == "source-native-report-v1"
    assert contract.semantic_version == "sina-lrb-source-native-v1"
    assert contract.source.source == "sina"
    assert contract.source.source_id == "sina-lrb"
    assert contract.business_key == (
        "instrument_id",
        "capture_id",
        "statement_code",
        "record_kind",
        "record_key",
    )
    assert contract.filter_fields == ("instrument_id", "capture_id")
    assert contract.temporal_semantics == "none"
    assert contract.pit_policy == "unsupported"
    assert contract.field_units == {
        "report_list_ordinal": "source_ordinal",
        "report_date_ordinal": "source_ordinal",
    }


def test_serialized_context_and_report_rows_reconstruct_complete_source_tree() -> None:
    body = _body()
    original = parse_sina_lrb_response(body)
    batch = _batch(body)
    payloads = [record.payload for record in batch.records]
    context = next(payload for payload in payloads if payload["record_kind"] == "context")
    reports = [payload for payload in payloads if payload["record_kind"] == "report"]

    assert len(payloads) == 3
    assert context["response_encoding"] == SINA_LRB_RESPONSE_ENCODING
    assert context["response_context"] is not None
    assert [(row["report_list_key"], row["report_list_ordinal"]) for row in reports] == [
        ("20231231", 1),
        ("20240630", 0),
    ]
    assert [(row["report_list_key"], row["report_date_ordinal"]) for row in reports] == [
        ("20231231", 0),
        ("20240630", 1),
    ]

    reconstructed = reconstruct_sina_lrb_source_document(payloads)
    assert reconstructed == original.source_document
    result = next(value for key, value in reconstructed.fields if key == "result")
    assert isinstance(result, SinaLrbRawObject)
    data = next(value for key, value in result.fields if key == "data")
    assert isinstance(data, SinaLrbRawObject)
    date_values = next(value for key, value in data.fields if key == "report_date")
    list_values = next(value for key, value in data.fields if key == "report_list")
    assert isinstance(date_values, SinaLrbRawArray)
    assert isinstance(list_values, SinaLrbRawObject)
    assert [
        next(value for key, value in entry.fields if key == "date_value")
        for entry in date_values.items
        if isinstance(entry, SinaLrbRawObject)
    ] == ["20231231", "20240630"]
    assert [key for key, _value in list_values.fields] == ["20240630", "20231231"]

    decimal_source = original.reports[0].source_fields
    data = next(value for key, value in decimal_source.fields if key == "data")
    first_row = data.items[0]
    decimal_value = next(value for key, value in first_row.fields if key == "item_value")
    assert isinstance(decimal_value, Decimal)
    assert decimal_value.as_tuple() == Decimal("1.2300e+02").as_tuple()
    signed_zero = next(value for key, value in first_row.fields if key == "signed_zero")
    assert isinstance(signed_zero, Decimal)
    assert signed_zero.as_tuple() == Decimal("-0.00").as_tuple()
    wire_negative_zero = next(
        value for key, value in first_row.fields if key == "wire_negative_zero"
    )
    assert type(wire_negative_zero) is int
    assert wire_negative_zero == 0


def test_serializer_emits_contract_compatible_unknown_quality_summary() -> None:
    batch = _batch()

    assert batch.quality == {"status": "unknown", "report_id": None, "issue_count": 0}


def test_empty_capture_keeps_context_and_reconstructs_empty_collections() -> None:
    body = (
        b'{"result":{"status":{"code":0},"data":{"report_count":"0",'
        b'"report_date":[],"report_list":{},"extra":null}}}'
    )
    batch = _batch(body)
    assert len(batch.records) == 1
    assert batch.records[0].payload["record_kind"] == "context"
    assert reconstruct_sina_lrb_source_document([record.payload for record in batch.records]) == (
        parse_sina_lrb_response(body).source_document
    )


def test_serializer_rejects_non_bijective_date_key_join_without_source_details() -> None:
    body = _body().replace(b"20240630", b"20240629", 1)

    with pytest.raises(SinaLrbPublicationError) as raised:
        _batch(body)

    assert raised.value.code == "invalid_response"
    assert "20240630" not in str(raised.value)


def test_reconstructor_requires_complete_report_ordinals() -> None:
    payloads = [record.payload for record in _batch().records]
    incomplete = [payload for payload in payloads if payload["record_key"] != "20231231"]

    with pytest.raises(SinaLrbPublicationError) as raised:
        reconstruct_sina_lrb_source_document(incomplete)

    assert raised.value.code == "invalid_response"


def test_oversized_single_report_is_rejected_without_truncation() -> None:
    wide_row = {f"field_{index:04d}": "x" * 100 for index in range(3_000)}
    data = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": "1",
                "report_date": [{"date_value": "20231231"}],
                "report_list": {"20231231": {"data": [wide_row]}},
            },
        }
    }
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")

    with pytest.raises(SinaLrbPublicationError) as raised:
        _batch(body)

    assert raised.value.code == "limit_exceeded"


def test_publication_error_rejects_unregistered_codes() -> None:
    with pytest.raises(ValueError, match="Unsupported Sina LRB publication error code"):
        SinaLrbPublicationError("unregistered")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"body": None}, "invalid_input"),
        ({"instrument_id": "600519.BJ"}, "invalid_input"),
        ({"capture_id": "not-a-capture"}, "invalid_input"),
        ({"retrieved_at_utc": datetime(2026, 10, 30)}, "invalid_input"),
        (
            {"retrieved_at_utc": datetime(2026, 10, 30, tzinfo=timezone(timedelta(hours=8)))},
            "invalid_input",
        ),
    ],
)
def test_serializer_rejects_invalid_capture_metadata(
    overrides: dict[str, object], expected_code: str
) -> None:
    arguments: dict[str, object] = {
        "body": _body(),
        "instrument_id": "600519.SH",
        "retrieved_at_utc": _RETRIEVED_AT,
        "capture_id": _CAPTURE_ID,
    }
    arguments.update(overrides)

    with pytest.raises(SinaLrbPublicationError) as raised:
        serialize_sina_lrb_response(**arguments)  # type: ignore[arg-type]

    assert raised.value.code == expected_code


class _BrokenTimezone(tzinfo):
    """Timezone whose offset access fails, exercising the sanitized boundary."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        del dt
        raise RuntimeError("timezone implementation detail")

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None


def test_serializer_hides_timezone_failures_and_parser_errors() -> None:
    broken_time = datetime(2026, 10, 30, tzinfo=_BrokenTimezone())
    with pytest.raises(SinaLrbPublicationError) as timezone_error:
        serialize_sina_lrb_response(
            _body(),
            instrument_id="600519.SH",
            retrieved_at_utc=broken_time,
            capture_id=_CAPTURE_ID,
        )
    assert timezone_error.value.code == "invalid_input"
    assert "timezone implementation detail" not in str(timezone_error.value)

    with pytest.raises(SinaLrbPublicationError) as parser_error:
        _batch(b"not-json")
    assert parser_error.value.code == "invalid_response"
    assert "not-json" not in str(parser_error.value)

    with pytest.raises(SinaLrbPublicationError) as size_error:
        _batch(b" " * (4 * 1024 * 1024 + 1))
    assert size_error.value.code == "limit_exceeded"


def test_serializer_respects_capture_record_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publication, "_MAX_CAPTURE_RECORDS", 2)

    with pytest.raises(SinaLrbPublicationError) as raised:
        _batch()

    assert raised.value.code == "limit_exceeded"


def test_date_association_rejects_malformed_or_unbounded_source_collections() -> None:
    with pytest.raises(SinaLrbPublicationError) as invalid_type:
        publication._date_entries_by_report_key(cast("SinaLrbRawResponse", object()))
    assert invalid_type.value.code == "invalid_response"

    document = SinaLrbRawObject(fields=())
    one_date = _date_entry("20231231")
    with pytest.raises(SinaLrbPublicationError) as length_mismatch:
        publication._date_entries_by_report_key(SinaLrbRawResponse(document, (one_date,), ()))
    assert length_mismatch.value.code == "invalid_response"

    too_many = SinaLrbRawResponse(
        document,
        tuple(SinaLrbRawObject(fields=()) for _ in range(257)),
        tuple(_raw_report(f"{20230000 + index}", index) for index in range(257)),
    )
    with pytest.raises(SinaLrbPublicationError) as limit:
        publication._date_entries_by_report_key(too_many)
    assert limit.value.code == "limit_exceeded"

    duplicate_dates = SinaLrbRawResponse(
        document,
        (one_date, one_date),
        (_raw_report("20231231", 0), _raw_report("20240630", 1)),
    )
    with pytest.raises(SinaLrbPublicationError) as duplicate:
        publication._date_entries_by_report_key(duplicate_dates)
    assert duplicate.value.code == "invalid_response"

    mismatched_keys = SinaLrbRawResponse(
        document,
        (one_date,),
        (_raw_report("20240630"),),
    )
    with pytest.raises(SinaLrbPublicationError) as mismatch:
        publication._date_entries_by_report_key(mismatched_keys)
    assert mismatch.value.code == "invalid_response"


@pytest.mark.parametrize(
    "raw_date",
    [
        None,
        SinaLrbRawObject(fields=()),
        _date_entry(20231231),
        _date_entry("2023-12-31"),
    ],
)
def test_date_value_requires_an_opaque_eight_digit_string(raw_date: object) -> None:
    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._date_value(raw_date)
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing-field", "invalid_response"),
        ("non-string-identity", "invalid_response"),
        ("context-shape", "invalid_response"),
        ("report-shape", "invalid_response"),
        ("unknown-kind", "invalid_response"),
        ("bad-paper-code", "invalid_response"),
    ],
)
def test_payload_schema_and_identity_validation(mutation: str, expected_code: str) -> None:
    payload = _payloads()[0]
    if mutation == "missing-field":
        payload.pop("response_sha256")
    elif mutation == "non-string-identity":
        payload["instrument_id"] = 600519
    elif mutation == "context-shape":
        payload["response_context"] = None
    elif mutation == "report-shape":
        report = next(row for row in _payloads() if row["record_kind"] == "report")
        report["report_list_ordinal"] = True
        payload = report
    elif mutation == "unknown-kind":
        payload["record_kind"] = "third-kind"
    elif mutation == "bad-paper-code":
        payload["source_paper_code"] = "sz600519"

    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._validate_payload_shape(payload)
    assert raised.value.code == expected_code


def test_reconstructor_rejects_invalid_collection_envelopes() -> None:
    valid = _payloads()
    reports = [row for row in valid if row["record_kind"] == "report"]
    cases: list[object] = ["not-rows", [], [None], [None] * 258, reports]
    for rows in cases:
        with pytest.raises(SinaLrbPublicationError):
            reconstruct_sina_lrb_source_document(cast("list[dict[str, object]]", rows))


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("identity-mismatch", "invalid_response"),
        ("duplicate-report", "invalid_response"),
        ("bad-context-key", "invalid_response"),
        ("out-of-range-ordinal", "invalid_response"),
        ("date-entry-mismatch", "invalid_response"),
        ("duplicate-ordinal", "invalid_response"),
        ("array-context-root", "invalid_response"),
        ("missing-context-reference", "invalid_response"),
    ],
)
def test_reconstructor_rejects_inconsistent_rows_and_fragments(
    mutation: str, expected_code: str
) -> None:
    payloads = _payloads()
    reports = [row for row in payloads if row["record_kind"] == "report"]
    context = next(row for row in payloads if row["record_kind"] == "context")
    if mutation == "identity-mismatch":
        reports[0]["capture_id"] = "b" * 64
    elif mutation == "duplicate-report":
        payloads.append(deepcopy(reports[0]))
    elif mutation == "bad-context-key":
        context["record_key"] = "not-context"
    elif mutation == "out-of-range-ordinal":
        reports[0]["report_list_ordinal"] = 99
    elif mutation == "date-entry-mismatch":
        table = cast("dict[str, object]", reports[0]["report_date_entry"])
        nodes = cast("list[dict[str, object]]", table["nodes"])
        date_node = next(
            node
            for node in nodes
            if node.get("kind") == "string" and node.get("value") == reports[0]["record_key"]
        )
        date_node["value"] = "19990101"
    elif mutation == "duplicate-ordinal":
        reports[1]["report_list_ordinal"] = reports[0]["report_list_ordinal"]
    elif mutation == "array-context-root":
        context["response_context"] = _node_table([{"id": 0, "kind": "array", "items": []}])
    elif mutation == "missing-context-reference":
        table = cast("dict[str, object]", context["response_context"])
        nodes = cast("list[dict[str, object]]", table["nodes"])
        ref_node = next(node for node in nodes if node.get("kind") == "reference")
        node_id = ref_node["id"]
        ref_node.clear()
        ref_node.update({"id": node_id, "kind": "string", "value": "detached"})

    with pytest.raises(SinaLrbPublicationError) as raised:
        reconstruct_sina_lrb_source_document(payloads)
    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    ("table", "allow_references"),
    [
        (None, False),
        ({"encoding": "other", "root_id": 0, "nodes": []}, False),
        (_node_table([]), False),
        (_node_table([None]), False),  # type: ignore[list-item]
        (_node_table([{"id": 1, "kind": "null"}]), False),
        (_node_table([{"id": 0, "kind": "object", "fields": [["self", 0]]}]), False),
        (_node_table([{"id": 0, "kind": "integer", "value": 10**4096}]), False),
        (_node_table([{"id": 0, "kind": "string", "value": 7}]), False),
        (
            _node_table(
                [
                    {
                        "id": 0,
                        "kind": "decimal",
                        "sign": 0,
                        "digits": "not-digits",
                        "exponent": 0,
                    }
                ]
            ),
            False,
        ),
        (_node_table([{"id": 0, "kind": "array", "items": None}]), False),
        (_node_table([{"id": 0, "kind": "array", "items": [1]}]), False),
        (
            _node_table(
                [
                    {"id": 0, "kind": "array", "items": [1]},
                    {"id": 1, "kind": "reference", "name": "report_date"},
                ]
            ),
            False,
        ),
        (_node_table([{"id": 0, "kind": "object", "fields": None}]), False),
        (_node_table([{"id": 0, "kind": "object", "fields": [["only-one"]]}]), False),
        (
            _node_table(
                [
                    {
                        "id": 0,
                        "kind": "object",
                        "fields": [["same", 1], ["same", 1]],
                    },
                    {"id": 1, "kind": "null"},
                ]
            ),
            False,
        ),
        (
            _node_table(
                [
                    {"id": 0, "kind": "object", "fields": [["ref", 1]]},
                    {"id": 1, "kind": "reference", "name": "report_list"},
                ]
            ),
            False,
        ),
        (_node_table([{"id": 0, "kind": "unknown"}]), False),
        (
            _node_table(
                [
                    {"id": 0, "kind": "reference", "name": "unsupported"},
                ]
            ),
            True,
        ),
        (
            _node_table(
                [
                    {"id": 0, "kind": "null"},
                    {"id": 1, "kind": "null"},
                ]
            ),
            False,
        ),
    ],
)
def test_node_table_decoder_rejects_malformed_storage_forms(
    table: object, allow_references: bool
) -> None:
    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._decode_node_table(table, allow_references=allow_references)
    assert raised.value.code == "invalid_response"


def test_node_table_decoder_accepts_shared_scalar_nodes_but_requires_object_roots() -> None:
    shared = _node_table(
        [
            {"id": 0, "kind": "object", "fields": [["first", 1], ["second", 1]]},
            {"id": 1, "kind": "string", "value": "shared"},
        ]
    )
    decoded = publication._decode_node_table(shared, allow_references=False)
    assert isinstance(decoded, SinaLrbRawObject)
    assert decoded.fields == (("first", "shared"), ("second", "shared"))

    scalar = _node_table([{"id": 0, "kind": "null"}])
    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._decode_table_object(scalar)
    assert raised.value.code == "invalid_response"


def test_node_table_decoder_rejects_container_dags_but_encoder_preserves_alias_values() -> None:
    shared_container = SinaLrbRawArray(items=("shared",))
    source = SinaLrbRawObject(fields=(("first", shared_container), ("second", shared_container)))
    encoded = publication._encode_node_table(source)
    decoded = publication._decode_table_object(encoded)
    assert decoded.fields == source.fields

    dag = _node_table(
        [
            {"id": 0, "kind": "object", "fields": [["left", 1], ["right", 1]]},
            {"id": 1, "kind": "array", "items": [2, 2]},
            {"id": 2, "kind": "string", "value": "leaf"},
        ]
    )
    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._decode_node_table(dag, allow_references=False)
    assert raised.value.code == "invalid_response"


def test_node_table_decoder_rejects_duplicate_expanding_fragment_references() -> None:
    cases = [
        _node_table(
            [
                {"id": 0, "kind": "object", "fields": [["first", 1], ["second", 1]]},
                {"id": 1, "kind": "reference", "name": "report_list"},
            ]
        ),
        _node_table(
            [
                {"id": 0, "kind": "object", "fields": [["first", 1], ["second", 2]]},
                {"id": 1, "kind": "reference", "name": "report_list"},
                {"id": 2, "kind": "reference", "name": "report_list"},
            ]
        ),
    ]
    for table in cases:
        with pytest.raises(SinaLrbPublicationError) as raised:
            publication._decode_node_table(table, allow_references=True)
        assert raised.value.code == "invalid_response"


def test_node_table_depth_guard_precedes_shared_scalar_cache_lookup() -> None:
    max_depth = publication._MAX_NODE_TABLE_DEPTH
    nodes = [
        {"id": 0, "kind": "object", "fields": [["shallow", 1], ["deep", 2]]},
        {"id": 1, "kind": "string", "value": "shared"},
    ]
    nodes.extend(
        {
            "id": node_id,
            "kind": "array",
            "items": [node_id + 1 if node_id < max_depth + 1 else 1],
        }
        for node_id in range(2, max_depth + 2)
    )

    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._decode_node_table(_node_table(nodes), allow_references=False)
    assert raised.value.code == "invalid_response"


def test_node_table_decoder_enforces_source_json_depth_without_truncating_valid_data() -> None:
    max_depth = publication._MAX_NODE_TABLE_DEPTH
    nodes = [{"id": 0, "kind": "object", "fields": [["chain", 1]]}]
    nodes.extend(
        {"id": node_id, "kind": "array", "items": [node_id + 1]} for node_id in range(1, max_depth)
    )
    nodes.append({"id": max_depth, "kind": "null"})

    decoded = publication._decode_table_object(_node_table(nodes))
    cursor: object = decoded.fields[0][1]
    for _ in range(max_depth - 1):
        assert isinstance(cursor, SinaLrbRawArray)
        assert len(cursor.items) == 1
        cursor = cursor.items[0]
    assert cursor is None

    over_depth_nodes = [
        {"id": 0, "kind": "object", "fields": [["chain", 1]]},
        *[
            {"id": node_id, "kind": "array", "items": [node_id + 1]}
            for node_id in range(1, max_depth + 1)
        ],
        {"id": max_depth + 1, "kind": "null"},
    ]
    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._decode_node_table(_node_table(over_depth_nodes), allow_references=False)
    assert raised.value.code == "invalid_response"
    assert str(raised.value) == "Sina LRB publication response is invalid."


def test_node_encoder_rejects_oversized_or_unsupported_source_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    too_long_key = SinaLrbRawObject(fields=(("k" * 4097, None),))
    too_long_text = SinaLrbRawObject(fields=(("value", "x" * 4097),))
    too_large_integer = SinaLrbRawObject(fields=(("value", 10**4096),))
    oversized_decimal = Decimal((0, tuple(1 for _ in range(4097)), 0))
    too_many_digits = SinaLrbRawObject(fields=(("value", oversized_decimal),))
    unsupported = cast("SinaLrbRawObject", object())

    for value in (too_long_key, too_long_text, too_large_integer, too_many_digits, unsupported):
        with pytest.raises(SinaLrbPublicationError):
            publication._encode_node_table(value)

    monkeypatch.setattr(publication, "_MAX_NODE_TABLE_NODES", 1)
    with pytest.raises(SinaLrbPublicationError) as raised:
        publication._encode_node_table(SinaLrbRawArray(items=(None,)))
    assert raised.value.code == "limit_exceeded"


def test_fragment_replacement_preserves_raw_container_branches() -> None:
    raw = SinaLrbRawObject(fields=(("array", SinaLrbRawArray(items=(None, "value"))),))
    result, counts = publication._replace_fragment_references(
        raw,
        report_date=SinaLrbRawArray(items=()),
        report_list=SinaLrbRawObject(fields=()),
    )
    assert result == raw
    assert counts == {"report_date": 0, "report_list": 0}


def test_business_key_and_run_service_preflight_reject_unrepresentable_data() -> None:
    payload = _payloads()[0]
    payload["record_key"] = object()
    with pytest.raises(SinaLrbPublicationError) as business_key_error:
        publication._business_key(payload)
    assert business_key_error.value.code == "invalid_response"

    cases = [
        {"text": "x" * 4097},
        {"\x01bad-key": "value"},
    ]
    for data in cases:
        with pytest.raises(SinaLrbPublicationError) as raised:
            publication._preflight_batch(_normalized_batch([data]))
        assert raised.value.code == "limit_exceeded"

    with pytest.raises(SinaLrbPublicationError) as unsupported:
        publication._count_json_nodes(object(), depth=0)
    assert unsupported.value.code == "invalid_response"

    too_deep: object = None
    for _ in range(10):
        too_deep = [too_deep]
    with pytest.raises(SinaLrbPublicationError) as count_depth:
        publication._count_json_nodes(too_deep, depth=0)
    assert count_depth.value.code == "limit_exceeded"
    with pytest.raises(SinaLrbPublicationError) as validator_depth:
        publication._validate_json_depth(too_deep, depth=0)
    assert validator_depth.value.code == "limit_exceeded"
    with pytest.raises(SinaLrbPublicationError) as json_size_error:
        publication._json_size(object())
    assert json_size_error.value.code == "invalid_response"


def test_preflight_rejects_total_node_and_serialized_batch_limits() -> None:
    node_heavy_payload = {"nodes": [None] * 25_000}
    with pytest.raises(SinaLrbPublicationError) as node_limit:
        publication._preflight_batch(
            _normalized_batch([deepcopy(node_heavy_payload) for _ in range(41)])
        )
    assert node_limit.value.code == "limit_exceeded"

    text_heavy_payload = {"values": ["x" * 4096] * 15}
    with pytest.raises(SinaLrbPublicationError) as byte_limit:
        publication._preflight_batch(
            _normalized_batch([deepcopy(text_heavy_payload) for _ in range(257)])
        )
    assert byte_limit.value.code == "limit_exceeded"


def test_paper_code_and_timestamp_helpers_reject_invalid_boundary_values() -> None:
    with pytest.raises(SinaLrbPublicationError) as paper_code_error:
        publication._paper_code("600519.BJ")
    assert paper_code_error.value.code == "invalid_input"
    with pytest.raises(SinaLrbPublicationError) as utc_error:
        publication._utc_iso(None)
    assert utc_error.value.code == "invalid_response"
