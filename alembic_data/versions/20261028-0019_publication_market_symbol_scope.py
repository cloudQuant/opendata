"""Add an exact market/symbol selector to immutable published history.

Revision ID: 0019_publication_market_symbol_scope
Revises: 0018_publication_outbox
Create Date: 2026-10-28

The new selector is nullable on both publication facts and fixed read
snapshots. Existing history remains NULL and is never interpreted or backfilled
from record_json. The new selector pair is mutually exclusive with the older
instrument/capture selector pair so one snapshot cannot mix identity profiles.

SQLite uses batch table rebuilds to add columns and checks. MySQL 8.0.16+ uses
native DDL, which is not one transaction across the fact and snapshot tables.
If MySQL fails after applying part of this revision, its marker remains at
0018; inspect both table shapes offline before any retry or repair. This
revision never claims an automatic rollback for partial MySQL DDL. No live
MySQL server acceptance is performed or claimed by this migration or its tests.

Automatic downgrade is deliberately disabled. A selector data scan cannot be
fenced against every writer, and MySQL DDL implicitly commits; a value could be
inserted after a scan and then be lost by the schema change. Any rollback needs
a separately reviewed, explicitly quiesced offline procedure.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    Integer,
    String,
    Text,
    column,
    inspect,
    select,
    table,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.engine.reflection import Inspector

revision = "0019_publication_market_symbol_scope"
down_revision = "0018_publication_outbox"
branch_labels = None
depends_on = None

_FACT_TABLE = "od_published_fact_versions"
_SNAPSHOT_TABLE = "od_publication_snapshots"
_VERSION_TABLE_FALLBACK = "alembic_version_data"
_MARKET_INDEX = "ix_od_fact_market_symbol_read"
_SQLITE_SNAPSHOT_COLLATION_PROBE = "ix_od_snapshot_market_symbol_probe_0019"
_EXACT_MYSQL_COLLATION = "utf8mb4_0900_bin"
_MYSQL_MIN_VERSION = (8, 0, 16)
_MYSQL_MAX_INDEX_BYTES = 3_072
_CHECK_TOKEN_PATTERN = re.compile(r"\s*(>=|<=|<>|!=|[()><=]|[a-z_][a-z0-9_$]*|[0-9]+)")
_CHECK_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_$]*")
_CHECK_IDENTIFIER_NAMES = {
    "instrument_id",
    "capture_id",
    "market",
    "symbol",
    "revision",
    "page_size",
    "record_count",
}

_CAPTURE_PAIR_SQL = (
    "(instrument_id IS NULL AND capture_id IS NULL) OR "
    "(instrument_id IS NOT NULL AND capture_id IS NOT NULL)"
)
_MARKET_SYMBOL_PAIR_SQL = (
    "(market IS NULL AND symbol IS NULL) OR (market IS NOT NULL AND symbol IS NOT NULL)"
)
_SELECTOR_PROFILE_SQL = (
    "(instrument_id IS NULL AND capture_id IS NULL) OR (market IS NULL AND symbol IS NULL)"
)

# The frozen table shapes are owned by revisions 0011/0012. Revisions 0013–0018
# add unrelated journal, gate, index, and outbox objects.
_FACT_COLUMNS: dict[str, tuple[type[Any], int | None, bool]] = {
    "dataset_id": (String, 128, False),
    "business_key": (String, 512, False),
    "revision": (Integer, None, False),
    "publication_id": (String, 32, False),
    "publication_sequence": (BigInteger, None, False),
    "business_date": (Date, None, True),
    "event_at": (String, 40, True),
    "observed_at": (String, 40, True),
    "source_published_at": (String, 40, True),
    "available_at": (String, 40, True),
    "record_json": (Text, None, False),
    "record_hash": (String, 64, False),
    "is_deleted": (Boolean, None, False),
    "instrument_id": (String, 128, True),
    "capture_id": (String, 64, True),
}
_SNAPSHOT_COLUMNS: dict[str, tuple[type[Any], int | None, bool]] = {
    "snapshot_id": (String, 32, False),
    "dataset_id": (String, 128, False),
    "schema_version": (String, 64, False),
    "publication_id": (String, 32, False),
    "publication_sequence": (BigInteger, None, False),
    "start_date": (Date, None, True),
    "end_date": (Date, None, True),
    "page_size": (Integer, None, False),
    "access_scope": (String, 128, False),
    "selection_sha256": (String, 64, False),
    "content_hash": (String, 64, False),
    "record_count": (BigInteger, None, False),
    "as_of_us": (BigInteger, None, False),
    "created_at_us": (BigInteger, None, False),
    "expires_at_us": (BigInteger, None, False),
    "instrument_id": (String, 128, True),
    "capture_id": (String, 64, True),
}
_NEW_COLUMNS: dict[str, tuple[type[Any], int, bool]] = {
    "market": (String, 32, True),
    "symbol": (String, 128, True),
}

_FACT_CHECKS = {
    "ck_od_published_fact_revision": "revision > 0",
    "ck_od_fact_capture_pair": _CAPTURE_PAIR_SQL,
}
_SNAPSHOT_CHECKS = {
    "ck_od_snapshot_page_size": "page_size > 0",
    "ck_od_snapshot_record_count": "record_count >= 0",
    "ck_od_snapshot_capture_pair": _CAPTURE_PAIR_SQL,
}
_NEW_CHECKS = {
    _FACT_TABLE: {
        "ck_od_fact_market_symbol_pair": _MARKET_SYMBOL_PAIR_SQL,
        "ck_od_fact_selector_profile": _SELECTOR_PROFILE_SQL,
    },
    _SNAPSHOT_TABLE: {
        "ck_od_snapshot_market_symbol_pair": _MARKET_SYMBOL_PAIR_SQL,
        "ck_od_snapshot_selector_profile": _SELECTOR_PROFILE_SQL,
    },
}
_FACT_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_od_published_fact_asof": (
        "dataset_id",
        "business_key",
        "publication_sequence",
        "revision",
    ),
    "ix_od_published_fact_date": ("dataset_id", "business_date", "business_key"),
    "ix_od_fact_capture_read": (
        "dataset_id",
        "instrument_id",
        "capture_id",
        "publication_sequence",
    ),
    "ix_od_fact_capture_discovery": (
        "dataset_id",
        "publication_id",
        "instrument_id",
        "publication_sequence",
        "is_deleted",
        "observed_at",
        "capture_id",
    ),
}
_SNAPSHOT_INDEXES: dict[str, tuple[str, ...]] = {"ix_od_snapshot_expiry": ("expires_at_us",)}
_NEW_FACT_INDEXES: dict[str, tuple[str, ...]] = {
    _MARKET_INDEX: ("dataset_id", "market", "symbol", "publication_sequence")
}
_MYSQL_PUBLICATION_FK_INDEXES: dict[str, tuple[str, ...]] = {
    # MySQL creates this non-unique support index for the 0011 FK when absent.
    "publication_id": ("publication_id",),
}


def _selector_type(length: int) -> String:
    """Freeze binary selector equality on SQLite and NO PAD equality on MySQL."""
    return String(length, collation="BINARY").with_variant(
        VARCHAR(length, collation=_EXACT_MYSQL_COLLATION),
        "mysql",
    )


def _target_mode() -> str:
    """Require an online SQLite or supported MySQL 8 target."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("revision 0019 requires a live connection for schema checks")
    dialect = op.get_bind().dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0019 supports SQLite and MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0019 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < _MYSQL_MIN_VERSION
    ):
        raise RuntimeError("revision 0019 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _version_table_name(context: object) -> str:
    """Return the configured Alembic data-version table."""
    value = getattr(context, "version_table", None) or _VERSION_TABLE_FALLBACK
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("revision 0019 requires a valid Alembic version table name")
    return value


def _require_revision_marker(expected: str, mode: str) -> None:
    """Require one exact migration marker before inspecting or changing tables."""
    context = op.get_context()
    version_table = _version_table_name(context)
    schema = getattr(context, "version_table_schema", None)
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(version_table, schema=schema):
        raise RuntimeError("revision 0019 requires the configured Alembic version table")
    columns = inspector.get_columns(version_table, schema=schema)
    if len(columns) != 1 or columns[0].get("name") != "version_num":
        raise RuntimeError("revision 0019 requires a version table containing only version_num")
    item = columns[0]
    version_type = item.get("type")
    if (
        not isinstance(version_type, String)
        or getattr(version_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_type.length, int)
        or item.get("nullable") is not False
        or item.get("default") is not None
    ):
        raise RuntimeError("revision 0019 requires a bounded, non-null VARCHAR version_num")
    if mode == "mysql" and version_type.length < max(len(expected), len(revision)):
        raise RuntimeError("revision 0019 requires version_num wide enough for its revision IDs")
    if inspector.get_pk_constraint(version_table, schema=schema).get("constrained_columns") != [
        "version_num"
    ]:
        raise RuntimeError("revision 0019 requires version_num as the sole version-table key")
    marker_table = table(
        version_table,
        column("version_num", version_type),
        schema=schema,
    )
    markers = bind.execute(select(marker_table.c.version_num)).scalars().all()
    if markers != [expected]:
        raise RuntimeError(
            f"revision 0019 requires exactly one Alembic marker matching {expected}; "
            "unexpected or partial migration state requires offline verification"
        )


def _matches_column_type(actual: object, expected: type[Any], name: str, mode: str) -> bool:
    """Match reflected types, including the reviewed MySQL dialect variants."""
    if name == "is_deleted" and mode == "mysql":
        return isinstance(actual, Boolean) or (
            getattr(actual, "__visit_name__", "").upper() == "TINYINT"
        )
    if name == "record_json" and mode == "mysql":
        return isinstance(actual, MEDIUMTEXT)
    return isinstance(actual, expected)


def _require_columns(
    inspector: Inspector,
    table_name: str,
    expected: Mapping[str, tuple[type[Any], int | None, bool]],
    mode: str,
) -> None:
    """Require an exact set of reviewed columns, types, widths, and nullability."""
    columns = inspector.get_columns(table_name)
    actual_names = {str(item.get("name")) for item in columns}
    if actual_names != set(expected):
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} column set")
    for item in columns:
        name = str(item.get("name"))
        expected_type, expected_length, nullable = expected[name]
        actual_type = item.get("type")
        if (
            not _matches_column_type(actual_type, expected_type, name, mode)
            or (
                expected_length is not None
                and getattr(actual_type, "length", None) != expected_length
            )
            or item.get("nullable") is not nullable
            or item.get("default") is not None
            or item.get("computed")
            or item.get("identity")
        ):
            raise RuntimeError(f"revision 0019 requires the reviewed {table_name}.{name} shape")


