"""Fail-closed unit tests for the warehouse DDL builders (``opendata/pipeline/ddl``).

The module only renders SQL *text*: no engine is created, no connector is
imported, no connection is opened and no statement is executed here. Every
assertion is made either on the returned string or on the exact ``ValueError``
message, so a guard that stopped failing closed would fail these tests.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime
from typing import Annotated, Literal, Union, _GenericAlias

import pytest

import opendata.pipeline.ddl as ddl
from opendata.data.models.base import ContractModel
from opendata.pipeline.ddl import Column, contract_columns, dwd_table_ddl, ods_table_ddl

BAR_COLUMNS = [
    Column("thscode", "varchar(64)", nullable=False),
    Column("trade_date", "date", nullable=False),
    Column("close_price", "double", nullable=True),
]
BAR_KEY = ("thscode", "trade_date")


def _table_ddl(table: str, columns: list[Column], key: tuple[str, ...]) -> str:
    """Render one un-partitioned statement, for the validation paths."""
    return ddl._table_ddl(
        table,
        columns,
        key=key,
        partition_key=None,
        start_year=2024,
        years=1,
    )


def test_year_partitions_are_exclusive_upper_bounds_of_whole_years() -> None:
    """``p{year}`` holds everything before 1 January of the next year."""
    assert ddl.year_partitions(2024, 2) == [
        ("p2024", date(2025, 1, 1)),
        ("p2025", date(2026, 1, 1)),
    ]

    with pytest.raises(ValueError, match="years must be positive, got 0"):
        ddl.year_partitions(2024, 0)
    with pytest.raises(ValueError, match="years must be positive, got -1"):
        ddl.year_partitions(2024, -1)


def test_partition_column_must_exist_and_join_the_primary_key() -> None:
    """MySQL requires the partition key in every unique key (design §8.1)."""
    with pytest.raises(ValueError) as unknown:
        ods_table_ddl(
            "stock_daily",
            "ths",
            BAR_COLUMNS,
            key=BAR_KEY,
            partition_key="amount",
            start_year=2024,
            years=1,
        )
    assert "unknown partition key column 'amount' in 'ods_stock_daily_ths'" in str(unknown.value)

    with pytest.raises(ValueError) as not_key:
        ods_table_ddl(
            "stock_daily",
            "ths",
            BAR_COLUMNS,
            key=("thscode",),
            partition_key="trade_date",
            start_year=2024,
            years=1,
        )
    assert "must be part of every unique key (primary key ('thscode',)) - MySQL requires it" in str(
        not_key.value
    )


@pytest.mark.parametrize(
    ("annotation", "sql_type"),
    [
        (bool, "tinyint(1)"),
        (float, "double"),
        (int, "bigint"),
        (datetime, "datetime"),
        (date, "date"),
        (str, "varchar(255)"),
        (bool | None, "tinyint(1)"),
        (Literal["x"], "varchar(255)"),
    ],
    ids=("bool", "float", "int", "datetime", "date", "str", "optional-bool", "literal-str"),
)
def test_scalar_annotations_map_to_one_mysql_type(annotation, sql_type) -> None:
    """Each scalar annotation maps to one type; ``bool`` is checked before ``int``."""
    assert ddl._sql_type(annotation, "amount") == sql_type


def test_key_fields_get_their_declared_bounded_width() -> None:
    """Key-shaped strings are bounded so the primary key fits MySQL's limit."""
    assert ddl._sql_type(str, "symbol") == "varchar(64)"
    assert ddl._sql_type(str, "index_symbol") == "varchar(64)"
    assert ddl._sql_type(str, "exchange") == "varchar(32)"


def test_multi_arm_union_is_not_unwrapped_into_one_type() -> None:
    """Only a single-arm optional is stripped; a real union stays unsupported."""
    assert ddl._strip_optional(int | str | None) == int | str | None
    assert ddl._strip_optional(str) is str

    with pytest.raises(ValueError, match="unsupported contract field type"):
        ddl._sql_type(int | str | None, "amount")


def test_absent_business_key_fails_closed() -> None:
    """A table without a declared key is refused, never rendered key-less."""
    with pytest.raises(ValueError) as ods:
        ods_table_ddl("stock_daily", "ths", BAR_COLUMNS, key=())
    assert "table 'ods_stock_daily_ths' needs a business primary key (fail closed)" in str(
        ods.value
    )

    with pytest.raises(ValueError) as dwd:
        dwd_table_ddl("stock_daily", key=())
    assert "table 'dwd_stock_daily' needs a business primary key (fail closed)" in str(dwd.value)


def test_key_column_missing_from_the_table_fails_closed() -> None:
    """Every key column has to be a declared column of that very table."""
    with pytest.raises(ValueError) as refused:
        ods_table_ddl("stock_daily", "ths", BAR_COLUMNS, key=("thscode", "trade_date", "symbol"))

    assert "primary key columns ['symbol'] are not part of 'ods_stock_daily_ths'" in str(
        refused.value
    )

    with pytest.raises(ValueError) as both:
        _table_ddl("dwd_stock_daily", BAR_COLUMNS, key=("symbol", "trade_date"))
    assert "primary key columns ['symbol'] are not part of 'dwd_stock_daily'" in str(both.value)


