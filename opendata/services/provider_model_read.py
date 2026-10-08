"""Bounded reads from the reviewed native provider-model DWD tables.

Each call owns one read transaction and decodes every returned row through
the central provider-model codec. Separate offset pages do not share a
snapshot and are not an export-consistency guarantee.
"""

from __future__ import annotations

import math
import types
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING, Annotated, Literal, Union, cast, get_args, get_origin

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text

from opendata.data import domains
from opendata.data.models.equity_price import EquityHistorical
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
from opendata.services import provider_model_store

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.sql.elements import TextClause

    from opendata.data.domains import DomainSpec
    from opendata.data.models.base import ContractModel
    from opendata.data.registry import ProviderRegistry


_MAX_LIMIT = 1_000
_IDENTIFIER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_NONBLANK_FILTERS = {
    "fred_series": frozenset({"series_id"}),
    "bls_series": frozenset({"series_id", "period"}),
    "equity_historical": frozenset({"symbol"}),
}


class ProviderModelReadError(RuntimeError):
    """Raised when a native provider-model page cannot be read safely."""


def _validate_context(ctx: FetchContext | None) -> FetchContext:
    if ctx is None:
        return FetchContext(operation=RequestOperation.QUERY)
    if not isinstance(ctx, FetchContext):
        raise TypeError("ctx must be a FetchContext")
    if ctx.operation is not RequestOperation.QUERY:
        raise ValueError("provider-model reads require a QUERY FetchContext")
    return ctx


@contextmanager
def _authorized_query_scope(
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
        operation=RequestOperation.QUERY,
        budget=context.request_budget,
        deadline=deadline,
        cancellation=context._thread_cancel_event,
        inherited_scopes=inherited,
    ):
        yield current_request_scopes()


def _check_query_scope(scopes: tuple[RequestExecutionScope, ...]) -> None:
    validate_scope_grants(scopes)


def _contains_integer_annotation(annotation: object) -> bool:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return bool(args) and _contains_integer_annotation(args[0])
    if origin is Literal:
        return any(type(value) is int for value in args)
    if origin in (Union, types.UnionType):
        return any(_contains_integer_annotation(value) for value in args)
    return annotation is int


def _contains_boolean_annotation(annotation: object) -> bool:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return bool(args) and _contains_boolean_annotation(args[0])
    if origin is Literal:
        return any(type(value) is bool for value in args)
    if origin in (Union, types.UnionType):
        return any(_contains_boolean_annotation(value) for value in args)
    return annotation is bool


def _contains_integer_literal(annotation: object) -> bool:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return bool(args) and _contains_integer_literal(args[0])
    if origin is Literal:
        return any(type(value) is int for value in args)
    if origin in (Union, types.UnionType):
        return any(_contains_integer_literal(value) for value in args)
    return False