class _CheckExpressionParser:
    """Parse the small boolean CHECK grammar used by publication revisions."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._position = 0

    def parse(self) -> tuple[object, ...]:
        """Parse one complete expression or reject unsupported syntax."""
        if not self._tokens:
            raise ValueError("empty CHECK expression")
        expression = self._parse_or()
        if self._position != len(self._tokens):
            raise ValueError("unsupported CHECK expression syntax")
        return expression

    def _parse_or(self) -> tuple[object, ...]:
        expression = self._parse_and()
        while self._accept("or"):
            expression = ("or", expression, self._parse_and())
        return expression

    def _parse_and(self) -> tuple[object, ...]:
        expression = self._parse_primary()
        while self._accept("and"):
            expression = ("and", expression, self._parse_primary())
        return expression

    def _parse_primary(self) -> tuple[object, ...]:
        if self._accept("("):
            expression = self._parse_or()
            self._require(")")
            return expression
        return self._parse_predicate()

    def _parse_predicate(self) -> tuple[object, ...]:
        identifier = self._take()
        if _CHECK_IDENTIFIER_PATTERN.fullmatch(identifier) is None or identifier in {
            "and",
            "or",
            "is",
            "not",
            "null",
        }:
            raise ValueError("unsupported CHECK predicate")

        if self._accept("is"):
            is_not = self._accept("not")
            self._require("null")
            return ("is_null", identifier, is_not)

        operator = self._peek()
        if operator in {">", ">="}:
            self._position += 1
            literal = self._take()
            if not literal.isdecimal():
                raise ValueError("CHECK comparison requires an integer literal")
            return ("compare", identifier, operator, int(literal))
        raise ValueError("unsupported CHECK predicate")

    def _peek(self) -> str | None:
        if self._position >= len(self._tokens):
            return None
        return self._tokens[self._position]

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("incomplete CHECK expression")
        self._position += 1
        return token

    def _accept(self, expected: str) -> bool:
        if self._peek() != expected:
            return False
        self._position += 1
        return True

    def _require(self, expected: str) -> None:
        if not self._accept(expected):
            raise ValueError("unsupported CHECK expression syntax")


def _canonical_check(sql: object, mode: str) -> tuple[object, ...]:
    """Parse a supported CHECK expression without changing dialect quote semantics."""
    normalized_mode = mode.casefold()
    if normalized_mode not in {"mysql", "sqlite"}:
        raise ValueError("unsupported CHECK expression dialect")

    value = str(sql).casefold().strip()
    tokens: list[str] = []
    position = 0
    while position < len(value):
        character = value[position]
        if character.isspace():
            position += 1
            continue
        if character in {"`", '"'}:
            if character == '"' and normalized_mode != "sqlite":
                raise ValueError("double quotes are not accepted in MySQL CHECK expressions")
            closing_quote = value.find(character, position + 1)
            if closing_quote < 0:
                raise ValueError("unterminated quoted CHECK identifier")
            identifier = value[position + 1 : closing_quote]
            if (
                _CHECK_IDENTIFIER_PATTERN.fullmatch(identifier) is None
                or identifier not in _CHECK_IDENTIFIER_NAMES
            ):
                raise ValueError("quoted CHECK token is not a reviewed identifier")
            tokens.append(identifier)
            position = closing_quote + 1
            continue
        match = _CHECK_TOKEN_PATTERN.match(value, position)
        if match is None:
            raise ValueError("unsupported CHECK expression syntax")
        tokens.append(match.group(1))
        position = match.end()
    return _CheckExpressionParser(tokens).parse()


def _require_checks(
    inspector: Inspector,
    table_name: str,
    expected: Mapping[str, str],
    mode: str,
) -> None:
    """Require exact named checks and, on MySQL, require each check is enforced."""
    checks = inspector.get_check_constraints(table_name)
    names: list[str] = []
    for item in checks:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"revision 0019 found an unexpected {table_name} CHECK set")
        names.append(name)
    if len(names) != len(expected) or len(set(names)) != len(names):
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} CHECK set")
    actual = {names[index]: item.get("sqltext") for index, item in enumerate(checks)}
    if set(actual) != set(expected):
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} CHECK set")
    for name, sql in expected.items():
        try:
            actual_expression = _canonical_check(actual[name], mode)
            expected_expression = _canonical_check(sql, mode)
        except ValueError as error:
            raise RuntimeError(
                f"revision 0019 found an unexpected definition for {name}"
            ) from error
        if actual_expression != expected_expression:
            raise RuntimeError(f"revision 0019 found an unexpected definition for {name}")
    if mode == "mysql":
        rows = (
            op.get_bind()
            .execute(
                text(
                    "SELECT CONSTRAINT_NAME, ENFORCED FROM information_schema.TABLE_CONSTRAINTS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                    "AND CONSTRAINT_TYPE = 'CHECK'"
                ),
                {"table": table_name},
            )
            .mappings()
            .all()
        )
        enforced = {str(row.get("CONSTRAINT_NAME")): row.get("ENFORCED") for row in rows}
        if set(enforced) != set(expected) or any(
            str(value).upper() != "YES" for value in enforced.values()
        ):
            raise RuntimeError(f"revision 0019 requires enforced checks on {table_name}")


def _index_columns(item: Mapping[str, object]) -> tuple[str, ...] | None:
    """Read an ordinary, full-column, non-unique secondary index signature."""
    columns = item.get("column_names")
    options = item.get("dialect_options", {})
    unique = item.get("unique")
    if (
        not (unique is False or (type(unique) is int and unique == 0))
        or not isinstance(columns, (list, tuple))
        or any(not isinstance(name, str) or not name for name in columns)
        or not isinstance(options, dict)
        or not set(options).issubset({"sqlite_where", "mysql_length"})
        or options.get("sqlite_where") is not None
        or options.get("mysql_length") not in (None, {})
    ):
        return None
    return tuple(columns)


def _sqlite_index_sql(name: str) -> str | None:
    """Return one SQLite index definition without consulting application metadata."""
    row = (
        op.get_bind()
        .execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": name},
        )
        .first()
    )
    return str(row[0]) if row is not None and row[0] is not None else None


def _sqlite_index_parts(name: str) -> tuple[RowMapping, ...]:
    """Read actual SQLite index key columns, collations, and sort directions."""
    return tuple(
        op.get_bind()
        .execute(
            text(
                'SELECT seqno, name, "desc" AS descending, coll, "key" AS is_key '
                "FROM pragma_index_xinfo(:index_name) ORDER BY seqno"
            ),
            {"index_name": name},
        )
        .mappings()
        .all()
    )


def _require_sqlite_index(
    inspector: Inspector,
    table_name: str,
    name: str,
    expected_columns: tuple[str, ...],
) -> None:
    """Reject expression, prefix, partial, reordered, or non-BINARY SQLite indexes."""
    indexes = [item for item in inspector.get_indexes(table_name) if item.get("name") == name]
    if len(indexes) != 1 or _index_columns(indexes[0]) != expected_columns:
        raise RuntimeError(f"revision 0019 requires the reviewed SQLite {name} index shape")
    definition = _sqlite_index_sql(name)
    if (
        not definition
        or " WHERE " in definition.upper()
        or "CREATE INDEX" not in definition.upper()
    ):
        raise RuntimeError(f"revision 0019 requires the reviewed SQLite {name} index shape")
    parts = tuple(row for row in _sqlite_index_parts(name) if row.get("is_key") == 1)
    if (
        len(parts) != len(expected_columns)
        or tuple(row.get("name") for row in parts) != expected_columns
        or any(row.get("descending") not in (0, False) for row in parts)
        or any(str(row.get("coll", "")).casefold() != "binary" for row in parts)
    ):
        raise RuntimeError(f"revision 0019 requires BINARY comparison for SQLite {name}")


def _mysql_index_rows(table_name: str, index_name: str) -> tuple[RowMapping, ...]:
    """Read MySQL's effective index definition, including visibility and prefixes."""
    return tuple(
        op.get_bind()
        .execute(
            text(
                "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, "
                "INDEX_TYPE, IS_VISIBLE FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                "AND INDEX_NAME = :index_name ORDER BY SEQ_IN_INDEX"
            ),
            {"table": table_name, "index_name": index_name},
        )
        .mappings()
        .all()
    )


