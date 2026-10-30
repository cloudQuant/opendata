"""Compose a disabled, unscheduled Sina LRB one-shot process job."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pydantic import ValidationError

from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    DatasetContractRegistryError,
    canonical_sha256,
)
from opendata.data.platform_contracts import DatasetContract
from opendata.data.providers.sina.lrb_statement_process import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_LRB_PROCESS_OPERATION_ID,
    SINA_LRB_PROCESS_PROTOCOL,
    SinaLrbProcessError,
    SinaLrbQuery,
    fetch_sina_lrb_capture,
)
from opendata.data.providers.sina.lrb_statement_publication import (
    build_sina_lrb_observation_contract,
    serialize_sina_lrb_response,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_service import NormalizedBatch, ReviewedJob, RunExecutionContext

if TYPE_CHECKING:  # pragma: no cover
    from pydantic import JsonValue

    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

__all__ = [
    "SinaLrbProcessCapture",
    "build_sina_lrb_job",
    "build_sina_lrb_manual_payload",
    "build_sina_lrb_process_source",
    "normalize_sina_lrb_capture",
]

_JOB_ID = "sina-lrb-capture"
_JOB_VERSION = "process-v1"
_DATASET_ID = "sina_lrb_report_observations"
_SCHEMA_VERSION = "source-native-report-v1"
_SEMANTIC_VERSION = "sina-lrb-source-native-v1"
_PROVIDER = "sina"
_MODEL = "financial_statement"
_PROCESS_TIMEOUT_SECONDS = 30.0
_CAPTURE_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z",
    re.ASCII,
)
_MAX_BASE64_BODY_CHARS = 4 * ((MAX_RAW_RESPONSE_BYTES + 2) // 3)
_PROCESS_REQUEST_ERROR = "sina_lrb_process_request_invalid"
_PROCESS_RESULT_ERROR = "sina_lrb_process_result_invalid"
_CAPTURE_ERROR = "sina_lrb_capture_invalid"
_TASK_ERROR = "sina_lrb_task_definition_invalid"
_CONTRACT_ERROR = "sina_lrb_dataset_contract_invalid"
_NORMALIZE_ERROR = "sina_lrb_normalization_failed"
_UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class SinaLrbProcessCapture:
    """One bounded LRB response decoded from the fixed child protocol."""

    query: SinaLrbQuery
    status_code: int
    retrieved_at_utc: datetime
    body: bytes


def build_sina_lrb_manual_payload(
    instrument_id: object,
    *,
    idempotency_key: object,
) -> dict[str, JsonValue]:
    """Build one explicit manual partition for the disabled LRB job.

    Args:
        instrument_id: One canonical six-digit SH/SZ symbol.
        idempotency_key: Caller-supplied request token for RunLedger identity.

    Returns:
        A fixed one-symbol manual payload without source or transport controls.

    Raises:
        SinaLrbProcessError: If the instrument identifier is not canonical.
        ValueError: If the idempotency token does not satisfy the run contract.
    """
    query = _canonical_query(instrument_id)
    if type(idempotency_key) is not str:
        raise ValueError("manual Sina LRB payload is invalid")
    try:
        request = RunRequest(
            run_type="manual",
            query={},
            partition={"instrument_id": query.symbol},
            shard=None,
            idempotency_key=idempotency_key,
        )
    except (TypeError, ValidationError):
        raise ValueError("manual Sina LRB payload is invalid") from None
    return {
        "query": {},
        "partition": {"instrument_id": query.symbol},
        "shard": None,
        "idempotency_key": request.idempotency_key,
    }


def build_sina_lrb_process_source() -> ProcessFetchHandler:
    """Build a fixed child-process fetcher with bounded request/result sizes.

    The generic executor inherits its parent's environment. This provider uses
    no credential and reads no dotenv files, but this launch path does not remove
    unrelated environment variables; the job remains unreviewed and is not
    registered by production composition in this change.

    Returns:
        A parent-side ProcessFetchHandler bound to the sole LRB operation.
    """
    from opendata.pipeline.process_executor import (
        MAX_RESULT_BYTES,
        MAX_RESULT_TEXT_BYTES,
        ReviewedProcessExecutor,
        ReviewedProcessRegistry,
    )
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

    registry = ReviewedProcessRegistry({SINA_LRB_PROCESS_OPERATION_ID: fetch_sina_lrb_capture})
    executor = ReviewedProcessExecutor(
        registry,
        max_result_bytes=MAX_RESULT_BYTES,
        max_result_text_bytes=MAX_RESULT_TEXT_BYTES,
        poll_interval_seconds=0.05,
        checkpoint_interval_seconds=1.0,
        shutdown_grace_seconds=0.25,
    )
    return ProcessFetchHandler(
        executor,
        SINA_LRB_PROCESS_OPERATION_ID,
        _build_process_parameters,
        _decode_process_result,
        timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
    )


def build_sina_lrb_job(
    task: TaskDefinition,
    *,
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled, unscheduled, unreviewed LRB one-shot process job.

    The current process composition is opt-in, sets ``reviewed=False``, and is
    not registered by a production app. Even if a later version is reviewed,
    ``one_shot_only`` restricts requests to explicit manual submissions. These
    controls do not establish a persistent source quota across separate runs.

    Args:
        task: Exact disabled ``sina-lrb-capture/process-v1`` metadata.
        contract_registry: Explicit registry containing the LRB source contract.

    Returns:
        A preparation-only job with process fetch, source-native normalization,
        and the existing fenced contract publication writer.

    Raises:
        TypeError: If dependencies do not have their required runtime types.
        ValueError: If task metadata or registered contract differs from LRB.
    """
    task = _validated_task(task)
    if (
        task.job_id != _JOB_ID
        or task.version != _JOB_VERSION
        or task.dataset_id != _DATASET_ID
        or task.provider != _PROVIDER
        or task.model != _MODEL
        or task.enabled
        or task.schedule is not None
    ):
        raise ValueError(_TASK_ERROR)
    if not isinstance(contract_registry, DatasetContractRegistry):
        raise TypeError("contract_registry must be a DatasetContractRegistry")
    _validate_registered_contract(contract_registry)
    return ReviewedJob(
        task=task,
        fetch=build_sina_lrb_process_source(),
        normalize=normalize_sina_lrb_capture,
        publish=ValidatedPublicationWriter(contract_registry),
        reviewed=False,
        one_shot_only=True,
    )


