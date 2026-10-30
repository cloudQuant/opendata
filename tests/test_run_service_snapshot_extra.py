"""Additional offline checks for run snapshot identity and refusal boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, update

import opendata.pipeline.run_service as run_service_module
from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
    RunService,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


def _task(
    *, job_id: str = "snapshot-job", enabled: bool = True, schedule: bool = True
) -> TaskDefinition:
    """Return a reviewed task shape for canonical snapshot fixtures."""
    return TaskDefinition(
        job_id=job_id,
        version="v1",
        dataset_id="cn.snapshot.data",
        provider="provider-cn",
        model="snapshot-v1",
        schedule=(
            ScheduleMetadata(kind="cron", expression="0 16 * * 1-5") if schedule else None
        ),
        enabled=enabled,
    )


def _request(run_type: str) -> RunRequest:
    """Return one valid request with distinctive query, partition, and shard data."""
    common: dict[str, object] = {
        "run_type": run_type,
        "query": {"symbol": "600519.SH", "fields": ["close", "volume"]},
        "partition": {"trade_date": "2026-10-23"},
        "shard": "shard-a",
    }
    if run_type == "scheduled":
        common["occurrence"] = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
    elif run_type == "manual":
        common["idempotency_key"] = "caller-key-1"
    else:
        common.update(
            idempotency_key="caller-backfill-1",
            backfill_start=date(2026, 1, 1),
            backfill_end=date(2026, 10, 23),
        )
    return RunRequest.model_validate(common)


def _snapshot(run_type: str) -> tuple[str, str, dict[str, object]]:
    """Build the canonical fixture and parsed JSON envelope for one run mode."""
    identity = build_run_identity(_task(), _request(run_type))
    return (
        identity.canonical_json,
        identity.idempotency_key,
        json.loads(identity.canonical_json),
    )


def _reviewed_job(
    *, task: TaskDefinition | None = None, **declarations: object
) -> ReviewedJob:
    """Create a reviewed job whose optional sidecar declaration can be varied."""
    task = task or _task()
    return ReviewedJob(
        task=task,
        fetch=lambda *_args: {"rows": 1},
        normalize=lambda *_args: NormalizedBatch(
            records=(NormalizedRecord("600519.SH:2026-10-23", {"close": 1}),),
            schema_version="1",
            semantic_version="1",
            unit="CNY/share",
            adjustment_basis="none",
            quality={},
            provenance={},
        ),
        publish=lambda _connection, _task, _request, batch, _key: PublicationReceipt(
            "publication-1", task.dataset_id, len(batch.records)
        ),
        reviewed=True,
        **declarations,  # type: ignore[arg-type]
    )


def test_verified_snapshot_json_revalidates_issuer_digest_and_request_key() -> None:
    """A snapshot is exposed only while its issuer, hash, and run key still agree."""
    canonical_json, run_key, _snapshot_value = _snapshot("manual")
    payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    issuer = run_service_module._RUN_SNAPSHOT_IDENTITY_ISSUER
    identity = run_service_module._VerifiedRunSnapshot(
        run_key=run_key,
        payload_hash=payload_hash,
        canonical_json=canonical_json,
        _issuer=issuer,
    )

    context = RunExecutionContext(
        run_key=run_key,
        worker="snapshot-test",
        fencing_token=1,
        _checkpoint=lambda: None,
        _snapshot_identity=identity,
    )
    assert context._verified_snapshot_json() == canonical_json

    assert RunExecutionContext(
        run_key=run_key,
        worker="snapshot-test",
        fencing_token=1,
        _checkpoint=lambda: None,
    )._verified_snapshot_json() is None

    tampered_payload = json.loads(canonical_json)
    tampered_payload["run"]["business_request"]["partition"]["trade_date"] = "2026-10-22"
    tampered_json = json.dumps(tampered_payload, sort_keys=True, separators=(",", ":"))
    invalid_identities = (
        run_service_module._VerifiedRunSnapshot(
            run_key=run_key,
            payload_hash="0" * 64,
            canonical_json=canonical_json,
            _issuer=issuer,
        ),
        run_service_module._VerifiedRunSnapshot(
            run_key="f" * 64,
            payload_hash=payload_hash,
            canonical_json=canonical_json,
            _issuer=issuer,
        ),
        run_service_module._VerifiedRunSnapshot(
            run_key=run_key,
            payload_hash=payload_hash,
            canonical_json=tampered_json,
            _issuer=issuer,
        ),
        run_service_module._VerifiedRunSnapshot(
            run_key=run_key,
            payload_hash=payload_hash,
            canonical_json=canonical_json,
            _issuer=object(),
        ),
        object(),
    )
    for invalid_identity in invalid_identities:
        invalid_context = RunExecutionContext(
            run_key=run_key,
            worker="snapshot-test",
            fencing_token=1,
            _checkpoint=lambda: None,
            _snapshot_identity=invalid_identity,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="snapshot identity is invalid"):
            invalid_context._verified_snapshot_json()


@pytest.mark.parametrize("run_type", ["manual", "scheduled", "backfill"])
def test_request_snapshot_round_trip_preserves_business_identity(run_type: str) -> None:
    """Snapshot parsing restores query, partition, shard, and mode-specific fields."""
    _canonical_json, run_key, snapshot_value = _snapshot(run_type)

    reconstructed = run_service_module._request_from_snapshot(snapshot_value, run_key)
    expected = _request(run_type)

    assert reconstructed.run_type == expected.run_type
    assert reconstructed.query == expected.query
    assert reconstructed.partition == expected.partition
    assert reconstructed.shard == expected.shard
    assert reconstructed.occurrence == expected.occurrence
    assert reconstructed.backfill_start == expected.backfill_start
    assert reconstructed.backfill_end == expected.backfill_end
    if run_type == "scheduled":
        assert reconstructed.idempotency_key is None
    else:
        assert reconstructed.idempotency_key == (
            "sha256-" + hashlib.sha256(expected.idempotency_key.encode("utf-8")).hexdigest()
        )


@pytest.mark.parametrize("run_type", ["manual", "scheduled", "backfill"])
def test_snapshot_run_key_is_bound_to_the_exact_request_slot(run_type: str) -> None:
    """A well-shaped snapshot cannot be replayed under a different ledger key."""
    _canonical_json, _run_key, snapshot_value = _snapshot(run_type)

    with pytest.raises(run_service_module._SnapshotInvalidError, match="run_key_mismatch"):
        run_service_module._request_from_snapshot(snapshot_value, "0" * 64)


@pytest.mark.parametrize(
    ("run_type", "mutation", "error_code"),
    [
        (
            "scheduled",
            lambda run: run.update(occurrence=None),
            "snapshot_occurrence_invalid",
        ),
        (
            "scheduled",
            lambda run: run.update(backfill_range={"start": "2026-01-01", "end": "2026-01-02"}),
            "snapshot_backfill_invalid",
        ),
        (
            "manual",
            lambda run: run.update(backfill_range={"start": "2026-01-01", "end": "2026-01-02"}),
            "snapshot_backfill_invalid",
        ),
        (
            "backfill",
            lambda run: run.update(backfill_range={"start": "20260101", "end": "2026-01-02"}),
            "snapshot_backfill_invalid",
        ),
        (
            "backfill",
            lambda run: run.update(backfill_range={"start": "2026-02-30", "end": "2026-03-01"}),
            "snapshot_backfill_invalid",
        ),
        (
            "backfill",
            lambda run: run.update(backfill_range={"start": "2026-01-01"}),
            "snapshot_backfill_invalid",
        ),
    ],
)
def test_snapshot_run_mode_rejects_incompatible_metadata(
    run_type: str,
    mutation: Callable[[dict[str, object]], None],
    error_code: str,
) -> None:
    """Mode-specific dates and timestamps cannot be reinterpreted on replay."""
    _canonical_json, run_key, snapshot_value = _snapshot(run_type)
    run = snapshot_value["run"]
    assert isinstance(run, dict)
    mutation(run)

    with pytest.raises(run_service_module._SnapshotInvalidError, match=error_code):
        run_service_module._request_from_snapshot(snapshot_value, run_key)


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        (None, "snapshot_size_invalid"),
        ("\ud800", "snapshot_json_invalid"),
        ('{"job":{},"run":{}}', "snapshot_contract_invalid"),
    ],
)
def test_snapshot_parser_rejects_unencodable_or_incomplete_envelopes(
    value: object, error_code: str
) -> None:
    """Non-text, invalid Unicode, and incomplete contract envelopes fail closed."""
    with pytest.raises(run_service_module._SnapshotInvalidError, match=error_code):
        run_service_module._parse_snapshot(value)  # type: ignore[arg-type]


def test_snapshot_request_helpers_reject_untrusted_shapes() -> None:
    """Request and slot helpers reject malformed nested values before reconstruction."""
    malformed = (
        ({"run": "invalid"}, "snapshot_request_invalid"),
        ({"run": {"business_request": "invalid"}}, "snapshot_request_invalid"),
    )
    for snapshot_value, error_code in malformed:
        with pytest.raises(run_service_module._SnapshotInvalidError, match=error_code):
            run_service_module._request_from_snapshot(snapshot_value, "a" * 64)

    with pytest.raises(run_service_module._SnapshotInvalidError, match="snapshot_slot_invalid"):
        run_service_module._expected_scheduled_key({"job": None, "run": {}})
    with pytest.raises(run_service_module._SnapshotInvalidError, match="snapshot_slot_invalid"):
        run_service_module._expected_scheduled_key(
            {"job": {}, "run": {"business_request": None}}
        )
    with pytest.raises(run_service_module._SnapshotInvalidError, match="snapshot_slot_invalid"):
        run_service_module._expected_scheduled_key(
            {"job": {}, "run": {"business_request": {}, "occurrence": None}}
        )
    with pytest.raises(run_service_module._SnapshotInvalidError, match="snapshot_slot_invalid"):
        run_service_module._expected_run_key({"job": None, "run": {}}, "a" * 64)


@pytest.mark.parametrize(
    ("value", "error_code"),
    [
        ("2026-10-26T02:00:00", "snapshot_occurrence_invalid"),
        ("not-a-timestampZ", "snapshot_occurrence_invalid"),
        ("2026-10-26T02:00:00Z", "snapshot_occurrence_invalid"),
    ],
)
def test_snapshot_occurrence_must_be_exact_canonical_utc(
    value: str, error_code: str
) -> None:
    """UTC timestamp parsing rejects missing zones, bad dates, and omitted precision."""
    with pytest.raises(run_service_module._SnapshotInvalidError, match=error_code):
        run_service_module._parse_utc_datetime(value)


def test_run_context_builder_rejects_identity_from_another_run() -> None:
    """A validated run context cannot be built around another run's snapshot."""
    ledger = SimpleNamespace(
        enqueue=lambda *_args: None,
        claim=lambda *_args: None,
        heartbeat=lambda *_args: None,
        fail=lambda *_args: None,
        get=lambda *_args: None,
        commit_result=lambda *_args, **_kwargs: None,
    )
    service = RunService(ledger, ReviewedJobRegistry(), clock=lambda: NOW)  # type: ignore[arg-type]
    _canonical_json, run_key, _snapshot_value = _snapshot("manual")
    identity = run_service_module._VerifiedRunSnapshot(
        run_key="f" * 64,
        payload_hash="0" * 64,
        canonical_json="{}",
        _issuer=run_service_module._RUN_SNAPSHOT_IDENTITY_ISSUER,
    )
    record = SimpleNamespace(run_key=run_key, fencing_token=1)

    with pytest.raises(ValueError, match="run snapshot identity is invalid"):
        service._execution_context(
            record, "worker", timedelta(minutes=1), identity  # type: ignore[arg-type]
        )