def _require_mysql_index(
    table_name: str,
    index_name: str,
    expected_columns: tuple[str, ...],
) -> None:
    """Require a visible, full-column, non-unique InnoDB BTREE index."""
    rows = _mysql_index_rows(table_name, index_name)
    if len(rows) != len(expected_columns):
        raise RuntimeError(f"revision 0019 requires the reviewed MySQL {index_name} index")
    for position, row in enumerate(rows, start=1):
        if (
            row.get("INDEX_NAME") != index_name
            or row.get("NON_UNIQUE") not in (1, True)
            or row.get("SEQ_IN_INDEX") != position
            or row.get("COLUMN_NAME") != expected_columns[position - 1]
            or row.get("SUB_PART") is not None
            or str(row.get("INDEX_TYPE", "")).upper() != "BTREE"
            or str(row.get("IS_VISIBLE", "")).upper() != "YES"
        ):
            raise RuntimeError(f"revision 0019 requires the reviewed MySQL {index_name} index")


def _require_mysql_selector_columns(
    table_name: str,
    *,
    include_market_symbol: bool,
) -> int:
    """Validate NO PAD key columns and return the new index's full byte width."""
    names = {"dataset_id": 128, "instrument_id": 128, "capture_id": 64}
    if include_market_symbol:
        names.update({"market": 32, "symbol": 128})
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, "
                "CHARACTER_OCTET_LENGTH, CHARACTER_SET_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table"
            ),
            {"table": table_name},
        )
        .mappings()
        .all()
    )
    columns = {str(row.get("COLUMN_NAME")): row for row in rows}
    if not set(names).issubset(columns):
        raise RuntimeError(f"revision 0019 cannot inspect every key column on {table_name}")

    expected_nullable = {"dataset_id": "NO", "instrument_id": "YES", "capture_id": "YES"}
    if include_market_symbol:
        expected_nullable.update({"market": "YES", "symbol": "YES"})
    index_bytes = 8  # signed BIGINT publication_sequence
    for name, length in names.items():
        item = columns[name]
        if (
            str(item.get("DATA_TYPE", "")).casefold() != "varchar"
            or item.get("IS_NULLABLE") != expected_nullable[name]
            or item.get("CHARACTER_MAXIMUM_LENGTH") != length
            or item.get("CHARACTER_OCTET_LENGTH") != length * 4
            or str(item.get("CHARACTER_SET_NAME", "")).casefold() != "utf8mb4"
            or str(item.get("COLLATION_NAME", "")).casefold() != _EXACT_MYSQL_COLLATION
        ):
            raise RuntimeError(
                f"revision 0019 requires {table_name}.{name} to use its reviewed "
                "utf8mb4 NO PAD key shape"
            )
        if name in {"dataset_id", "market", "symbol"}:
            index_bytes += length * 4

    sequence = columns.get("publication_sequence")
    if (
        sequence is None
        or str(sequence.get("DATA_TYPE", "")).casefold() != "bigint"
        or str(sequence.get("IS_NULLABLE", "")) != "NO"
    ):
        raise RuntimeError("revision 0019 requires non-null BIGINT publication_sequence")

    collation_rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT COLLATION_NAME, PAD_ATTRIBUTE FROM information_schema.COLLATIONS "
                "WHERE CHARACTER_SET_NAME = 'utf8mb4' AND COLLATION_NAME = :collation"
            ),
            {"collation": _EXACT_MYSQL_COLLATION},
        )
        .mappings()
        .all()
    )
    if (
        len(collation_rows) != 1
        or str(collation_rows[0].get("PAD_ATTRIBUTE", "")).upper() != "NO PAD"
    ):
        raise RuntimeError("revision 0019 requires MySQL utf8mb4_0900_bin with NO PAD semantics")
    return index_bytes