def test_rendered_ods_statement_keeps_trio_and_partition_bounds() -> None:
    """The happy path renders columns, the metadata trio and the partition list."""
    statement = ods_table_ddl(
        "stock_daily",
        "ths",
        BAR_COLUMNS,
        key=BAR_KEY,
        partition_key="trade_date",
        start_year=2024,
        years=2,
    )

    assert statement.startswith("CREATE TABLE IF NOT EXISTS `ods_stock_daily_ths` (\n")
    assert "  `thscode` varchar(64) NOT NULL," in statement
    assert "  `close_price` double NULL," in statement
    assert "  `_source` varchar(32) NOT NULL," in statement
    assert "  `_fetched_at` datetime NOT NULL," in statement
    assert "  `_batch_id` char(36) NOT NULL," in statement
    assert "  PRIMARY KEY (`thscode`, `trade_date`)\n" in statement
    assert statement.endswith(
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4\n"
        "PARTITION BY RANGE COLUMNS(`trade_date`) (\n"
        "  PARTITION p2024 VALUES LESS THAN ('2025-01-01'),\n"
        "  PARTITION p2025 VALUES LESS THAN ('2026-01-01'),\n"
        "  PARTITION pmax VALUES LESS THAN (MAXVALUE)\n"
        ");"
    )


def test_rendered_dwd_statement_keeps_trace_columns_and_default() -> None:
    """Traceability columns and the ``_diff_flag`` default are always emitted."""
    statement = dwd_table_ddl("stock_daily", key=("symbol", "trade_date"))

    assert "CREATE TABLE IF NOT EXISTS `dwd_stock_daily` (" in statement
    assert "  `source` varchar(32) NOT NULL," in statement
    assert "  `_merged_at` datetime NOT NULL," in statement
    assert "  `_diff_flag` tinyint(1) NOT NULL DEFAULT 0," in statement
    assert "  `_as_of` date NOT NULL," in statement
    assert "  PRIMARY KEY (`symbol`, `trade_date`)" in statement
    assert "PARTITION BY" not in statement


@pytest.mark.parametrize(
    ("table", "columns", "expected"),
    [
        ("ods stock daily", BAR_COLUMNS, "invalid SQL identifier 'ods stock daily'"),
        (
            "ods_stock_daily_ths; DROP TABLE users",
            BAR_COLUMNS,
            "invalid SQL identifier 'ods_stock_daily_ths; DROP TABLE users'",
        ),
        ("`quoted`", BAR_COLUMNS, "invalid SQL identifier '`quoted`'"),
        ("1ods", BAR_COLUMNS, "invalid SQL identifier '1ods'"),
        ("ods_stock_daily_ths", [Column("1leading", "int")], "invalid SQL identifier '1leading'"),
        ("ods_stock_daily_ths", [Column(None, "int")], "invalid SQL identifier None"),
        ("ods_stock_daily_ths", [Column(42, "int")], "invalid SQL identifier 42"),
    ],
    ids=("spaces", "separator", "backtick", "leading-digit", "leading-digit-column", "none", "int"),
)
def test_non_identifier_names_are_rejected(table: object, columns: list[Column], expected) -> None:
    """An identifier can never carry a quote, a separator or a non-string type."""
    with pytest.raises(ValueError) as refused:
        _table_ddl(table, columns, key=("trade_date",))

    assert str(refused.value) == expected


def test_validate_identifier_accepts_unicode_and_rejects_empty() -> None:
    """Chinese source columns stay legal (design §8.1); an empty name does not."""
    ddl._validate_identifier("收盘价")
    ddl._validate_identifier("_source")

    with pytest.raises(ValueError, match=r"invalid SQL identifier ''"):
        ddl._validate_identifier("")


def _degenerate_annotated():
    """Build an ``Annotated`` wrapper that carries no arguments at all.

    ``Annotated[...]`` needs at least two arguments, so the public spelling
    cannot produce this shape; the guard in :func:`ddl._strip_annotated`
    exists for exactly that degenerate wrapper, so it is built through the
    same internal alias type ``typing.get_origin``/``get_args`` read.
    """
    alias = _GenericAlias(list, (int,))
    alias.__origin__ = Annotated
    alias.__args__ = ()
    return alias


def test_degenerate_annotated_wrapper_stops_the_unwrap_loop() -> None:
    """An argument-less ``Annotated`` is returned unchanged, never IndexError."""
    broken = _degenerate_annotated()

    assert ddl._strip_annotated(broken) is broken
    with pytest.raises(ValueError) as refused:
        ddl._sql_type(broken, "amount")
    assert "unsupported contract field type" in str(refused.value)