def test_registry_rejects_invalid_review_flags_and_one_shot_schedule_contracts() -> None:
    """Review flags and disabled, unscheduled one-shot declarations are strict booleans."""
    valid_job = _reviewed_job()
    invalid_review_flag = replace(valid_job, reviewed=1)  # type: ignore[arg-type]
    invalid_one_shot_flag = replace(valid_job, one_shot_only=1)  # type: ignore[arg-type]
    enabled_one_shot = replace(valid_job, one_shot_only=True)
    scheduled_one_shot = replace(
        valid_job,
        task=_task(enabled=False, schedule=True),
        one_shot_only=True,
    )

    with pytest.raises(TypeError, match="reviewed must be a bool"):
        ReviewedJobRegistry([invalid_review_flag])
    with pytest.raises(TypeError, match="one_shot_only must be a bool"):
        ReviewedJobRegistry([invalid_one_shot_flag])
    with pytest.raises(ValueError, match="one-shot-only jobs must be disabled"):
        ReviewedJobRegistry([enabled_one_shot])
    with pytest.raises(ValueError, match="one-shot-only jobs must not declare a schedule"):
        ReviewedJobRegistry([scheduled_one_shot])


def test_commit_payload_declaration_fails_closed_on_incomplete_contracts() -> None:
    """Sidecars require exact bytes, bounded size, and one reviewed validation mode."""
    base = _reviewed_job()

    async def async_validator(_payload: bytes) -> bool:
        return True

    def generator_validator(_payload: bytes):
        yield True

    async def async_generator_validator(_payload: bytes):
        yield True

    invalid_declarations = (
        (replace(base, commit_payload_type=bytearray), TypeError, "exactly bytes"),
        (
            replace(base, commit_payload_type=bytes, max_commit_payload_bytes=True),
            ValueError,
            "max_commit_payload_bytes",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                validate_commit_payload=lambda _payload: True,
                decode_commit_payload=lambda _payload: object(),
                commit_context_type=object,
            ),
            TypeError,
            "cannot be combined",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                validate_commit_payload=object(),  # type: ignore[arg-type]
            ),
            TypeError,
            "explicit function",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                validate_commit_payload=async_validator,
            ),
            TypeError,
            "explicit function",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                validate_commit_payload=generator_validator,
            ),
            TypeError,
            "explicit function",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                validate_commit_payload=async_generator_validator,
            ),
            TypeError,
            "explicit function",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                commit_context_type=object,
            ),
            TypeError,
            "declared together",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                decode_commit_payload=lambda _payload: object(),
                commit_context_type=object,
            ),
            TypeError,
            "reviewed prepared-capture decoder",
        ),
        (
            replace(
                base,
                commit_payload_type=bytes,
                max_commit_payload_bytes=16,
                decode_commit_payload=lambda _payload: object(),
            ),
            TypeError,
            "decoder and context type",
        ),
    )
    for job, exception_type, message in invalid_declarations:
        with pytest.raises(exception_type, match=message):
            ReviewedJobRegistry([job])


