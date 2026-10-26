"""Offline-reviewed adapter from bounded stock JSON evidence to publication batches.

This module contains no HTTP client, database connection, dynamic import, SQL,
or sink selection. An application supplies an explicit response source, a
private raw-artifact writer, and a versioned contract registry. The returned
job is disabled and unreviewed; enabling it requires a separate explicit
reviewed fixture registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    DatasetContractRegistryError,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models.stock_daily import (
    AkshareStockDailyFetcher,
    StockDailyQuery,
)
from opendata.data.providers.akshare.stock_daily_wire import (
    MAX_RAW_RESPONSE_BYTES,
    parse_stock_daily_wire,
)
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.raw_evidence import (
    CapturedRawResponse,
    RawResponseArtifactWriter,
    RawResponseManifest,
    RawResponseSource,
    request_summary_sha256,
)
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    ReviewedJob,
    RunExecutionContext,
)

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from pydantic import JsonValue

MAX_STOCK_DAILY_WINDOW_DAYS = 3_660
MAX_MANIFEST_BYTES = 16 * 1024
_OPERATION_ID = "stock-daily-wire"
_QUALITY_REPORT_ID = "stock-daily-offline-normalization-v1"
_QUERY_FIELDS = frozenset({"source", "market", "symbol", "start_date", "end_date", "adjust"})
_EXPECTED_FIELD_TYPES = {
    "market": "string",
    "symbol": "string",
    "trade_date": "date",
    "open": "number",
    "high": "number",
    "low": "number",
    "close": "number",
    "volume": "number",
    "amount": "number",
}
_EXPECTED_UNITS = {
    "open": "CNY/share",
    "high": "CNY/share",
    "low": "CNY/share",
    "close": "CNY/share",
    "volume": "share",
    "amount": "CNY",
}
_EXPECTED_ADJUSTMENTS = {
    "open": "raw",
    "high": "raw",
    "low": "raw",
    "close": "raw",
    "volume": "not_applicable",
    "amount": "not_applicable",
}
_BUSINESS_KEY = ("market", "symbol", "trade_date")
_ORDERING = ("trade_date", "market", "symbol")
_UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class StockDailyJsonResponse:
    """One bounded raw response plus its independently observed UTC instant."""

    body: bytes
    status_code: int
    observed_at: datetime


class StockDailyResponseSource(Protocol):
    """Explicit code-level source for one validated stock query."""

    def __call__(self, query: StockDailyQuery) -> StockDailyJsonResponse:
        """Return raw JSON and the source observation time for a query."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class CapturedStockDailyResponse:
    """Raw evidence reference paired with the source observation timestamp."""

    capture: CapturedRawResponse
    observed_at: datetime


