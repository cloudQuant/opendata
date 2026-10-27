"""Offline acceptance tests for the parent Sina process adapter and W11 bridge."""

from __future__ import annotations

import base64
import json
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SinaFzbQuery,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_process import (
    SINA_FZB_PROCESS_OPERATION_ID,
    SINA_FZB_PROCESS_PROTOCOL,
)
from opendata.data.providers.sina.financial_statement_publication import (
    build_sina_fzb_observation_contract,
)
from opendata.models.user import User, UserRole
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.process_executor import (
    MAX_RESULT_BYTES,
    MAX_RESULT_TEXT_BYTES,
    ReviewedProcessExecutor,
    ReviewedProcessRegistry,
)
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    SNAPSHOTS,
    PublicationStore,
)
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    ProcessFetchHandler,
    create_worker_engine,
)
from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RunLedgerConflictError,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
)
from opendata.pipeline.sina_fzb_process_job import (
    SinaFzbProcessCapture,
    _build_sina_fzb_process_parameters,
    _decode_sina_fzb_process_result,
    build_sina_fzb_job,
    build_sina_fzb_manual_payload,
    build_sina_fzb_process_source,
    build_sina_fzb_scheduled_payload,
    normalize_sina_fzb_capture,
)
from opendata.utils.cooperative_checkpoint import _CooperativeCheckpoint
from tests.sina_fzb_process_fixture_handlers import (
    CANCEL_OPERATION_ID,
    CHILD_ERROR_OPERATION_ID,
    CHILD_FIXTURE_MAX_RAW_RESPONSE_BYTES,
    CHILD_FIXTURE_PROTOCOL,
    FIXTURE_OPERATION_ID,
    FIXTURE_SHA256,
    MALFORMED_RESULT_OPERATION_ID,
    MAX_BODY_OPERATION_ID,
    OTHER_SYMBOL,
    TIMEOUT_OPERATION_ID,
    WRONG_SYMBOL_OPERATION_ID,
    _fixture_envelope,
    _private_marker_path,
    _recorded_body,
    _sleeping_fixture,
    fixture_sina_fzb_capture,
    fixture_sina_fzb_child_error,
    fixture_sina_fzb_malformed_result,
    fixture_sina_fzb_max_body,
    fixture_sina_fzb_wrong_symbol,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import JsonValue
    from sqlalchemy.engine import Engine

UTC = timezone.utc
DATASET_ID = "sina_fzb_report_observations"
SYMBOL = "600519.SH"
RETRIEVED_AT = datetime(2026, 10, 27, 2, 0, 0, 123456, tzinfo=UTC)
SIGNING_KEY = b"w12-process-test-snapshot-signing-key"
MODEL_COPY_SENTINEL = "w12-model-copy-sentinel-do-not-leak"

IDENTITY_SEMANTICS = json.loads(
    '{"version":1,"identity_key_encoding":"canonical-json-array-v1","revision":'
    '"latest non-deleted revision per business_key at the pinned publication sequence",'
    '"business_key_fields":["instrument_id","capture_id","statement_code","report_period"],'
    '"ordering_fields":["instrument_id","capture_id","statement_code","report_period"],'
    '"storage_order":[{"field":"business_date","direction":"asc","nulls":"last",'
    '"comparison":"native"},{"field":"business_key","direction":"asc",'
    '"comparison":"binary_lexicographic"}],"temporal_semantics":"none","time_field":null,'
    '"temporal_fields":{"business_date":"null for atemporal table records; not a historical '
    'business date","observed_at":"source observation time","source_published_at":'
    '"source publication time","available_at":"platform availability time",'
    '"event_at":"source event time"}}'
)


def _disabled_task(**overrides: object) -> TaskDefinition:
    """Build the exact disabled, schedule-free source preparation task."""
    values: dict[str, object] = {
        "job_id": "sina-fzb-capture",
        "version": "process-v1",
        "dataset_id": DATASET_ID,
        "provider": "sina",
        "model": "financial_statement",
        "schedule": None,
        "enabled": False,
    }
    values.update(overrides)
    return TaskDefinition(**values)


def _enabled_test_task() -> TaskDefinition:
    """Create a test-only runnable job identity for the synthetic fixture flow."""
    return TaskDefinition(
        job_id="sina-fzb-capture",
        version="process-v1",
        dataset_id=DATASET_ID,
        provider="sina",
        model="financial_statement",
        schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *"),
        enabled=True,
    )


def _contract_registry() -> DatasetContractRegistry:
    """Return an explicit registry containing only the W11 Sina contract."""
    return DatasetContractRegistry([build_sina_fzb_observation_contract()])


def _manual_request(symbol: str, token: str) -> RunRequest:
    """Use the fixed payload builder before constructing a test run request."""
    payload = build_sina_fzb_manual_payload(symbol, idempotency_key=token)
    return RunRequest(run_type="manual", **payload)


def _scheduled_request(symbol: str, occurrence: datetime) -> RunRequest:
    """Use the fixed payload builder for one natural scheduled slot."""
    payload = build_sina_fzb_scheduled_payload(symbol)
    return RunRequest(run_type="scheduled", occurrence=occurrence, **payload)


def _make_executor(
    operation_id: str,
    handler: object,
    *,
    checkpoint_interval_seconds: float = 0.05,
) -> ReviewedProcessExecutor:
    """Create a test-owned fixed-handler executor with the W12 result budget."""
    return ReviewedProcessExecutor(
        ReviewedProcessRegistry({operation_id: handler}),  # type: ignore[arg-type]
        max_result_bytes=MAX_RESULT_BYTES,
        max_result_text_bytes=MAX_RESULT_TEXT_BYTES,
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
        shutdown_grace_seconds=0.1,
    )


def _make_worker(
    database_path: Path,
    *,
    operation_id: str = FIXTURE_OPERATION_ID,
    process_handler: object = fixture_sina_fzb_capture,
    process_timeout_seconds: float = 15.0,
    lease_ttl: timedelta = timedelta(seconds=2),
) -> tuple[PersistentRunWorker, Engine]:
    """Build a real worker and source-specific fenced writer on owned SQLite."""
    bootstrap_engine = create_engine(f"sqlite:///{database_path}")
    try:
        RUN_LEDGER_METADATA.create_all(bootstrap_engine)
        PUBLICATION_METADATA.create_all(bootstrap_engine)
    finally:
        bootstrap_engine.dispose()

    database_timeout = 0.05
    worker_engine = create_worker_engine(
        database_path,
        database_io_timeout_seconds=database_timeout,
    )
    executor = _make_executor(operation_id, process_handler)
    fetch = ProcessFetchHandler(
        executor,
        operation_id,
        _build_sina_fzb_process_parameters,
        _decode_sina_fzb_process_result,
        timeout_seconds=process_timeout_seconds,
    )
    job = ReviewedJob(
        task=_enabled_test_task(),
        fetch=fetch,
        normalize=normalize_sina_fzb_capture,
        publish=ValidatedPublicationWriter(_contract_registry()),
        reviewed=True,
    )
    worker = PersistentRunWorker(
        worker_engine,
        ReviewedJobRegistry([job]),
        worker_id="w12-offline-worker",
        lease_ttl=lease_ttl,
        heartbeat_interval_seconds=0.05,
        database_io_timeout_seconds=database_timeout,
    )
    return worker, worker_engine


def _assert_no_publication(engine: Engine) -> None:
    """Prove a failed run did not publish facts, a head, snapshot, or watermark."""
    with engine.connect() as connection:
        for table in (PUBLICATIONS, PUBLICATION_HEADS, PUBLISHED_FACT_VERSIONS, SNAPSHOTS):
            assert connection.scalar(select(func.count()).select_from(table)) == 0


def _assert_process_reaped(pid: int) -> None:
    """Check that a timed-out or fenced child no longer has a live process ID."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _wait_for_marker(path: Path, *, timeout_seconds: float = 8.0) -> int:
    """Wait for a synthetic child to start without changing cwd or environment."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            marker_text = path.read_text(encoding="ascii")
            if marker_text:
                return int(marker_text)
        time.sleep(0.01)
    raise AssertionError("synthetic Popen child did not reach its start marker")


def _make_envelope(symbol: str = SYMBOL) -> dict[str, JsonValue]:
    """Return a reusable local result object for strict decoder/normalizer cases."""
    return _fixture_envelope(symbol)


def _make_run_context(run_key: str, checkpoints: list[str]) -> RunExecutionContext:
    """Record normalizer lease checkpoints without simulating the process worker."""
    return RunExecutionContext(
        run_key=run_key,
        worker="offline-test",
        fencing_token=1,
        _checkpoint=lambda: checkpoints.append(run_key),
    )


def _assert_sanitized_model_copy_rejection(
    operation: Callable[[], object],
    *,
    expected_error: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Require a fixed model-copy boundary error without warnings or input echo."""
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        with pytest.raises(ValueError) as raised:
            operation()
    captured = capsys.readouterr()

    assert str(raised.value) == expected_error
    assert emitted == []
    assert MODEL_COPY_SENTINEL not in str(raised.value)
    assert MODEL_COPY_SENTINEL not in captured.out + captured.err


def _expected_snapshot_rows(rows: list[dict[str, JsonValue]], capture_id: str) -> None:
    """Check one selected capture retains all source-native report rows."""
    assert len(rows) == 103
    all_ordinals = 0
    marker_count = 0
    for row in rows:
        assert row["business_date"] is None and row["event_at"] is None
        observed_at_text = row["observed_at"]
        assert isinstance(observed_at_text, str)
        normalized_observed_at_text = (
            f"{observed_at_text[:-1]}+00:00" if observed_at_text.endswith("Z") else observed_at_text
        )
        observed_at = datetime.fromisoformat(normalized_observed_at_text)
        assert observed_at.tzinfo is not None
        assert observed_at.utcoffset() == timedelta(0)
        assert observed_at.isoformat(timespec="microseconds") == normalized_observed_at_text
        assert observed_at == RETRIEVED_AT
        payload = row["payload"]
        assert isinstance(payload, dict)
        assert payload["instrument_id"] == SYMBOL
        assert payload["capture_id"] == capture_id
        assert payload["statement_code"] == "fzb"
        entries = payload["entries"]
        assert isinstance(entries, list)
        ordinals = [
            ordinal
            for entry in entries
            if isinstance(entry, dict)
            for ordinal in entry["source_ordinals"]
        ]
        assert all(type(ordinal) is int for ordinal in ordinals)
        assert sorted(ordinals) == list(range(len(ordinals)))
        all_ordinals += len(ordinals)
        marker_count += sum(
            entry["kind"] == "group_marker" for entry in entries if isinstance(entry, dict)
        )
    assert len({row["payload"]["report_period"] for row in rows}) == 103
    assert all_ordinals == 14_523
    assert marker_count == 515


def test_fixed_payload_builders_and_run_identity_keep_capture_slots_explicit() -> None:
    task = _enabled_test_task()
    occurrence = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    scheduled = _scheduled_request(SYMBOL, occurrence)
    scheduled_replay = _scheduled_request(SYMBOL, occurrence)
    scheduled_other_symbol = _scheduled_request(OTHER_SYMBOL, occurrence)

    assert build_sina_fzb_scheduled_payload(SYMBOL) == {
        "query": {},
        "partition": {"instrument_id": SYMBOL},
        "shard": None,
    }
    assert build_sina_fzb_manual_payload(SYMBOL, idempotency_key="capture-1") == {
        "query": {},
        "partition": {"instrument_id": SYMBOL},
        "shard": None,
        "idempotency_key": "capture-1",
    }
    scheduled_a = build_run_identity(task, scheduled)
    scheduled_replayed = build_run_identity(task, scheduled_replay)
    scheduled_b = build_run_identity(task, scheduled_other_symbol)
    assert scheduled_a.idempotency_key == scheduled_replayed.idempotency_key
    assert scheduled_a.payload_hash == scheduled_replayed.payload_hash
    assert scheduled_a.idempotency_key != scheduled_b.idempotency_key

    manual = _manual_request(SYMBOL, "capture-token")
    manual_replay = _manual_request(SYMBOL, "capture-token")
    manual_conflict = _manual_request(OTHER_SYMBOL, "capture-token")
    manual_new_capture = _manual_request(SYMBOL, "capture-token-next")
    identity_a = build_run_identity(task, manual)
    identity_replay = build_run_identity(task, manual_replay)
    identity_conflict = build_run_identity(task, manual_conflict)
    identity_next = build_run_identity(task, manual_new_capture)
    assert identity_a.idempotency_key == identity_replay.idempotency_key
    assert identity_a.payload_hash == identity_replay.payload_hash
    assert identity_a.idempotency_key == identity_conflict.idempotency_key
    assert identity_a.payload_hash != identity_conflict.payload_hash
    assert identity_a.idempotency_key != identity_next.idempotency_key

    process_parameters = _build_sina_fzb_process_parameters(manual)
    assert process_parameters == {
        "protocol": SINA_FZB_PROCESS_PROTOCOL,
        "symbol": SYMBOL,
    }
    assert "idempotency_key" not in process_parameters
    assert identity_a.idempotency_key not in json.dumps(process_parameters)


@pytest.mark.parametrize(
    "run_request",
    [
        RunRequest(
            run_type="backfill",
            idempotency_key="no-backfill",
            backfill_start=date(2020, 1, 1),
            backfill_end=date(2020, 1, 2),
            query={},
            partition={"instrument_id": SYMBOL},
        ),
        RunRequest(
            run_type="manual",
            idempotency_key="query-controls",
            query={"page": 2},
            partition={"instrument_id": SYMBOL},
        ),
        RunRequest(
            run_type="manual",
            idempotency_key="extra-partition",
            query={},
            partition={"instrument_id": SYMBOL, "cohort": "all"},
        ),
        RunRequest(
            run_type="manual",
            idempotency_key="bad-symbol",
            query={},
            partition={"instrument_id": "600519"},
        ),
        RunRequest(
            run_type="manual",
            idempotency_key="non-null-shard",
            query={},
            partition={"instrument_id": SYMBOL},
            shard="shard-1",
        ),
    ],
)
def test_parent_parameter_builder_rejects_unsupported_run_shapes(
    run_request: RunRequest,
) -> None:
    with pytest.raises((TypeError, ValueError, SinaFzbWireError)):
        _build_sina_fzb_process_parameters(run_request)


def test_parent_parameter_builder_rejects_nonstring_instrument_id_model_copy() -> None:
    request = _manual_request(SYMBOL, "numeric-instrument-id").model_copy(
        update={"partition": {"instrument_id": 600519}}
    )

    with pytest.raises(ValueError, match="^sina_fzb_process_request_invalid$"):
        _build_sina_fzb_process_parameters(request)


def test_parent_parameter_builder_rejects_non_run_request_input() -> None:
    with pytest.raises(TypeError, match="^request must be a RunRequest$"):
        _build_sina_fzb_process_parameters(cast("RunRequest", object()))


def test_manual_payload_rejects_nonstring_idempotency_key() -> None:
    with pytest.raises(ValueError, match="^manual Sina FZB payload is invalid$"):
        build_sina_fzb_manual_payload(SYMBOL, idempotency_key=object())


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"query": MODEL_COPY_SENTINEL}, id="query-is-not-an-object"),
        pytest.param({"contract_version": True}, id="boolean-contract-version"),
    ],
)
def test_process_parameter_builder_rejects_run_request_model_copy_without_warning_or_echo(
    update: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    request = _manual_request(SYMBOL, "model-copy-request-token").model_copy(update=update)

    _assert_sanitized_model_copy_rejection(
        lambda: _build_sina_fzb_process_parameters(request),
        expected_error="sina_fzb_process_request_invalid",
        capsys=capsys,
    )


def test_child_fixture_constants_match_the_production_contract() -> None:
    assert CHILD_FIXTURE_PROTOCOL == SINA_FZB_PROCESS_PROTOCOL
    assert CHILD_FIXTURE_MAX_RAW_RESPONSE_BYTES == MAX_RAW_RESPONSE_BYTES
    assert FIXTURE_SHA256 == "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"


def test_response_decoder_accepts_the_exact_canonical_fixture_envelope() -> None:
    body = _recorded_body()
    envelope = _make_envelope()
    decoded = _decode_sina_fzb_process_result(envelope)

    assert isinstance(decoded, SinaFzbProcessCapture)
    assert decoded.query == SinaFzbQuery(SYMBOL)
    assert type(decoded.status_code) is int and decoded.status_code == 200
    assert decoded.retrieved_at_utc == RETRIEVED_AT
    assert decoded.body == body
    assert len(envelope["body_b64"]) <= 5_592_408
    assert len(envelope["body_b64"]) < MAX_RESULT_TEXT_BYTES


def test_real_popen_accepts_the_exact_four_mib_body_under_eight_mib_result_text_cap() -> None:
    expected_body = b"x" * MAX_RAW_RESPONSE_BYTES
    expected_base64_length = 4 * ((MAX_RAW_RESPONSE_BYTES + 2) // 3)
    assert expected_base64_length == 5_592_408
    assert expected_base64_length < MAX_RESULT_TEXT_BYTES
    executor = _make_executor(MAX_BODY_OPERATION_ID, fixture_sina_fzb_max_body)

    result = executor.run(
        MAX_BODY_OPERATION_ID,
        {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL},
        timeout_seconds=15.0,
    )
    decoded = _decode_sina_fzb_process_result(result.value)

    assert decoded.body == expected_body
    assert len(decoded.body) == MAX_RAW_RESPONSE_BYTES


@pytest.mark.parametrize(
    "mutate",
    [
        lambda envelope: envelope.pop("status_code"),
        lambda envelope: envelope.update(extra="field"),
        lambda envelope: envelope.update(protocol="wrong"),
        lambda envelope: envelope.update(query={"symbol": SYMBOL, "page": 2}),
        lambda envelope: envelope.update(query={"symbol": "600519"}),
        lambda envelope: envelope.update(status_code=True),
        lambda envelope: envelope.update(status_code=503),
        lambda envelope: envelope.update(retrieved_at_utc="2026-10-27T02:00:00Z"),
        lambda envelope: envelope.update(retrieved_at_utc="2026-10-27T02:00:00.12345Z"),
        lambda envelope: envelope.update(retrieved_at_utc="2026-10-27T10:00:00.123456+08:00"),
        lambda envelope: envelope.update(retrieved_at_utc="2026-02-30T02:00:00.123456Z"),
        lambda envelope: envelope.update(body_b64="/x=="),
        lambda envelope: envelope.update(body_b64="/w==\n"),
        lambda envelope: envelope.update(body_b64="A==="),
        lambda envelope: envelope.update(body_b64="é==="),
    ],
)
def test_response_decoder_rejects_noncanonical_envelopes_without_echoing_input(mutate) -> None:
    envelope = _make_envelope()
    mutate(envelope)

    with pytest.raises(ValueError) as raised:
        _decode_sina_fzb_process_result(envelope)

    assert str(raised.value) == "sina_fzb_process_result_invalid"
    assert "quotes.sina.cn" not in str(raised.value)
    assert "body_b64" not in str(raised.value)


def test_response_decoder_rejects_raw_body_over_four_mib_before_normalization() -> None:
    too_large = base64.b64encode(b"x" * (MAX_RAW_RESPONSE_BYTES + 1)).decode("ascii")
    envelope = _make_envelope()
    envelope["body_b64"] = too_large

    with pytest.raises(ValueError, match="^sina_fzb_process_result_invalid$"):
        _decode_sina_fzb_process_result(envelope)


def test_parent_normalizer_binds_run_key_and_preserves_w11_source_observation_shape() -> None:
    capture = _decode_sina_fzb_process_result(_make_envelope())
    request = _manual_request(SYMBOL, "parent-only-token")
    run_key = "a" * 64
    checkpoints: list[str] = []

    batch = normalize_sina_fzb_capture(
        capture,
        request,
        _make_run_context(run_key, checkpoints),
    )

    assert isinstance(batch, NormalizedBatch)
    assert len(checkpoints) >= 2
    assert set(checkpoints) == {run_key}
    assert len(batch.records) == 103
    total_ordinals = 0
    marker_count = 0
    for record in batch.records:
        assert record.business_date is None and record.event_at is None
        assert record.observed_at == RETRIEVED_AT
        assert record.payload["capture_id"] == run_key
        assert record.payload["instrument_id"] == SYMBOL
        assert record.payload["statement_code"] == "fzb"
        entries = record.payload["entries"]
        assert isinstance(entries, list)
        ordinals = [
            ordinal
            for entry in entries
            if isinstance(entry, dict)
            for ordinal in entry["source_ordinals"]
        ]
        assert sorted(ordinals) == list(range(len(ordinals)))
        total_ordinals += len(ordinals)
        marker_count += sum(
            entry["kind"] == "group_marker" for entry in entries if isinstance(entry, dict)
        )
    assert len({record.payload["report_period"] for record in batch.records}) == 103
    assert total_ordinals == 14_523
    assert marker_count == 515
    assert "parent-only-token" not in json.dumps(batch.provenance)


def test_parent_normalizer_propagates_lease_loss_from_parser_checkpoint() -> None:
    capture = _decode_sina_fzb_process_result(_make_envelope())
    request = _manual_request(SYMBOL, "parser-checkpoint-cancel")
    run_key = "1" * 64
    error = ValueError("cancel parsing")
    calls = 0

    class _AdvancingClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 0.02
            return self.value

    def renew() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error

    clock = _AdvancingClock()
    cooperative = _CooperativeCheckpoint(
        renew,
        interval_seconds=0.01,
        immediate=False,
        clock=clock,
    )
    context = RunExecutionContext(
        run_key=run_key,
        worker="offline-test",
        fencing_token=1,
        _checkpoint=renew,
        _cooperative_checkpoint=cooperative,
    )

    with pytest.raises(ValueError) as raised:
        normalize_sina_fzb_capture(capture, request, context)

    assert raised.value is error
    assert calls == 2


def test_parent_normalizer_rejects_symbol_mismatch_and_invalid_capture_id_before_parse() -> None:
    capture = _decode_sina_fzb_process_result(_make_envelope())
    mismatch_checkpoints: list[str] = []
    with pytest.raises(ValueError, match="^sina_fzb_capture_invalid$"):
        normalize_sina_fzb_capture(
            capture,
            _manual_request(OTHER_SYMBOL, "mismatch-token"),
            _make_run_context("b" * 64, mismatch_checkpoints),
        )
    assert mismatch_checkpoints == ["b" * 64]

    invalid_key_checkpoints: list[str] = []
    with pytest.raises(ValueError, match="^sina_fzb_capture_invalid$"):
        normalize_sina_fzb_capture(
            capture,
            _manual_request(SYMBOL, "invalid-key-token"),
            _make_run_context("not-a-run-key", invalid_key_checkpoints),
        )
    assert invalid_key_checkpoints == ["not-a-run-key"]


def test_parent_normalizer_rejects_non_capture_input_before_serialization() -> None:
    checkpoints: list[str] = []
    run_key = "e" * 64

    with pytest.raises(ValueError, match="^sina_fzb_capture_invalid$"):
        normalize_sina_fzb_capture(
            object(),
            _manual_request(SYMBOL, "wrong-capture-type"),
            _make_run_context(run_key, checkpoints),
        )

    assert checkpoints == [run_key]


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        pytest.param("query", object(), id="wrong-query-type"),
        pytest.param("status_code", True, id="boolean-status"),
        pytest.param("status_code", 503, id="non-200-status"),
        pytest.param("body", "not-bytes", id="non-byte-body"),
        pytest.param(
            "body",
            b"x" * (MAX_RAW_RESPONSE_BYTES + 1),
            id="oversized-body",
        ),
        pytest.param("retrieved_at_utc", None, id="missing-time"),
        pytest.param(
            "retrieved_at_utc",
            datetime(2026, 10, 27, 10, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))),
            id="non-utc-time",
        ),
    ],
)
def test_parent_normalizer_rejects_corrupted_capture_fields_before_serialization(
    field_name: str,
    invalid_value: object,
) -> None:
    capture = SinaFzbProcessCapture(
        query=SinaFzbQuery(SYMBOL),
        status_code=200,
        retrieved_at_utc=RETRIEVED_AT,
        body=b"x",
    )
    object.__setattr__(capture, field_name, invalid_value)
    checkpoints: list[str] = []
    run_key = "c" * 64

    with pytest.raises(ValueError, match="^sina_fzb_capture_invalid$"):
        normalize_sina_fzb_capture(
            capture,
            _manual_request(SYMBOL, "corrupt-capture"),
            _make_run_context(run_key, checkpoints),
        )

    assert checkpoints == [run_key]


def test_parent_normalizer_sanitizes_timezone_callback_failure() -> None:
    class BrokenTimezone(tzinfo):
        def utcoffset(self, _dt: datetime | None) -> timedelta:
            raise RuntimeError("timezone details must not escape")

        def dst(self, _dt: datetime | None) -> timedelta | None:
            return None

    capture = SinaFzbProcessCapture(
        query=SinaFzbQuery(SYMBOL),
        status_code=200,
        retrieved_at_utc=RETRIEVED_AT,
        body=b"x",
    )
    object.__setattr__(
        capture,
        "retrieved_at_utc",
        datetime(2026, 10, 27, 2, 0, 0, 123456, tzinfo=BrokenTimezone()),
    )
    checkpoints: list[str] = []

    with pytest.raises(ValueError, match="^sina_fzb_capture_invalid$"):
        normalize_sina_fzb_capture(
            capture,
            _manual_request(SYMBOL, "broken-timezone"),
            _make_run_context("d" * 64, checkpoints),
        )

    assert checkpoints == ["d" * 64]


def test_parent_normalizer_rejects_corrupted_frozen_query() -> None:
    capture = SinaFzbProcessCapture(
        query=SinaFzbQuery(SYMBOL),
        status_code=200,
        retrieved_at_utc=RETRIEVED_AT,
        body=b"x",
    )
    invalid_query = SinaFzbQuery(SYMBOL)
    object.__setattr__(invalid_query, "symbol", "600519")
    object.__setattr__(capture, "query", invalid_query)

    with pytest.raises(ValueError, match="^sina_fzb_capture_invalid$"):
        normalize_sina_fzb_capture(
            capture,
            _manual_request(SYMBOL, "corrupted-query"),
            _make_run_context("f" * 64, []),
        )


def test_parent_normalizer_rejects_wrong_context_type_before_checkpoint() -> None:
    capture = _decode_sina_fzb_process_result(_make_envelope())

    with pytest.raises(TypeError, match="^context must be a RunExecutionContext$"):
        normalize_sina_fzb_capture(
            capture,
            _manual_request(SYMBOL, "wrong-context-type"),
            cast("RunExecutionContext", object()),
        )


def test_factory_builds_only_a_disabled_schedule_free_fixed_sina_job() -> None:
    task = _disabled_task()
    job = build_sina_fzb_job(task, contract_registry=_contract_registry())
    standalone_source = build_sina_fzb_process_source()

    assert job.task == task
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert job.reviewed is False
    assert type(job.fetch) is ProcessFetchHandler
    assert job.fetch._operation_id == SINA_FZB_PROCESS_OPERATION_ID
    assert job.fetch._timeout_seconds == 30.0
    assert standalone_source._operation_id == SINA_FZB_PROCESS_OPERATION_ID
    assert job.normalize is normalize_sina_fzb_capture
    assert isinstance(job.publish, ValidatedPublicationWriter)
    executor = job.fetch._executor
    standalone_executor = standalone_source._executor
    assert executor._max_result_bytes == 16 * 1024 * 1024
    assert executor._max_result_text_bytes == 8 * 1024 * 1024
    assert executor._poll_interval == 0.05
    assert executor._checkpoint_interval == 1.0
    assert executor._shutdown_grace == 0.25
    assert executor._registry.resolve(SINA_FZB_PROCESS_OPERATION_ID) is not None
    assert executor._registry.resolve("test.sina.fixture.capture") is None
    assert standalone_executor._max_result_bytes == MAX_RESULT_BYTES
    assert standalone_executor._max_result_text_bytes == MAX_RESULT_TEXT_BYTES


@pytest.mark.parametrize(
    "task",
    [
        _disabled_task(
            enabled=True, schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *")
        ),
        _disabled_task(schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *")),
        _disabled_task(version="wrong-version"),
        _disabled_task(model="wrong-model"),
    ],
)
def test_factory_rejects_enabled_scheduled_or_drifted_tasks(task: TaskDefinition) -> None:
    with pytest.raises(ValueError, match="sina_fzb_task_definition_invalid"):
        build_sina_fzb_job(task, contract_registry=_contract_registry())


def test_factory_requires_the_explicit_w11_contract_registry() -> None:
    with pytest.raises(ValueError, match="sina_fzb_dataset_contract_invalid"):
        build_sina_fzb_job(_disabled_task(), contract_registry=DatasetContractRegistry([]))


def test_factory_rejects_a_wrong_registry_dependency_type() -> None:
    with pytest.raises(TypeError, match="^contract_registry must be a DatasetContractRegistry$"):
        build_sina_fzb_job(
            _disabled_task(),
            contract_registry=cast("DatasetContractRegistry", object()),
        )


def test_factory_rejects_a_registered_contract_with_semantic_drift() -> None:
    drifted_contract = build_sina_fzb_observation_contract().model_copy(
        update={"timezone": "Asia/Shanghai"}
    )
    registry = DatasetContractRegistry([drifted_contract])

    with pytest.raises(ValueError, match="^sina_fzb_dataset_contract_invalid$"):
        build_sina_fzb_job(_disabled_task(), contract_registry=registry)


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"enabled": MODEL_COPY_SENTINEL}, id="enabled-is-not-a-bool"),
        pytest.param({"contract_version": True}, id="boolean-contract-version"),
    ],
)
def test_job_factory_rejects_task_model_copy_without_warning_or_echo(
    update: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    task = _disabled_task().model_copy(update=update)

    _assert_sanitized_model_copy_rejection(
        lambda: build_sina_fzb_job(task, contract_registry=_contract_registry()),
        expected_error="sina_fzb_task_definition_invalid",
        capsys=capsys,
    )


def test_job_factory_rejects_nested_schedule_model_copy_without_warning_or_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    schedule = ScheduleMetadata(kind="cron", expression="0 0 * * *").model_copy(
        update={"expression": 1729}
    )
    task = _disabled_task().model_copy(update={"schedule": schedule})

    _assert_sanitized_model_copy_rejection(
        lambda: build_sina_fzb_job(task, contract_registry=_contract_registry()),
        expected_error="sina_fzb_task_definition_invalid",
        capsys=capsys,
    )


def test_real_popen_fixture_worker_publishes_two_isolated_captures_and_scoped_pages(
    tmp_path: Path,
) -> None:
    from opendata_client.snapshots import SnapshotClient

    body = _recorded_body()
    assert len(base64.b64encode(body)) <= 5_592_408
    database_path = tmp_path / "sina-process-publication.sqlite"
    database_path.touch()
    worker, worker_engine = _make_worker(
        database_path,
        lease_ttl=timedelta(seconds=60),
    )
    store = PublicationStore(worker_engine, cursor_signing_key=SIGNING_KEY)
    task = _enabled_test_task()
    request_a = _manual_request(SYMBOL, "capture-a")
    request_b = _manual_request(SYMBOL, "capture-b")
    identity_a = build_run_identity(task, request_a)
    identity_b = build_run_identity(task, request_b)

    try:
        queued_a = worker.submit(task.job_id, task.version, request_a)
        replay_a = worker.submit(task.job_id, task.version, request_a)
        assert queued_a.run_key == replay_a.run_key == identity_a.idempotency_key
        with pytest.raises(RunLedgerConflictError):
            worker.submit(task.job_id, task.version, _manual_request(OTHER_SYMBOL, "capture-a"))

        outcome_a = worker.run_once()
        assert outcome_a is not None and outcome_a.record is not None
        assert outcome_a.publication is not None
        assert outcome_a.record.status == "succeeded"
        assert outcome_a.record.run_key == identity_a.idempotency_key
        assert outcome_a.publication.dataset_id == DATASET_ID
        assert outcome_a.publication.record_count == 103
        replay_after_success = worker.submit(task.job_id, task.version, request_a)
        assert replay_after_success.run_key == identity_a.idempotency_key
        assert replay_after_success.status == "succeeded"

        outcome_b_submit = worker.submit(task.job_id, task.version, request_b)
        assert outcome_b_submit.run_key == identity_b.idempotency_key
        assert identity_b.idempotency_key != identity_a.idempotency_key
        outcome_b = worker.run_once()
        assert outcome_b is not None and outcome_b.record is not None
        assert outcome_b.publication is not None
        assert outcome_b.record.status == "succeeded"
        assert outcome_b.record.run_key == identity_b.idempotency_key
        assert outcome_b.publication.record_count == 103

        grants = DatasetGrantRegistry(
            [
                DatasetGrant(
                    dataset_id=DATASET_ID,
                    domain="equity",
                    source_id="sina-fzb",
                    permitted_operations=("data.read", "snapshot.create", "snapshot.read"),
                    purposes=("research",),
                    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                    valid_until=datetime(2100, 1, 1, tzinfo=UTC),
                    review_evidence="OD204-W12-TEST",
                )
            ]
        )
        principal = Principal(
            user=User(
                id=17,
                username="w12-sina-test",
                email="w12-sina-test@example.test",
                hashed_password="not-a-real-password-hash",
                role=UserRole.USER,
                is_active=True,
            )
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_dataset_grants] = lambda: grants
        app.dependency_overrides[get_dataset_contracts] = _contract_registry
        app.dependency_overrides[get_publication_store] = lambda: store

        with (
            TestClient(app) as http_client,
            SnapshotClient(
                "http://testserver",
                api_key="offline-test-key",
                http_client=http_client,
            ) as client,
        ):

            def selected_snapshot(capture_id: str, publication_id: str):
                return client.create_snapshot(
                    DATASET_ID,
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                    page_size=64,
                    publication_id=publication_id,
                    instrument_id=SYMBOL,
                    capture_id=capture_id,
                )

            snapshot_a = selected_snapshot(
                identity_a.idempotency_key, outcome_a.publication.publication_id
            )
            assert snapshot_a.record_count == 103
            assert snapshot_a.sequence == 1
            assert snapshot_a.selection is not None
            assert snapshot_a.selection.instrument_id == SYMBOL
            assert snapshot_a.selection.capture_id == identity_a.idempotency_key
            rows_a = list(
                client.iter_records(
                    snapshot_a,
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )
            )
            _expected_snapshot_rows(rows_a, identity_a.idempotency_key)

            wrong_instrument = client.create_snapshot(
                DATASET_ID,
                expected_identity_semantics=IDENTITY_SEMANTICS,
                page_size=64,
                publication_id=outcome_a.publication.publication_id,
                instrument_id=OTHER_SYMBOL,
                capture_id=identity_a.idempotency_key,
            )
            assert wrong_instrument.record_count == 0
            assert (
                list(
                    client.iter_records(
                        wrong_instrument,
                        expected_identity_semantics=IDENTITY_SEMANTICS,
                    )
                )
                == []
            )

            snapshot_b = selected_snapshot(
                identity_b.idempotency_key, outcome_b.publication.publication_id
            )
            assert snapshot_b.record_count == 103
            assert snapshot_b.sequence == 2
            assert snapshot_b.selection is not None
            assert snapshot_b.selection.capture_id == identity_b.idempotency_key
            rows_b = list(
                client.iter_records(
                    snapshot_b,
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                )
            )
            _expected_snapshot_rows(rows_b, identity_b.idempotency_key)
            assert {row["business_key"] for row in rows_a}.isdisjoint(
                row["business_key"] for row in rows_b
            )

            pinned_again = client.get_snapshot(
                snapshot_a.snapshot_id,
                dataset_id=DATASET_ID,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
            assert pinned_again.sequence == snapshot_a.sequence
            assert pinned_again.content_sha256 == snapshot_a.content_sha256
            assert (
                list(
                    client.iter_records(
                        pinned_again,
                        expected_identity_semantics=IDENTITY_SEMANTICS,
                    )
                )
                == rows_a
            )

        with worker_engine.connect() as connection:
            heads = connection.execute(select(PUBLICATION_HEADS)).mappings().all()
            publications = connection.execute(select(PUBLICATIONS)).mappings().all()
        assert len(heads) == 1 and heads[0]["watermark_json"] is None
        assert len(publications) == 2
        assert all(row["watermark_json"] is None for row in publications)
    finally:
        worker_engine.dispose()


@pytest.mark.parametrize(
    "operation_id, handler, timeout, expected_error",
    [
        (
            WRONG_SYMBOL_OPERATION_ID,
            fixture_sina_fzb_wrong_symbol,
            8.0,
            "execution_failed:ValueError",
        ),
        (
            CHILD_ERROR_OPERATION_ID,
            fixture_sina_fzb_child_error,
            8.0,
            "execution_failed:ProcessExecutionHandlerError",
        ),
        (
            MALFORMED_RESULT_OPERATION_ID,
            fixture_sina_fzb_malformed_result,
            8.0,
            "execution_failed:ValueError",
        ),
    ],
)
def test_invalid_child_results_leave_publication_state_unchanged(
    tmp_path: Path,
    operation_id: str,
    handler: object,
    timeout: float,
    expected_error: str,
) -> None:
    marker = (
        _private_marker_path("wave12-sina-child.malformed.started")
        if operation_id == MALFORMED_RESULT_OPERATION_ID
        else None
    )
    if marker is not None:
        marker.unlink(missing_ok=True)
    database_path = tmp_path / f"{operation_id.replace('.', '-')}.sqlite"
    database_path.touch()
    worker, worker_engine = _make_worker(
        database_path,
        operation_id=operation_id,
        process_handler=handler,
        process_timeout_seconds=timeout,
    )

    try:
        request = _manual_request(SYMBOL, f"{operation_id}-token")
        submitted = worker.submit("sina-fzb-capture", "process-v1", request)
        outcome = worker.run_once()

        assert outcome is not None and outcome.record is not None
        assert outcome.record.run_key == submitted.run_key
        assert outcome.record.status == "failed"
        assert outcome.publication is None
        assert outcome.record.error_text == expected_error
        assert "secret-wave12-fixture-value" not in outcome.record.error_text
        assert "Authorization" not in outcome.record.error_text
        if marker is not None:
            child_pid = _wait_for_marker(marker)
            _assert_process_reaped(child_pid)
        _assert_no_publication(worker_engine)
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)
        worker_engine.dispose()


def test_parent_timeout_kills_reaps_child_and_does_not_publish(tmp_path: Path) -> None:
    marker = _private_marker_path("wave12-sina-child.started")
    marker.unlink(missing_ok=True)
    database_path = tmp_path / "sina-process-timeout.sqlite"
    database_path.touch()
    worker, worker_engine = _make_worker(
        database_path,
        operation_id=TIMEOUT_OPERATION_ID,
        process_handler=_sleeping_fixture,
        process_timeout_seconds=3.0,
    )

    try:
        submitted = worker.submit(
            "sina-fzb-capture",
            "process-v1",
            _manual_request(SYMBOL, "timeout-token"),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker.run_once)
            child_pid = _wait_for_marker(marker)
            outcome = future.result(timeout=8.0)

        assert outcome is not None and outcome.record is not None
        assert outcome.record.run_key == submitted.run_key
        assert outcome.record.status == "failed"
        assert outcome.publication is None
        assert outcome.record.error_text == "execution_failed:ProcessExecutionTimeoutError"
        _assert_process_reaped(child_pid)
        _assert_no_publication(worker_engine)
    finally:
        marker.unlink(missing_ok=True)
        worker_engine.dispose()


def test_run_ledger_cancel_cancels_and_reaps_active_child_without_publication(
    tmp_path: Path,
) -> None:
    marker = _private_marker_path("wave12-sina-child.started")
    marker.unlink(missing_ok=True)
    database_path = tmp_path / "sina-process-ledger-cancel.sqlite"
    database_path.touch()
    worker, worker_engine = _make_worker(
        database_path,
        operation_id=CANCEL_OPERATION_ID,
        process_handler=_sleeping_fixture,
        process_timeout_seconds=20.0,
    )
    request = _manual_request(SYMBOL, "ledger-cancel-token")
    submitted = worker.submit("sina-fzb-capture", "process-v1", request)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker.run_once)
            child_pid = _wait_for_marker(marker)
            active = worker.ledger.get(submitted.run_key)
            assert active is not None and active.status == "running"
            cancelled = worker.ledger.cancel(submitted.run_key, datetime.now(UTC))
            assert cancelled is not None and cancelled.status == "cancelled"
            outcome = future.result(timeout=8.0)

        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "cancelled"
        assert outcome.publication is None
        _assert_process_reaped(child_pid)
        _assert_no_publication(worker_engine)
    finally:
        marker.unlink(missing_ok=True)
        worker_engine.dispose()
