"""Offline checks for the disabled, fixed Sina LRB process and publication job."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.sina_lrb_process_job as job_module
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.sina.lrb_statement_process import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_LRB_PROCESS_OPERATION_ID,
    SINA_LRB_PROCESS_PROTOCOL,
    SinaLrbProcessError,
    SinaLrbQuery,
)
from opendata.data.providers.sina.lrb_statement_publication import (
    build_sina_lrb_observation_contract,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_service import NormalizedBatch, NormalizedRecord, RunExecutionContext
from opendata.pipeline.sina_lrb_process_job import (
    SinaLrbProcessCapture,
    _build_process_parameters,
    _decode_body,
    _decode_process_result,
    _decode_retrieved_at,
    _request_symbol,
    _validate_capture,
    _validated_task,
    build_sina_lrb_job,
    build_sina_lrb_manual_payload,
    build_sina_lrb_process_source,
    normalize_sina_lrb_capture,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SYMBOL = "600519.SH"
_OTHER_SYMBOL = "000001.SZ"
_RETRIEVED_AT = datetime(2026, 10, 30, 1, 2, 3, 456789, tzinfo=timezone.utc)
_RUN_KEY = "a" * 64
_BODY = json.dumps(
    {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": "1",
                "report_date": [{"date_value": "20301231", "note": "synthetic"}],
                "report_list": {
                    "20301231": {
                        "source_note": "synthetic report",
                        "data": [{"item_field": "TEST", "item_value": "001.2300"}],
                    }
                },
            },
        }
    },
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")


def _task(**changes: object) -> TaskDefinition:
    values: dict[str, object] = {
        "job_id": "sina-lrb-capture",
        "version": "process-v1",
        "dataset_id": "sina_lrb_report_observations",
        "provider": "sina",
        "model": "financial_statement",
        "schedule": None,
        "enabled": False,
    }
    values.update(changes)
    return TaskDefinition(**values)


def _registry() -> DatasetContractRegistry:
    return DatasetContractRegistry([build_sina_lrb_observation_contract()])


def _request(symbol: str = _SYMBOL, token: str = "manual-capture") -> RunRequest:
    return RunRequest(
        run_type="manual",
        **build_sina_lrb_manual_payload(symbol, idempotency_key=token),
    )


def _context(
    run_key: str = _RUN_KEY,
    checkpoint: Callable[[], None] | None = None,
) -> RunExecutionContext:
    return RunExecutionContext(
        run_key=run_key,
        worker="offline-test",
        fencing_token=1,
        _checkpoint=checkpoint if checkpoint is not None else (lambda: None),
    )


def _capture(symbol: str = _SYMBOL, body: bytes = _BODY) -> SinaLrbProcessCapture:
    return SinaLrbProcessCapture(
        query=SinaLrbQuery(symbol),
        status_code=200,
        retrieved_at_utc=_RETRIEVED_AT,
        body=body,
    )


def _synthetic_batch(
    *,
    schema_version: str = "source-native-report-v1",
    semantic_version: str = "sina-lrb-source-native-v1",
    record_count: int = 2,
) -> NormalizedBatch:
    return NormalizedBatch(
        records=tuple(
            NormalizedRecord(business_key=f"test-{index}", payload={})
            for index in range(record_count)
        ),
        schema_version=schema_version,
        semantic_version=semantic_version,
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={},
        provenance={},
    )


def test_builder_exposes_only_disabled_unreviewed_manual_one_shot_job() -> None:
    task = _task()
    job = build_sina_lrb_job(task, contract_registry=_registry())
    source = build_sina_lrb_process_source()

    assert job.task == task
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert job.reviewed is False
    assert job.one_shot_only is True
    assert type(job.fetch) is ProcessFetchHandler
    assert job.fetch._operation_id == SINA_LRB_PROCESS_OPERATION_ID
    assert job.fetch._timeout_seconds == 30.0
    assert job.normalize is normalize_sina_lrb_capture
    assert isinstance(job.publish, ValidatedPublicationWriter)
    assert source._operation_id == SINA_LRB_PROCESS_OPERATION_ID
    assert source._executor._registry.resolve(SINA_LRB_PROCESS_OPERATION_ID) is not None
    assert source._executor._registry.resolve("other.operation") is None


@pytest.mark.parametrize(
    "task",
    [
        _task(
            enabled=True,
            schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *"),
        ),
        _task(schedule=None, version="wrong-version"),
        _task(dataset_id="other-dataset"),
        _task(model="other-model"),
    ],
)
def test_builder_rejects_enabled_or_drifted_task(task: TaskDefinition) -> None:
    with pytest.raises(ValueError, match="sina_lrb_task_definition_invalid"):
        build_sina_lrb_job(task, contract_registry=_registry())


def test_builder_requires_exact_dataset_contract() -> None:
    with pytest.raises(ValueError, match="sina_lrb_dataset_contract_invalid"):
        build_sina_lrb_job(_task(), contract_registry=DatasetContractRegistry([]))

    drifted = build_sina_lrb_observation_contract().model_copy(update={"timezone": "Asia/Shanghai"})
    with pytest.raises(ValueError, match="sina_lrb_dataset_contract_invalid"):
        build_sina_lrb_job(_task(), contract_registry=DatasetContractRegistry([drifted]))

    # The public builder validates dependencies before resolving any contract.
    with pytest.raises(TypeError, match="contract_registry must be a DatasetContractRegistry"):
        build_sina_lrb_job(_task(), contract_registry=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="task must be a TaskDefinition"):
        build_sina_lrb_job(object(), contract_registry=_registry())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="task must be a TaskDefinition"):
        _validated_task(object())  # type: ignore[arg-type]

    malformed_task = _task().model_copy(update={"timezone": "Not/A_Real_Zone"})
    with pytest.raises(ValueError, match="sina_lrb_task_definition_invalid"):
        build_sina_lrb_job(malformed_task, contract_registry=_registry())


def test_builder_rejects_non_contract_registry_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()
    monkeypatch.setattr(DatasetContractRegistry, "resolve", lambda self, *args: object())

    with pytest.raises(ValueError, match="sina_lrb_dataset_contract_invalid"):
        build_sina_lrb_job(_task(), contract_registry=registry)


def test_manual_payload_and_process_request_have_one_fixed_instrument_selector() -> None:
    payload = build_sina_lrb_manual_payload(_SYMBOL, idempotency_key="manual-001")
    request = RunRequest(run_type="manual", **payload)

    assert payload == {
        "query": {},
        "partition": {"instrument_id": _SYMBOL},
        "shard": None,
        "idempotency_key": "manual-001",
    }
    assert _request_symbol(request) == _SYMBOL
    assert _build_process_parameters(request) == {
        "protocol": SINA_LRB_PROCESS_PROTOCOL,
        "symbol": _SYMBOL,
    }
    with pytest.raises(ValueError, match="sina_lrb_process_request_invalid"):
        _request_symbol(request.model_copy(update={"query": {"source": "sina"}}))
    with pytest.raises(TypeError, match="request must be a RunRequest"):
        _request_symbol(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sina_lrb_process_request_invalid"):
        _request_symbol(RunRequest.model_construct())
    for invalid_request in (
        request.model_copy(update={"run_type": "scheduled"}),
        request.model_copy(update={"partition": {"instrument_id": _SYMBOL, "other": "x"}}),
        request.model_copy(update={"partition": {"instrument_id": 123}}),
        request.model_copy(update={"partition": {"instrument_id": "600519.BJ"}}),
        request.model_copy(update={"shard": "shard-1"}),
        request.model_copy(update={"idempotency_key": None}),
        request.model_copy(update={"occurrence": _RETRIEVED_AT}),
        request.model_copy(update={"backfill_start": date(2020, 1, 1)}),
        request.model_copy(update={"backfill_end": date(2020, 12, 31)}),
    ):
        with pytest.raises(ValueError, match="sina_lrb_process_request_invalid"):
            _request_symbol(invalid_request)
    for invalid in ("600519", "600519.BJ", " 600519.SH", "600519.sh"):
        with pytest.raises(SinaLrbProcessError, match="invalid_query"):
            build_sina_lrb_manual_payload(invalid, idempotency_key="manual-invalid")
    with pytest.raises(SinaLrbProcessError, match="invalid_query"):
        build_sina_lrb_manual_payload(object(), idempotency_key="manual-invalid")
    with pytest.raises(ValueError, match="manual Sina LRB payload is invalid"):
        build_sina_lrb_manual_payload(_SYMBOL, idempotency_key=object())
    with pytest.raises(ValueError, match="manual Sina LRB payload is invalid"):
        build_sina_lrb_manual_payload(_SYMBOL, idempotency_key="")


def test_process_result_decoder_checks_protocol_symbol_time_and_base64() -> None:
    envelope = {
        "protocol": SINA_LRB_PROCESS_PROTOCOL,
        "query": {"symbol": _SYMBOL},
        "retrieved_at_utc": "2026-10-30T01:02:03.456789Z",
        "status_code": 200,
        "body_b64": base64.b64encode(_BODY).decode("ascii"),
    }
    decoded = _decode_process_result(envelope)
    assert decoded.query.symbol == _SYMBOL
    assert decoded.retrieved_at_utc == _RETRIEVED_AT
    assert decoded.body == _BODY

    valid_different_symbol = _decode_process_result(
        {**envelope, "query": {"symbol": _OTHER_SYMBOL}}
    )
    assert valid_different_symbol.query.symbol == _OTHER_SYMBOL

    invalid = (
        None,
        {**envelope, "extra": "not allowed"},
        {**envelope, "protocol": "other"},
        {**envelope, "status_code": 201},
        {**envelope, "status_code": True},
        {**envelope, "query": None},
        {**envelope, "query": {"symbol": _SYMBOL, "extra": "not allowed"}},
        {**envelope, "query": {"symbol": 600519}},
        {**envelope, "query": {"symbol": "600519.BJ"}},
        {**envelope, "retrieved_at_utc": "2026-10-30T01:02:03Z"},
        {**envelope, "retrieved_at_utc": "2026-99-30T01:02:03.456789Z"},
        {**envelope, "retrieved_at_utc": None},
        {**envelope, "body_b64": None},
        {**envelope, "body_b64": "%%%="},
        {**envelope, "body_b64": "é==="},
        {**envelope, "body_b64": "/x=="},
    )
    for value in invalid:
        with pytest.raises(ValueError, match="sina_lrb_process_result_invalid"):
            _decode_process_result(value)


@pytest.mark.parametrize(
    "value,code",
    [
        (None, "invalid_envelope"),
        ("A", "invalid_envelope"),
        ("é===", "invalid_envelope"),
        ("/x==", "invalid_envelope"),
    ],
)
def test_base64_decoder_rejects_noncanonical_or_nontext_envelopes(value: object, code: str) -> None:
    with pytest.raises(SinaLrbProcessError, match=code):
        _decode_body(value)


def test_base64_decoder_checks_encoded_and_decoded_size_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_module, "_MAX_BASE64_BODY_CHARS", 3)
    with pytest.raises(SinaLrbProcessError, match="response_too_large"):
        _decode_body("AAAA")

    monkeypatch.setattr(job_module, "_MAX_BASE64_BODY_CHARS", 8)
    monkeypatch.setattr(job_module, "MAX_RAW_RESPONSE_BYTES", 3)
    with pytest.raises(SinaLrbProcessError, match="response_too_large"):
        _decode_body("AAAAAA==")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-10-30T01:02:03Z",
        "2026-99-30T01:02:03.456789Z",
    ],
)
def test_retrieval_timestamp_decoder_requires_canonical_valid_utc_text(value: object) -> None:
    with pytest.raises(SinaLrbProcessError, match="invalid_envelope"):
        _decode_retrieved_at(value)


def test_parent_normalizer_binds_capture_to_manual_request_and_preserves_source_evidence() -> None:
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    batch = normalize_sina_lrb_capture(
        _capture(),
        _request(),
        _context(checkpoint=checkpoint),
    )

    assert checkpoints == 2
    assert batch.schema_version == "source-native-report-v1"
    assert batch.semantic_version == "sina-lrb-source-native-v1"
    assert len(batch.records) == 2
    assert {record.payload["record_kind"] for record in batch.records} == {"context", "report"}
    assert all(record.payload["capture_id"] == _RUN_KEY for record in batch.records)
    assert all(record.payload["instrument_id"] == _SYMBOL for record in batch.records)
    assert all(record.business_date is None for record in batch.records)
    assert batch.provenance["response_sha256"] == hashlib.sha256(_BODY).hexdigest()
    assert batch.provenance["raw_response_persisted"] is False


def test_parent_normalizer_rejects_mismatched_instrument_capture_and_run_identity() -> None:
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(_capture(_OTHER_SYMBOL), _request(), _context())

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(_capture(), _request(_OTHER_SYMBOL), _context())

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(_capture(), _request(), _context("invalid-run-key"))
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(_capture(), _request(), _context(123))  # type: ignore[arg-type]


def test_normalizer_checks_context_capture_and_lease_checkpoints() -> None:
    with pytest.raises(TypeError, match="context must be a RunExecutionContext"):
        normalize_sina_lrb_capture(_capture(), _request(), object())  # type: ignore[arg-type]

    for fail_on_checkpoint in (1, 2):
        checkpoints = 0

        def checkpoint(fail_on_checkpoint: int = fail_on_checkpoint) -> None:
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == fail_on_checkpoint:
                raise RuntimeError("lease no longer owned")

        with pytest.raises(RuntimeError, match="lease no longer owned"):
            normalize_sina_lrb_capture(_capture(), _request(), _context(checkpoint=checkpoint))


@pytest.mark.parametrize(
    "serializer_result",
    [
        RuntimeError("details must not escape"),
        object(),
        _synthetic_batch(schema_version="unexpected-schema"),
        _synthetic_batch(semantic_version="unexpected-semantics"),
        _synthetic_batch(record_count=258),
    ],
)
def test_normalizer_rejects_serializer_output_outside_the_contract(
    serializer_result: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serialize(*args: object, **kwargs: object) -> object:
        if isinstance(serializer_result, Exception):
            raise serializer_result
        return serializer_result

    monkeypatch.setattr(job_module, "serialize_sina_lrb_response", serialize)
    with pytest.raises(ValueError, match="sina_lrb_normalization_failed"):
        normalize_sina_lrb_capture(_capture(), _request(), _context())


class _BrokenTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise RuntimeError("timezone details must not escape")

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


@pytest.mark.parametrize(
    "capture",
    [
        object(),
        SinaLrbProcessCapture(
            query=object(),  # type: ignore[arg-type]
            status_code=200,
            retrieved_at_utc=_RETRIEVED_AT,
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery(_SYMBOL),
            status_code=True,  # type: ignore[arg-type]
            retrieved_at_utc=_RETRIEVED_AT,
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery(_SYMBOL),
            status_code=200,
            retrieved_at_utc=datetime(2026, 10, 30, 1, 2, 3),
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery(_SYMBOL),
            status_code=200,
            retrieved_at_utc=datetime(2026, 10, 30, 1, 2, 3, tzinfo=timezone(timedelta(hours=1))),
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery(_SYMBOL),
            status_code=200,
            retrieved_at_utc=datetime(2026, 10, 30, tzinfo=_BrokenTimezone()),
            body=_BODY,
        ),
        SinaLrbProcessCapture(
            query=SinaLrbQuery(_SYMBOL),
            status_code=200,
            retrieved_at_utc=_RETRIEVED_AT,
            body=bytearray(_BODY),  # type: ignore[arg-type]
        ),
    ],
)
def test_capture_dto_rejects_untrusted_or_noncanonical_fields(capture: object) -> None:
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        _validate_capture(capture, expected_symbol=_SYMBOL)


def test_parent_normalizer_rejects_invalid_source_document_without_partial_batch() -> None:
    mismatched_date = _BODY.replace(b"20301231", b"20301230", 1)
    with pytest.raises(ValueError, match="sina_lrb_normalization_failed"):
        normalize_sina_lrb_capture(_capture(body=mismatched_date), _request(), _context())

    oversized = b"x" * (MAX_RAW_RESPONSE_BYTES + 1)
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(_capture(body=oversized), _request(), _context())
