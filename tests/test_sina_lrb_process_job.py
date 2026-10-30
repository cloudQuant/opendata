"""Offline checks for the disabled, fixed Sina LRB process and publication job."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
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
    sina_lrb_request_summary_sha256,
)
from opendata.data.providers.sina.lrb_statement_publication import (
    build_sina_lrb_observation_contract,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    ReviewedJob,
    RunExecutionContext,
)
from opendata.pipeline.sina_lrb_process_job import (
    SinaLrbProcessCapture,
    SinaLrbRawCaptureNormalizer,
    _build_process_parameters,
    _decode_body,
    _decode_process_result,
    _decode_retrieved_at,
    _request_symbol,
    _validated_task,
    build_sina_lrb_job,
    build_sina_lrb_manual_payload,
    build_sina_lrb_process_source,
    normalize_sina_lrb_capture,
)
from opendata.pipeline.sina_lrb_raw_capture import (
    SinaLrbArchivedCapture,
    SinaLrbRawAuthorization,
    SinaLrbRawCaptureError,
    SinaLrbRawCaptureHandler,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_SYMBOL = "600519.SH"
_OTHER_SYMBOL = "000001.SZ"
_RETRIEVED_AT = datetime(2020, 1, 2, 3, 4, 5, 456789, tzinfo=timezone.utc)
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


def _authorization(
    *, source_access: bool = True, raw_retention: bool = True
) -> SinaLrbRawAuthorization:
    return SinaLrbRawAuthorization(
        source_access_ref="source-auth:synthetic-grant-1",
        raw_retention_ref="raw-retention:synthetic-grant-1",
        source_access_authorized=source_access,
        raw_retention_authorized=raw_retention,
    )


def _build_job(
    task: TaskDefinition,
    root: Path,
    *,
    registry: DatasetContractRegistry | None = None,
    authorization: SinaLrbRawAuthorization | None = None,
) -> ReviewedJob:
    """Build the production job with explicit synthetic archival policy."""
    return build_sina_lrb_job(
        task,
        contract_registry=_registry() if registry is None else registry,
        artifact_root=root,
        authorization=_authorization() if authorization is None else authorization,
        retention_class="restricted",
        source_history_limit="unknown",
    )


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


class _FixedProcessHandler(ProcessFetchHandler):
    """Return one synthetic process capture without starting a child process."""

    def __init__(self, capture: object) -> None:
        self.capture = capture

    @property
    def heartbeat_interval_seconds(self) -> float:
        return 0.05

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        del request, context
        return self.capture


def _archive(
    capture: object,
    root: Path,
    *,
    run_key: str = _RUN_KEY,
    requested_symbol: str = _SYMBOL,
) -> SinaLrbArchivedCapture:
    """Persist and verify one synthetic capture through the production wrapper."""
    handler = SinaLrbRawCaptureHandler(
        _FixedProcessHandler(capture),
        artifact_root=root,
        authorization=_authorization(),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    return handler(_request(requested_symbol), _context(run_key))  # type: ignore[return-value]


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


def test_builder_exposes_only_disabled_unreviewed_manual_one_shot_job(
    tmp_path: Path,
) -> None:
    task = _task()
    job = _build_job(task, tmp_path / "raw")
    source = build_sina_lrb_process_source()

    assert job.task == task
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert job.reviewed is False
    assert job.one_shot_only is True
    assert isinstance(job.fetch, ProcessFetchHandler)
    assert type(job.fetch) is SinaLrbRawCaptureHandler
    assert job.fetch._delegate._operation_id == SINA_LRB_PROCESS_OPERATION_ID
    assert job.fetch._delegate._timeout_seconds == 30.0
    assert type(job.normalize) is SinaLrbRawCaptureNormalizer
    assert job.normalize.artifact_root == tmp_path / "raw"
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
def test_builder_rejects_enabled_or_drifted_task(task: TaskDefinition, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sina_lrb_task_definition_invalid"):
        _build_job(task, tmp_path / "raw")


@pytest.mark.parametrize(
    "authorization",
    [
        _authorization(source_access=False),
        _authorization(raw_retention=False),
    ],
)
def test_builder_requires_both_source_and_retention_authorizations(
    authorization: SinaLrbRawAuthorization,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="sina_lrb_raw_authorization_denied"):
        _build_job(_task(), tmp_path / "raw", authorization=authorization)


def test_builder_requires_exact_dataset_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sina_lrb_dataset_contract_invalid"):
        _build_job(
            _task(),
            tmp_path / "raw",
            registry=DatasetContractRegistry([]),
        )

    drifted = build_sina_lrb_observation_contract().model_copy(update={"timezone": "Asia/Shanghai"})
    with pytest.raises(ValueError, match="sina_lrb_dataset_contract_invalid"):
        _build_job(
            _task(),
            tmp_path / "raw",
            registry=DatasetContractRegistry([drifted]),
        )

    # The public builder validates dependencies before resolving any contract.
    with pytest.raises(TypeError, match="contract_registry must be a DatasetContractRegistry"):
        build_sina_lrb_job(
            _task(),
            contract_registry=object(),  # type: ignore[arg-type]
            artifact_root=tmp_path / "raw",
            authorization=_authorization(),
            retention_class="restricted",
            source_history_limit="unknown",
        )
    with pytest.raises(TypeError, match="task must be a TaskDefinition"):
        build_sina_lrb_job(
            object(),  # type: ignore[arg-type]
            contract_registry=_registry(),
            artifact_root=tmp_path / "raw",
            authorization=_authorization(),
            retention_class="restricted",
            source_history_limit="unknown",
        )
    with pytest.raises(TypeError, match="task must be a TaskDefinition"):
        _validated_task(object())  # type: ignore[arg-type]

    malformed_task = _task().model_copy(update={"timezone": "Not/A_Real_Zone"})
    with pytest.raises(ValueError, match="sina_lrb_task_definition_invalid"):
        _build_job(malformed_task, tmp_path / "raw")


def test_builder_rejects_non_contract_registry_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = _registry()
    monkeypatch.setattr(DatasetContractRegistry, "resolve", lambda self, *args: object())

    with pytest.raises(ValueError, match="sina_lrb_dataset_contract_invalid"):
        _build_job(_task(), tmp_path / "raw", registry=registry)


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
        "retrieved_at_utc": "2020-01-02T03:04:05.456789Z",
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
        {**envelope, "retrieved_at_utc": "2020-01-02T03:04:05Z"},
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
        "2020-01-02T03:04:05Z",
        "2026-99-30T01:02:03.456789Z",
    ],
)
def test_retrieval_timestamp_decoder_requires_canonical_valid_utc_text(value: object) -> None:
    with pytest.raises(SinaLrbProcessError, match="invalid_envelope"):
        _decode_retrieved_at(value)


def test_parent_normalizer_binds_archive_to_run_and_publication_provenance(
    tmp_path: Path,
) -> None:
    checkpoints = 0
    root = tmp_path / "raw"
    archived = _archive(_capture(), root)

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    batch = normalize_sina_lrb_capture(
        archived,
        _request(),
        _context(checkpoint=checkpoint),
        artifact_root=root,
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
    assert batch.provenance["raw_response_persisted"] is True
    assert batch.provenance["run_key"] == _RUN_KEY
    assert batch.provenance["raw_manifest_sha256"] == archived.manifest_sha256
    assert batch.provenance["request_summary_sha256"] == sina_lrb_request_summary_sha256(_SYMBOL)
    assert batch.provenance["source_access_ref"] == archived.source_access_ref
    assert batch.provenance["raw_retention_ref"] == archived.raw_retention_ref


def test_parent_normalizer_rejects_mismatched_instrument_capture_and_run_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    wrong_symbol = _archive(
        _capture(_OTHER_SYMBOL),
        root,
        requested_symbol=_OTHER_SYMBOL,
    )
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            wrong_symbol,
            _request(),
            _context(),
            artifact_root=root,
        )

    archived = _archive(_capture(), root)
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            archived,
            _request(_OTHER_SYMBOL),
            _context(),
            artifact_root=root,
        )

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            _context("invalid-run-key"),
            artifact_root=root,
        )
    wrong_run = _archive(_capture(), root, run_key="c" * 64)
    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(wrong_run, _request(), _context(), artifact_root=root)


def test_normalizer_requires_archived_capture_and_revalidates_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    archived = _archive(_capture(), root)

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(_capture(), _request(), _context(), artifact_root=root)

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(object(), _request(), _context(), artifact_root=root)

    with pytest.raises(ValueError, match="sina_lrb_capture_invalid"):
        normalize_sina_lrb_capture(
            replace(archived, response_sha256="d" * 64),
            _request(),
            _context(),
            artifact_root=root,
        )


def test_normalizer_checks_context_capture_and_lease_checkpoints(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    archived = _archive(_capture(), root)
    with pytest.raises(TypeError, match="context must be a RunExecutionContext"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            object(),  # type: ignore[arg-type]
            artifact_root=root,
        )

    for fail_on_checkpoint in (1, 2):
        checkpoints = 0

        def checkpoint(fail_on_checkpoint: int = fail_on_checkpoint) -> None:
            nonlocal checkpoints
            checkpoints += 1
            if checkpoints == fail_on_checkpoint:
                raise RuntimeError("lease no longer owned")

        with pytest.raises(RuntimeError, match="lease no longer owned"):
            normalize_sina_lrb_capture(
                archived,
                _request(),
                _context(checkpoint=checkpoint),
                artifact_root=root,
            )


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
    tmp_path: Path,
) -> None:
    def serialize(*args: object, **kwargs: object) -> object:
        if isinstance(serializer_result, Exception):
            raise serializer_result
        return serializer_result

    monkeypatch.setattr(job_module, "serialize_sina_lrb_response", serialize)
    root = tmp_path / "raw"
    archived = _archive(_capture(), root)
    with pytest.raises(ValueError, match="sina_lrb_normalization_failed"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            _context(),
            artifact_root=root,
        )


def test_parent_normalizer_rejects_invalid_source_document_without_partial_batch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    mismatched_date = _BODY.replace(b"20301231", b"20301230", 1)
    archived = _archive(_capture(body=mismatched_date), root)
    with pytest.raises(ValueError, match="sina_lrb_normalization_failed"):
        normalize_sina_lrb_capture(
            archived,
            _request(),
            _context(),
            artifact_root=root,
        )

    oversized = b"x" * (MAX_RAW_RESPONSE_BYTES + 1)
    with pytest.raises(SinaLrbRawCaptureError) as error:
        _archive(_capture(body=oversized), root)
    assert error.value.code == "invalid_capture"