def test_commit_payload_preflight_checks_presence_type_and_byte_limit() -> None:
    """Payloads must match the registered exact-byte contract before batch copying."""
    def validator(_payload: bytes) -> bool:
        return True

    registered = _reviewed_job(
        commit_payload_type=bytes,
        validate_commit_payload=validator,
        max_commit_payload_bytes=4,
    )
    cases = (
        (
            _reviewed_job(),
            b"data",
            run_service_module._CommitPayloadInvalidError,
            "commit_payload_not_registered",
        ),
        (registered, None, run_service_module._CommitPayloadInvalidError, "commit_payload_missing"),
        (
            registered,
            b"12345",
            run_service_module._CommitPayloadInvalidError,
            "commit_payload_size_invalid",
        ),
        (
            registered,
            "bytes",
            run_service_module._ResultInvalidError,
            "commit_payload_type_invalid",
        ),
        (registered, b"ok", None, None),
    )
    for job, payload, error_type, error_code in cases:
        if error_code is None:
            contract = RunService._preflight_commit_payload(job, payload)  # type: ignore[arg-type]
            assert contract.validator is validator
        else:
            with pytest.raises(error_type, match=error_code):
                RunService._preflight_commit_payload(job, payload)  # type: ignore[arg-type]


def test_commit_payload_validator_sanitizes_failure_and_requires_literal_true() -> None:
    """Validator errors and truthy non-booleans cannot authorize a transaction sidecar."""
    def raises(_payload: bytes) -> bool:
        raise RuntimeError("secret validator detail")

    validators = (
        (lambda _payload: True, True),
        (lambda _payload: False, False),
        (lambda _payload: 1, False),
        (raises, False),
    )
    RunService._validate_commit_payload(b"prepared", None)
    for validator, accepted in validators:
        if accepted:
            RunService._validate_commit_payload(b"prepared", validator)
        else:
            with pytest.raises(
                run_service_module._CommitPayloadInvalidError,
                match="commit_payload_validation_failed",
            ) as captured:
                RunService._validate_commit_payload(b"prepared", validator)
            assert "secret validator detail" not in str(captured.value)

    with pytest.raises(run_service_module._CommitPayloadInvalidError, match="type_invalid"):
        RunService._validate_commit_payload(None, lambda _payload: True)


