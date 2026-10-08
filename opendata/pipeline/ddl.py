"""Warehouse DDL generation (design §8.1/§8.3, milestone A4.1).

Single source of truth for the ``opendata_data`` schema: the
independent alembic environment (``alembic_data/``) renders its
migrations from these builders, so the generated DDL and the
migrations can never drift.

Layers:

* **ods** - one table per ``(domain, source)``: the source's raw
  columns as delivered **plus** the metadata trio ``_source`` /
  ``_fetched_at`` / ``_batch_id`` (design §8.1). Columns are declared
  by the caller (the source schema is source-specific); the generator
  owns naming, the business-key primary key and the partition trio.
* **dwd** - one table per domain, derived from the contract model
  plus the traceability columns ``source`` / ``_merged_at`` /
  ``_diff_flag`` / ``_as_of`` (design §8.3).

Partition trio (design §8.1): yearly ``RANGE COLUMNS`` partitions, a
``MAXVALUE`` fallback partition for the maintenance task (A4.3), and
the fail-closed rule that every unique key - here the primary key -
contains the partition key.
"""

from __future__ import annotations

import re
import types
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal, TypeGuard, Union, get_args, get_origin

from opendata.data.domains import contract_model, dwd_table, ods_table, require_domain
from opendata.data.models.base import ContractModel

#: DDL identifiers. Word characters only (never a backtick, quote,
#: whitespace or separator), so an identifier can never break out of
#: its quoting; Unicode letters are allowed because the ods layer
#: keeps source naming (design §8.1: sina/em columns are Chinese).
_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)

#: String columns that take part in business keys get a bounded width.
_KEY_STRING_LENGTHS = {"symbol": 64, "index_symbol": 64, "exchange": 32}
_DEFAULT_STRING_LENGTH = 255

#: Cap applied when the caller does not pin the partition start year.
_DEFAULT_PARTITION_YEARS = 3


@dataclass(frozen=True)
class Column:
    """One generated table column.

    Attributes:
        name: Column name (validated before use).
        sql_type: MySQL type, e.g. ``varchar(64)``.
        nullable: Whether ``NULL`` is allowed.
        sql_default: Optional SQL default expression.
    """

    name: str
    sql_type: str
    nullable: bool = True
    sql_default: str | None = None


#: ods metadata trio (design §8.1).
ODS_METADATA_COLUMNS = (
    Column("_source", "varchar(32)", nullable=False),
    Column("_fetched_at", "datetime", nullable=False),
    Column("_batch_id", "char(36)", nullable=False),
)

#: dwd traceability columns (design §8.3).
DWD_TRACE_COLUMNS = (
    Column("source", "varchar(32)", nullable=False),
    Column("_merged_at", "datetime", nullable=False),
    Column("_diff_flag", "tinyint(1)", nullable=False, sql_default="0"),
    Column("_as_of", "date", nullable=False),
)


def year_partitions(start_year: int, years: int) -> list[tuple[str, date]]:
    """Build the yearly partition upper bounds.

    The same helper feeds the DDL and the partition maintenance task
    (A4.3), which checks whether the latest bound is running out.

    Args:
        start_year: First partitioned year.
        years: Number of yearly partitions (must be positive).

    Returns:
        ``(partition_name, exclusive_upper_bound)`` pairs in order.

    Raises:
        ValueError: If ``years`` is not positive.
    """
    if years <= 0:
        raise ValueError(f"years must be positive, got {years}")
    return [(f"p{year}", date(year + 1, 1, 1)) for year in range(start_year, start_year + years)]


def contract_columns(domain: str) -> list[Column]:
    """Derive the dwd value columns of a domain from its contract model.

    Args:
        domain: Registered domain identifier.

    Returns:
        One column per contract field; a field typed ``X | None`` is
        nullable, everything else is not.

    Raises:
        LookupError: If the domain is unknown.
        ValueError: If a field type has no MySQL mapping (fail closed).
    """
    model = contract_model(domain)
    return [
        Column(name, _sql_type(field.annotation, name), nullable=_is_optional(field.annotation))
        for name, field in model.model_fields.items()
    ]


