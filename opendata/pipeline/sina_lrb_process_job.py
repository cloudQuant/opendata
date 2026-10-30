"""Compose a disabled, unscheduled Sina LRB one-shot process job."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    SinaLrbPublicationError,
    bind_sina_lrb_raw_provenance,
    build_sina_lrb_observation_contract,
    serialize_sina_lrb_response,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_service import NormalizedBatch, ReviewedJob, RunExecutionContext
from opendata.pipeline.sina_lrb_raw_capture import (
    CaptureReceiptSigner,
    SinaLrbArchivedCapture,
    SinaLrbCaptureRecoveryJournal,
    SinaLrbRawAuthorization,
    SinaLrbRawCaptureError,
    SinaLrbRawCaptureHandler,
    _sina_lrb_request_identity,
    read_sina_lrb_archived_evidence,
    sina_lrb_request_summary_sha256,
)

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from pydantic import JsonValue

    from opendata.pipeline.raw_evidence import HistoryLimit, RetentionClass
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler

__all__ = [
    "SinaLrbProcessCapture",
    "SinaLrbRawCaptureNormalizer",
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
_RAW_AUTHORIZATION_ERROR = "sina_lrb_raw_authorization_denied"
_UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class SinaLrbProcessCapture:
    """One bounded LRB response decoded from the fixed child protocol."""

    query: SinaLrbQuery
    status_code: int
    retrieved_at_utc: datetime
    body: bytes


@dataclass(frozen=True, slots=True)
class SinaLrbRawCaptureNormalizer:
    """Normalize only body-free archive references from the explicit raw root."""

    artifact_root: Path | str
    task: TaskDefinition
    receipt_signer: CaptureReceiptSigner | None = None

    def __call__(
        self,
        raw: object,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        """Read the configured verified artifact and build its bound batch."""
        return normalize_sina_lrb_capture(
            raw,
            request,
            context,
            artifact_root=self.artifact_root,
            task=self.task,
            receipt_signer=self.receipt_signer,
        )


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
    artifact_root: Path | str,
    authorization: SinaLrbRawAuthorization,
    retention_class: RetentionClass,
    source_history_limit: HistoryLimit,
    receipt_signer: CaptureReceiptSigner | None = None,
    reuse_prior_captures: bool = False,
) -> ReviewedJob:
    """Build the disabled, unscheduled, unreviewed LRB one-shot process job.

    The process fetch is wrapped by a bounded raw archive writer. Both source
    access and raw retention require explicit caller decisions, and the raw root
    and retention labels are mandatory inputs. The returned job remains opt-in,
    ``reviewed=False``, disabled, unscheduled, and unregistered. Successful
    publication provenance links the run key, manifest digest, response digest,
    request-summary digest, and authorization references. An optional external
    signer enables durable receipt journaling. Reuse remains disabled unless
    ``reuse_prior_captures`` is explicitly true and a live ledger lease is
    checked before and after the receipt and artifact validation. The ledger
    schema does not join capture receipts to publication transactions, so this
    remains a disabled, unreviewed preparation job.

    Args:
        task: Exact disabled ``sina-lrb-capture/process-v1`` metadata.
        contract_registry: Explicit registry containing the LRB source contract.
        artifact_root: Explicit private root for raw response objects/manifests.
        authorization: Separate caller-reviewed source and raw-retention grants.
        retention_class: Explicit retention classification for the manifest.
        source_history_limit: Explicit source-history boundary for the manifest.
        receipt_signer: Optional external signer whose verification keys remain
            available after worker restarts. No key is stored in the repository.
        reuse_prior_captures: Explicitly permit reusing one verified capture
            under a new active fencing token. It does not claim a ledger lease.

    Returns:
        A preparation-only job with process fetch, source-native normalization,
        and the existing fenced contract publication writer.

    Raises:
        TypeError: If dependencies do not have their required runtime types.
        ValueError: If task, contract, authorization, root, or retention policy
            differs from the reviewed LRB contract.
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
    if type(authorization) is not SinaLrbRawAuthorization:
        raise TypeError("authorization must be a SinaLrbRawAuthorization")
    if (
        authorization.source_access_authorized is not True
        or authorization.raw_retention_authorized is not True
    ):
        raise ValueError(_RAW_AUTHORIZATION_ERROR)
    _validate_registered_contract(contract_registry)
    process_fetch = build_sina_lrb_process_source()
    archived_fetch = SinaLrbRawCaptureHandler(
        process_fetch,
        artifact_root=artifact_root,
        authorization=authorization,
        retention_class=retention_class,
        source_history_limit=source_history_limit,
        task=task,
        receipt_signer=receipt_signer,
        reuse_prior_captures=reuse_prior_captures,
    )
    return ReviewedJob(
        task=task,
        fetch=archived_fetch,
        normalize=SinaLrbRawCaptureNormalizer(artifact_root, task, receipt_signer),
        publish=ValidatedPublicationWriter(contract_registry),
        reviewed=False,
        one_shot_only=True,
    )