def normalize_sina_lrb_capture(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Parse one process capture and build its source-native LRB publication batch.

    The run key is the capture identity. The LRB serializer preserves report and
    context JSON structure without assigning accounting, unit, currency, or PIT
    semantics. Exact response bytes are carried in memory only; this job does
    not persist a raw-evidence manifest or provide crash recovery.

    Args:
        raw: Exact capture DTO returned by the fixed process result decoder.
        request: Manual single-instrument RunRequest.
        context: Run context carrying the fenced lease checkpoints.

    Returns:
        The LRB source-native NormalizedBatch from the explicit contract module.

    Raises:
        TypeError: If request or execution context has an unsupported type.
        ValueError: If request, capture, or publication normalization is invalid.
    """
    if not isinstance(context, RunExecutionContext):
        raise TypeError("context must be a RunExecutionContext")
    context.checkpoint()
    expected_symbol = _request_symbol(request)
    capture = _validate_capture(raw, expected_symbol=expected_symbol)
    if type(context.run_key) is not str or _CAPTURE_ID_PATTERN.fullmatch(context.run_key) is None:
        raise ValueError(_CAPTURE_ERROR)
    try:
        batch = serialize_sina_lrb_response(
            capture.body,
            instrument_id=expected_symbol,
            retrieved_at_utc=capture.retrieved_at_utc,
            capture_id=context.run_key,
        )
    except Exception:
        raise ValueError(_NORMALIZE_ERROR) from None
    context.checkpoint()
    if type(batch) is not NormalizedBatch:
        raise ValueError(_NORMALIZE_ERROR)
    if batch.schema_version != _SCHEMA_VERSION or batch.semantic_version != _SEMANTIC_VERSION:
        raise ValueError(_NORMALIZE_ERROR)
    if len(batch.records) > 257:
        raise ValueError(_NORMALIZE_ERROR)
    return batch


def _build_process_parameters(request: RunRequest) -> dict[str, JsonValue]:
    """Build the exact child request from one strict manual RunRequest."""
    symbol = _request_symbol(request)
    return {"protocol": SINA_LRB_PROCESS_PROTOCOL, "symbol": symbol}


def _decode_process_result(value: object) -> SinaLrbProcessCapture:
    """Decode the exact bounded child response and hide malformed payload details."""
    required_fields = {
        "protocol",
        "query",
        "retrieved_at_utc",
        "status_code",
        "body_b64",
    }
    if type(value) is not dict or set(value) != required_fields:
        raise ValueError(_PROCESS_RESULT_ERROR)
    if (
        type(value["protocol"]) is not str
        or value["protocol"] != SINA_LRB_PROCESS_PROTOCOL
        or type(value["status_code"]) is not int
        or value["status_code"] != 200
    ):
        raise ValueError(_PROCESS_RESULT_ERROR)
    query_value = value["query"]
    if (
        type(query_value) is not dict
        or set(query_value) != {"symbol"}
        or type(query_value["symbol"]) is not str
    ):
        raise ValueError(_PROCESS_RESULT_ERROR)
    try:
        query = SinaLrbQuery(query_value["symbol"])
        retrieved_at_utc = _decode_retrieved_at(value["retrieved_at_utc"])
        body = _decode_body(value["body_b64"])
    except SinaLrbProcessError:
        raise ValueError(_PROCESS_RESULT_ERROR) from None
    return SinaLrbProcessCapture(
        query=query,
        status_code=200,
        retrieved_at_utc=retrieved_at_utc,
        body=body,
    )


def _decode_body(value: object) -> bytes:
    """Decode canonical padded Base64 only after checking the allocation bound."""
    if type(value) is not str:
        raise SinaLrbProcessError("invalid_envelope")
    if len(value) > _MAX_BASE64_BODY_CHARS:
        raise SinaLrbProcessError("response_too_large")
    if len(value) % 4 != 0:
        raise SinaLrbProcessError("invalid_envelope")
    try:
        encoded = value.encode("ascii", errors="strict")
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise SinaLrbProcessError("invalid_envelope") from None
    if len(body) > MAX_RAW_RESPONSE_BYTES:
        raise SinaLrbProcessError("response_too_large")
    if base64.b64encode(body) != encoded:
        raise SinaLrbProcessError("invalid_envelope")
    return body


def _decode_retrieved_at(value: object) -> datetime:
    """Parse canonical UTC ISO text with six fractional digits and a trailing Z."""
    if type(value) is not str or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise SinaLrbProcessError("invalid_envelope")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise SinaLrbProcessError("invalid_envelope") from None
    return parsed


def _request_symbol(request: RunRequest) -> str:
    """Revalidate the exact manual request shape and its one SH/SZ partition."""
    if type(request) is not RunRequest:
        raise TypeError("request must be a RunRequest")
    try:
        validated = RunRequest.model_validate(
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
    if (
        validated.run_type != "manual"
        or type(validated.query) is not dict
        or validated.query
        or type(validated.partition) is not dict
        or set(validated.partition) != {"instrument_id"}
        or validated.shard is not None
        or type(validated.idempotency_key) is not str
        or validated.occurrence is not None
        or validated.backfill_start is not None
        or validated.backfill_end is not None
    ):
        raise ValueError(_PROCESS_REQUEST_ERROR)
    symbol = validated.partition["instrument_id"]
    if type(symbol) is not str:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    try:
        return _canonical_query(symbol).symbol
    except SinaLrbProcessError:
        raise ValueError(_PROCESS_REQUEST_ERROR) from None


def _validate_capture(raw: object, *, expected_symbol: str) -> SinaLrbProcessCapture:
    """Require one successful response bound to the requested instrument."""
    if type(raw) is not SinaLrbProcessCapture:
        raise ValueError(_CAPTURE_ERROR)
    if (
        type(raw.query) is not SinaLrbQuery
        or raw.query.symbol != expected_symbol
        or type(raw.status_code) is not int
        or raw.status_code != 200
        or type(raw.body) is not bytes
        or len(raw.body) > MAX_RAW_RESPONSE_BYTES
    ):
        raise ValueError(_CAPTURE_ERROR)
    try:
        retrieved_at_utc = _canonicalize_utc_datetime(raw.retrieved_at_utc)
    except Exception:
        raise ValueError(_CAPTURE_ERROR) from None
    return SinaLrbProcessCapture(
        query=raw.query,
        status_code=200,
        retrieved_at_utc=retrieved_at_utc,
        body=raw.body,
    )


def _canonicalize_utc_datetime(value: object) -> datetime:
    """Require one valid aware UTC datetime and normalize its tzinfo object."""
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(_CAPTURE_ERROR)
    offset = value.utcoffset()
    if type(offset) is not timedelta or offset != timedelta(0):
        raise ValueError(_CAPTURE_ERROR)
    normalized = value.astimezone(_UTC)
    return normalized


def _canonical_query(instrument_id: object) -> SinaLrbQuery:
    """Construct the strict one-symbol query value."""
    if type(instrument_id) is not str:
        raise SinaLrbProcessError("invalid_query")
    return SinaLrbQuery(instrument_id)


def _validated_task(task: TaskDefinition) -> TaskDefinition:
    """Copy and strictly revalidate disabled, unscheduled task metadata."""
    if type(task) is not TaskDefinition:
        raise TypeError("task must be a TaskDefinition")
    try:
        if (
            type(task.contract_version) is not int
            or task.contract_version != 1
            or type(task.enabled) is not bool
            or task.enabled is not False
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


def _validate_registered_contract(contract_registry: DatasetContractRegistry) -> None:
    """Require the exact explicit source-native LRB dataset contract."""
    expected = build_sina_lrb_observation_contract()
    try:
        actual = contract_registry.resolve(_DATASET_ID, _SCHEMA_VERSION, _SEMANTIC_VERSION)
    except DatasetContractRegistryError:
        raise ValueError(_CONTRACT_ERROR) from None
    if type(actual) is not DatasetContract or canonical_sha256(actual) != canonical_sha256(
        expected
    ):
        raise ValueError(_CONTRACT_ERROR)