def build_stock_daily_job(
    task: TaskDefinition,
    *,
    schema_version: str,
    semantic_version: str,
    contract_registry: DatasetContractRegistry,
    artifact_writer: RawResponseArtifactWriter,
    response_source: StockDailyResponseSource,
) -> ReviewedJob:
    """Build a disabled, unreviewed A-share daily-bar fixture job.

    The query window is explicit and inclusive. This adapter does not claim
    source completeness or point-in-time availability; the caller must supply
    a separately reviewed job registration to execute fixture data.

    Args:
        task: Disabled, versioned stock-daily task metadata.
        schema_version: Exact schema version registered by the caller.
        semantic_version: Exact semantic version registered by the caller.
        contract_registry: Caller-owned immutable contract registry.
        artifact_writer: Caller-owned writer rooted in a private evidence path.
        response_source: Explicit code callable returning bounded raw JSON.

    Returns:
        A ``ReviewedJob`` whose task remains disabled and whose review flag is
        false. Its publish handler is the existing contract-validating writer.

    Raises:
        ValueError: If task, source, response bounds, or registered semantics
            do not meet this sample's fixed stock-daily contract.
        TypeError: If an injected dependency has the wrong explicit interface.
    """
    task = _revalidate_task(task)
    if task.enabled:
        raise ValueError("stock daily sample tasks must be disabled at construction")
    if task.provider != SOURCE or task.model != "stock_daily":
        raise ValueError("task must identify the registered akshare stock_daily model")
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("schema_version must be explicit")
    if not isinstance(semantic_version, str) or not semantic_version:
        raise ValueError("semantic_version must be explicit")
    if not isinstance(contract_registry, DatasetContractRegistry):
        raise TypeError("contract_registry must be a DatasetContractRegistry")
    if not isinstance(artifact_writer, RawResponseArtifactWriter):
        raise TypeError("artifact_writer must be a RawResponseArtifactWriter")
    if not callable(response_source):
        raise TypeError("response_source must be an explicit callable")

    try:
        contract = contract_registry.resolve(task.dataset_id, schema_version, semantic_version)
    except DatasetContractRegistryError as exc:
        raise ValueError("the exact stock daily contract is not registered") from exc
    _validate_stock_contract(task, contract)

    fetcher = AkshareStockDailyFetcher()
    source_identity = RawResponseSource(
        provider_id=task.provider,
        dataset_id=task.dataset_id,
        operation_id=_OPERATION_ID,
    )

    def fetch(request: RunRequest, context: RunExecutionContext) -> CapturedStockDailyResponse:
        query = _validated_query(fetcher, request)
        context.checkpoint()
        response = response_source(query.model_copy(deep=True))
        context.checkpoint()
        _validate_source_response(response)
        summary_hash = _request_summary_hash(query)
        capture = artifact_writer.capture(
            (response.body,),
            source=source_identity,
            request_summary_sha256=summary_hash,
            http_status=response.status_code,
            retention_class="ephemeral",
            source_history_limit="unknown",
        )
        return CapturedStockDailyResponse(
            capture=capture,
            observed_at=response.observed_at.astimezone(_UTC),
        )

    def normalize(
        raw: object,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        context.checkpoint()
        query = _validated_query(fetcher, request)
        if not isinstance(raw, CapturedStockDailyResponse):
            raise ValueError("stock daily job requires its captured raw response")
        observed_at = _validated_observation_time(raw.observed_at)
        request_hash = _request_summary_hash(query)
        body, manifest, manifest_sha256 = _verified_capture(
            raw.capture,
            artifact_writer=artifact_writer,
            expected_source=source_identity,
            expected_request_summary_sha256=request_hash,
        )
        expected_market_code = 1 if query.symbol.startswith("6") else 0
        source_frame = parse_stock_daily_wire(
            body,
            symbol=query.symbol,
            expected_market=expected_market_code,
        )
        bars = fetcher.transform_data(source_frame, query)
        records = tuple(
            _record_for_bar(bar, market=query.market or "cn", observed_at=observed_at)
            for bar in bars
        )
        provenance: dict[str, JsonValue] = {
            "acquisition_mode": "injected_offline_json",
            "normalizer": "AkshareStockDailyFetcher.transform_data",
            "provider": task.provider,
            "dataset_id": task.dataset_id,
            "operation_id": _OPERATION_ID,
            "market": query.market or "cn",
            "source_observed_at": observed_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "source_response_sha256": manifest.response_sha256,
            "source_manifest_sha256": manifest_sha256,
            "source_capture_id": manifest.capture_id,
            "source_retention_class": manifest.retention_class,
            "source_history_limit": manifest.source_history_limit,
            "source_verification": "not_performed",
        }
        return NormalizedBatch(
            records=records,
            schema_version=schema_version,
            semantic_version=semantic_version,
            unit="per-field",
            adjustment_basis="raw",
            quality={
                "status": "warning",
                "report_id": _QUALITY_REPORT_ID,
                "issue_count": 0,
            },
            provenance=provenance,
        )

    return ReviewedJob(
        task=task,
        fetch=fetch,
        normalize=normalize,
        publish=ValidatedPublicationWriter(contract_registry),
        reviewed=False,
    )


def _revalidate_task(task: TaskDefinition) -> TaskDefinition:
    """Copy and strictly revalidate caller-owned task metadata."""
    if not isinstance(task, TaskDefinition):
        raise TypeError("task must be a TaskDefinition")
    try:
        return TaskDefinition.model_validate(task.model_dump(mode="python"), strict=True)
    except Exception as exc:
        raise ValueError("task metadata failed boundary validation") from exc


def _validate_stock_contract(task: TaskDefinition, contract: DatasetContract) -> None:
    """Require the exact daily raw-bar shape and units used by this adapter."""
    if not isinstance(contract, DatasetContract):
        raise ValueError("contract registry returned an invalid dataset contract")
    field_map: dict[str, FieldContract] = {field.name: field for field in contract.fields}
    if set(field_map) != set(_EXPECTED_FIELD_TYPES):
        raise ValueError("stock daily contract fields do not match the reviewed bar schema")
    if any(
        field_map[name].data_type != expected_type or field_map[name].nullable
        for name, expected_type in _EXPECTED_FIELD_TYPES.items()
    ):
        raise ValueError("stock daily contract field types or nullability are incompatible")
    if (
        contract.dataset_id != task.dataset_id
        or contract.source.source != task.provider
        or contract.asset_class != "equity"
        or contract.frequency != "1d"
        or contract.record_kind != "time_series"
        or contract.business_key != _BUSINESS_KEY
        or contract.ordering != _ORDERING
        or contract.time_field != "trade_date"
        or contract.temporal_semantics != "business_date"
        or contract.adjustment_basis != "raw"
        or contract.pit_policy != "unsupported"
        or contract.currency != "CNY"
    ):
        raise ValueError("stock daily contract identity or temporal semantics are incompatible")
    if contract.field_units != _EXPECTED_UNITS:
        raise ValueError("stock daily contract units must preserve CNY, CNY/share, and shares")
    if contract.field_adjustments != _EXPECTED_ADJUSTMENTS:
        raise ValueError("stock daily contract must separate raw prices from non-price fields")
    expected_currencies = {
        "open": "CNY",
        "high": "CNY",
        "low": "CNY",
        "close": "CNY",
        "amount": "CNY",
    }
    if contract.field_currencies != expected_currencies:
        raise ValueError("stock daily contract currency metadata must identify CNY fields")


def _request_summary_hash(query: StockDailyQuery) -> str:
    """Hash the canonical stock query using the same safe evidence allowlist."""
    return request_summary_sha256(
        {
            "adjust": query.adjust,
            "end_date": query.end_date,
            "market": query.market,
            "source": query.source,
            "start_date": query.start_date,
            "symbol": query.symbol,
        },
        allowed_fields=("adjust", "end_date", "market", "source", "start_date", "symbol"),
    )


def _validated_query(fetcher: AkshareStockDailyFetcher, request: RunRequest) -> StockDailyQuery:
    """Revalidate the request and bind its single-symbol inclusive date window."""
    if not isinstance(request, RunRequest):
        raise TypeError("stock daily request must be a RunRequest")
    try:
        request_payload = request.model_dump(mode="python")
        validated_request = RunRequest.model_validate(request_payload, strict=True)
    except Exception as exc:
        raise ValueError("stock daily request failed boundary validation") from exc
    if set(validated_request.query) - _QUERY_FIELDS:
        raise ValueError("stock daily query contains unsupported fields")
    query_values = dict(validated_request.query)
    query_values.setdefault("source", SOURCE)
    query_values.setdefault("market", "cn")
    query = fetcher.transform_query(**query_values)
    if query.start_date is None or query.end_date is None:
        raise ValueError("stock daily jobs require explicit inclusive start_date and end_date")
    window_days = (query.end_date - query.start_date).days + 1
    if window_days > MAX_STOCK_DAILY_WINDOW_DAYS:
        raise ValueError("stock daily query window exceeds its inclusive date-range limit")
    if validated_request.partition:
        raise ValueError("single-symbol stock daily requests do not accept extra partitions")
    if validated_request.shard != query.symbol:
        raise ValueError("stock daily shard must equal the canonical requested symbol")
    if validated_request.run_type == "backfill" and (
        validated_request.backfill_start != query.start_date
        or validated_request.backfill_end != query.end_date
    ):
        raise ValueError("backfill bounds must equal the inclusive stock daily query window")
    return query


def _validate_source_response(response: StockDailyJsonResponse) -> None:
    """Reject unbounded bodies, unsuccessful status, and invalid observation time."""
    if type(response) is not StockDailyJsonResponse:
        raise TypeError("stock daily source must return StockDailyJsonResponse")
    if not isinstance(response.body, bytes):
        raise ValueError("stock daily source body must be immutable bytes")
    if len(response.body) > MAX_RAW_RESPONSE_BYTES:
        raise ValueError("stock daily source body exceeds the wire response limit")
    if type(response.status_code) is not int or response.status_code != 200:
        raise ValueError("stock daily source response must have HTTP status 200")
    _validated_observation_time(response.observed_at)


def _validated_observation_time(value: datetime) -> datetime:
    """Return one aware observation instant in UTC without using a business date."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source observed_at must be timezone-aware")
    return value.astimezone(_UTC)


def _record_for_bar(bar: object, *, market: str, observed_at: datetime) -> NormalizedRecord:
    """Build a canonical contract record while retaining market and observation time."""
    # The provider contract returns Bar instances; avoid accepting arbitrary
    # model-shaped objects from the injected source or from a changed provider.
    from opendata.data.models import Bar

    if not isinstance(bar, Bar):
        raise ValueError("stock daily normalizer returned a non-Bar record")
    payload: dict[str, JsonValue] = {"market": market}
    payload.update(bar.model_dump(mode="json"))
    business_key = json.dumps(
        [market, bar.symbol, bar.trade_date.isoformat()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return NormalizedRecord(
        business_key=business_key,
        payload=payload,
        business_date=bar.trade_date,
        observed_at=observed_at,
    )


def _verified_capture(
    capture: CapturedRawResponse,
    *,
    artifact_writer: RawResponseArtifactWriter,
    expected_source: RawResponseSource,
    expected_request_summary_sha256: str,
) -> tuple[bytes, RawResponseManifest, str]:
    """Revalidate manifest and content-addressed files before parsing bytes."""
    if not isinstance(capture, CapturedRawResponse):
        raise TypeError("stock daily response evidence must be CapturedRawResponse")
    if not isinstance(capture.manifest, RawResponseManifest):
        raise ValueError("stock daily evidence manifest has an invalid type")
    try:
        manifest = RawResponseManifest.model_validate(
            capture.manifest.model_dump(mode="python"), strict=True
        )
    except Exception as exc:
        raise ValueError("stock daily evidence manifest failed boundary validation") from exc
    if manifest.source != expected_source or manifest.http_status != 200:
        raise ValueError("stock daily evidence source identity or HTTP status changed")
    if manifest.request_summary_sha256 != expected_request_summary_sha256:
        raise ValueError("stock daily evidence request does not match the current normalized query")
    if manifest.response_size_bytes > MAX_RAW_RESPONSE_BYTES:
        raise ValueError("stock daily evidence body exceeds the wire response limit")

    body_path = (
        artifact_writer.artifact_root
        / "objects"
        / manifest.response_sha256[:2]
        / f"{manifest.response_sha256}.blob"
    )
    if capture.artifact_path != body_path:
        raise ValueError("stock daily evidence path does not match its content address")
    body = _read_private_managed_file(
        artifact_writer.artifact_root,
        ("objects", manifest.response_sha256[:2], f"{manifest.response_sha256}.blob"),
        maximum=MAX_RAW_RESPONSE_BYTES,
    )
    if (
        len(body) != manifest.response_size_bytes
        or hashlib.sha256(body).hexdigest() != manifest.response_sha256
    ):
        raise ValueError("stock daily raw response bytes failed hash or size verification")

    canonical_manifest = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical_manifest) > MAX_MANIFEST_BYTES:
        raise ValueError("stock daily evidence manifest exceeds its size limit")
    manifest_sha256 = hashlib.sha256(canonical_manifest).hexdigest()
    manifest_path = (
        artifact_writer.artifact_root
        / "manifests"
        / manifest_sha256[:2]
        / f"{manifest_sha256}.json"
    )
    if capture.manifest_path != manifest_path:
        raise ValueError("stock daily manifest path does not match its content address")
    stored_manifest = _read_private_managed_file(
        artifact_writer.artifact_root,
        ("manifests", manifest_sha256[:2], f"{manifest_sha256}.json"),
        maximum=MAX_MANIFEST_BYTES,
    )
    if stored_manifest != canonical_manifest or hashlib.sha256(stored_manifest).hexdigest() != (
        manifest_sha256
    ):
        raise ValueError("stock daily manifest bytes failed canonical hash verification")
    return body, manifest, manifest_sha256


def _read_private_managed_file(root: Path, parts: tuple[str, ...], *, maximum: int) -> bytes:
    """Read one private bounded file through no-follow directory descriptors.

    The artifact root, managed directories, and leaf file must remain owned by
    the current effective user with the writer's private modes (0700/0600).
    Ancestors such as ``/tmp`` are opened without following symlinks but are
    not required to use private modes.
    """
    if not root.is_absolute() or len(root.parts) < 2 or not parts:
        raise ValueError("managed evidence path must be absolute and non-empty")
    if any(
        not component or component in {".", ".."} or "/" in component or "\\" in component
        for component in parts
    ):
        raise ValueError("managed evidence path contains an unsafe component")
    root_components = root.parts[1:]
    if any(
        not component or component in {".", ".."} or "/" in component or "\\" in component
        for component in root_components
    ):
        raise ValueError("managed evidence path contains an unsafe component")
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "geteuid")
    ):
        raise ValueError("platform cannot safely verify raw evidence paths")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open("/", directory_flags)
    directory_fds = [directory_fd]
    managed_directory_fds: list[int] = []
    file_fd: int | None = None
    root_component_index = len(root_components) - 1
    try:
        for index, component in enumerate((*root_components, *parts[:-1])):
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            directory_fds.append(next_fd)
            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("managed evidence path component is not a directory")
            if index >= root_component_index:
                _require_private_owner(
                    info,
                    required_mode=0o700,
                    description="managed artifact directory",
                )
            directory_fd = next_fd
        root_fd = directory_fds[root_component_index + 1]
        objects_fd = _open_private_child_directory(root_fd, "objects")
        managed_directory_fds.append(objects_fd)
        managed_directory_fds.append(_open_private_child_directory(objects_fd, "pending"))
        managed_directory_fds.append(_open_private_child_directory(root_fd, "manifests"))
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("managed evidence file is not a bounded regular file")
        _require_private_owner(
            before,
            required_mode=0o600,
            description="managed artifact file",
        )
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(file_fd, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(file_fd)
        if (
            len(content) > maximum
            or len(content) != before.st_size
            or after.st_size != before.st_size
        ):
            raise ValueError("managed evidence file changed or exceeded its size limit")
        _require_private_owner(
            after,
            required_mode=0o600,
            description="managed artifact file",
        )
        for descriptor in directory_fds[root_component_index + 1 :] + managed_directory_fds:
            _require_private_owner(
                os.fstat(descriptor),
                required_mode=0o700,
                description="managed artifact directory",
            )
        return bytes(content)
    except OSError as exc:
        raise ValueError("managed evidence file is unavailable or unsafe") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(managed_directory_fds):
            os.close(descriptor)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


def _require_private_owner(info: os.stat_result, *, required_mode: int, description: str) -> None:
    """Require one opened artifact inode to be private and owned by this user."""
    if info.st_uid != os.geteuid():
        raise ValueError(f"{description} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != required_mode:
        mode = f"{required_mode:04o}"
        raise ValueError(f"{description} must have mode {mode}")


def _open_private_child_directory(parent_fd: int, name: str) -> int:
    """Open and verify a fixed writer-managed subdirectory without following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("managed evidence path component is not a directory")
        _require_private_owner(
            info,
            required_mode=0o700,
            description="managed artifact directory",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


__all__ = [
    "CapturedStockDailyResponse",
    "StockDailyJsonResponse",
    "StockDailyResponseSource",
    "build_stock_daily_job",
]