def _require_mysql_fact_index_budget() -> None:
    """Require the market/symbol lookup key to fit this InnoDB table's page size."""
    row = (
        op.get_bind()
        .execute(
            text(
                "SELECT ENGINE, ROW_FORMAT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
            ),
            {"table": _FACT_TABLE},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or str(row.get("ENGINE", "")).casefold() != "innodb":
        raise RuntimeError("revision 0019 requires the publication fact table to use InnoDB")
    if str(row.get("ROW_FORMAT", "")).casefold() not in {"dynamic", "compressed"}:
        raise RuntimeError("revision 0019 requires a DYNAMIC or COMPRESSED fact table")
    page_size = op.get_bind().exec_driver_sql("SELECT @@GLOBAL.innodb_page_size").scalar_one()
    if type(page_size) is not int or page_size not in {4_096, 8_192, 16_384, 32_768, 65_536}:
        raise RuntimeError("revision 0019 cannot validate the InnoDB page-size index limit")
    maximum = min(_MYSQL_MAX_INDEX_BYTES, page_size // 2)
    key_bytes = _require_mysql_selector_columns(
        _FACT_TABLE,
        include_market_symbol=True,
    )
    if key_bytes > maximum:
        raise RuntimeError(
            "revision 0019 market/symbol index exceeds this InnoDB page-size key limit"
        )


def _require_table_foreign_keys(inspector: Inspector, table_name: str) -> None:
    """Require the publication reference declared by revision 0011 and no extras."""
    keys = inspector.get_foreign_keys(table_name)
    signature = {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
        )
        for item in keys
    }
    expected = {(("publication_id",), "od_publications", ("publication_id",))}
    if signature != expected or len(keys) != 1:
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} foreign-key set")


def _require_indexes(
    inspector: Inspector,
    table_name: str,
    expected: Mapping[str, tuple[str, ...]],
    mode: str,
) -> None:
    """Require exact secondary-index names and ordered key columns."""
    if mode == "mysql" and table_name in {_FACT_TABLE, _SNAPSHOT_TABLE}:
        # Unlike SQLite, MySQL creates a support index for the 0011 publication FK.
        # The expected signature is still checked below, then information_schema
        # verifies it is a full-column, non-unique, visible BTREE index.
        expected = {**expected, **_MYSQL_PUBLICATION_FK_INDEXES}
    indexes = inspector.get_indexes(table_name)
    actual = {str(item.get("name")): item for item in indexes}
    if set(actual) != set(expected):
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} index set")
    for name, columns in expected.items():
        if _index_columns(actual[name]) != columns:
            raise RuntimeError(f"revision 0019 found an unexpected definition for {name}")
        if mode == "sqlite":
            _require_sqlite_index(inspector, table_name, name, columns)
        else:
            _require_mysql_index(table_name, name, columns)


