"""Consistent, bounded local exports of reviewed native provider models.

This synchronous service reads one REPEATABLE READ snapshot from the local
warehouse. It does not contact providers or expose its private temporary file
path as part of the artifact's public metadata.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol, cast

from opendata.data import domains
from opendata.data.protocol import FetchContext
from opendata.data.request_budget import (
    RequestBudgetError,
    RequestExecutionScope,
    RequestOperation,
    current_request_scopes,
    request_execution_scope,
    validate_scope_grants,
)
from opendata.pipeline import ddl, provider_model_codec
from opendata.services import provider_model_read, provider_model_store
from opendata.services.provider_models import (
    ProviderModelIdentityError,
    find_provider_model,
    validate_model_identity,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from opendata.data.models.base import ContractModel
    from opendata.data.registry import ProviderRegistry


_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_RECORDS = 200_000
_MAX_BYTES = 64 * 1024 * 1024
_NDJSON_SCHEMA_VERSION: Literal[1] = 1
_CONSISTENCY: Literal["single_repeatable_read_transaction"] = "single_repeatable_read_transaction"
_COMPLETENESS: Literal["NOT_ASSESSED"] = "NOT_ASSESSED"
_MODEL_DOMAINS: dict[tuple[str, str], str] = {
    ("fred", "FredSeries"): "fred_series",
    ("bls", "BlsSeries"): "bls_series",
    ("fmp", "EquityHistorical"): "equity_historical",
}


class _DomainPrincipal(Protocol):
    """The in-process authorization surface required for a local export."""

    def allows_domain(self, domain: str) -> bool: ...


class ProviderModelExportError(RuntimeError):
    """A native model export could not be completed safely."""


class ProviderModelExportValidationError(ValueError):
    """Export identity, request context, or bounds are invalid."""


class ProviderModelExportForbiddenError(PermissionError):
    """The principal cannot export the requested domain."""


class ProviderModelExportLimitError(ProviderModelExportError):
    """The complete snapshot would exceed its explicit resource budget."""


@dataclass(slots=True)
class NativeModelExportArtifact:
    """Private NDJSON snapshot plus public, verifiable completion metadata.

    ``_path`` is intentionally private and is not included in ``repr``. The
    next service layer can stream the artifact with :meth:`open_binary` and
    must call :meth:`cleanup` after the transfer finishes or is cancelled.
    """

    schema_version: Literal[1]
    source: str
    model: str
    domain: str
    verified: bool
    created_at: datetime
    consistency: Literal["single_repeatable_read_transaction"]
    completeness: Literal["NOT_ASSESSED"]
    row_count: int
    byte_count: int
    sha256: str
    snapshot_complete: Literal[True]
    _temporary_directory: tempfile.TemporaryDirectory[str] = field(repr=False)
    _path: Path = field(repr=False)
    _cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cleaned: bool = field(default=False, repr=False)

    def open_binary(self) -> BinaryIO:
        """Open a fresh binary reader while the private artifact is alive."""
        with self._cleanup_lock:
            if self._cleaned:
                raise ProviderModelExportError("export artifact has already been cleaned up")
            try:
                return self._path.open("rb")
            except OSError:
                raise ProviderModelExportError("export artifact is unavailable") from None

    def cleanup(self) -> None:
        """Delete only the service-owned temporary directory; safe to repeat."""
        with self._cleanup_lock:
            if self._cleaned:
                return
            try:
                self._temporary_directory.cleanup()
            except OSError:
                raise ProviderModelExportError("export artifact cleanup failed") from None
            self._cleaned = True

    def __enter__(self) -> NativeModelExportArtifact:
        """Return this artifact for a scoped ``with`` lifetime."""
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Clean the owned temporary directory at the end of the scope."""
        self.cleanup()