def test_nested_annotated_wrappers_unwrap_to_the_base_type() -> None:
    """``Annotated`` layers are peeled off without changing the wrapped type."""
    nested = Annotated[Annotated[str | None, "unit"], "column"]
    single = Annotated[str, "column"]

    assert ddl._strip_annotated(single) is str
    assert ddl._strip_annotated(nested) == str | None
    assert ddl._sql_type(nested, "symbol") == "varchar(64)"
    assert ddl._sql_type(single, "exchange") == "varchar(32)"
    assert ddl._is_optional(nested) is True


def test_empty_literal_has_no_native_value_type() -> None:
    """``Literal[()]`` carries no values, so no scalar type can be inferred."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        empty = Literal[()]

    assert ddl._literal_value_type(empty) is None
    with pytest.raises(ValueError) as refused:
        ddl._sql_type(empty, "period")
    assert "unsupported contract field type" in str(refused.value)


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (Literal["daily", "monthly"], str),
        (Literal[1, 2], int),
        (Literal[True, False], bool),
        (Literal[b"bytes"], None),
        (Literal[None], None),
        (Literal["mixed", 1], None),
    ],
    ids=("str", "int", "bool", "bytes", "none", "mixed"),
)
def test_literal_value_type_only_accepts_scalar_spellings(annotation, expected) -> None:
    """bytes/None/mixed Literals have no single bounded MySQL column type."""
    assert ddl._literal_value_type(annotation) is expected


@pytest.mark.parametrize(
    ("annotation", "json_value"),
    [
        (Union[int, str], False),  # noqa: UP007 -- the legacy typing.Union arm
        (int | str | None, False),
        (int | None, True),
        (str, True),
        (type(None), True),
        (Literal["a", "b"], True),
        (Literal[b"a"], False),
        (list[int], True),
        (list[int | str], False),
        (tuple[()], False),
        (tuple[str, int], True),
        (tuple[str, ...], True),
        (tuple[str, set[int]], False),
        (dict[str, int], True),
        (dict[str, set[str]], False),
        (dict[int, str], False),
        (set[str], False),
        (bytes, False),
        (complex, False),
    ],
    ids=(
        "union-two-arms",
        "union-three-arms",
        "optional",
        "str",
        "none",
        "literal-str",
        "literal-bytes",
        "list-int",
        "list-union",
        "tuple-empty",
        "tuple-fixed",
        "tuple-variadic",
        "tuple-fixed-bad-arm",
        "dict-str-key",
        "dict-set-value",
        "dict-int-key",
        "set",
        "bytes",
        "complex",
    ),
)
def test_is_json_value_type_classifies_every_origin(annotation, json_value: bool) -> None:
    """Only JSON scalars, single-arm optionals and typed containers are JSON."""
    assert ddl._is_json_value_type(annotation, frozenset()) is json_value


def test_containers_become_one_json_column_or_fail_closed() -> None:
    """A supported container renders ``json``; a refused one names the column."""
    assert ddl._sql_type(dict[str, Literal["a", "b"]], "metadata") == "json"
    assert ddl._sql_type(list[tuple[str, ...]], "metadata") == "json"

    with pytest.raises(ValueError) as bad_value:
        ddl._sql_type(dict[str, set[str]], "meta")
    assert "unsupported contract field type dict[str, set[str]] for column 'meta'" in str(
        bad_value.value
    )

    with pytest.raises(ValueError) as bad_list:
        ddl._sql_type(list[int | str], "meta")
    assert "for column 'meta'" in str(bad_list.value)

    with pytest.raises(ValueError) as empty_tuple:
        ddl._sql_type(list[tuple[()]], "meta")
    assert "for column 'meta'" in str(empty_tuple.value)


class _Tree(ContractModel):
    """A self-referencing contract; the recursion guard must terminate."""

    name: str
    children: list[_Tree] = []


class _Leaf(ContractModel):
    """A contract holding a value no JSON column can carry."""

    payload: set[str]


def test_self_referencing_contract_model_terminates_on_the_seen_set() -> None:
    """Re-entering a model already on the stack is accepted, not recursed."""
    assert ddl._is_json_value_type(_Tree, frozenset({_Tree})) is True
    assert ddl._is_json_value_type(list[_Tree], frozenset()) is True
    assert ddl._sql_type(list[_Tree], "catalog") == "json"


def test_contract_model_with_unsupported_field_is_not_json() -> None:
    """A nested contract is JSON only when every one of its fields is."""
    assert ddl._is_json_value_type(_Leaf, frozenset()) is False
    assert ddl._is_json_structured_type(list[_Leaf]) is False
    assert ddl._is_json_structured_type(_Leaf) is False


def test_contract_columns_are_the_model_fields_in_declaration_order() -> None:
    """The generated dwd value columns mirror the contract model exactly."""
    columns = {column.name: column for column in contract_columns("stock_daily")}
    model = ddl.contract_model("stock_daily")

    assert list(columns) == list(model.model_fields)
    assert columns["symbol"] == Column("symbol", "varchar(64)", nullable=False)
    assert columns["trade_date"].sql_type == "date"
    assert columns["trade_date"].nullable is False