def test_commit_payload_declaration_accepts_only_the_prepared_capture_decoder() -> None:
    """The exact reviewed decoder and output type form the supported decoder contract."""
    decoder, context_type = run_service_module._prepared_capture_decoder_contract()
    job = _reviewed_job(
        commit_payload_type=bytes,
        max_commit_payload_bytes=64,
        decode_commit_payload=decoder,
        commit_context_type=context_type,
    )

    contract = run_service_module._validate_commit_payload_declaration(job)

    assert contract is not None
    assert contract.decoder is decoder
    assert contract.context_type is context_type


def test_commit_payload_decoder_refuses_missing_throwing_and_wrong_result_contracts() -> None:
    """Decoder failures are translated and only the declared exact context type survives."""
    contract_type = type("PreparedContext", (), {})
    accepted = contract_type()
    good_contract = run_service_module._CommitPayloadContract(
        decoder=lambda _payload: accepted,
        context_type=contract_type,
    )
    assert RunService._decode_commit_payload(b"payload", good_contract) is accepted

    cases = (
        (None, run_service_module._CommitPayloadContract(), "type_invalid"),
        (b"payload", run_service_module._CommitPayloadContract(), "not_registered"),
        (
            b"payload",
            run_service_module._CommitPayloadContract(
                decoder=lambda _payload: (_ for _ in ()).throw(RuntimeError("secret decoder")),
                context_type=contract_type,
            ),
            "validation_failed",
        ),
        (
            b"payload",
            run_service_module._CommitPayloadContract(
                decoder=lambda _payload: object(),
                context_type=contract_type,
            ),
            "validation_failed",
        ),
    )
    for payload, contract, error_code in cases:
        with pytest.raises(run_service_module._CommitPayloadInvalidError, match=error_code):
            RunService._decode_commit_payload(payload, contract)