def ods_table_ddl(
    domain: str,
    source: str,
    columns: list[Column],
    *,
    key: tuple[str, ...],
    partition_key: str | None = None,
    start_year: int | None = None,
    years: int = _DEFAULT_PARTITION_YEARS,
) -> str:
    """Render the DDL of one ods table.

    Args:
        domain: Registered domain identifier.
        source: Source identifier (e.g. ``akshare``).
        columns: Source-raw columns as delivered by the provider.
        key: Business-key columns (the primary key).
        partition_key: Column to partition by, or None.
        start_year: First partition year; defaults to the current year.
        years: Number of yearly partitions.

    Returns:
        The ``CREATE TABLE IF NOT EXISTS`` statement.

    Raises:
        LookupError: If the domain is unknown.
        ValueError: If the source identifier, a column name, the key
            or the partition declaration is invalid (fail closed).
    """
    table = ods_table(domain, source)
    return _table_ddl(
        table,
        [*columns, *ODS_METADATA_COLUMNS],
        key=key,
        partition_key=partition_key,
        start_year=start_year,
        years=years,
    )


def dwd_table_ddl(
    domain: str,
    *,
    key: tuple[str, ...],
    partition_key: str | None = None,
    start_year: int | None = None,
    years: int = _DEFAULT_PARTITION_YEARS,
) -> str:
    """Render the DDL of one dwd table.

    Args:
        domain: Registered domain identifier.
        key: Business-key columns (the primary key).
        partition_key: Column to partition by, or None.
        start_year: First partition year; defaults to the current year.
        years: Number of yearly partitions.

    Returns:
        The ``CREATE TABLE IF NOT EXISTS`` statement.

    Raises:
        LookupError: If the domain is unknown.
        ValueError: If the key or the partition declaration is
            invalid (fail closed).
    """
    require_domain(domain)
    return _table_ddl(
        dwd_table(domain),
        [*contract_columns(domain), *DWD_TRACE_COLUMNS],
        key=key,
        partition_key=partition_key,
        start_year=start_year,
        years=years,
    )