def normalize_sina_lrb_capture(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
    *,
    artifact_root: Path | str,
    task: TaskDefinition,
    receipt_signer: CaptureReceiptSigner | None = None,
) -> NormalizedBatch:
    """Read an archived response and build its raw-evidence-bound publication batch.

    The archived run key and non-null fencing token must match the current
    RunExecutionContext, and its instrument must equal the strict manual request.
    The required task metadata binds the full canonical payload hash even when
    no signed journal is configured. The reader revalidates the content-addressed
    manifest and body before parsing. The serializer's
    independently computed response hash is then checked and linked, together
    with manifest/request digests and authorization references, into batch
    provenance. When a receipt signer is configured, the signed receipt must
    match the current run identity and fencing token before the raw bytes are
    accepted. RunLedger remains responsible for the atomic publication fence.

    Args:
        raw: Exact body-free :class:`SinaLrbArchivedCapture` returned by the
            raw-capture fetch wrapper.
        request: Manual single-instrument RunRequest.
        context: Run context carrying the fenced lease checkpoints.
        artifact_root: Explicit root injected into the fetch wrapper.
        task: Required fixed task metadata used to recompute the canonical run
            identity and payload hash.
        receipt_signer: Optional external signer required to verify a capture
            receipt from the configured journal.

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
    if type(context.run_key) is not str or _CAPTURE_ID_PATTERN.fullmatch(context.run_key) is None:
        raise ValueError(_CAPTURE_ERROR)
    capture = _validate_archived_capture(
        raw,
        expected_symbol=expected_symbol,
        expected_run_key=context.run_key,
    )
    if (
        type(context.fencing_token) is not int
        or context.fencing_token < 1
        or capture.fencing_token is None
        or capture.fencing_token != context.fencing_token
    ):
        raise ValueError(_CAPTURE_ERROR)
    try:
        identity_run_key, identity_payload_sha256 = _sina_lrb_request_identity(
            task,
            request,
            expected_run_key=context.run_key,
            snapshot_json=context._verified_snapshot_json(),
        )
    except Exception:
        raise ValueError(_CAPTURE_ERROR) from None
    if (
        identity_run_key != context.run_key
        or capture.request_payload_sha256 != identity_payload_sha256
    ):
        raise ValueError(_CAPTURE_ERROR)
    request_payload_sha256 = identity_payload_sha256
    if receipt_signer is not None:
        context.checkpoint()
        journal = SinaLrbCaptureRecoveryJournal(artifact_root, receipt_signer)
        journal.verify_capture_receipt(
            capture,
            request_payload_sha256=request_payload_sha256,
            fencing_token=context.fencing_token,
        )
        context.checkpoint()
    try:
        evidence = read_sina_lrb_archived_evidence(capture, artifact_root=artifact_root)
    except SinaLrbRawCaptureError:
        raise ValueError(_CAPTURE_ERROR) from None
    if hashlib.sha256(evidence.body).hexdigest() != capture.response_sha256:
        raise ValueError(_CAPTURE_ERROR)
    try:
        batch = serialize_sina_lrb_response(
            evidence.body,
            instrument_id=expected_symbol,
            retrieved_at_utc=capture.retrieved_at_utc,
            capture_id=context.run_key,
        )
    except Exception:
        raise ValueError(_NORMALIZE_ERROR) from None
    try:
        batch = bind_sina_lrb_raw_provenance(
            batch,
            evidence=evidence,
            instrument_id=capture.instrument_id,
            run_key=capture.run_key,
            source_access_ref=capture.source_access_ref,
            raw_retention_ref=capture.raw_retention_ref,
            retrieved_at_utc=capture.retrieved_at_utc,
        )
    except SinaLrbPublicationError:
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


def _validate_archived_capture(
    raw: object,
    *,
    expected_symbol: str,
    expected_run_key: str,
) -> SinaLrbArchivedCapture:
    """Rebuild and validate one body-free archive reference for this run."""
    if type(raw) is not SinaLrbArchivedCapture:
        raise ValueError(_CAPTURE_ERROR)
    try:
        capture = SinaLrbArchivedCapture(
            instrument_id=raw.instrument_id,
            run_key=raw.run_key,
            status_code=raw.status_code,
            retrieved_at_utc=raw.retrieved_at_utc,
            captured_at_utc=raw.captured_at_utc,
            request_summary_sha256=raw.request_summary_sha256,
            response_sha256=raw.response_sha256,
            manifest_sha256=raw.manifest_sha256,
            source_access_ref=raw.source_access_ref,
            raw_retention_ref=raw.raw_retention_ref,
            request_payload_sha256=raw.request_payload_sha256,
            fencing_token=raw.fencing_token,
            origin_fencing_token=raw.origin_fencing_token,
        )
    except Exception:
        raise ValueError(_CAPTURE_ERROR) from None
    if (
        capture.instrument_id != expected_symbol
        or capture.run_key != expected_run_key
        or capture.status_code != 200
        or capture.request_summary_sha256 != sina_lrb_request_summary_sha256(expected_symbol)
    ):
        raise ValueError(_CAPTURE_ERROR)
    return capture


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
