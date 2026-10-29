"""Compose a disabled, single-instrument Fuyao financial capture job.

This module builds an explicit process adapter; it does not read settings,
discover a key, register a production job, create a SourceAttemptGate grant,
or schedule work. The archive and succeeded RunLedger transition share the
caller-provided SQL transaction. Raw artifact writes happen earlier and are
not atomic with that database transaction.

This slice has no supported production activation/configuration root, no
durable prepared-capture intent or crash recovery, and no authenticated
association between the caller's run key and the source transport attempt.
Attempt association is only a local trace match marked unverified. It handles
one explicitly bundled instrument and does not fan out across all A shares.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from opendata.data.providers.ths.endpoints import (
    build_financial_statements_request,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.pipeline.financial_observation_store import (
    FinancialObservationStoreReceipt,
    PreparedCapture,
    persist_prepared_financial_observation_capture,
)
from opendata.pipeline.financial_prepared_codec import (
    FinancialPreparedCaptureCodecError,
    decode_prepared_capture,
)
from opendata.pipeline.fuyao_financial_process import (
    MAX_FUYAO_CAPTURE_PAYLOAD_BYTES,
    execute_fuyao_financial_request,
)
from opendata.pipeline.process_executor import (
    FUYAO_PROCESS_OPERATION_ID,
    MAX_PARAMETER_BYTES,
    MAX_RESULT_BYTES,
    MAX_RESULT_TEXT_BYTES,
    FuyaoProcessDeploymentBinding,
    JsonValue,
    ProcessExecutionResult,
    ReviewedProcessExecutor,
    ReviewedProcessRegistry,
)
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from sqlalchemy.engine import Connection

__all__ = [
    "FuyaoFinancialProcessCapture",
    "FuyaoFinancialProcessFetchHandler",
    "build_fuyao_financial_backfill_payload",
    "build_fuyao_financial_job",
    "build_fuyao_financial_manual_payload",
    "build_fuyao_financial_process_source",
    "normalize_fuyao_financial_capture",
    "publish_prepared_fuyao_financial_capture",
]

_JOB_ID = "fuyao-financial-observation"
_JOB_VERSION = "process-v1"
_DATASET_ID = "financial_observation_archive"
_PROVIDER = "ths"
_MODEL = "financial_statement"
_SCHEMA_VERSION = "financial-observation-archive-v1"
_SEMANTIC_VERSION = "ths-fuyao-current-as-observed-v1"
_UNIT = "source-native-operator-declared"
_ADJUSTMENT_BASIS = "not-applicable-source-native"
_PROCESS_TIMEOUT_SECONDS = 45.0
_MAX_QUERY_WINDOW_DAYS = 3650
_INSTRUMENT_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_STATEMENTS = frozenset({"income", "balance", "cashflow"})
_PERIODICITIES = frozenset({"annual", "quarterly"})
_REQUEST_FIELDS = frozenset({"statement_type", "periodicity", "start_date", "end_date", "limit"})
_RESULT_FIELDS = frozenset(
    {
        "attempt_association_status",
        "authorization_status",
        "capture_manifest_sha256",
        "coverage_assessment",
        "financial_bundle_sha256",
        "instrument_id",
        "observation_count",
        "observation_ids",
        "periodicity",
        "prepared_capture_b64",
        "quarantined_row_count",
        "request_summary_sha256",
        "retention_class",
        "source_history_limit",
        "source_policy_sha256",
        "source_request_id_match_status",
        "statement_type",
        "transport_max_attempts",
        "unit_mapping_declaration_status",
        "unit_mapping_sha256_declared",
        "unit_mapping_version_declared",
    }
)
_MAX_CAPTURE_B64_CHARS = 4 * ((MAX_FUYAO_CAPTURE_PAYLOAD_BYTES + 2) // 3)
_PROCESS_REQUEST_ERROR = "fuyao_financial_process_request_invalid"
_PROCESS_RESULT_ERROR = "fuyao_financial_process_result_invalid"
_CAPTURE_ERROR = "fuyao_financial_capture_invalid"
_TASK_ERROR = "fuyao_financial_task_definition_invalid"

_StatementType = Literal["income", "balance", "cashflow"]
_Periodicity = Literal["annual", "quarterly"]


@dataclass(frozen=True, slots=True)
class FuyaoFinancialProcessCapture:
    """Parent-validated process response for one instrument and query.

    The prepared DTO is checked outside the warehouse transaction. The exact
    canonical bytes are retained for the runner's allowlisted decoder, which
    binds its decoded object to the publication batch before calling the
    publisher.
    """

    instrument_id: str
    statement_type: _StatementType
    periodicity: _Periodicity
    observation_ids: tuple[str, ...]
    quarantined_row_count: int
    prepared_payload: bytes = field(repr=False)
    prepared_capture: PreparedCapture = field(repr=False, compare=False)
    capture_manifest_sha256: str
    request_summary_sha256: str
    financial_bundle_sha256: str
    source_policy_sha256: str
    retention_class: str
    source_history_limit: str
    attempt_association_status: Literal["local_trace_matched_unverified"]
    source_request_id_match_status: Literal["matched_unverified", "unavailable_unverified"]
    unit_mapping_declaration_status: Literal["operator_declared_unverified"]
    unit_mapping_version_declared: str
    unit_mapping_sha256_declared: str
    authorization_status: Literal["operator_policy_allowed_unverified"]
    coverage_assessment: Literal["not_assessed"]


def build_fuyao_financial_manual_payload(
    instrument_id: object,
    *,
    statement_type: object,
    periodicity: object,
    start_date: object = None,
    end_date: object = None,
    limit: object = None,
    idempotency_key: object,
) -> dict[str, JsonValue]:
    """Build one explicit manual query without deployment or source controls.

    A date window is inclusive and the elapsed distance between its endpoints
    is limited to 3,650 days, so it can include up to 3,651 calendar dates. A
    limit query requests up to 20 recent source periods. Neither mode implies
    complete history; callers must use bounded, reconciled windows for
    historical recovery.

    Args:
        instrument_id: One canonical exchange-qualified A-share identity.
        statement_type: ``income``, ``balance`` or ``cashflow``.
        periodicity: ``annual`` or ``quarterly``.
        start_date: Inclusive ISO date; must be paired with ``end_date``.
        end_date: Inclusive ISO date; must be paired with ``start_date``.
        limit: Recent source-period limit, mutually exclusive with dates.
        idempotency_key: Caller-supplied stable run token.

    Returns:
        The request payload accepted by ``RunRequest`` for one manual run.

    Raises:
        ValueError: If the instrument or query is outside the fixed contract.
    """
    symbol = _canonical_instrument(instrument_id)
    query = _validated_query_values(
        statement_type=statement_type,
        periodicity=periodicity,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )
    request = _manual_request(symbol, query, idempotency_key=idempotency_key)
    return _request_payload(request)


def build_fuyao_financial_backfill_payload(
    instrument_id: object,
    *,
    statement_type: object,
    periodicity: object,
    start_date: object,
    end_date: object,
    idempotency_key: object,
) -> dict[str, JsonValue]:
    """Build one explicit backfill window for one instrument and statement.

    Backfill windows are bounded to an endpoint distance of at most 3,650
    days, which permits at most 3,651 inclusive calendar dates. Full-history
    recovery therefore requires multiple independently identified windows and
    reconciliation; this helper does not fan out or infer listing dates.

    Args:
        instrument_id: One canonical exchange-qualified A-share identity.
        statement_type: ``income``, ``balance`` or ``cashflow``.
        periodicity: ``annual`` or ``quarterly``.
        start_date: Inclusive ISO date for this bounded backfill window.
        end_date: Inclusive ISO date for this bounded backfill window.
        idempotency_key: Caller-supplied stable run token.

    Returns:
        The request payload accepted by ``RunRequest`` for one backfill run.

    Raises:
        ValueError: If the instrument, window, or query is outside the contract.
    """
    symbol = _canonical_instrument(instrument_id)
    query = _validated_query_values(
        statement_type=statement_type,
        periodicity=periodicity,
        start_date=start_date,
        end_date=end_date,
        limit=None,
    )
    start = _parse_iso_date(query["start_date"])
    end = _parse_iso_date(query["end_date"])
    if type(idempotency_key) is not str:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    try:
        request = RunRequest(
            run_type="backfill",
            query=query,
            partition={"instrument_id": symbol},
            shard=None,
            idempotency_key=idempotency_key,
            backfill_start=start,
            backfill_end=end,
        )
    except (TypeError, ValidationError):
        raise ValueError(_PROCESS_REQUEST_ERROR) from None
    return _request_payload(request)


def build_fuyao_financial_process_source(
    binding: FuyaoProcessDeploymentBinding,
) -> FuyaoFinancialProcessFetchHandler:
    """Build the one fixed process handler from explicit deployment binding.

    The registered function and operation identity are constants in reviewed
    code. The API key and independent bundle/policy pins enter only through the
    typed deployment binding; no settings, dotenv, source control, or user run
    data are consulted here.

    Args:
        binding: Explicit trusted deployment configuration supplied by the
            application after its own secure configuration loading.

    Returns:
        A process-backed handler suitable for ``PersistentRunWorker``.

    Raises:
        TypeError: If the binding is not the exact deployment DTO.
        ValueError: If its explicit fields fail closed validation.
    """
    checked_binding = _copy_deployment_binding(binding)
    registry = ReviewedProcessRegistry(
        {FUYAO_PROCESS_OPERATION_ID: execute_fuyao_financial_request}
    )
    executor = ReviewedProcessExecutor(
        registry,
        max_parameter_bytes=min(MAX_PARAMETER_BYTES, 16 * 1024),
        max_result_bytes=MAX_RESULT_BYTES,
        max_result_text_bytes=MAX_RESULT_TEXT_BYTES,
        poll_interval_seconds=0.05,
        checkpoint_interval_seconds=1.0,
        shutdown_grace_seconds=0.25,
    )
    return FuyaoFinancialProcessFetchHandler(executor, checked_binding)


class FuyaoFinancialProcessFetchHandler(ProcessFetchHandler):
    """Adapt the fixed Fuyao process operation to the reviewed run contract."""

    def __init__(
        self,
        executor: ReviewedProcessExecutor,
        binding: FuyaoProcessDeploymentBinding,
    ) -> None:
        """Bind one exact process executor, trusted deployment, and deadline."""
        if type(executor) is not ReviewedProcessExecutor:
            raise TypeError("executor must be an exact ReviewedProcessExecutor")
        checked_binding = _copy_deployment_binding(binding)
        super().__init__(
            executor,
            FUYAO_PROCESS_OPERATION_ID,
            _unused_process_parameter_builder,
            _identity_process_result_decoder,
            timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
        )
        self._fuyao_binding = checked_binding

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        """Run the exact Fuyao binding with server-owned run key and checkpoint."""
        if type(context) is not RunExecutionContext:
            raise TypeError("context must be an exact RunExecutionContext")
        context.checkpoint()
        parameters = _build_process_parameters(request, context.run_key)
        result = self._executor.run_fuyao(
            self._fuyao_binding,
            parameters,
            timeout_seconds=self._timeout_seconds,
            checkpoint=context.checkpoint,
        )
        if type(result) is not ProcessExecutionResult:
            raise ValueError(_PROCESS_RESULT_ERROR)
        return _decode_process_result(
            result.value,
            parameters=parameters,
            binding=self._fuyao_binding,
        )


def build_fuyao_financial_job(
    task: TaskDefinition,
    *,
    deployment_binding: FuyaoProcessDeploymentBinding,
) -> ReviewedJob:
    """Build a disabled, unreviewed job; this factory never registers it.

    Args:
        task: Exact ``fuyao-financial-observation/process-v1`` metadata. It
            must remain disabled and have no schedule.
        deployment_binding: Explicit deployment-owned credentials, bundle and
            policy pins, source-gate identity, and raw artifact location.

    Returns:
        A ``ReviewedJob`` using the fenced archive publisher and a strict
        prepared-capture sidecar decoder.

    Raises:
        TypeError: If either dependency has an unsupported exact type.
        ValueError: If the task identity or disabled state differs from the
            fixed job contract.
    """
    checked_task = _validated_task(task)
    _copy_deployment_binding(deployment_binding)
    if (
        checked_task.job_id != _JOB_ID
        or checked_task.version != _JOB_VERSION
        or checked_task.dataset_id != _DATASET_ID
        or checked_task.provider != _PROVIDER
        or checked_task.model != _MODEL
        or checked_task.enabled
        or checked_task.schedule is not None
    ):
        raise ValueError(_TASK_ERROR)
    return ReviewedJob(
        task=checked_task,
        fetch=build_fuyao_financial_process_source(deployment_binding),
        normalize=normalize_fuyao_financial_capture,
        publish=publish_prepared_fuyao_financial_capture,
        reviewed=False,
        commit_payload_type=bytes,
        validate_commit_payload=None,
        max_commit_payload_bytes=MAX_FUYAO_CAPTURE_PAYLOAD_BYTES,
        decode_commit_payload=decode_prepared_capture,
        commit_context_type=PreparedCapture,
    )


def normalize_fuyao_financial_capture(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Build a ledger-safe receipt batch and same-run prepared-capture sidecar.

    The source-native archive itself is not expanded into copied business
    values here. Its bounded PreparedCapture is decoded and persisted by the
    registered archive store through the caller's fenced SQL transaction.
    Record IDs reflect the decoded observation IDs; quarantined rows are
    counted separately and coverage remains explicitly unassessed.

    Args:
        raw: Parent-validated fixed process result.
        request: The immutable run request that names the instrument/query.
        context: Parent run context with its ledger idempotency key.

    Returns:
        A bounded result batch whose encoded sidecar is decoded outside the
        commit callback by ``RunService``.

    Raises:
        TypeError: If runtime inputs do not use the exact reviewed types.
        ValueError: If the response scope or prepared capture is inconsistent.
    """
    if type(context) is not RunExecutionContext:
        raise TypeError("context must be an exact RunExecutionContext")
    context.checkpoint()
    parameters = _build_process_parameters(request, context.run_key)
    capture = _validate_capture(raw, parameters=parameters)
    records = tuple(
        NormalizedRecord(
            business_key=observation_id,
            payload={"observation_id": observation_id},
        )
        for observation_id in capture.observation_ids
    )
    context.checkpoint()
    return NormalizedBatch(
        records=records,
        schema_version=_SCHEMA_VERSION,
        semantic_version=_SEMANTIC_VERSION,
        unit=_UNIT,
        adjustment_basis=_ADJUSTMENT_BASIS,
        quality={
            "attempt_association_status": capture.attempt_association_status,
            "coverage_assessment": capture.coverage_assessment,
            "observation_count": len(capture.observation_ids),
            "quarantined_row_count": capture.quarantined_row_count,
            # Keep the safe operator-policy declaration visible without using
            # the generic runner's credential-sensitive `authorization` key.
            "source_policy_decision_status": capture.authorization_status,
            "source_request_id_match_status": capture.source_request_id_match_status,
            "unit_mapping_declaration_status": capture.unit_mapping_declaration_status,
        },
        provenance={
            "capture_manifest_sha256": capture.capture_manifest_sha256,
            "financial_bundle_sha256": capture.financial_bundle_sha256,
            "instrument_id": capture.instrument_id,
            "periodicity": capture.periodicity,
            "provider_id": _PROVIDER,
            "raw_dataset_id": "ths_financial_statement",
            "retention_class": capture.retention_class,
            "request_summary_sha256": capture.request_summary_sha256,
            "source_history_limit": capture.source_history_limit,
            "source_id": "ths_fuyao",
            "source_policy_sha256": capture.source_policy_sha256,
            "statement_type": capture.statement_type,
            "unit_mapping_sha256_declared": capture.unit_mapping_sha256_declared,
            "unit_mapping_version_declared": capture.unit_mapping_version_declared,
        },
        commit_payload=capture.prepared_payload,
    )