def _require_table_shape(
    table_name: str,
    mode: str,
    *,
    include_market_symbol: bool,
) -> None:
    """Fail closed unless one affected table matches its frozen migration shape."""
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        raise RuntimeError(f"revision 0019 requires {table_name} to exist")
    if table_name == _FACT_TABLE:
        expected_columns = dict(_FACT_COLUMNS)
        expected_checks = dict(_FACT_CHECKS)
        expected_indexes = dict(_FACT_INDEXES)
        primary_key = ["dataset_id", "business_key", "revision"]
        if include_market_symbol:
            expected_indexes.update(_NEW_FACT_INDEXES)
    else:
        expected_columns = dict(_SNAPSHOT_COLUMNS)
        expected_checks = dict(_SNAPSHOT_CHECKS)
        expected_indexes = dict(_SNAPSHOT_INDEXES)
        primary_key = ["snapshot_id"]
    if include_market_symbol:
        expected_columns.update(_NEW_COLUMNS)
        expected_checks.update(_NEW_CHECKS[table_name])

    _require_columns(inspector, table_name, expected_columns, mode)
    if inspector.get_pk_constraint(table_name).get("constrained_columns") != primary_key:
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} primary key")
    _require_table_foreign_keys(inspector, table_name)
    if inspector.get_unique_constraints(table_name):
        raise RuntimeError(f"revision 0019 found an unexpected {table_name} unique constraint")
    _require_checks(inspector, table_name, expected_checks, mode)
    _require_indexes(inspector, table_name, expected_indexes, mode)

    if mode == "mysql":
        if table_name == _FACT_TABLE:
            if include_market_symbol:
                _require_mysql_fact_index_budget()
            _require_mysql_selector_columns(
                _FACT_TABLE,
                include_market_symbol=include_market_symbol,
            )
        else:
            _require_mysql_selector_columns(
                _SNAPSHOT_TABLE,
                include_market_symbol=include_market_symbol,
            )
        options = inspector.get_table_options(table_name)
        engine = options.get("mysql_engine") if isinstance(options, dict) else None
        if not isinstance(engine, str) or engine.casefold() != "innodb":
            raise RuntimeError(f"revision 0019 requires {table_name} to use InnoDB")


