"""Bounded native DWD storage for explicitly registered provider models."""

from __future__ import annotations

import math
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import column as sql_column
from sqlalchemy import insert as sql_insert
from sqlalchemy import inspect, text
from sqlalchemy import table as sql_table
from sqlalchemy.sql.elements import quoted_name

from opendata.data import domains
from opendata.data.protocol import FetchContext
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    RequestBudgetError,
    RequestExecutionDeadlineError,
    RequestExecutionScope,
    RequestOperation,
    current_request_scopes,
    request_execution_scope,
    validate_scope_grants,
)
from opendata.pipeline import ddl, ods_writer, provider_model_codec, provider_model_schema
from opendata.pipeline.provider_model_raw import (
    NativeRawCapture,
    validate_provider_model_capture,
)
from opendata.services.provider_models import find_provider_model

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.sql.base import Executable

    from opendata.data.models.base import ContractModel


_MODEL_DOMAINS: dict[tuple[str, str], tuple[str, tuple[str, str, str, str, str]]] = {
    ("fred", "FredSeries"): (
        "fred_series",
        ("macro", "fred_series", "variable", "us", "fred"),
    ),
    ("bls", "BlsSeries"): (
        "bls_series",
        ("macro", "bls_series", "variable", "us", "bls"),
    ),
    ("fmp", "EquityHistorical"): (
        "equity_historical",
        ("stock", "equity_historical", "1d", "us", "fmp"),
    ),
}
_MAX_ROWS = 10_000
_CHUNK_SIZE = 1_000
_VARCHAR_RE = re.compile(r"^varchar\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
_COLLATION_RE = re.compile(r"\bCOLLATE\s+([A-Za-z0-9_]+)", re.IGNORECASE)
_CHARSET_RE = re.compile(r"\bCHARACTER\s+SET\s+([A-Za-z0-9_]+)", re.IGNORECASE)


class ProviderModelStoreError(RuntimeError):
    """Raised when a native provider-model DWD write cannot be proven safe."""


def _aware_utc_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("observed_at must be a timezone-aware datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("observed_at must be a valid timezone-aware datetime") from None


def _validate_context(ctx: FetchContext | None) -> FetchContext:
    if ctx is None:
        context = FetchContext(operation=RequestOperation.STORE)
    elif isinstance(ctx, FetchContext):
        context = ctx
    else:
        raise TypeError("ctx must be a FetchContext")
    if context.operation is not RequestOperation.STORE:
        raise ValueError("provider-model storage requires a STORE FetchContext")
    return context


def _context_deadline(
    context: FetchContext,
    admission_started: float,
    inherited_scopes: tuple[RequestExecutionScope, ...],
) -> float | None:
    deadlines: list[float] = []
    for scope in inherited_scopes:
        deadline = scope.deadline
        if deadline is None:
            continue
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise RequestExecutionDeadlineError("request execution deadline is invalid")
        deadlines.append(float(deadline))

    context_deadline = context._deadline_monotonic
    if context_deadline is not None and (
        isinstance(context_deadline, bool)
        or not isinstance(context_deadline, (int, float))
        or not math.isfinite(float(context_deadline))
    ):
        raise ValueError("FetchContext deadline must be finite monotonic time")
    if context_deadline is not None:
        deadlines.append(float(context_deadline))
    if context.timeout is not None:
        timeout_deadline = admission_started + float(context.timeout)
        if not math.isfinite(timeout_deadline):
            raise ValueError("FetchContext timeout deadline is invalid")
        deadlines.append(timeout_deadline)
    return min(deadlines) if deadlines else None


@contextmanager
def _authorized_store_scope(
    source: str,
    model: str,
    context: FetchContext,
    admission_started: float,
) -> Iterator[tuple[RequestExecutionScope, ...]]:
    inherited = current_request_scopes()
    deadline = _context_deadline(context, admission_started, inherited)
    with request_execution_scope(
        source=source,
        canonical_model=model,
        operation=RequestOperation.STORE,
        budget=context.request_budget,
        deadline=deadline,
        cancellation=context._thread_cancel_event,
        inherited_scopes=inherited,
    ):
        scopes = current_request_scopes()
        yield scopes


def _check_store_scope(scopes: tuple[RequestExecutionScope, ...]) -> None:
    validate_scope_grants(scopes)


def _registered_domain(
    registry: ProviderRegistry,
    source: str,
    model: str,
) -> str:
    expected = _MODEL_DOMAINS.get((source, model))
    if expected is None:
        raise ProviderModelStoreError("provider model is not enabled for native DWD storage")
    expected_domain, expected_identity = expected
    if not isinstance(registry, ProviderRegistry):
        raise TypeError("registry must be a ProviderRegistry")

    try:
        descriptor = find_provider_model(registry, source, model)
        fetcher = registry.resolve_model(source, model)
    except Exception:
        raise ProviderModelStoreError(
            "provider model is not registered in the supplied registry"
        ) from None

    capability = getattr(fetcher, "capability", None)
    if capability is None:
        raise ProviderModelStoreError("registered provider-model capability is unavailable")
    capability_identity = tuple(
        getattr(capability, field, None)
        for field in ("asset_class", "domain", "period", "market", "source")
    )
    if (
        descriptor.source != source
        or descriptor.model != model
        or descriptor.domain != expected_domain
        or descriptor.full_capability_identity != expected_identity
        or getattr(fetcher, "canonical_model", None) != model
        or capability_identity != expected_identity
        or type(descriptor.verified) is not bool
        or type(getattr(capability, "verified", None)) is not bool
        or descriptor.verified != capability.verified
    ):
        raise ProviderModelStoreError(
            "registered provider-model identity failed its reviewed checks"
        )
    return expected_domain


def _expected_columns(domain: str) -> tuple[ddl.Column, ...]:
    try:
        storage_columns = provider_model_schema.model_storage_columns(domain)
        rendered_ddl = provider_model_schema.model_dwd_table_ddl(domain)
    except Exception:
        raise ProviderModelStoreError("native DWD schema is not fully reviewed") from None

    table = domains.dwd_table(domain)
    if not rendered_ddl.startswith(f"CREATE TABLE IF NOT EXISTS `{table}` ("):
        raise ProviderModelStoreError("native DWD schema table identity is inconsistent")
    trace_columns = (
        *ddl.DWD_TRACE_COLUMNS[:-1],
        ddl.Column("_as_of", "date", nullable=True),
    )
    columns = (*storage_columns, *trace_columns)
    names = tuple(column.name for column in columns)
    if len(names) != len(set(names)):
        raise ProviderModelStoreError("native DWD schema contains duplicate columns")
    return columns


def _validate_text_widths(
    records: Sequence[Mapping[str, object]],
    columns: Sequence[ddl.Column],
) -> None:
    for record in records:
        for column in columns:
            value = record[column.name]
            if value is None:
                if not column.nullable:
                    raise ValueError("native DWD row contains NULL in a required column")
                continue
            match = _VARCHAR_RE.match(column.sql_type)
            if match is None:
                continue
            if type(value) is not str or len(value) > int(match.group(1)):
                raise ValueError("native DWD text value exceeds its reviewed column width")


def _prepare_records(
    *,
    domain: str,
    source: str,
    rows: Sequence[ContractModel],
    observed_at: datetime,
    columns: tuple[ddl.Column, ...],
    scope_check: Callable[[], None],
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    scope_check()
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a finite sequence of central contract rows")
    row_count = len(rows)
    if row_count > _MAX_ROWS:
        raise ValueError(f"provider-model write is limited to {_MAX_ROWS} rows")
    row_snapshot = tuple(rows)
    if len(row_snapshot) != row_count:
        raise ValueError("provider-model rows changed while being snapshotted")
    scope_check()

    physical_key = provider_model_codec.physical_model_key(domain)
    storage_columns = provider_model_schema.model_storage_columns(domain)
    storage_names = tuple(column.name for column in storage_columns)
    expected_names = tuple(column.name for column in columns)
    records: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    merged_at = _aware_utc_naive(observed_at)

    for row in row_snapshot:
        scope_check()
        encoded = provider_model_codec.encode_model_storage_row(domain, row)
        scope_check()
        if set(encoded) != set(storage_names):
            raise ProviderModelStoreError("provider model codec returned an unexpected column set")
        values: dict[str, object] = dict(encoded)
        values.update(
            {
                "source": source,
                "_merged_at": merged_at,
                "_diff_flag": 0,
                "_as_of": None,
            }
        )
        if set(values) != set(expected_names):
            raise ProviderModelStoreError(
                "provider-model row does not match the reviewed DWD schema"
            )
        record = {name: values[name] for name in expected_names}

        key_values = tuple(record[name] for name in physical_key)
        if any(value is None for value in key_values):
            raise ValueError("provider-model physical key must not contain NULL")
        try:
            if key_values in seen:
                raise ValueError("provider-model batch contains duplicate physical keys")
            seen.add(key_values)
        except TypeError:
            raise ValueError("provider-model physical key is not hashable") from None
        records.append(record)

    _validate_text_widths(records, columns)
    scope_check()
    return records, physical_key


def _normalized_type_sql(sql_type: str) -> str:
    without_charset = _CHARSET_RE.sub("", sql_type)
    without_collation = _COLLATION_RE.sub("", without_charset)
    normalized = re.sub(r"\s+", "", without_collation).upper()
    return re.sub(r"\bINTEGER\b", "INT", normalized)


def _collation_and_charset(
    reflected: Mapping[str, object],
    compiled: str,
) -> tuple[str | None, str | None]:
    reflected_type = reflected["type"]
    collation = getattr(reflected_type, "collation", None) or reflected.get("collation")
    charset = getattr(reflected_type, "charset", None) or reflected.get("charset")
    if not isinstance(collation, str):
        match = _COLLATION_RE.search(compiled)
        collation = None if match is None else match.group(1)
    if not isinstance(charset, str):
        match = _CHARSET_RE.search(compiled)
        charset = None if match is None else match.group(1)
    if charset is None and isinstance(collation, str):
        prefix = re.match(r"^(utf8mb4|utf8mb3|utf8|latin1|ascii)_", collation, re.IGNORECASE)
        if prefix is not None:
            charset = prefix.group(1)
    return (
        collation.lower() if isinstance(collation, str) else None,
        charset.lower() if isinstance(charset, str) else None,
    )


def _validate_reflected_type(
    reflected: Mapping[str, object],
    expected: ddl.Column,
    connection: Connection,
    physical_key: tuple[str, ...],
) -> None:
    reflected_type = reflected.get("type")
    if reflected_type is None:
        raise ProviderModelStoreError("database column type is unknown")
    try:
        compile_type = getattr(reflected_type, "compile", None)
        if not callable(compile_type):
            raise TypeError
        compiled = str(compile_type(dialect=connection.dialect))
    except Exception:
        raise ProviderModelStoreError("database column type could not be compiled") from None

    if _normalized_type_sql(compiled) != _normalized_type_sql(expected.sql_type):
        raise ProviderModelStoreError("database column type differs from the reviewed schema")

    expected_varchar = _VARCHAR_RE.match(expected.sql_type)
    if expected_varchar is not None:
        width = int(expected_varchar.group(1))
        reflected_length = getattr(reflected_type, "length", None)
        compiled_width = re.match(r"^VARCHAR\s*\(\s*(\d+)\s*\)", compiled, re.IGNORECASE)
        if (
            reflected_length != width
            or compiled_width is None
            or int(compiled_width.group(1)) != width
        ):
            raise ProviderModelStoreError("database varchar width differs from the reviewed schema")

    if expected.name in physical_key and expected_varchar is not None:
        expected_collation_match = _COLLATION_RE.search(expected.sql_type)
        expected_charset_match = _CHARSET_RE.search(expected.sql_type)
        actual_collation, actual_charset = _collation_and_charset(reflected, compiled)
        expected_collation = (
            None if expected_collation_match is None else expected_collation_match.group(1).lower()
        )
        expected_charset = (
            None if expected_charset_match is None else expected_charset_match.group(1).lower()
        )
        if (
            expected_collation is None
            or expected_charset is None
            or actual_collation != expected_collation
            or actual_charset != expected_charset
        ):
            raise ProviderModelStoreError("database text primary-key collation is not reviewed")


def _validate_unique_keys(
    inspector: object,
    table: str,
) -> None:
    try:
        unique_constraints = inspector.get_unique_constraints(table)  # type: ignore[attr-defined]
        indexes = inspector.get_indexes(table)  # type: ignore[attr-defined]
    except Exception:
        raise ProviderModelStoreError("database unique constraints could not be verified") from None

    if unique_constraints:
        raise ProviderModelStoreError("database has an unreviewed extra unique constraint")
    for index in indexes:
        if not index.get("unique") or index.get("name") == "PRIMARY":
            continue
        raise ProviderModelStoreError("database has an unreviewed extra unique index")


def _validate_index_statistics(
    connection: Connection,
    table: str,
    physical_key: tuple[str, ...],
) -> None:
    """Prove full MySQL key columns, including prefix lengths omitted by Inspector."""
    statement = text(
        "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, "
        "EXPRESSION, INDEX_TYPE FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
    )
    try:
        result = connection.execute(statement, {"table": table})
        rows = result.mappings().all()
    except Exception:
        raise ProviderModelStoreError("database index statistics could not be verified") from None

    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise ProviderModelStoreError("database index statistics returned an unknown shape")

    expected_fields = {
        "INDEX_NAME",
        "NON_UNIQUE",
        "SEQ_IN_INDEX",
        "COLUMN_NAME",
        "SUB_PART",
        "EXPRESSION",
        "INDEX_TYPE",
    }
    known_index_types = {"BTREE", "FULLTEXT", "HASH", "RTREE"}
    indexes: dict[str, dict[int, tuple[str, int | None, str]]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            raise ProviderModelStoreError("database index statistics contain unknown metadata")
        index_name = row["INDEX_NAME"]
        non_unique = row["NON_UNIQUE"]
        sequence = row["SEQ_IN_INDEX"]
        column_name = row["COLUMN_NAME"]
        sub_part = row["SUB_PART"]
        expression = row["EXPRESSION"]
        index_type = row["INDEX_TYPE"]
        if (
            type(index_name) is not str
            or not index_name
            or type(non_unique) is not int
            or non_unique not in (0, 1)
            or type(sequence) is not int
            or sequence < 1
            or type(column_name) is not str
            or not column_name
            or (sub_part is not None and (type(sub_part) is not int or sub_part < 1))
            or expression is not None
            or type(index_type) is not str
            or index_type.upper() not in known_index_types
        ):
            raise ProviderModelStoreError("database index statistics contain unknown metadata")
        if index_name != "PRIMARY" and non_unique == 0:
            raise ProviderModelStoreError("database has an unreviewed extra unique index")
        if index_name == "PRIMARY" and (
            non_unique != 0
            or sub_part is not None
            or expression is not None
            or index_type.upper() != "BTREE"
        ):
            raise ProviderModelStoreError("database primary key is not a full BTREE key")

        parts = indexes.setdefault(index_name, {})
        if sequence in parts:
            raise ProviderModelStoreError("database index statistics repeat a key position")
        parts[sequence] = (column_name, sub_part, index_type.upper())

    primary = indexes.get("PRIMARY")
    if primary is None:
        raise ProviderModelStoreError("database primary key statistics are missing")
    for index_name, parts in indexes.items():
        if sorted(parts) != list(range(1, len(parts) + 1)):
            raise ProviderModelStoreError("database index statistics have invalid key positions")
        if index_name == "PRIMARY":
            primary_columns = tuple(parts[position][0] for position in range(1, len(parts) + 1))
            if primary_columns != physical_key:
                raise ProviderModelStoreError(
                    "database primary key statistics differ from the reviewed codec"
                )


def _validate_existing_table(
    connection: Connection,
    table: str,
    columns: tuple[ddl.Column, ...],
    physical_key: tuple[str, ...],
) -> None:
    try:
        inspector = inspect(connection)
        if not inspector.has_table(table):
            raise ProviderModelStoreError("target native DWD table does not exist")
        reflected_columns = inspector.get_columns(table)
    except ProviderModelStoreError:
        raise
    except Exception:
        raise ProviderModelStoreError(
            "target native DWD table schema could not be inspected"
        ) from None

    expected_names = tuple(column.name for column in columns)
    actual_names = tuple(column.get("name") for column in reflected_columns)
    if actual_names != expected_names:
        raise ProviderModelStoreError("target native DWD columns differ from the reviewed schema")

    for actual, expected in zip(reflected_columns, columns, strict=True):
        if type(actual.get("nullable")) is not bool or actual["nullable"] is not expected.nullable:
            raise ProviderModelStoreError(
                "target native DWD nullability differs from the reviewed schema"
            )
        _validate_reflected_type(actual, expected, connection, physical_key)

    try:
        primary_key = inspector.get_pk_constraint(table)
    except Exception:
        raise ProviderModelStoreError(
            "target native DWD primary key could not be verified"
        ) from None
    reflected_key = primary_key.get("constrained_columns")
    if not isinstance(reflected_key, (list, tuple)) or tuple(reflected_key) != physical_key:
        raise ProviderModelStoreError(
            "target native DWD primary key differs from the reviewed codec"
        )

    reflected_by_name = {column["name"]: column for column in reflected_columns}
    if any(reflected_by_name[name].get("nullable") is not False for name in physical_key):
        raise ProviderModelStoreError("target native DWD primary key contains nullable columns")

    _validate_unique_keys(inspector, table)


def _chunks(records: Sequence[dict[str, object]]) -> Iterator[Sequence[dict[str, object]]]:
    for start in range(0, len(records), _CHUNK_SIZE):
        yield records[start : start + _CHUNK_SIZE]


def write_provider_model_rows(
    *,
    engine: Engine,
    registry: ProviderRegistry,
    source: str,
    model: str,
    rows: Sequence[ContractModel],
    observed_at: datetime,
    ctx: FetchContext | None = None,
) -> int:
    """Validate, authorize, and atomically upsert one bounded native DWD batch."""
    admission_started = time.monotonic()
    if not isinstance(source, str) or not isinstance(model, str):
        raise TypeError("source and model must be strings")
    context = _validate_context(ctx)
    with _authorized_store_scope(source, model, context, admission_started) as scopes:
        _check_store_scope(scopes)
        observed_at_naive = _aware_utc_naive(observed_at)
        domain = _registered_domain(registry, source, model)
        _check_store_scope(scopes)
        columns = _expected_columns(domain)
        _check_store_scope(scopes)
        records, physical_key = _prepare_records(
            domain=domain,
            source=source,
            rows=rows,
            observed_at=observed_at_naive.replace(tzinfo=timezone.utc),
            columns=columns,
            scope_check=lambda: _check_store_scope(scopes),
        )
        _check_store_scope(scopes)
        table = domains.dwd_table(domain)
        dialect = getattr(getattr(engine, "dialect", None), "name", None)
        if dialect != "mysql":
            raise ProviderModelStoreError("native provider-model storage requires MySQL")
        if not records:
            return 0

        column_names = tuple(column.name for column in columns)
        statement = text(ods_writer.build_upsert_sql(table, column_names, physical_key))
        try:
            with engine.begin() as connection:
                _check_store_scope(scopes)
                if getattr(getattr(connection, "dialect", None), "name", None) != "mysql":
                    raise ProviderModelStoreError("native provider-model storage requires MySQL")
                _validate_existing_table(connection, table, columns, physical_key)
                _validate_index_statistics(connection, table, physical_key)
                for chunk in _chunks(records):
                    _check_store_scope(scopes)
                    connection.execute(statement, list(chunk))
                _check_store_scope(scopes)
        except (ProviderModelStoreError, RequestBudgetError):
            raise
        except Exception:
            raise ProviderModelStoreError("native provider-model transaction failed") from None
    return len(records)


def _validate_uuid4_batch_id(batch_id: object) -> str:
    if type(batch_id) is not str:
        raise ValueError("batch_id must be a canonical UUIDv4")
    try:
        parsed = uuid.UUID(batch_id)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("batch_id must be a canonical UUIDv4") from None
    if str(parsed) != batch_id or parsed.version != 4:
        raise ValueError("batch_id must be a canonical UUIDv4")
    return batch_id


def _expected_ods_columns(domain: str, source: str) -> tuple[ddl.Column, ...]:
    try:
        columns = provider_model_schema.model_ods_storage_columns(domain, source)
        rendered_ddl = provider_model_schema.model_ods_table_ddl(domain, source)
        table = domains.ods_table(domain, source)
    except Exception:
        raise ProviderModelStoreError("native ODS capture schema is not fully reviewed") from None
    if not rendered_ddl.startswith(f"CREATE TABLE IF NOT EXISTS `{table}` ("):
        raise ProviderModelStoreError("native ODS schema table identity is inconsistent")
    names = tuple(column.name for column in columns)
    if len(names) != len(set(names)):
        raise ProviderModelStoreError("native ODS capture schema contains duplicate columns")
    return columns


def _validate_innodb_table(connection: Connection, table: str) -> None:
    statement = text(
        "SELECT TABLE_NAME, ENGINE FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
    )
    try:
        result = connection.execute(statement, {"table": table})
        rows = result.mappings().all()
    except Exception:
        raise ProviderModelStoreError("database table engine could not be verified") from None
    if (
        isinstance(rows, (str, bytes, bytearray))
        or not isinstance(rows, Sequence)
        or len(rows) != 1
    ):
        raise ProviderModelStoreError("database table engine metadata is missing or ambiguous")
    row = rows[0]
    if (
        not isinstance(row, Mapping)
        or set(row) != {"TABLE_NAME", "ENGINE"}
        or type(row["TABLE_NAME"]) is not str
        or row["TABLE_NAME"] != table
        or type(row["ENGINE"]) is not str
        or row["ENGINE"] != "InnoDB"
    ):
        raise ProviderModelStoreError("native ODS and DWD tables must use InnoDB")


def _build_plain_insert_statement(table: str, columns: Sequence[ddl.Column]) -> Executable:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ProviderModelStoreError("native ODS table identifier is invalid")
    names = tuple(column.name for column in columns)
    if not names or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
        raise ProviderModelStoreError("native ODS column identifiers are invalid")
    table_clause = sql_table(
        quoted_name(table, True),
        *(sql_column(quoted_name(name, True)) for name in names),
    )
    return sql_insert(table_clause)


def _prepare_ods_record(
    *,
    capture: NativeRawCapture,
    source: str,
    batch_id: str,
    fetched_at: datetime,
    dwd_row_count: int,
    columns: tuple[ddl.Column, ...],
) -> dict[str, object]:
    values: dict[str, object] = {
        "_batch_id": batch_id,
        "_source": source,
        "_fetched_at": fetched_at,
        "_raw_scope": capture.raw_scope,
        "_schema_version": capture.schema_version,
        "_raw_row_count": capture.raw_row_count,
        "_dwd_row_count": dwd_row_count,
        "_payload": capture.raw_json,
        "_query_context": capture.query_json,
        "_payload_sha256": capture.raw_sha256,
        "_query_sha256": capture.query_sha256,
    }
    expected_names = tuple(column.name for column in columns)
    if set(values) != set(expected_names):
        raise ProviderModelStoreError("native ODS capture does not match its reviewed columns")
    record = {name: values[name] for name in expected_names}
    _validate_text_widths((record,), columns)
    return record


def write_provider_model_batch(
    *,
    engine: Engine,
    registry: ProviderRegistry,
    source: str,
    model: str,
    capture: NativeRawCapture,
    rows: Sequence[ContractModel],
    observed_at: datetime,
    batch_id: str,
    ctx: FetchContext | None = None,
) -> int:
    """Atomically append one extractor capture and upsert its typed DWD rows."""
    admission_started = time.monotonic()
    if type(source) is not str or type(model) is not str:
        raise TypeError("source and model must be strings")
    canonical_batch_id = _validate_uuid4_batch_id(batch_id)
    context = _validate_context(ctx)

    with _authorized_store_scope(source, model, context, admission_started) as scopes:

        def scope_check() -> None:
            _check_store_scope(scopes)

        scope_check()
        observed_at_naive = _aware_utc_naive(observed_at)
        domain = _registered_domain(registry, source, model)
        scope_check()
        validated_capture = validate_provider_model_capture(
            source=source,
            model=model,
            capture=capture,
        )
        scope_check()
        dwd_columns = _expected_columns(domain)
        ods_columns = _expected_ods_columns(domain, source)
        scope_check()
        dwd_records, dwd_key = _prepare_records(
            domain=domain,
            source=source,
            rows=rows,
            observed_at=observed_at_naive.replace(tzinfo=timezone.utc),
            columns=dwd_columns,
            scope_check=scope_check,
        )
        ods_record = _prepare_ods_record(
            capture=validated_capture,
            source=source,
            batch_id=canonical_batch_id,
            fetched_at=observed_at_naive,
            dwd_row_count=len(dwd_records),
            columns=ods_columns,
        )
        scope_check()

        dwd_table = domains.dwd_table(domain)
        ods_table = domains.ods_table(domain, source)
        if getattr(getattr(engine, "dialect", None), "name", None) != "mysql":
            raise ProviderModelStoreError("native provider-model storage requires MySQL")

        ods_insert = _build_plain_insert_statement(ods_table, ods_columns)
        dwd_upsert = text(
            ods_writer.build_upsert_sql(
                dwd_table,
                tuple(column.name for column in dwd_columns),
                dwd_key,
            )
        )
        try:
            with engine.begin() as connection:
                scope_check()
                if getattr(getattr(connection, "dialect", None), "name", None) != "mysql":
                    raise ProviderModelStoreError("native provider-model storage requires MySQL")

                scope_check()
                _validate_existing_table(connection, ods_table, ods_columns, ("_batch_id",))
                scope_check()
                _validate_index_statistics(connection, ods_table, ("_batch_id",))
                scope_check()
                _validate_innodb_table(connection, ods_table)
                scope_check()

                _validate_existing_table(connection, dwd_table, dwd_columns, dwd_key)
                scope_check()
                _validate_index_statistics(connection, dwd_table, dwd_key)
                scope_check()
                _validate_innodb_table(connection, dwd_table)
                scope_check()

                connection.execute(ods_insert, ods_record)
                scope_check()
                for chunk in _chunks(dwd_records):
                    scope_check()
                    connection.execute(dwd_upsert, list(chunk))
                    scope_check()
                scope_check()
        except (ProviderModelStoreError, RequestBudgetError):
            raise
        except Exception:
            raise ProviderModelStoreError(
                "native provider-model batch transaction failed"
            ) from None
    return len(dwd_records)


__all__ = [
    "ProviderModelStoreError",
    "write_provider_model_batch",
    "write_provider_model_rows",
]