def publish_prepared_fuyao_financial_capture(
    connection: Connection,
    task: TaskDefinition,
    request: RunRequest,
    batch: NormalizedBatch,
    idempotency_key: str,
) -> PublicationReceipt:
    """Persist only the runner-decoded DTO on its fenced warehouse connection.

    Args:
        connection: The exact active connection supplied by ``RunLedger``.
        task: The registered fixed job metadata.
        request: The claimed one-instrument run request.
        batch: The runner-reconstructed batch with decoded commit context.
        idempotency_key: The current run key; no second database is opened.

    Returns:
        A receipt for the logical observation IDs represented by the batch.

    Raises:
        ValueError: If the task, request, context, or receipt scope is invalid.
    """
    if (
        type(task) is not TaskDefinition
        or task.job_id != _JOB_ID
        or task.version != _JOB_VERSION
        or task.dataset_id != _DATASET_ID
        or type(batch) is not NormalizedBatch
        or type(batch.commit_context) is not PreparedCapture
        or type(idempotency_key) is not str
        or _SHA256_PATTERN.fullmatch(idempotency_key) is None
        or type(batch.records) is not tuple
        or type(batch.provenance) is not dict
    ):
        raise ValueError(_CAPTURE_ERROR)
    parameters = _build_process_parameters(request, idempotency_key)
    if type(batch.commit_context) is not PreparedCapture:
        raise ValueError(_CAPTURE_ERROR)
    prepared = batch.commit_context
    provenance = batch.provenance
    expected_records = tuple(
        observation_id for observation_id, _values in prepared._observation_values
    )
    if (
        any(type(record) is not NormalizedRecord for record in batch.records)
        or tuple(record.business_key for record in batch.records) != expected_records
        or any(
            type(record.payload) is not dict
            or record.payload != {"observation_id": record.business_key}
            for record in batch.records
        )
    ):
        raise ValueError(_CAPTURE_ERROR)
    manifest_hash = provenance.get("capture_manifest_sha256")
    request_hash = provenance.get("request_summary_sha256")
    unit_version = provenance.get("unit_mapping_version_declared")
    unit_hash = provenance.get("unit_mapping_sha256_declared")
    retention_class = provenance.get("retention_class")
    source_history_limit = provenance.get("source_history_limit")
    if any(
        type(value) is not str
        for value in (
            manifest_hash,
            request_hash,
            unit_version,
            unit_hash,
            retention_class,
            source_history_limit,
        )
    ):
        raise ValueError(_CAPTURE_ERROR)
    _validate_prepared_capture_scope(
        prepared,
        parameters=parameters,
        capture_manifest_sha256=cast("str", manifest_hash),
        request_summary_sha256=cast("str", request_hash),
        unit_mapping_version_declared=cast("str", unit_version),
        unit_mapping_sha256_declared=cast("str", unit_hash),
        retention_class=cast("str", retention_class),
        source_history_limit=cast("str", source_history_limit),
    )
    receipt: FinancialObservationStoreReceipt = persist_prepared_financial_observation_capture(
        connection,
        prepared=prepared,
    )
    if type(receipt) is not FinancialObservationStoreReceipt:
        raise ValueError(_CAPTURE_ERROR)
    return PublicationReceipt(
        publication_id=receipt.capture_id,
        dataset_id=task.dataset_id,
        record_count=len(batch.records),
    )