def test_sidecar_refusal_happens_before_publication_and_valid_bytes_reach_writer(
    tmp_path: Path,
) -> None:
    """Preloaded contexts and rejected sidecars fail before publish; valid bytes remain local."""
    tasks = (
        _task(job_id="preloaded-context"),
        _task(job_id="rejected-sidecar"),
        _task(job_id="accepted-sidecar"),
    )
    published: list[tuple[str, bytes | None]] = []

    def make_batch(*, commit_payload: bytes | None = None, context: object | None = None):
        return NormalizedBatch(
            records=(NormalizedRecord("600519.SH:2026-10-23", {"close": 1}),),
            schema_version="1",
            semantic_version="1",
            unit="CNY/share",
            adjustment_basis="none",
            quality={},
            provenance={},
            commit_payload=commit_payload,
            commit_context=context,
        )

    def publish(_connection, task, _request, batch, _key) -> PublicationReceipt:
        published.append((task.job_id, batch.commit_payload))
        return PublicationReceipt("publication-1", task.dataset_id, len(batch.records))

    preloaded_context_job = _reviewed_job(task=tasks[0])
    preloaded_context_job = replace(
        preloaded_context_job,
        normalize=lambda *_args: make_batch(context=object()),
        publish=publish,
    )
    rejecting_validator_job = _reviewed_job(
        task=tasks[1],
        commit_payload_type=bytes,
        validate_commit_payload=lambda _payload: 1,
        max_commit_payload_bytes=16,
    )
    rejecting_validator_job = replace(
        rejecting_validator_job,
        normalize=lambda *_args: make_batch(commit_payload=b"prepared"),
        publish=publish,
    )
    accepting_validator_job = _reviewed_job(
        task=tasks[2],
        commit_payload_type=bytes,
        validate_commit_payload=lambda payload: payload == b"prepared",
        max_commit_payload_bytes=16,
    )
    accepting_validator_job = replace(
        accepting_validator_job,
        normalize=lambda *_args: make_batch(commit_payload=b"prepared"),
        publish=publish,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'sidecar-boundaries.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    service = RunService(
        RunLedger(engine, clock_policy="caller"),
        ReviewedJobRegistry(
            [preloaded_context_job, rejecting_validator_job, accepting_validator_job]
        ),
        clock=lambda: NOW,
    )
    try:
        outcomes = []
        for job in (preloaded_context_job, rejecting_validator_job, accepting_validator_job):
            service.submit(
                job.task.job_id,
                job.task.version,
                RunRequest(run_type="manual", idempotency_key=f"request-{job.task.job_id}"),
            )
            outcomes.append(service.run_once("sidecar-worker", timedelta(minutes=2)))

        assert all(outcome is not None and outcome.record is not None for outcome in outcomes)
        assert [outcome.record.status for outcome in outcomes if outcome is not None] == [
            "failed",
            "failed",
            "succeeded",
        ]
        assert [outcome.record.error_text for outcome in outcomes[:2] if outcome is not None] == [
            "execution_failed:_CommitPayloadInvalidError",
            "execution_failed:_CommitPayloadInvalidError",
        ]
        assert published == [("accepted-sidecar", b"prepared")]
    finally:
        engine.dispose()


