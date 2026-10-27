"""Compose a fixed, disabled Sina FZB process job from explicit dependencies."""

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
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SinaFzbQuery,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_publication import (
    _serialize_sina_fzb_response,
    build_sina_fzb_observation_contract,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_service import NormalizedBatch, ReviewedJob, RunExecutionContext

if TYPE_CHECKING:
    from pydantic import JsonValue

    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

__all__ = [
    "SinaFzbProcessCapture",
    "build_sina_fzb_job",
    "build_sina_fzb_manual_payload",
    "build_sina_fzb_process_source",
    "build_sina_fzb_scheduled_payload",
    "normalize_sina_fzb_capture",
]

_JOB_ID = "sina-fzb-capture"
_JOB_VERSION = "process-v1"
_DATASET_ID = "sina_fzb_report_observations"
_SCHEMA_VERSION = "source-native-report-v1"
_SEMANTIC_VERSION = "sina-fzb-source-native-v1"
_PROVIDER = "sina"
_MODEL = "financial_statement"
_PROCESS_TIMEOUT_SECONDS = 30.0
_CAPTURE_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z",
    re.ASCII,
)
_MAX_BASE64_BODY_CHARS = 4 * ((MAX_RAW_RESPONSE_BYTES + 2) // 3)
_PROCESS_REQUEST_ERROR = "sina_fzb_process_request_invalid"
_PROCESS_RESULT_ERROR = "sina_fzb_process_result_invalid"
_CAPTURE_ERROR = "sina_fzb_capture_invalid"
_TASK_ERROR = "sina_fzb_task_definition_invalid"
_CONTRACT_ERROR = "sina_fzb_dataset_contract_invalid"
_UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class SinaFzbProcessCapture:
    """One bounded raw Sina response decoded from the fixed child protocol."""

    query: SinaFzbQuery
    status_code: int
    retrieved_at_utc: datetime
    body: bytes


def build_sina_fzb_scheduled_payload(instrument_id: object) -> dict[str, JsonValue]:
    """Build one scheduled partition for a canonical exchange-qualified symbol.

    Args:
        instrument_id: One exact canonical Sina symbol such as ``600519.SH``.

    Returns:
        The dispatcher payload with an empty query, one instrument partition,
        and a null shard.

    Raises:
        SinaFzbWireError: If the symbol is not canonical.
    """
    query = _canonical_query(instrument_id)
    return {
        "query": {},
        "partition": {"instrument_id": query.symbol},
        "shard": None,
    }


def build_sina_fzb_manual_payload(
    instrument_id: object,
    *,
    idempotency_key: object,
) -> dict[str, JsonValue]:
    """Build one manual partition while retaining its caller-supplied token.

    Args:
        instrument_id: One exact canonical Sina symbol such as ``600519.SH``.
        idempotency_key: Explicit stable token for this requested capture.

    Returns:
        The dispatcher payload with no range, cohort, or source controls.

    Raises:
        SinaFzbWireError: If the symbol is not canonical.
        ValueError: If the explicit token does not satisfy the run contract.
    """
    query = _canonical_query(instrument_id)
    if type(idempotency_key) is not str:
        raise ValueError("manual Sina FZB payload is invalid")
    try:
        request = RunRequest(
            run_type="manual",
            query={},
            partition={"instrument_id": query.symbol},
            shard=None,
            idempotency_key=idempotency_key,
        )
    except (TypeError, ValidationError):
        raise ValueError("manual Sina FZB payload is invalid") from None
    if type(request.idempotency_key) is not str:
        raise ValueError("manual Sina FZB payload is invalid")
    return {
        "query": {},
        "partition": {"instrument_id": query.symbol},
        "shard": None,
        "idempotency_key": request.idempotency_key,
    }


def build_sina_fzb_process_source() -> ProcessFetchHandler:
    """Build a dedicated fixed-operation process adapter with bounded limits.

    The executor contains only the frozen Sina capture operation. Its default
    child grant remains UNKNOWN, and this builder accepts no executor, handler,
    operation ID, transport, or grant from a caller.

    Returns:
        The exact ``ProcessFetchHandler`` used by the preparation job.
    """
    from opendata.data.providers.sina.financial_statement_process import (
        SINA_FZB_PROCESS_OPERATION_ID,
        fetch_sina_fzb_capture,
    )
    from opendata.pipeline.process_executor import (
        MAX_RESULT_BYTES,
        MAX_RESULT_TEXT_BYTES,
        ReviewedProcessExecutor,
        ReviewedProcessRegistry,
    )
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

    registry = ReviewedProcessRegistry({SINA_FZB_PROCESS_OPERATION_ID: fetch_sina_fzb_capture})
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
        SINA_FZB_PROCESS_OPERATION_ID,
        _build_sina_fzb_process_parameters,
        _decode_sina_fzb_process_result,
        timeout_seconds=_PROCESS_TIMEOUT_SECONDS,
    )