def _build_process_parameters(request: RunRequest, run_key: object) -> dict[str, JsonValue]:
    """Validate the run payload and bind only seven bounded business fields."""
    checked_request = _validated_run_request(request)
    if (
        type(checked_request.partition) is not dict
        or set(checked_request.partition) != {"instrument_id"}
        or checked_request.shard is not None
        or checked_request.run_type not in {"scheduled", "manual", "backfill"}
    ):
        raise ValueError(_PROCESS_REQUEST_ERROR)
    instrument = _canonical_instrument(checked_request.partition.get("instrument_id"))
    if type(run_key) is not str or _SHA256_PATTERN.fullmatch(run_key) is None:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    query = checked_request.query
    if type(query) is not dict or set(query) != _REQUEST_FIELDS:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    values = _validated_query_values(
        statement_type=query.get("statement_type"),
        periodicity=query.get("periodicity"),
        start_date=query.get("start_date"),
        end_date=query.get("end_date"),
        limit=query.get("limit"),
    )
    if checked_request.run_type == "backfill":
        start = _parse_iso_date(values["start_date"])
        end = _parse_iso_date(values["end_date"])
        if checked_request.backfill_start != start or checked_request.backfill_end != end:
            raise ValueError(_PROCESS_REQUEST_ERROR)
    elif checked_request.backfill_start is not None or checked_request.backfill_end is not None:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    return {
        "instrument_id": instrument,
        "statement_type": values["statement_type"],
        "periodicity": values["periodicity"],
        "start_date": values["start_date"],
        "end_date": values["end_date"],
        "limit": values["limit"],
        "run_key": run_key,
    }