def _require_sqlite_rebuild_safety() -> None:
    """Reject SQLite objects batch table recreation could silently remove."""
    bind = op.get_bind()
    table_names = (_FACT_TABLE, _SNAPSHOT_TABLE)
    triggers = (
        bind.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN (:fact, :snapshot)"
            ),
            {"fact": _FACT_TABLE, "snapshot": _SNAPSHOT_TABLE},
        )
        .scalars()
        .all()
    )
    if triggers:
        raise RuntimeError("revision 0019 refuses SQLite tables with user-defined triggers")

    views = bind.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql IS NOT NULL")
    ).all()
    for name, sql in views:
        references_rebuilt_table = any(
            re.search(rf"\b{re.escape(table_name)}\b", str(sql), re.IGNORECASE)
            for table_name in table_names
        )
        if references_rebuilt_table:
            raise RuntimeError(
                f"revision 0019 refuses SQLite view {name} that depends on a rebuilt table"
            )

    inspector = inspect(bind)
    for other_table in inspector.get_table_names():
        if other_table in table_names:
            continue
        for foreign_key in inspector.get_foreign_keys(other_table):
            if foreign_key.get("referred_table") in table_names:
                raise RuntimeError(
                    f"revision 0019 refuses SQLite foreign-key dependents on {other_table}"
                )