def _table_ddl(
    table: str,
    columns: list[Column],
    *,
    key: tuple[str, ...],
    partition_key: str | None,
    start_year: int | None,
    years: int,
) -> str:
    """Render a full ``CREATE TABLE`` statement from validated specs."""
    names = [column.name for column in columns]
    _validate_identifier(table)
    for name in names:
        _validate_identifier(name)
    if not key:
        raise ValueError(f"table {table!r} needs a business primary key (fail closed)")
    unknown = [column for column in key if column not in names]
    if unknown:
        raise ValueError(f"primary key columns {unknown} are not part of {table!r}")
    partition_clause = ""
    if partition_key is not None:
        if partition_key not in names:
            raise ValueError(f"unknown partition key column {partition_key!r} in {table!r}")
        if partition_key not in key:
            raise ValueError(
                f"partition key {partition_key!r} must be part of every unique key "
                f"(primary key {key}) - MySQL requires it"
            )
        bounds = year_partitions(start_year or date.today().year, years)
        parts = [
            f"  PARTITION {name} VALUES LESS THAN ('{bound.isoformat()}')" for name, bound in bounds
        ]
        parts.append("  PARTITION pmax VALUES LESS THAN (MAXVALUE)")
        partition_clause = (
            f"\nPARTITION BY RANGE COLUMNS(`{partition_key}`) (\n" + ",\n".join(parts) + "\n)"
        )
    lines = [f"CREATE TABLE IF NOT EXISTS `{table}` ("]
    lines.extend(f"  {_column_fragment(column)}," for column in columns)
    key_list = ", ".join(f"`{column}`" for column in key)
    lines.append(f"  PRIMARY KEY ({key_list})")
    lines.append(f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4{partition_clause}")
    return "\n".join(lines) + ";"


def _column_fragment(column: Column) -> str:
    """Render one column definition."""
    nullability = "NULL" if column.nullable else "NOT NULL"
    default = f" DEFAULT {column.sql_default}" if column.sql_default is not None else ""
    return f"`{column.name}` {column.sql_type} {nullability}{default}"


def _validate_identifier(name: str) -> None:
    """Fail closed on anything that is not a plain SQL identifier."""
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid SQL identifier {name!r}")


def _is_optional(annotation: object) -> bool:
    """Whether a contract field annotation allows None."""
    base = _strip_annotated(annotation)
    if get_origin(base) not in (Union, types.UnionType):
        return False
    args = get_args(base)
    non_none = tuple(arg for arg in args if arg is not type(None))
    return len(args) == 2 and len(non_none) == 1


def _sql_type(annotation: object, name: str) -> str:
    """Map a contract field annotation to a MySQL type.

    Args:
        annotation: The pydantic field annotation.
        name: Field name (bounded widths for key fields).

    Returns:
        The MySQL type string.

    Raises:
        ValueError: If the type has no mapping (fail closed rather
            than guessing a column type).
    """
    base = _strip_optional(annotation)
    if get_origin(base) is Literal:
        base = _literal_value_type(base)
        if base is None:
            raise ValueError(f"unsupported contract field type {annotation!r} for column {name!r}")
    if base is bool:  # before int: bool is an int subclass
        return "tinyint(1)"
    if base is float:
        return "double"
    if base is int:
        return "bigint"
    if base is datetime:
        return "datetime"
    if base is date:
        return "date"
    if base is str:
        return f"varchar({_KEY_STRING_LENGTHS.get(name, _DEFAULT_STRING_LENGTH)})"
    if _is_json_structured_type(base):
        return "json"
    raise ValueError(f"unsupported contract field type {base!r} for column {name!r}")


def _strip_optional(annotation: object) -> object:
    """Unwrap one optional arm and all surrounding ``Annotated`` wrappers."""
    base = _strip_annotated(annotation)
    if get_origin(base) not in (Union, types.UnionType):
        return base
    args = get_args(base)
    non_none = tuple(arg for arg in args if arg is not type(None))
    if len(args) == 2 and len(non_none) == 1:
        return _strip_annotated(non_none[0])
    return base


def _strip_annotated(annotation: object) -> object:
    """Unwrap nested ``Annotated`` metadata without changing the base type."""
    base = annotation
    while get_origin(base) is Annotated:
        args = get_args(base)
        if not args:
            break
        base = args[0]
    return base


def _literal_value_type(annotation: object) -> type[object] | None:
    """Return the homogeneous native scalar type of a supported Literal."""
    values = get_args(annotation)
    if not values:
        return None
    value_types = {type(value) for value in values}
    if len(value_types) != 1:
        return None
    value_type = next(iter(value_types))
    if value_type not in (str, int, bool):
        return None
    return value_type


def _is_json_structured_type(annotation: object) -> bool:
    """Whether a root annotation is a safely representable JSON structure."""
    base = _strip_annotated(annotation)
    origin = get_origin(base)
    seen_models: frozenset[type[ContractModel]] = frozenset()
    if origin in (dict, list, tuple):
        return _is_json_value_type(base, seen_models)
    return _is_contract_model_type(base) and _is_json_value_type(base, seen_models)


def _is_json_value_type(
    annotation: object,
    seen_models: frozenset[type[ContractModel]],
) -> bool:
    """Recursively validate a type made only of JSON values and contracts."""
    base = _strip_annotated(annotation)
    origin = get_origin(base)

    if origin in (Union, types.UnionType):
        args = get_args(base)
        non_none = tuple(arg for arg in args if arg is not type(None))
        if len(args) == 2 and len(non_none) == 1:
            return _is_json_value_type(non_none[0], seen_models)
        return False
    if base is type(None) or base in (str, int, float, bool):
        return True
    if origin is Literal:
        return _literal_value_type(base) is not None
    if origin is dict:
        args = get_args(base)
        return (
            len(args) == 2
            and _strip_annotated(args[0]) is str
            and _is_json_value_type(args[1], seen_models)
        )
    if origin is list:
        args = get_args(base)
        return len(args) == 1 and _is_json_value_type(args[0], seen_models)
    if origin is tuple:
        args = get_args(base)
        if not args:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return _is_json_value_type(args[0], seen_models)
        return all(_is_json_value_type(arg, seen_models) for arg in args)
    if _is_contract_model_type(base):
        if base in seen_models:
            return True
        nested_seen = seen_models | {base}
        return all(
            _is_json_value_type(field.annotation, nested_seen)
            for field in base.model_fields.values()
        )
    return False


def _is_contract_model_type(annotation: object) -> TypeGuard[type[ContractModel]]:
    """Whether an annotation is a concrete ContractModel subclass."""
    return (
        isinstance(annotation, type)
        and annotation is not ContractModel
        and issubclass(annotation, ContractModel)
    )