def _validate_filter_value(
    domain: str,
    model: type[ContractModel],
    field_name: str,
    value: object,
) -> object:
    field = model.model_fields[field_name]
    annotation = field.rebuild_annotation()
    if (
        type(value) is bool
        and _contains_integer_annotation(annotation)
        and not _contains_boolean_annotation(annotation)
    ):
        raise ValueError("boolean is not an integer filter value")
    if type(value) is float and _contains_integer_literal(annotation):
        raise ValueError("float is not an integer literal filter value")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("filter number must be finite")
    try:
        validated = TypeAdapter(annotation).validate_python(value, strict=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise ValueError("filter value does not match the central contract field") from None
    if field_name in _NONBLANK_FILTERS.get(domain, ()) and (
        type(validated) is not str or not validated.strip()
    ):
        raise ValueError("filter value must satisfy its central nonempty text rule")
    if domain == "equity_historical" and field_name == "symbol":
        if model is not EquityHistorical or type(validated) is not str:
            raise ValueError("FMP symbol filter does not match its central contract")
        try:
            validated = EquityHistorical._validate_source_symbol(validated)
        except (TypeError, ValueError):
            raise ValueError("FMP symbol filter violates the central source-symbol rule") from None
    return validated


def _validate_filters(
    domain: str,
    filters: Mapping[str, object] | None,
    spec: DomainSpec,
    model: type[ContractModel],
) -> dict[str, object]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise TypeError("filters must be a mapping of contract dimensions")

    allowed = set(spec.filter_dims)
    copied: dict[str, object] = {}
    try:
        for field_name, value in filters.items():
            if type(field_name) is not str or field_name not in allowed:
                raise ValueError("filter field is not a reviewed domain dimension")
            copied[field_name] = _validate_filter_value(domain, model, field_name, value)
    except (TypeError, ValueError):
        raise
    except Exception:
        raise ValueError("filters mapping could not be read safely") from None
    return copied


def _validate_range(
    domain: str,
    model: type[ContractModel],
    start: date | int | None,
    end: date | int | None,
) -> tuple[date | int | None, date | int | None]:
    if domain == "fred_series":
        if start is not None and type(start) is not date:
            raise TypeError("FRED range bounds must be plain dates")
        if end is not None and type(end) is not date:
            raise TypeError("FRED range bounds must be plain dates")
    elif domain == "equity_historical":
        if start is not None and type(start) is not date:
            raise TypeError("FMP range bounds must be plain dates")
        if end is not None and type(end) is not date:
            raise TypeError("FMP range bounds must be plain dates")
    elif domain == "bls_series":
        if start is not None and type(start) is not int:
            raise TypeError("BLS range bounds must be exact integer years")
        if end is not None and type(end) is not int:
            raise TypeError("BLS range bounds must be exact integer years")
    else:  # pragma: no cover - guarded by the reviewed source/model pair
        raise ProviderModelReadError("provider-model range domain is not supported")
    time_field = "year" if domain == "bls_series" else "date"
    adapter = TypeAdapter(model.model_fields[time_field].rebuild_annotation())
    try:
        if start is not None:
            adapter.validate_python(start, strict=True)
        if end is not None:
            adapter.validate_python(end, strict=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise ValueError("range bound is outside the central contract time field") from None
    if start is not None and end is not None:
        start_after_end = (
            cast("int", start) > cast("int", end)
            if domain == "bls_series"
            else cast("date", start) > cast("date", end)
        )
        if start_after_end:
            raise ValueError("start must not be after end")
    return start, end


def _validate_page_bounds(limit: int, offset: int) -> tuple[int, int]:
    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 through {_MAX_LIMIT}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative exact integer")
    return limit, offset


def _quoted_identifier(value: str) -> str:
    if not value or any(character not in _IDENTIFIER_CHARS for character in value):
        raise ProviderModelReadError("reviewed SQL identifier is invalid")
    return f"`{value}`"


def _filter_expression(domain: str, field_name: str, storage_names: set[str]) -> str:
    if field_name not in storage_names or field_name.startswith("_"):
        raise ProviderModelReadError("filter dimension has no reviewed physical mapping")
    return _quoted_identifier(field_name)


def _build_select(
    *,
    domain: str,
    spec: DomainSpec,
    columns: Sequence[ddl.Column],
    physical_key: tuple[str, ...],
    filters: Mapping[str, object],
    start: date | int | None,
    end: date | int | None,
    limit: int,
    offset: int,
) -> tuple[TextClause, dict[str, object]]:
    column_names = tuple(column.name for column in columns)
    if not column_names:
        raise ProviderModelReadError("reviewed read columns are invalid")
    storage_names = set(column_names)
    quoted_columns = ", ".join(_quoted_identifier(name) for name in column_names)
    table = _quoted_identifier(domains.dwd_table(domain))
    clauses: list[str] = []
    parameters: dict[str, object] = {"limit": limit, "offset": offset}

    for position, field_name in enumerate(sorted(filters)):
        if field_name not in spec.filter_dims:
            raise ProviderModelReadError("filter dimension is outside reviewed semantics")
        expression = _filter_expression(domain, field_name, storage_names)
        value = filters[field_name]
        if value is None:
            clauses.append(f"{expression} IS NULL")
        else:
            parameter = f"filter_{position}"
            clauses.append(f"{expression} = :{parameter}")
            parameters[parameter] = value

    time_field = spec.time_field
    if not isinstance(time_field, str) or time_field not in storage_names:
        raise ProviderModelReadError("domain time field has no reviewed physical column")
    quoted_time = _quoted_identifier(time_field)
    if start is not None:
        clauses.append(f"{quoted_time} >= :start")
        parameters["start"] = start
    if end is not None:
        clauses.append(f"{quoted_time} <= :end")
        parameters["end"] = end

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = ", ".join(_quoted_identifier(name) for name in physical_key)
    statement = text(
        f"SELECT {quoted_columns} FROM {table}{where} ORDER BY {order} "  # noqa: S608  # nosec B608; identifiers are validated reviewed schema/codec values
        f"LIMIT :limit OFFSET :offset"
    )
    return statement, parameters


def _decode_page_rows(
    *,
    domain: str,
    source: str,
    expected_names: tuple[str, ...],
    raw_rows: Sequence[object],
    page_limit: int,
    scopes: tuple[RequestExecutionScope, ...],
) -> tuple[ContractModel, ...]:
    if len(raw_rows) > page_limit:
        raise ProviderModelReadError("database returned more rows than the bounded page limit")
    decoded: list[ContractModel] = []
    for raw_row in raw_rows:
        _check_query_scope(scopes)
        if not isinstance(raw_row, Mapping):
            raise ProviderModelReadError("database row has an unknown shape")
        try:
            record = dict(raw_row.items())
        except Exception:
            raise ProviderModelReadError("database row could not be mapped safely") from None
        _check_query_scope(scopes)
        if set(record) != set(expected_names):
            raise ProviderModelReadError("database row columns differ from the reviewed schema")
        if type(record.get("source")) is not str or record["source"] != source:
            raise ProviderModelReadError("stored row source differs from the requested provider")
        try:
            decoded.append(provider_model_codec.decode_model_storage_row(domain, record))
        except Exception:
            raise ProviderModelReadError(
                "stored provider-model row failed contract decoding"
            ) from None
        _check_query_scope(scopes)
    return tuple(decoded)


def read_provider_model_rows(
    *,
    engine: Engine,
    registry: ProviderRegistry,
    source: str,
    model: str,
    filters: Mapping[str, object] | None = None,
    start: date | int | None = None,
    end: date | int | None = None,
    limit: int = 1_000,
    offset: int = 0,
    ctx: FetchContext | None = None,
) -> tuple[ContractModel, ...]:
    """Read one stable-key-sorted bounded page from an existing native DWD.

    The time limit begins at function admission. Each call validates the
    current QUERY grant and uses one read transaction; independently requested
    offset pages do not share a snapshot. Reads perform no provider extraction,
    HTTP attempt accounting, DDL, or DML.
    """
    admission_started = provider_model_store.time.monotonic()
    if type(source) is not str or type(model) is not str:
        raise TypeError("source and model must be strings")
    context = _validate_context(ctx)

    with _authorized_query_scope(source, model, context, admission_started) as scopes:
        _check_query_scope(scopes)
        try:
            domain = provider_model_store._registered_domain(registry, source, model)
        except Exception:
            raise ProviderModelReadError(
                "registered provider model failed reviewed identity"
            ) from None
        _check_query_scope(scopes)

        try:
            columns = provider_model_store._expected_columns(domain)
            physical_key = provider_model_codec.physical_model_key(domain)
            spec = domains.require_domain_semantics(domain)
            contract = domains.contract_model(domain)
        except Exception:
            raise ProviderModelReadError(
                "provider-model read schema is not fully reviewed"
            ) from None
        _check_query_scope(scopes)

        if "query" not in spec.permissions:
            raise ProviderModelReadError("domain does not declare local QUERY permission")
        validated_filters = _validate_filters(domain, filters, spec, contract)
        validated_start, validated_end = _validate_range(domain, contract, start, end)
        validated_limit, validated_offset = _validate_page_bounds(limit, offset)
        statement, parameters = _build_select(
            domain=domain,
            spec=spec,
            columns=columns,
            physical_key=physical_key,
            filters=validated_filters,
            start=validated_start,
            end=validated_end,
            limit=validated_limit,
            offset=validated_offset,
        )
        _check_query_scope(scopes)

        if getattr(getattr(engine, "dialect", None), "name", None) != "mysql":
            raise ProviderModelReadError("native provider-model reads require MySQL")

        expected_names = tuple(column.name for column in columns)
        try:
            with engine.begin() as connection:
                _check_query_scope(scopes)
                if getattr(getattr(connection, "dialect", None), "name", None) != "mysql":
                    raise ProviderModelReadError("native provider-model reads require MySQL")
                provider_model_store._validate_existing_table(
                    connection,
                    domains.dwd_table(domain),
                    columns,
                    physical_key,
                )
                _check_query_scope(scopes)
                provider_model_store._validate_index_statistics(
                    connection,
                    domains.dwd_table(domain),
                    physical_key,
                )
                _check_query_scope(scopes)
                result = connection.execute(statement, parameters)
                raw_rows = result.mappings().fetchmany(validated_limit)
                if isinstance(raw_rows, (str, bytes, bytearray)) or not isinstance(
                    raw_rows, Sequence
                ):
                    raise ProviderModelReadError("database page has an unknown shape")
                _check_query_scope(scopes)
                decoded = _decode_page_rows(
                    domain=domain,
                    source=source,
                    expected_names=expected_names,
                    raw_rows=raw_rows,
                    page_limit=validated_limit,
                    scopes=scopes,
                )
                _check_query_scope(scopes)
        except RequestBudgetError:
            raise
        except ProviderModelReadError:
            raise
        except Exception:
            raise ProviderModelReadError("native provider-model read failed") from None
    return decoded


__all__ = ["ProviderModelReadError", "read_provider_model_rows"]