def _require_sqlite_snapshot_selector_collation() -> None:
    """Probe snapshot selector columns to ensure both inherit SQLite BINARY semantics."""
    bind = op.get_bind()
    existing = bind.execute(
        text("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = :name"),
        {"name": _SQLITE_SNAPSHOT_COLLATION_PROBE},
    ).first()
    if existing is not None:
        raise RuntimeError("revision 0019 found a stale SQLite snapshot collation probe")

    created = False
    validation_error: BaseException | None = None
    try:
        op.create_index(
            _SQLITE_SNAPSHOT_COLLATION_PROBE,
            _SNAPSHOT_TABLE,
            ["market", "symbol"],
            unique=False,
            sqlite_where=text("0"),
        )
        created = True
        key_parts = tuple(
            row
            for row in _sqlite_index_parts(_SQLITE_SNAPSHOT_COLLATION_PROBE)
            if row.get("is_key") == 1
        )
        if (
            len(key_parts) != 2
            or tuple(row.get("name") for row in key_parts) != ("market", "symbol")
            or any(row.get("descending") not in (0, False) for row in key_parts)
            or any(str(row.get("coll", "")).casefold() != "binary" for row in key_parts)
        ):
            raise RuntimeError(
                "revision 0019 requires BINARY comparison for SQLite snapshot selectors"
            )
    except BaseException as error:
        validation_error = error
        raise
    finally:
        if created:
            try:
                op.drop_index(_SQLITE_SNAPSHOT_COLLATION_PROBE, table_name=_SNAPSHOT_TABLE)
            except Exception as cleanup_error:
                if validation_error is not None:
                    raise RuntimeError(
                        "revision 0019 SQLite snapshot collation validation failed and its "
                        "temporary probe could not be removed"
                    ) from cleanup_error
                raise RuntimeError(
                    "revision 0019 could not remove its SQLite snapshot collation probe"
                ) from cleanup_error