def build_sina_fzb_job(
    task: TaskDefinition,
    *,
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled, unreviewed process job from an explicit registry.

    Args:
        task: Exact disabled ``sina-fzb-capture/process-v1`` task metadata.
        contract_registry: Explicit immutable registry containing the W11
            source-native observation contract.

    Returns:
        A preparation-only job using the fixed process adapter, W11 normalizer,
        and existing fenced contract writer.

    Raises:
        TypeError: If task or registry dependencies have the wrong type.
        ValueError: If the identity, disabled state, or registered contract is
            not the exact frozen W11 job contract.
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
        fetch=build_sina_fzb_process_source(),
        normalize=normalize_sina_fzb_capture,
        publish=ValidatedPublicationWriter(contract_registry),
        reviewed=False,
    )


def normalize_sina_fzb_capture(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Validate one child result and return its W11 publication batch unchanged.

    The parent binds the local run identity as the W11 capture ID. The child
    result never supplies that identity, and parsing remains outside the
    warehouse transaction with lease checkpoints on both sides.

    Args:
        raw: Exact in-memory DTO returned by the fixed result decoder.
        request: Claimed scheduled or manual run request.
        context: Parent execution context carrying the fenced lease checkpoint.

    Returns:
        The W11 source-native ``NormalizedBatch`` for this observation.

    Raises:
        TypeError: If request or context uses an unsupported runtime type.
        ValueError: If the process capture, request identity, or run key is
            invalid or mismatched.
    """
    if not isinstance(context, RunExecutionContext):
        raise TypeError("context must be a RunExecutionContext")
    context.checkpoint()
    expected_symbol = _request_symbol(request)
    capture = _validate_capture(raw, expected_symbol=expected_symbol)
    if type(context.run_key) is not str or _CAPTURE_ID_PATTERN.fullmatch(context.run_key) is None:
        raise ValueError(_CAPTURE_ERROR)
    batch = _serialize_sina_fzb_response(
        capture.body,
        query=capture.query,
        retrieved_at_utc=capture.retrieved_at_utc,
        capture_id=context.run_key,
        checkpoint=context.checkpoint_cooperatively,
    )
    context.checkpoint()
    return batch


def _build_sina_fzb_process_parameters(request: RunRequest) -> dict[str, JsonValue]:
    """Build the exact child request from one revalidated parent run request."""
    from opendata.data.providers.sina.financial_statement_process import (
        SINA_FZB_PROCESS_PROTOCOL,
    )

    query = _request_query(request)
    return {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": query.symbol}


def _decode_sina_fzb_process_result(value: object) -> SinaFzbProcessCapture:
    """Strictly decode the exact bounded response envelope from the child."""
    from opendata.data.providers.sina.financial_statement_process import (
        SINA_FZB_PROCESS_PROTOCOL,
    )

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
        or value["protocol"] != SINA_FZB_PROCESS_PROTOCOL
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
        query = SinaFzbQuery(query_value["symbol"])
    except SinaFzbWireError:
        raise ValueError(_PROCESS_RESULT_ERROR) from None

    retrieved_at_utc = _decode_retrieved_at(value["retrieved_at_utc"])
    body = _decode_body(value["body_b64"])
    return SinaFzbProcessCapture(
        query=query,
        status_code=200,
        retrieved_at_utc=retrieved_at_utc,
        body=body,
    )


def _decode_body(value: object) -> bytes:
    """Decode canonical padded Base64 after checking its allocation bound."""
    if type(value) is not str or len(value) > _MAX_BASE64_BODY_CHARS or len(value) % 4 != 0:
        raise ValueError(_PROCESS_RESULT_ERROR)
    try:
        encoded = value.encode("ascii", errors="strict")
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ValueError(_PROCESS_RESULT_ERROR) from None
    if len(body) > MAX_RAW_RESPONSE_BYTES or base64.b64encode(body) != encoded:
        raise ValueError(_PROCESS_RESULT_ERROR)
    return body


def _decode_retrieved_at(value: object) -> datetime:
    """Parse only canonical UTC ISO text with six fractional digits and ``Z``."""
    if type(value) is not str or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(_PROCESS_RESULT_ERROR)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(_PROCESS_RESULT_ERROR) from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != value
    ):
        raise ValueError(_PROCESS_RESULT_ERROR)
    return parsed


def _request_symbol(request: RunRequest) -> str:
    """Validate run mode, empty query, single-symbol partition, and null shard."""
    if type(request) is not RunRequest:
        raise TypeError("request must be a RunRequest")
    try:
        contract_version = request.contract_version
        if type(contract_version) is not int or contract_version != 1:
            raise ValueError(_PROCESS_REQUEST_ERROR)
        validated = RunRequest.model_validate(
            {
                "contract_version": contract_version,
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
    if validated.run_type not in {"scheduled", "manual"}:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    if (
        type(validated.query) is not dict
        or validated.query
        or type(validated.partition) is not dict
        or set(validated.partition) != {"instrument_id"}
        or validated.shard is not None
    ):
        raise ValueError(_PROCESS_REQUEST_ERROR)
    symbol = validated.partition["instrument_id"]
    if type(symbol) is not str:
        raise ValueError(_PROCESS_REQUEST_ERROR)
    return _canonical_query(symbol).symbol


def _request_query(request: RunRequest) -> SinaFzbQuery:
    """Return the exact canonical query for a validated run request."""
    return _canonical_query(_request_symbol(request))


def _canonical_query(value: object) -> SinaFzbQuery:
    """Validate one exact symbol through the existing Sina query contract."""
    if type(value) is not str:
        raise SinaFzbWireError("invalid_query")
    return SinaFzbQuery(value)


def _validate_capture(raw: object, *, expected_symbol: str) -> SinaFzbProcessCapture:
    """Recheck the exact DTO fields, source symbol, status, time, and body cap."""
    if type(raw) is not SinaFzbProcessCapture:
        raise ValueError(_CAPTURE_ERROR)
    if type(raw.query) is not SinaFzbQuery or type(raw.query.symbol) is not str:
        raise ValueError(_CAPTURE_ERROR)
    try:
        query = SinaFzbQuery(raw.query.symbol)
    except SinaFzbWireError:
        raise ValueError(_CAPTURE_ERROR) from None
    if (
        query.symbol != expected_symbol
        or type(raw.status_code) is not int
        or raw.status_code != 200
        or type(raw.body) is not bytes
        or len(raw.body) > MAX_RAW_RESPONSE_BYTES
    ):
        raise ValueError(_CAPTURE_ERROR)
    retrieved_at_utc = _validate_utc_datetime(raw.retrieved_at_utc)
    return SinaFzbProcessCapture(
        query=query,
        status_code=200,
        retrieved_at_utc=retrieved_at_utc,
        body=raw.body,
    )


def _validate_utc_datetime(value: object) -> datetime:
    """Require an exact aware datetime at zero UTC offset."""
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(_CAPTURE_ERROR)
    try:
        offset = value.utcoffset()
    except Exception:
        raise ValueError(_CAPTURE_ERROR) from None
    if type(offset) is not timedelta or offset != timedelta(0):
        raise ValueError(_CAPTURE_ERROR)
    try:
        normalized = value.astimezone(_UTC)
    except Exception:
        raise ValueError(_CAPTURE_ERROR) from None
    if type(normalized) is not datetime or normalized.tzinfo is not _UTC:
        raise ValueError(_CAPTURE_ERROR)
    return normalized


def _validated_task(task: TaskDefinition) -> TaskDefinition:
    """Copy and strictly revalidate caller-owned task metadata."""
    if type(task) is not TaskDefinition:
        raise TypeError("task must be a TaskDefinition")
    try:
        contract_version = task.contract_version
        enabled = task.enabled
        if (
            type(contract_version) is not int
            or contract_version != 1
            or type(enabled) is not bool
            or enabled is not False
            or task.schedule is not None
        ):
            raise ValueError(_TASK_ERROR)
        return TaskDefinition.model_validate(
            {
                "contract_version": contract_version,
                "job_id": task.job_id,
                "version": task.version,
                "dataset_id": task.dataset_id,
                "provider": task.provider,
                "model": task.model,
                "timezone": task.timezone,
                "calendar": task.calendar,
                "schedule": None,
                "enabled": enabled,
            },
            strict=True,
        )
    except (AttributeError, OverflowError, RecursionError, TypeError, ValidationError, ValueError):
        raise ValueError(_TASK_ERROR) from None


def _validate_registered_contract(contract_registry: DatasetContractRegistry) -> None:
    """Require the exact explicit W11 contract in the supplied registry."""
    expected = build_sina_fzb_observation_contract()
    try:
        actual = contract_registry.resolve(_DATASET_ID, _SCHEMA_VERSION, _SEMANTIC_VERSION)
    except DatasetContractRegistryError:
        raise ValueError(_CONTRACT_ERROR) from None
    if type(actual) is not DatasetContract or canonical_sha256(actual) != canonical_sha256(
        expected
    ):
        raise ValueError(_CONTRACT_ERROR)