class _NdjsonSink:
    """Incrementally encode complete NDJSON lines under one total byte cap."""

    def __init__(self, stream: BinaryIO, *, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self.byte_count = 0
        self._digest = hashlib.sha256()
        self._encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def _write(self, payload: bytes) -> None:
        if len(payload) > self._max_bytes - self.byte_count:
            raise ProviderModelExportLimitError("export exceeds its total byte limit")
        try:
            written = self._stream.write(payload)
        except OSError:
            raise ProviderModelExportError("export snapshot could not be written") from None
        if written != len(payload):
            raise ProviderModelExportError("export snapshot write was incomplete")
        self._digest.update(payload)
        self.byte_count += written

    def write_line(self, value: object) -> None:
        try:
            for fragment in self._encoder.iterencode(value):
                self._write(fragment.encode("utf-8"))
        except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
            raise ProviderModelExportError("export row is not finite canonical JSON") from None
        self._write(b"\n")


def _trusted_export_context(ctx: FetchContext | None) -> FetchContext:
    """Require EXPORT mode and cap an otherwise unbounded trusted context."""
    if ctx is None:
        return FetchContext(timeout=_DEFAULT_TIMEOUT_SECONDS, operation=RequestOperation.EXPORT)
    if not isinstance(ctx, FetchContext) or ctx.operation is not RequestOperation.EXPORT:
        raise ProviderModelExportValidationError("native export requires an EXPORT context")
    timeout = ctx.timeout
    if isinstance(timeout, bool) or (
        timeout is not None
        and (
            not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        )
    ):
        raise ProviderModelExportValidationError("native export timeout is invalid")
    if timeout is not None:
        return ctx
    return FetchContext(
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        _deadline_monotonic=ctx._deadline_monotonic,
        _thread_cancel_event=ctx._thread_cancel_event,
        request_budget=ctx.request_budget,
        operation=RequestOperation.EXPORT,
    )


@contextmanager
def _authorized_export_scope(
    source: str,
    model: str,
    context: FetchContext,
    admission_started: float,
) -> Iterator[tuple[RequestExecutionScope, ...]]:
    inherited = current_request_scopes()
    deadline = provider_model_store._context_deadline(context, admission_started, inherited)
    with request_execution_scope(
        source=source,
        canonical_model=model,
        operation=RequestOperation.EXPORT,
        budget=context.request_budget,
        deadline=deadline,
        cancellation=context._thread_cancel_event,
        inherited_scopes=inherited,
    ):
        yield current_request_scopes()


def _check_export_scope(scopes: tuple[RequestExecutionScope, ...]) -> None:
    """Recheck every inherited liveness condition and exact EXPORT grant."""
    validate_scope_grants(scopes)


def _reviewed_export_profile(
    *,
    registry: ProviderRegistry,
    source: str,
    model: str,
    principal: _DomainPrincipal,
) -> tuple[
    str,
    bool,
    type[ContractModel],
    domains.DomainSpec,
    tuple[ddl.Column, ...],
    tuple[str, ...],
]:
    try:
        validate_model_identity(source, model)
    except (ProviderModelIdentityError, TypeError, ValueError):
        raise ProviderModelExportValidationError("native export identity is invalid") from None
    expected_domain = _MODEL_DOMAINS.get((source, model))
    if expected_domain is None:
        raise ProviderModelExportValidationError("native export model is not supported")

    try:
        domain = provider_model_store._registered_domain(registry, source, model)
        descriptor = find_provider_model(registry, source, model)
        fetcher = registry.resolve_model(source, model)
    except Exception:
        raise ProviderModelExportError("registered export model failed reviewed identity") from None
    expected_capability = provider_model_store._MODEL_DOMAINS[(source, model)][1]
    capability = getattr(fetcher, "capability", None)
    capability_identity = tuple(
        getattr(capability, field_name, None)
        for field_name in ("asset_class", "domain", "period", "market", "source")
    )
    if (
        domain != expected_domain
        or descriptor.source != source
        or descriptor.model != model
        or descriptor.domain != domain
        or descriptor.full_capability_identity != expected_capability
        or getattr(fetcher, "canonical_model", None) != model
        or capability_identity != expected_capability
        or type(descriptor.verified) is not bool
    ):
        raise ProviderModelExportError("registered export model failed reviewed identity")

    try:
        allowed = principal.allows_domain(domain)
    except Exception:
        raise ProviderModelExportError("export principal could not be checked") from None
    if type(allowed) is not bool:
        raise ProviderModelExportError("export principal returned an invalid authorization result")
    if not allowed:
        raise ProviderModelExportForbiddenError("principal cannot export this domain")

    try:
        spec = domains.require_domain_semantics(domain)
        contract_type = domains.contract_model(domain)
        columns = provider_model_store._expected_columns(domain)
        physical_key = provider_model_codec.physical_model_key(domain)
    except Exception:
        raise ProviderModelExportError("native export schema is not fully reviewed") from None
    if (
        not spec.semantics_declared
        or spec.contract != contract_type.__name__
        or spec.storage_mode != "upsert"
        or "export" not in spec.permissions
        or not physical_key
        or len(physical_key) != len(set(physical_key))
    ):
        raise ProviderModelExportError("domain does not declare the reviewed export contract")
    return domain, descriptor.verified, contract_type, spec, columns, physical_key


def _validate_limits(max_records: object, max_bytes: object) -> tuple[int, int]:
    if type(max_records) is not int or not 1 <= max_records <= _MAX_RECORDS:
        raise ProviderModelExportValidationError(
            f"max_records must be an exact integer from 1 through {_MAX_RECORDS}"
        )
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BYTES:
        raise ProviderModelExportValidationError(
            f"max_bytes must be an exact integer from 1 through {_MAX_BYTES}"
        )
    return max_records, max_bytes


def _created_at_json(created_at: datetime) -> str:
    return created_at.isoformat().replace("+00:00", "Z")


def _create_private_file() -> tuple[tempfile.TemporaryDirectory[str], Path, BinaryIO]:
    owner: tempfile.TemporaryDirectory[str] | None = None
    descriptor: int | None = None
    try:
        owner = tempfile.TemporaryDirectory(prefix="opendata-native-export-")
        directory = Path(owner.name)
        os.chmod(directory, 0o700)
        path = directory / "snapshot.ndjson"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.chmod(path, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        return owner, path, cast("BinaryIO", stream)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if owner is not None:
            owner.cleanup()
        raise ProviderModelExportError("private export file could not be created") from None


def _decode_export_row(
    *,
    domain: str,
    source: str,
    expected_names: tuple[str, ...],
    raw_row: object,
) -> ContractModel:
    if not isinstance(raw_row, Mapping):
        raise ProviderModelExportError("database export row has an unknown shape")
    try:
        record = dict(raw_row.items())
    except Exception:
        raise ProviderModelExportError("database export row could not be mapped safely") from None
    if set(record) != set(expected_names):
        raise ProviderModelExportError("database row columns differ from the reviewed schema")
    if type(record.get("source")) is not str or record["source"] != source:
        raise ProviderModelExportError("stored row source differs from the export identity")
    try:
        row = provider_model_codec.decode_model_storage_row(domain, record)
    except Exception:
        raise ProviderModelExportError(
            "stored provider-model row failed contract decoding"
        ) from None
    return row


def _stream_result_rows(
    *,
    result: object,
    sink: _NdjsonSink,
    domain: str,
    source: str,
    expected_names: tuple[str, ...],
    max_records: int,
    scopes: tuple[RequestExecutionScope, ...],
) -> int:
    try:
        mappings = result.mappings()  # type: ignore[attr-defined]
    except Exception:
        raise ProviderModelExportError("database export result has an unknown shape") from None

    count = 0
    while True:
        _check_export_scope(scopes)
        try:
            batch = mappings.fetchmany(1)
        except Exception:
            raise ProviderModelExportError("database export stream failed") from None
        _check_export_scope(scopes)
        if isinstance(batch, (str, bytes, bytearray)) or not isinstance(batch, Sequence):
            raise ProviderModelExportError("database export batch has an unknown shape")
        if len(batch) > 1:
            raise ProviderModelExportError("database export exceeded its one-row fetch buffer")
        if not batch:
            return count
        if count >= max_records:
            raise ProviderModelExportLimitError("export exceeds its complete row limit")
        row = _decode_export_row(
            domain=domain,
            source=source,
            expected_names=expected_names,
            raw_row=batch[0],
        )
        _check_export_scope(scopes)
        try:
            row_data = row.model_dump(mode="json", by_alias=False)
        except Exception:
            raise ProviderModelExportError("contract row could not be serialized") from None
        sink.write_line({"kind": "row", "data": row_data})
        count += 1


def export_provider_model_snapshot(
    *,
    engine_factory: Callable[[], Engine],
    registry: ProviderRegistry,
    source: str,
    model: str,
    principal: _DomainPrincipal,
    filters: Mapping[str, object] | None = None,
    start: date | int | None = None,
    end: date | int | None = None,
    ctx: FetchContext | None = None,
    max_records: int = _MAX_RECORDS,
    max_bytes: int = _MAX_BYTES,
) -> NativeModelExportArtifact:
    """Export one complete matching local snapshot as private bounded NDJSON.

    All identity, ACL, semantic, request-scope, filter, and limit checks run
    before calling the lazy engine factory or creating a temporary file. This
    function checks liveness between blocking database fetches; a synchronous
    driver call itself is not interruptible and must not be moved into an
    async request task without a bounded worker that remains alive until it
    finishes.
    """
    admission_started = time.monotonic()
    if type(source) is not str or type(model) is not str:
        raise ProviderModelExportValidationError("native export identity must be exact text")
    if not callable(engine_factory):
        raise ProviderModelExportValidationError("native export engine factory is invalid")
    context = _trusted_export_context(ctx)
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    scopes: tuple[RequestExecutionScope, ...] = ()
    artifact: NativeModelExportArtifact | None = None

    try:
        with _authorized_export_scope(source, model, context, admission_started) as scopes:
            _check_export_scope(scopes)
            domain, verified, contract_type, spec, columns, physical_key = _reviewed_export_profile(
                registry=registry,
                source=source,
                model=model,
                principal=principal,
            )
            _check_export_scope(scopes)
            validated_filters = provider_model_read._validate_filters(
                domain,
                filters,
                spec,
                contract_type,
            )
            validated_start, validated_end = provider_model_read._validate_range(
                domain,
                contract_type,
                start,
                end,
            )
            record_limit, byte_limit = _validate_limits(max_records, max_bytes)
            statement, parameters = provider_model_read._build_select(
                domain=domain,
                spec=spec,
                columns=columns,
                physical_key=physical_key,
                filters=validated_filters,
                start=validated_start,
                end=validated_end,
                limit=record_limit + 1,
                offset=0,
            )
            expected_names = tuple(column.name for column in columns)
            table = domains.dwd_table(domain)
            _check_export_scope(scopes)

            try:
                engine = engine_factory()
            except Exception:
                raise ProviderModelExportError(
                    "native export engine could not be created"
                ) from None
            if getattr(getattr(engine, "dialect", None), "name", None) != "mysql":
                raise ProviderModelExportError("native model export requires MySQL")

            created_at = datetime.now(timezone.utc)
            try:
                with engine.connect() as base_connection:
                    connection = base_connection.execution_options(
                        isolation_level="REPEATABLE READ"
                    )
                    if getattr(getattr(connection, "dialect", None), "name", None) != "mysql":
                        raise ProviderModelExportError("native model export requires MySQL")
                    with connection.begin():
                        _check_export_scope(scopes)
                        try:
                            isolation_level = connection.get_isolation_level()
                        except Exception:
                            raise ProviderModelExportError(
                                "database isolation level could not be verified"
                            ) from None
                        normalized_isolation = (
                            isolation_level.upper().replace("_", " ")
                            if isinstance(isolation_level, str)
                            else ""
                        )
                        if normalized_isolation != "REPEATABLE READ":
                            raise ProviderModelExportError(
                                "native export requires verified REPEATABLE READ isolation"
                            )

                        _check_export_scope(scopes)
                        provider_model_store._validate_existing_table(
                            connection,
                            table,
                            columns,
                            physical_key,
                        )
                        _check_export_scope(scopes)
                        provider_model_store._validate_index_statistics(
                            connection,
                            table,
                            physical_key,
                        )
                        _check_export_scope(scopes)
                        provider_model_store._validate_innodb_table(connection, table)
                        _check_export_scope(scopes)

                        temporary_directory, path, stream = _create_private_file()
                        sink = _NdjsonSink(stream, max_bytes=byte_limit)
                        try:
                            sink.write_line(
                                {
                                    "kind": "metadata",
                                    "schema_version": _NDJSON_SCHEMA_VERSION,
                                    "source": source,
                                    "model": model,
                                    "domain": domain,
                                    "verified": verified,
                                    "created_at": _created_at_json(created_at),
                                    "consistency": _CONSISTENCY,
                                    "completeness": _COMPLETENESS,
                                }
                            )
                            _check_export_scope(scopes)
                            streaming_statement = statement.execution_options(
                                stream_results=True,
                                max_row_buffer=1,
                                yield_per=1,
                            )
                            try:
                                result = connection.execute(streaming_statement, parameters)
                            except Exception:
                                raise ProviderModelExportError(
                                    "native export SELECT could not be executed"
                                ) from None
                            try:
                                row_count = _stream_result_rows(
                                    result=result,
                                    sink=sink,
                                    domain=domain,
                                    source=source,
                                    expected_names=expected_names,
                                    max_records=record_limit,
                                    scopes=scopes,
                                )
                            finally:
                                try:
                                    result.close()
                                except Exception:
                                    raise ProviderModelExportError(
                                        "native export result could not be closed"
                                    ) from None
                            _check_export_scope(scopes)
                            sink.write_line(
                                {
                                    "kind": "summary",
                                    "rows": row_count,
                                    "snapshot_complete": True,
                                }
                            )
                            stream.flush()
                            os.fsync(stream.fileno())
                            if path.stat().st_size != sink.byte_count:
                                raise ProviderModelExportError(
                                    "native export byte count failed verification"
                                )
                            stream.close()
                            _check_export_scope(scopes)
                        except Exception:
                            with suppress(Exception):
                                stream.close()
                            raise
                    _check_export_scope(scopes)
            except RequestBudgetError:
                raise
            except ProviderModelExportError:
                raise
            except Exception:
                raise ProviderModelExportError("native model export transaction failed") from None

            if temporary_directory is None:
                raise ProviderModelExportError("native export artifact was not created")
            artifact = NativeModelExportArtifact(
                schema_version=_NDJSON_SCHEMA_VERSION,
                source=source,
                model=model,
                domain=domain,
                verified=verified,
                created_at=created_at,
                consistency=_CONSISTENCY,
                completeness=_COMPLETENESS,
                row_count=row_count,
                byte_count=sink.byte_count,
                sha256=sink.sha256,
                snapshot_complete=True,
                _temporary_directory=temporary_directory,
                _path=path,
            )
        _check_export_scope(scopes)
        temporary_directory = None
        return artifact
    except (
        RequestBudgetError,
        ProviderModelExportError,
        ProviderModelExportForbiddenError,
        ProviderModelExportValidationError,
    ):
        raise
    except (TypeError, ValueError):
        raise
    except Exception:
        raise ProviderModelExportError("native model export failed") from None
    finally:
        if temporary_directory is not None:
            with suppress(OSError):
                temporary_directory.cleanup()


__all__ = [
    "NativeModelExportArtifact",
    "ProviderModelExportError",
    "ProviderModelExportForbiddenError",
    "ProviderModelExportLimitError",
    "ProviderModelExportValidationError",
    "export_provider_model_snapshot",
]