def _decode_process_result(
    value: object,
    *,
    parameters: Mapping[str, JsonValue],
    binding: FuyaoProcessDeploymentBinding,
) -> FuyaoFinancialProcessCapture:
    """Strictly decode the bounded result and independently check store scope."""
    if type(value) is not dict or set(value) != _RESULT_FIELDS:
        raise ValueError(_PROCESS_RESULT_ERROR)
    try:
        if (
            type(value["transport_max_attempts"]) is not int
            or value["transport_max_attempts"] != binding.transport_max_attempts
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        instrument_id = value["instrument_id"]
        statement_type = value["statement_type"]
        periodicity = value["periodicity"]
        payload_b64 = value["prepared_capture_b64"]
        observation_ids_value = value["observation_ids"]
        quarantined_count = value["quarantined_row_count"]
        if (
            type(instrument_id) is not str
            or type(statement_type) is not str
            or type(periodicity) is not str
            or type(payload_b64) is not str
            or len(payload_b64) > _MAX_CAPTURE_B64_CHARS
            or type(observation_ids_value) is not list
            or len(observation_ids_value) > 1024
            or type(quarantined_count) is not int
            or not 0 <= quarantined_count <= 64
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        if (
            instrument_id != parameters["instrument_id"]
            or statement_type != parameters["statement_type"]
            or periodicity != parameters["periodicity"]
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        encoded = payload_b64.encode("ascii", errors="strict")
        prepared_payload = base64.b64decode(encoded, validate=True)
        if (
            not prepared_payload
            or len(prepared_payload) > MAX_FUYAO_CAPTURE_PAYLOAD_BYTES
            or base64.b64encode(prepared_payload) != encoded
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        prepared = decode_prepared_capture(prepared_payload)
        observation_ids = _validated_observation_ids(observation_ids_value)
        if type(value["observation_count"]) is not int or value["observation_count"] != len(
            observation_ids
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        if (
            type(value["attempt_association_status"]) is not str
            or value["attempt_association_status"] != "local_trace_matched_unverified"
            or type(value["authorization_status"]) is not str
            or value["authorization_status"] != "operator_policy_allowed_unverified"
            or type(value["coverage_assessment"]) is not str
            or value["coverage_assessment"] != "not_assessed"
            or type(value["source_request_id_match_status"]) is not str
            or value["source_request_id_match_status"]
            not in {"matched_unverified", "unavailable_unverified"}
            or type(value["unit_mapping_declaration_status"]) is not str
            or value["unit_mapping_declaration_status"] != "operator_declared_unverified"
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        hashes = (
            "capture_manifest_sha256",
            "financial_bundle_sha256",
            "request_summary_sha256",
            "source_policy_sha256",
            "unit_mapping_sha256_declared",
        )
        if any(
            type(value[name]) is not str or _SHA256_PATTERN.fullmatch(value[name]) is None
            for name in hashes
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        for name, expected in (
            ("financial_bundle_sha256", binding.financial_bundle_sha256),
            ("source_policy_sha256", binding.source_policy_sha256),
            ("retention_class", binding.retention_class),
            ("source_history_limit", binding.source_history_limit),
        ):
            if type(value[name]) is not str or value[name] != expected:
                raise ValueError(_PROCESS_RESULT_ERROR)
        unit_version = value["unit_mapping_version_declared"]
        if (
            type(unit_version) is not str
            or not unit_version
            or len(unit_version) > 128
            or any(ord(character) < 32 for character in unit_version)
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        _validate_prepared_capture_scope(
            prepared,
            parameters=parameters,
            capture_manifest_sha256=value["capture_manifest_sha256"],
            request_summary_sha256=value["request_summary_sha256"],
            unit_mapping_version_declared=unit_version,
            unit_mapping_sha256_declared=value["unit_mapping_sha256_declared"],
            retention_class=binding.retention_class,
            source_history_limit=binding.source_history_limit,
            observation_ids=observation_ids,
            quarantined_row_count=quarantined_count,
        )
        return FuyaoFinancialProcessCapture(
            instrument_id=instrument_id,
            statement_type=cast("_StatementType", statement_type),
            periodicity=cast("_Periodicity", periodicity),
            observation_ids=observation_ids,
            quarantined_row_count=quarantined_count,
            prepared_payload=prepared_payload,
            prepared_capture=prepared,
            capture_manifest_sha256=value["capture_manifest_sha256"],
            request_summary_sha256=value["request_summary_sha256"],
            financial_bundle_sha256=value["financial_bundle_sha256"],
            source_policy_sha256=value["source_policy_sha256"],
            retention_class=binding.retention_class,
            source_history_limit=binding.source_history_limit,
            attempt_association_status="local_trace_matched_unverified",
            source_request_id_match_status=cast(
                "Literal['matched_unverified', 'unavailable_unverified']",
                value["source_request_id_match_status"],
            ),
            unit_mapping_declaration_status="operator_declared_unverified",
            unit_mapping_version_declared=unit_version,
            unit_mapping_sha256_declared=value["unit_mapping_sha256_declared"],
            authorization_status="operator_policy_allowed_unverified",
            coverage_assessment="not_assessed",
        )
    except (
        FinancialPreparedCaptureCodecError,
        UnicodeEncodeError,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        raise ValueError(_PROCESS_RESULT_ERROR) from None


def _validate_capture(
    raw: object, *, parameters: Mapping[str, JsonValue]
) -> FuyaoFinancialProcessCapture:
    """Recheck a typed capture before producing the commit sidecar."""
    if type(raw) is not FuyaoFinancialProcessCapture:
        raise ValueError(_CAPTURE_ERROR)
    if (
        type(raw.instrument_id) is not str
        or raw.instrument_id != parameters["instrument_id"]
        or raw.statement_type != parameters["statement_type"]
        or raw.periodicity != parameters["periodicity"]
        or type(raw.prepared_payload) is not bytes
        or not 1 <= len(raw.prepared_payload) <= MAX_FUYAO_CAPTURE_PAYLOAD_BYTES
        or type(raw.prepared_capture) is not PreparedCapture
    ):
        raise ValueError(_CAPTURE_ERROR)
    _validate_prepared_capture_scope(
        raw.prepared_capture,
        parameters=parameters,
        capture_manifest_sha256=raw.capture_manifest_sha256,
        request_summary_sha256=raw.request_summary_sha256,
        unit_mapping_version_declared=raw.unit_mapping_version_declared,
        unit_mapping_sha256_declared=raw.unit_mapping_sha256_declared,
        retention_class=raw.retention_class,
        source_history_limit=raw.source_history_limit,
        observation_ids=raw.observation_ids,
        quarantined_row_count=raw.quarantined_row_count,
    )
    return raw


def _validate_prepared_capture_scope(
    prepared: PreparedCapture,
    *,
    parameters: Mapping[str, JsonValue],
    capture_manifest_sha256: str | None,
    request_summary_sha256: str | None,
    unit_mapping_version_declared: str | None = None,
    unit_mapping_sha256_declared: str | None = None,
    retention_class: str | None = None,
    source_history_limit: str | None = None,
    observation_ids: tuple[str, ...] | None = None,
    quarantined_row_count: int | None = None,
) -> None:
    """Match the DTO's immutable SQL projection to exact request scope."""
    if type(prepared) is not PreparedCapture:
        raise ValueError(_CAPTURE_ERROR)
    try:
        fields = dict(prepared._capture_values)
        expected_digest = _expected_request_summary_sha256(parameters)
        expected_operation = f"{parameters['statement_type']}_{parameters['periodicity']}"
        if (
            prepared._capture_id != fields["capture_id"]
            or fields["provider_id"] != _PROVIDER
            or fields["dataset_id"] != "ths_financial_statement"
            or fields["operation_id"] != expected_operation
            or fields["source_id"] != "ths_fuyao"
            or fields["instrument_id"] != parameters["instrument_id"]
            or fields["statement_type"] != parameters["statement_type"]
            or fields["periodicity"] != parameters["periodicity"]
            or fields["http_status"] != 200
            or fields["request_summary_sha256"] != expected_digest
            or (
                capture_manifest_sha256 is not None
                and fields["raw_manifest_sha256"] != capture_manifest_sha256
            )
            or (
                request_summary_sha256 is not None
                and fields["request_summary_sha256"] != request_summary_sha256
            )
            or (
                unit_mapping_version_declared is not None
                and fields["native_unit_mapping_version_declared"] != unit_mapping_version_declared
            )
            or (
                unit_mapping_sha256_declared is not None
                and fields["native_unit_mapping_sha256_declared"] != unit_mapping_sha256_declared
            )
            or (retention_class is not None and fields["retention_class"] != retention_class)
            or (
                source_history_limit is not None
                and fields["source_history_limit"] != source_history_limit
            )
            or fields["unit_mapping_declaration_status"] != "operator_declared"
        ):
            raise ValueError(_CAPTURE_ERROR)
        actual_observation_ids = tuple(
            observation_id for observation_id, _values in prepared._observation_values
        )
        actual_quarantine_count = len(prepared._quarantine_values)
        if observation_ids is not None and actual_observation_ids != observation_ids:
            raise ValueError(_CAPTURE_ERROR)
        if quarantined_row_count is not None and actual_quarantine_count != quarantined_row_count:
            raise ValueError(_CAPTURE_ERROR)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ValueError(_CAPTURE_ERROR) from None


def _expected_request_summary_sha256(parameters: Mapping[str, JsonValue]) -> str:
    """Compute the same bounded caller-side digest as the fixed fetcher."""
    start_text = parameters["start_date"]
    end_text = parameters["end_date"]
    start = _parse_iso_date(start_text) if start_text is not None else None
    end = _parse_iso_date(end_text) if end_text is not None else None
    limit_value = parameters["limit"]
    request = build_financial_statements_request(
        symbol=_canonical_instrument(parameters["instrument_id"]),
        period=cast("str", parameters["periodicity"]),
        start=start,
        end=end,
        limit=cast("int | None", limit_value),
    )
    return request_summary_sha256(
        request,
        allowed_fields={"thscode", "period", "start", "end", "limit"},
    )


def _validated_observation_ids(value: list[object]) -> tuple[str, ...]:
    """Validate a bounded ordered observation identity list without coercion."""
    result: list[str] = []
    seen: set[str] = set()
    for observation_id in value:
        if (
            type(observation_id) is not str
            or _SHA256_PATTERN.fullmatch(observation_id) is None
            or observation_id in seen
        ):
            raise ValueError(_PROCESS_RESULT_ERROR)
        result.append(observation_id)
        seen.add(observation_id)
    return tuple(result)


def _validated_query_values(
    *,
    statement_type: object,
    periodicity: object,
    start_date: object,
    end_date: object,
    limit: object,
) -> dict[str, JsonValue]:
    """Validate statement, periodicity, and one bounded window or limit."""
    if (
        type(statement_type) is not str
        or statement_type not in _STATEMENTS
        or type(periodicity) is not str
        or periodicity not in _PERIODICITIES
        or (start_date is None) != (end_date is None)
    ):
        raise ValueError(_PROCESS_REQUEST_ERROR)
    if start_date is None:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError(_PROCESS_REQUEST_ERROR)
        start_value: str | None = None
        end_value: str | None = None
    else:
        if (
            type(start_date) is not str
            or type(end_date) is not str
            or _DATE_PATTERN.fullmatch(start_date) is None
            or _DATE_PATTERN.fullmatch(end_date) is None
            or limit is not None
        ):
            raise ValueError(_PROCESS_REQUEST_ERROR)
        start = _parse_iso_date(start_date)
        end = _parse_iso_date(end_date)
        if start > end or end - start > timedelta(days=_MAX_QUERY_WINDOW_DAYS):
            raise ValueError(_PROCESS_REQUEST_ERROR)
        start_value, end_value = start.isoformat(), end.isoformat()
    return {
        "statement_type": statement_type,
        "periodicity": periodicity,
        "start_date": start_value,
        "end_date": end_value,
        "limit": limit,
    }


def _parse_iso_date(value: object) -> date:
    """Parse one exact canonical ISO calendar date."""
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(_PROCESS_REQUEST_ERROR) from None
    if parsed.isoformat() != value:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    return parsed


def _canonical_instrument(value: object) -> str:
    """Validate one exact exchange-qualified A-share identifier."""
    if type(value) is not str or _INSTRUMENT_PATTERN.fullmatch(value) is None:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    return value


def _manual_request(
    instrument_id: str,
    query: dict[str, JsonValue],
    *,
    idempotency_key: object,
) -> RunRequest:
    """Construct a strict manual run snapshot without mutating caller values."""
    if type(idempotency_key) is not str:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    try:
        return RunRequest(
            run_type="manual",
            query=query,
            partition={"instrument_id": instrument_id},
            shard=None,
            idempotency_key=idempotency_key,
        )
    except (TypeError, ValidationError):
        raise ValueError(_PROCESS_REQUEST_ERROR) from None


def _request_payload(request: RunRequest) -> dict[str, JsonValue]:
    """Return the dispatcher payload while omitting null run-only fields."""
    payload: dict[str, JsonValue] = {
        "query": request.query,
        "partition": request.partition,
        "shard": request.shard,
    }
    if request.idempotency_key is not None:
        payload["idempotency_key"] = request.idempotency_key
    if request.run_type == "backfill":
        if request.backfill_start is None or request.backfill_end is None:
            raise ValueError(_PROCESS_REQUEST_ERROR)
        payload["backfill_start"] = request.backfill_start.isoformat()
        payload["backfill_end"] = request.backfill_end.isoformat()
    return payload


def _validated_run_request(request: RunRequest) -> RunRequest:
    """Strictly revalidate run fields before building child parameters."""
    if type(request) is not RunRequest:
        raise TypeError("request must be an exact RunRequest")
    try:
        return RunRequest.model_validate(
            {
                "contract_version": request.contract_version,
                "run_type": request.run_type,
                "query": request.query,
                "partition": request.partition,
                "shard": request.shard,
                "occurrence": request.occurrence,
                "idempotency_key": request.idempotency_key,
                "backfill_start": request.backfill_start,
                "backfill_end": request.backfill_end,
            },
            strict=True,
        )
    except (AttributeError, OverflowError, RecursionError, TypeError, ValidationError, ValueError):
        raise ValueError(_PROCESS_REQUEST_ERROR) from None


def _validated_task(task: TaskDefinition) -> TaskDefinition:
    """Copy and strictly validate the disabled fixed job task."""
    if type(task) is not TaskDefinition:
        raise TypeError("task must be an exact TaskDefinition")
    try:
        if (
            type(task.contract_version) is not int
            or task.contract_version != 1
            or type(task.enabled) is not bool
            or task.enabled
            or task.schedule is not None
        ):
            raise ValueError(_TASK_ERROR)
        return TaskDefinition.model_validate(
            {
                "contract_version": task.contract_version,
                "job_id": task.job_id,
                "version": task.version,
                "dataset_id": task.dataset_id,
                "provider": task.provider,
                "model": task.model,
                "timezone": task.timezone,
                "calendar": task.calendar,
                "schedule": None,
                "enabled": False,
            },
            strict=True,
        )
    except (AttributeError, OverflowError, RecursionError, TypeError, ValidationError, ValueError):
        raise ValueError(_TASK_ERROR) from None


def _copy_deployment_binding(
    binding: FuyaoProcessDeploymentBinding,
) -> FuyaoProcessDeploymentBinding:
    """Reconstruct exact typed deployment values and hide invalid details."""
    if type(binding) is not FuyaoProcessDeploymentBinding:
        raise TypeError("binding must be an exact FuyaoProcessDeploymentBinding")
    try:
        return FuyaoProcessDeploymentBinding(
            api_key=binding.api_key,
            financial_bundle_path=binding.financial_bundle_path,
            financial_bundle_sha256=binding.financial_bundle_sha256,
            source_policy_path=binding.source_policy_path,
            source_policy_sha256=binding.source_policy_sha256,
            gate_database_path=binding.gate_database_path,
            source_attempt_grant_id=binding.source_attempt_grant_id,
            source_attempt_expected_revision=binding.source_attempt_expected_revision,
            source_attempt_principal_scope=binding.source_attempt_principal_scope,
            raw_artifact_root=binding.raw_artifact_root,
            retention_class=binding.retention_class,
            source_history_limit=binding.source_history_limit,
            transport_max_attempts=binding.transport_max_attempts,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Fuyao deployment binding is invalid") from None


def _unused_process_parameter_builder(_request: RunRequest) -> dict[str, JsonValue]:
    """Reject accidental fallback to the generic, non-Fuyao executor path."""
    raise RuntimeError("fixed Fuyao process adapter requires a run context")


def _identity_process_result_decoder(value: object) -> object:
    """Retain a total placeholder decoder for the unused generic adapter path."""
    return value