def _require_market_index_name_free() -> None:
    """Reject a stale or colliding market selector index before DDL."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        row = bind.execute(
            text("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": _MARKET_INDEX},
        ).first()
        if row is not None:
            raise RuntimeError("revision 0019 found a pre-existing market/symbol index")
        return
    if _mysql_index_rows(_FACT_TABLE, _MARKET_INDEX):
        raise RuntimeError("revision 0019 found a pre-existing market/symbol index")


def _apply_upgrade_ddl() -> None:
    """Add two nullable selector fields without reading or rewriting payloads."""
    with op.batch_alter_table(_FACT_TABLE) as batch:
        batch.add_column(Column("market", _selector_type(32), nullable=True))
        batch.add_column(Column("symbol", _selector_type(128), nullable=True))
        batch.create_check_constraint(
            "ck_od_fact_market_symbol_pair",
            _MARKET_SYMBOL_PAIR_SQL,
        )
        batch.create_check_constraint(
            "ck_od_fact_selector_profile",
            _SELECTOR_PROFILE_SQL,
        )
        batch.create_index(
            _MARKET_INDEX,
            ["dataset_id", "market", "symbol", "publication_sequence"],
            unique=False,
        )
    with op.batch_alter_table(_SNAPSHOT_TABLE) as batch:
        batch.add_column(Column("market", _selector_type(32), nullable=True))
        batch.add_column(Column("symbol", _selector_type(128), nullable=True))
        batch.create_check_constraint(
            "ck_od_snapshot_market_symbol_pair",
            _MARKET_SYMBOL_PAIR_SQL,
        )
        batch.create_check_constraint(
            "ck_od_snapshot_selector_profile",
            _SELECTOR_PROFILE_SQL,
        )


def upgrade() -> None:
    """Add nullable market/symbol pairs and an exact fact lookup index."""
    mode = _target_mode()
    _require_revision_marker(down_revision, mode)
    _require_table_shape(_FACT_TABLE, mode, include_market_symbol=False)
    _require_table_shape(_SNAPSHOT_TABLE, mode, include_market_symbol=False)
    if mode == "sqlite":
        _require_sqlite_rebuild_safety()
    _require_market_index_name_free()

    try:
        _apply_upgrade_ddl()
        _require_table_shape(_FACT_TABLE, mode, include_market_symbol=True)
        _require_table_shape(_SNAPSHOT_TABLE, mode, include_market_symbol=True)
        if mode == "sqlite":
            _require_sqlite_snapshot_selector_collation()
    except Exception as error:
        if mode == "mysql":
            raise RuntimeError(
                "revision 0019 MySQL DDL may be partially applied while its marker remains "
                "at 0018; verify both table schemas offline before retrying or repairing"
            ) from error
        raise


def downgrade() -> None:
    """Fail closed because no migration-level fence can protect selector data."""
    mode = _target_mode()
    _require_revision_marker(revision, mode)
    raise RuntimeError(
        "revision 0019 automatic downgrade is disabled: a selector scan cannot fence every "
        "writer, and MySQL DDL implicitly commits; use a separately reviewed, explicitly "
        "quiesced offline rollback procedure"
    )