def test_sqlite_claim_fails_malformed_snapshot_before_handler_dispatch(tmp_path: Path) -> None:
    """A malformed persisted snapshot is failed and never reaches source or publish code."""
    fetched: list[bool] = []
    published: list[bool] = []
    task = _task()
    job = ReviewedJob(
        task=task,
        fetch=lambda *_args: fetched.append(True),
        normalize=lambda *_args: NormalizedBatch(
            records=(NormalizedRecord("600519.SH:2026-10-23", {"close": 1}),),
            schema_version="1",
            semantic_version="1",
            unit="CNY/share",
            adjustment_basis="none",
            quality={},
            provenance={},
        ),
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("publication-1", task.dataset_id, 1),
        )[1],
        reviewed=True,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot-failure.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine, clock_policy="caller")
    service = RunService(ledger, ReviewedJobRegistry([job]), clock=lambda: NOW)
    try:
        queued = service.submit(
            task.job_id,
            task.version,
            RunRequest(
                run_type="manual",
                query={"symbol": "600519.SH"},
                idempotency_key="bad-snapshot",
            ),
        )
        with engine.begin() as connection:
            connection.execute(
                update(RUN_LEDGER_TABLE)
                .where(RUN_LEDGER_TABLE.c.run_key == queued.run_key)
                .values(job_snapshot_json="{}")
            )

        outcome = service.run_once("snapshot-worker", timedelta(minutes=2))

        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        assert outcome.record.error_text == "execution_failed:_SnapshotInvalidError"
        assert fetched == []
        assert published == []
    finally:
        engine.dispose()
