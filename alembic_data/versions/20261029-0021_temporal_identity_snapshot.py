"""Create immutable, chunked temporal identity snapshot storage.

Revision ID: 0021_temporal_identity_snapshot
Revises: 0020_run_ledger_claim_policy
Create Date: 2026-10-29

The warehouse migration is explicit and never runs at application startup. The
schema stores a complete snapshot header and bounded binary chunks in one
versioned namespace. A complete schema with the old marker is accepted after a
crash between MySQL DDL statements; a partial schema is left untouched for
offline inspection. Automatic downgrade is disabled because deleting persisted
identity history is destructive and cannot be fenced from concurrent writers.

SQLite uses BINARY identity collations and MySQL requires InnoDB on MySQL
8.0.16+, enforced CHECK constraints, and utf8mb4_0900_bin identity collations.
This migration does not claim live MySQL acceptance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import column, inspect, select, table, text

from alembic import op

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector

revision = "0021_temporal_identity_snapshot"
down_revision = "0020_run_ledger_claim_policy"
branch_labels = None
depends_on = None

_HEADER = "od_temporal_identity_snapshots"
_CHUNKS = "od_temporal_identity_snapshot_chunks"
_VERSION_TABLE_FALLBACK = "alembic_version_data"
_EXPECTED_MARKER = "0020_run_ledger_claim_policy"
_MYSQL_COLLATION = "utf8mb4_0900_bin"
_MYSQL_MIN_VERSION = (8, 0, 16)
_MAX_TOTAL_BYTES = 67_108_864
_MAX_CHUNKS = 256
_MAX_CHUNK_BYTES = 262_144
_ASCII_ID_CHECK = (
    "length({name}) BETWEEN 1 AND 128 AND length(CAST({name} AS BLOB)) = length({name})"
)
_MYSQL_ASCII_ID_CHECK = (
    "CHAR_LENGTH({name}) BETWEEN 1 AND 128 AND "
    "OCTET_LENGTH({name}) = CHAR_LENGTH({name}) AND LOCATE(CHAR(0), {name}) = 0"
)

_HEADER_COLUMNS: dict[str, tuple[str, int | None]] = {
    "authority_id": ("identity", 128),
    "registry_id": ("identity", 128),
    "reference_version": ("identity", 128),
    "total_bytes": ("bigint", None),
    "chunk_count": ("smallint", None),
    "snapshot_sha256": ("binary32", 32),
    "created_at_us": ("bigint", None),
}
_CHUNK_COLUMNS: dict[str, tuple[str, int | None]] = {
    "authority_id": ("identity", 128),
    "registry_id": ("identity", 128),
    "reference_version": ("identity", 128),
    "chunk_no": ("smallint", None),
    "chunk_bytes": ("blob", None),
    "chunk_sha256": ("binary32", 32),
}
_HEADER_CHECKS = {
    "ck_od_temporal_identity_snapshots_authority_id": _ASCII_ID_CHECK.format(name="authority_id"),
    "ck_od_temporal_identity_snapshots_registry_id": _ASCII_ID_CHECK.format(name="registry_id"),
    "ck_od_temporal_identity_snapshots_reference_version": _ASCII_ID_CHECK.format(
        name="reference_version"
    ),
    "ck_od_temporal_identity_snapshots_total_bytes": (
        f"total_bytes BETWEEN 1 AND {_MAX_TOTAL_BYTES}"
    ),
    "ck_od_temporal_identity_snapshots_chunk_count": (f"chunk_count BETWEEN 1 AND {_MAX_CHUNKS}"),
    "ck_od_temporal_identity_snapshots_created_at_us": "created_at_us >= 0",
    "ck_od_temporal_identity_snapshots_snapshot_sha256": (
        "typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32"
    ),
}
_CHUNK_CHECKS = {
    "ck_od_temporal_identity_snapshot_chunks_authority_id": _ASCII_ID_CHECK.format(
        name="authority_id"
    ),
    "ck_od_temporal_identity_snapshot_chunks_registry_id": _ASCII_ID_CHECK.format(
        name="registry_id"
    ),
    "ck_od_temporal_identity_snapshot_chunks_reference_version": _ASCII_ID_CHECK.format(
        name="reference_version"
    ),
    "ck_od_temporal_identity_snapshot_chunks_chunk_no": (
        f"chunk_no BETWEEN 0 AND {_MAX_CHUNKS - 1}"
    ),
    "ck_od_temporal_identity_snapshot_chunks_chunk_bytes": (
        f"typeof(chunk_bytes) = 'blob' AND length(chunk_bytes) BETWEEN 1 AND {_MAX_CHUNK_BYTES}"
    ),
    "ck_od_temporal_identity_snapshot_chunks_chunk_sha256": (
        "typeof(chunk_sha256) = 'blob' AND length(chunk_sha256) = 32"
    ),
}
_MYSQL_HEADER_CHECKS = {
    **{
        name: _MYSQL_ASCII_ID_CHECK.format(name=field)
        for name, field in (
            ("ck_od_temporal_identity_snapshots_authority_id", "authority_id"),
            ("ck_od_temporal_identity_snapshots_registry_id", "registry_id"),
            ("ck_od_temporal_identity_snapshots_reference_version", "reference_version"),
        )
    },
    "ck_od_temporal_identity_snapshots_total_bytes": (
        f"total_bytes BETWEEN 1 AND {_MAX_TOTAL_BYTES}"
    ),
    "ck_od_temporal_identity_snapshots_chunk_count": (f"chunk_count BETWEEN 1 AND {_MAX_CHUNKS}"),
    "ck_od_temporal_identity_snapshots_created_at_us": "created_at_us >= 0",
}
_MYSQL_CHUNK_CHECKS = {
    **{
        name: _MYSQL_ASCII_ID_CHECK.format(name=field)
        for name, field in (
            ("ck_od_temporal_identity_snapshot_chunks_authority_id", "authority_id"),
            ("ck_od_temporal_identity_snapshot_chunks_registry_id", "registry_id"),
            ("ck_od_temporal_identity_snapshot_chunks_reference_version", "reference_version"),
        )
    },
    "ck_od_temporal_identity_snapshot_chunks_chunk_no": (
        f"chunk_no BETWEEN 0 AND {_MAX_CHUNKS - 1}"
    ),
    "ck_od_temporal_identity_snapshot_chunks_chunk_bytes": (
        f"OCTET_LENGTH(chunk_bytes) BETWEEN 1 AND {_MAX_CHUNK_BYTES}"
    ),
}
_HEADER_PK = ("authority_id", "registry_id", "reference_version")
_CHUNK_PK = (*_HEADER_PK, "chunk_no")
_FK_NAME = "fk_od_temporal_identity_snapshot_chunks_snapshot"
_CHECK_TOKEN = re.compile(
    r"\s*('(?:''|[^'])*'|`[^`]+`|\"[^\"]+\"|>=|<=|<>|!=|[(),=]|"
    r"[a-z_][a-z0-9_$]*|[0-9]+)",
    re.IGNORECASE | re.ASCII,
)
_CHECK_COLUMNS = {
    "authority_id",
    "registry_id",
    "reference_version",
    "total_bytes",
    "chunk_count",
    "created_at_us",
    "snapshot_sha256",
    "chunk_no",
    "chunk_bytes",
    "chunk_sha256",
}
_CHECK_FUNCTIONS = {
    "length",
    "typeof",
    "cast",
    "char_length",
    "octet_length",
    "locate",
    "char",
}


class _CheckExpressionParser:
    """Parse only the reviewed CHECK grammar and discard grouping parentheses."""

    def __init__(self, tokens: list[tuple[str, str]], mode: str) -> None:
        self._tokens = tokens
        self._mode = mode
        self._position = 0

    def parse(self) -> tuple[object, ...]:
        """Return a stable expression tree or reject incomplete and unknown syntax."""
        if not self._tokens:
            raise ValueError("empty CHECK expression")
        expression = self._parse_and()
        if self._position != len(self._tokens):
            raise ValueError("unsupported CHECK expression syntax")
        return expression

    def _parse_and(self) -> tuple[object, ...]:
        expression = self._parse_boolean_primary()
        while self._accept("identifier", "and"):
            expression = ("and", expression, self._parse_boolean_primary())
        return expression

    def _parse_boolean_primary(self) -> tuple[object, ...]:
        if self._peek() == ("symbol", "(") and not self._starts_parenthesized_predicate():
            self._position += 1
            expression = self._parse_and()
            self._require("symbol", ")")
            return expression
        return self._parse_predicate()

    def _starts_parenthesized_predicate(self) -> bool:
        """Recognize `(scalar) comparison ...` without weakening boolean parsing."""
        depth = 0
        for position in range(self._position, len(self._tokens)):
            kind, value = self._tokens[position]
            if kind == "symbol" and value == "(":
                depth += 1
            elif kind == "symbol" and value == ")":
                depth -= 1
                if depth == 0:
                    if position + 1 >= len(self._tokens):
                        return False
                    next_kind, next_value = self._tokens[position + 1]
                    return (next_kind, next_value) in {
                        ("identifier", "between"),
                        ("operator", "="),
                        ("operator", ">="),
                        ("operator", "<="),
                        ("operator", "<>"),
                        ("operator", "!="),
                    }
        return False

    def _parse_predicate(self) -> tuple[object, ...]:
        left = self._parse_scalar()
        if self._accept("identifier", "between"):
            lower = self._parse_scalar()
            self._require("identifier", "and")
            upper = self._parse_scalar()
            return ("between", left, lower, upper)

        operator = self._peek()
        if (
            operator is None
            or operator[0] != "operator"
            or operator[1]
            not in {
                "=",
                ">=",
                "<=",
                "<>",
                "!=",
            }
        ):
            raise ValueError("unsupported CHECK predicate")
        self._position += 1
        return ("compare", left, operator[1], self._parse_scalar())

    def _parse_scalar(self) -> tuple[object, ...]:
        token = self._take()
        kind, value = token
        if kind == "number":
            return ("number", value)
        if kind == "string":
            return ("string", value)
        if kind == "symbol" and value == "(":
            expression = self._parse_scalar()
            self._require("symbol", ")")
            return expression
        if kind not in {"identifier", "quoted_identifier"}:
            raise ValueError("unsupported CHECK scalar")
        if kind == "quoted_identifier":
            if value not in _CHECK_COLUMNS:
                raise ValueError("quoted CHECK token is not a reviewed column")
            return ("column", value)
        if value in _CHECK_COLUMNS:
            return ("column", value)
        if value not in _CHECK_FUNCTIONS:
            raise ValueError("unsupported CHECK identifier")
        self._require("symbol", "(")
        if value == "cast":
            expression = self._parse_scalar()
            self._require("identifier", "as")
            self._require("identifier", "blob")
            self._require("symbol", ")")
            return ("cast", expression, "blob")
        function = value
        if self._mode == "mysql" and value in {"length", "octet_length"}:
            function = "mysql_byte_length"
        first = self._parse_scalar()
        if value == "locate":
            self._require("symbol", ",")
            second = self._parse_scalar()
            self._require("symbol", ")")
            return ("call", value, first, second)
        self._require("symbol", ")")
        return ("call", function, first)

    def _peek(self) -> tuple[str, str] | None:
        if self._position >= len(self._tokens):
            return None
        return self._tokens[self._position]

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ValueError("incomplete CHECK expression")
        self._position += 1
        return token

    def _accept(self, kind: str, value: str) -> bool:
        if self._peek() != (kind, value):
            return False
        self._position += 1
        return True

    def _require(self, kind: str, value: str) -> None:
        if not self._accept(kind, value):
            raise ValueError("unsupported CHECK expression syntax")


def _tokenize_check(sql: object, mode: str) -> list[tuple[str, str]]:
    """Tokenize reviewed expressions and preserve dialect literal rules."""
    if mode not in {"sqlite", "mysql"}:
        raise ValueError("unsupported CHECK expression dialect")
    value = str(sql).strip()
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(value):
        match = _CHECK_TOKEN.match(value, position)
        if match is None:
            raise ValueError("unsupported CHECK expression syntax")
        raw = match.group(1)
        position = match.end()
        if raw.startswith("'"):
            tokens.append(("string", raw[1:-1].replace("''", "'")))
        elif raw.startswith("`"):
            identifier = raw[1:-1]
            if not identifier.isascii():
                raise ValueError("non-ASCII CHECK identifiers are not accepted")
            tokens.append(("quoted_identifier", identifier.casefold()))
        elif raw.startswith('"'):
            if mode == "mysql":
                raise ValueError("double-quoted MySQL CHECK tokens are not accepted")
            identifier = raw[1:-1]
            if not identifier.isascii():
                raise ValueError("non-ASCII CHECK identifiers are not accepted")
            tokens.append(("quoted_identifier", identifier.casefold()))
        elif raw in {"(", ")", ","}:
            tokens.append(("symbol", raw))
        elif raw in {"=", "<>", "!=", ">=", "<="}:
            tokens.append(("operator", raw))
        elif raw.isdecimal():
            tokens.append(("number", raw))
        else:
            tokens.append(("identifier", raw.casefold()))
    return tokens


def _canonical_check(sql: object, mode: str) -> tuple[object, ...]:
    """Parse exact reviewed CHECK semantics while ignoring redundant grouping."""
    tokens = _tokenize_check(sql, mode)
    return _CheckExpressionParser(tokens, mode).parse()


def _check_matches(actual: object, expected: str, mode: str) -> bool:
    """Compare a reflected check and convert unsupported syntax to no-match."""
    try:
        return _canonical_check(actual, mode) == _canonical_check(expected, mode)
    except (TypeError, ValueError):
        return False


def _require_supported_target() -> str:
    """Allow only online SQLite and MySQL 8.0.16+ migration targets."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("revision 0021 requires an online warehouse connection")
    dialect = context.dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0021 supports SQLite and MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0021 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < _MYSQL_MIN_VERSION
    ):
        raise RuntimeError("revision 0021 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _version_table_name(context: object) -> str:
    """Return the configured warehouse Alembic version table name."""
    value = getattr(context, "version_table", None) or _VERSION_TABLE_FALLBACK
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("revision 0021 requires a valid Alembic version table name")
    return value


def _require_revision_marker() -> None:
    """Require exactly the 0020 marker before creating warehouse tables."""
    context = op.get_context()
    version_table = _version_table_name(context)
    schema = getattr(context, "version_table_schema", None)
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(version_table, schema=schema):
        raise RuntimeError("revision 0021 requires the configured Alembic version table")
    columns = inspector.get_columns(version_table, schema=schema)
    if len(columns) != 1 or columns[0].get("name") != "version_num":
        raise RuntimeError("revision 0021 requires a version table containing only version_num")
    version_column = columns[0]
    column_type = version_column.get("type")
    version_length = getattr(column_type, "length", None)
    if (
        getattr(column_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_length, int)
        or version_length < max(len(down_revision), len(revision))
        or version_column.get("nullable") is not False
        or version_column.get("default") is not None
        or inspector.get_pk_constraint(version_table, schema=schema).get("constrained_columns")
        != ["version_num"]
    ):
        raise RuntimeError("revision 0021 requires a bounded primary-key version_num column")
    marker_table = table(
        version_table,
        column("version_num", column_type),
        schema=schema,
    )
    marker = op.get_bind().execute(select(marker_table.c.version_num)).scalars().all()
    if marker != [_EXPECTED_MARKER]:
        raise RuntimeError("revision 0021 requires exactly the 0020 warehouse marker")


def _sqlite_ddl() -> tuple[str, str]:
    """Return the fixed SQLite table definitions for review and testing."""
    header_checks = [
        f"CONSTRAINT {name} CHECK ({expression})" for name, expression in _HEADER_CHECKS.items()
    ]
    chunks_checks = [
        f"CONSTRAINT {name} CHECK ({expression})" for name, expression in _CHUNK_CHECKS.items()
    ]
    header_checks_sql = ",\n        ".join(header_checks)
    chunks_checks_sql = ",\n        ".join(chunks_checks)
    foreign_key_sql = (
        f"CONSTRAINT {_FK_NAME} FOREIGN KEY (authority_id, registry_id, reference_version) "
        f'REFERENCES "{_HEADER}" (authority_id, registry_id, reference_version) '
        "ON DELETE RESTRICT"
    )
    header_ddl = f'''CREATE TABLE "{_HEADER}" (
        authority_id VARCHAR(128) COLLATE BINARY NOT NULL,
        registry_id VARCHAR(128) COLLATE BINARY NOT NULL,
        reference_version VARCHAR(128) COLLATE BINARY NOT NULL,
        total_bytes BIGINT NOT NULL,
        chunk_count SMALLINT NOT NULL,
        snapshot_sha256 BLOB NOT NULL,
        created_at_us BIGINT NOT NULL,
        CONSTRAINT pk_od_temporal_identity_snapshots
            PRIMARY KEY (authority_id, registry_id, reference_version),
        {header_checks_sql}
    )'''
    chunks_ddl = f'''CREATE TABLE "{_CHUNKS}" (
        authority_id VARCHAR(128) COLLATE BINARY NOT NULL,
        registry_id VARCHAR(128) COLLATE BINARY NOT NULL,
        reference_version VARCHAR(128) COLLATE BINARY NOT NULL,
        chunk_no SMALLINT NOT NULL,
        chunk_bytes BLOB NOT NULL,
        chunk_sha256 BLOB NOT NULL,
        CONSTRAINT pk_od_temporal_identity_snapshot_chunks
            PRIMARY KEY (authority_id, registry_id, reference_version, chunk_no),
        {chunks_checks_sql},
        {foreign_key_sql}
    )'''
    return header_ddl, chunks_ddl


def _mysql_ddl() -> tuple[str, str]:
    """Return MySQL 8 DDL without implying a live-server acceptance result."""
    identity_checks_header = [
        f"CONSTRAINT {name} CHECK ({expression})"
        for name, expression in _MYSQL_HEADER_CHECKS.items()
    ]
    identity_checks_chunks = [
        f"CONSTRAINT {name} CHECK ({expression})"
        for name, expression in _MYSQL_CHUNK_CHECKS.items()
    ]
    header_ddl = f"""CREATE TABLE `{_HEADER}` (
        authority_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE {_MYSQL_COLLATION} NOT NULL,
        registry_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE {_MYSQL_COLLATION} NOT NULL,
        reference_version VARCHAR(128) CHARACTER SET utf8mb4 COLLATE {_MYSQL_COLLATION} NOT NULL,
        total_bytes BIGINT NOT NULL,
        chunk_count SMALLINT NOT NULL,
        snapshot_sha256 BINARY(32) NOT NULL,
        created_at_us BIGINT NOT NULL,
        CONSTRAINT pk_od_temporal_identity_snapshots
            PRIMARY KEY (authority_id, registry_id, reference_version),
        {", ".join(identity_checks_header)}
    ) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8mb4 COLLATE={_MYSQL_COLLATION}"""
    chunks_ddl = f"""CREATE TABLE `{_CHUNKS}` (
        authority_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE {_MYSQL_COLLATION} NOT NULL,
        registry_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE {_MYSQL_COLLATION} NOT NULL,
        reference_version VARCHAR(128) CHARACTER SET utf8mb4 COLLATE {_MYSQL_COLLATION} NOT NULL,
        chunk_no SMALLINT NOT NULL,
        chunk_bytes MEDIUMBLOB NOT NULL,
        chunk_sha256 BINARY(32) NOT NULL,
        CONSTRAINT pk_od_temporal_identity_snapshot_chunks
            PRIMARY KEY (authority_id, registry_id, reference_version, chunk_no),
        {", ".join(identity_checks_chunks)},
        CONSTRAINT {_FK_NAME} FOREIGN KEY (authority_id, registry_id, reference_version)
            REFERENCES `{_HEADER}` (authority_id, registry_id, reference_version)
            ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8mb4 COLLATE={_MYSQL_COLLATION}"""
    return header_ddl, chunks_ddl


def _expected_columns(table_name: str) -> dict[str, tuple[str, int | None]]:
    """Return the reviewed column/type shape for one table."""
    if table_name == _HEADER:
        return _HEADER_COLUMNS
    if table_name == _CHUNKS:
        return _CHUNK_COLUMNS
    raise RuntimeError("revision 0021 cannot inspect an unknown table")


def _type_matches(actual: object, kind: str, length: int | None, mode: str) -> bool:
    """Match reflected SQLAlchemy types without depending on display strings."""
    from sqlalchemy import BigInteger, LargeBinary, SmallInteger, String
    from sqlalchemy.dialects.mysql import BINARY, MEDIUMBLOB, VARCHAR

    if kind == "identity":
        expected_type = VARCHAR if mode == "mysql" else String
        return isinstance(actual, expected_type) and getattr(actual, "length", None) == length
    if kind == "bigint":
        return isinstance(actual, BigInteger)
    if kind == "smallint":
        return isinstance(actual, SmallInteger)
    if kind == "binary32":
        if mode == "mysql":
            return isinstance(actual, BINARY) and getattr(actual, "length", None) == 32
        return isinstance(actual, LargeBinary)
    if kind == "blob":
        if mode == "mysql":
            return isinstance(actual, MEDIUMBLOB)
        return isinstance(actual, LargeBinary)
    return False


def _require_columns(inspector: Inspector, table_name: str, mode: str) -> None:
    """Require exact column names, types, nullability, and no server defaults."""
    columns = inspector.get_columns(table_name)
    by_name = {str(item.get("name")): item for item in columns}
    expected = _expected_columns(table_name)
    if len(by_name) != len(columns) or set(by_name) != set(expected):
        raise RuntimeError(f"revision 0021 found an unexpected {table_name} column set")
    for name, (kind, length) in expected.items():
        item = by_name[name]
        if (
            not _type_matches(item.get("type"), kind, length, mode)
            or item.get("nullable") is not False
            or item.get("default") is not None
            or item.get("computed")
            or item.get("identity")
        ):
            raise RuntimeError(f"revision 0021 found an unexpected {table_name}.{name} definition")


def _require_primary_key(inspector: Inspector, table_name: str, mode: str) -> None:
    """Require each table's exact ordered composite primary key."""
    expected = _HEADER_PK if table_name == _HEADER else _CHUNK_PK
    actual = inspector.get_pk_constraint(table_name)
    if tuple(actual.get("constrained_columns") or ()) != expected:
        raise RuntimeError(f"revision 0021 requires the exact {table_name} primary key")
    allowed_names = {f"pk_{table_name}", None}
    if mode == "mysql":
        allowed_names.add("PRIMARY")
    if actual.get("name") not in allowed_names:
        raise RuntimeError(f"revision 0021 found an unexpected {table_name} primary-key name")


def _require_checks(inspector: Inspector, table_name: str, mode: str) -> None:
    """Require exact named validation checks and enforced MySQL constraints."""
    if mode == "sqlite":
        expected = _HEADER_CHECKS if table_name == _HEADER else _CHUNK_CHECKS
    else:
        expected = _MYSQL_HEADER_CHECKS if table_name == _HEADER else _MYSQL_CHUNK_CHECKS
    checks = inspector.get_check_constraints(table_name)
    names = [item.get("name") for item in checks]
    if (
        len(names) != len(expected)
        or any(not isinstance(name, str) or not name for name in names)
        or len(set(names)) != len(names)
        or set(names) != set(expected)
    ):
        raise RuntimeError(f"revision 0021 found an unexpected {table_name} CHECK set")
    actual = {str(item["name"]): item.get("sqltext") for item in checks}
    for name, expression in expected.items():
        if not _check_matches(actual[name], expression, mode):
            raise RuntimeError(f"revision 0021 found an unexpected definition for {name}")
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
            raise RuntimeError(f"revision 0021 requires every {table_name} CHECK to be enforced")


def _require_foreign_key(inspector: Inspector) -> None:
    """Require the exact composite child-to-header RESTRICT relationship."""
    foreign_keys = inspector.get_foreign_keys(_CHUNKS)
    if len(foreign_keys) != 1:
        raise RuntimeError("revision 0021 requires one composite snapshot foreign key")
    foreign_key = foreign_keys[0]
    if (
        foreign_key.get("name") != _FK_NAME
        or foreign_key.get("referred_table") != _HEADER
        or tuple(foreign_key.get("constrained_columns") or ()) != _HEADER_PK
        or tuple(foreign_key.get("referred_columns") or ()) != _HEADER_PK
        or str(foreign_key.get("options", {}).get("ondelete", "")).upper() != "RESTRICT"
        or foreign_key.get("options", {}).get("onupdate") is not None
    ):
        raise RuntimeError("revision 0021 found an unexpected snapshot foreign key")


def _require_sqlite_key_collations(table_name: str) -> None:
    """Verify the effective SQLite primary-key comparison is BINARY."""
    bind = op.get_bind()
    indexes = (
        bind.execute(
            text("SELECT name FROM pragma_index_list(:table) WHERE origin = 'pk'"),
            {"table": table_name},
        )
        .scalars()
        .all()
    )
    if len(indexes) != 1 or not isinstance(indexes[0], str):
        raise RuntimeError(f"revision 0021 could not inspect {table_name} primary-key collations")
    parts = (
        bind.execute(
            text(
                'SELECT seqno, name, "desc" AS descending, coll, "key" AS is_key '
                "FROM pragma_index_xinfo(:index_name) ORDER BY seqno"
            ),
            {"index_name": indexes[0]},
        )
        .mappings()
        .all()
    )
    keys = [item for item in parts if item.get("is_key") == 1]
    expected = _HEADER_PK if table_name == _HEADER else _CHUNK_PK
    if (
        tuple(item.get("name") for item in keys) != expected
        or any(str(item.get("coll", "")).casefold() != "binary" for item in keys)
        or any(item.get("descending") not in (0, False) for item in keys)
    ):
        raise RuntimeError(f"revision 0021 requires BINARY {table_name} primary-key collations")


def _require_mysql_collations_and_engine(table_name: str) -> None:
    """Require utf8mb4 binary identity columns and an InnoDB table."""
    bind = op.get_bind()
    rows = (
        bind.execute(
            text(
                "SELECT COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table"
            ),
            {"table": table_name},
        )
        .mappings()
        .all()
    )
    identities = {row.get("COLUMN_NAME"): row for row in rows}
    if not set(_HEADER_PK).issubset(identities) or any(
        str(identities[name].get("CHARACTER_SET_NAME", "")).casefold() != "utf8mb4"
        or str(identities[name].get("COLLATION_NAME", "")).casefold() != _MYSQL_COLLATION
        for name in _HEADER_PK
    ):
        raise RuntimeError(f"revision 0021 requires {_MYSQL_COLLATION} identity columns")
    table_row = (
        bind.execute(
            text(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
            ),
            {"table": table_name},
        )
        .mappings()
        .one_or_none()
    )
    if table_row is None or str(table_row.get("ENGINE", "")).casefold() != "innodb":
        raise RuntimeError(f"revision 0021 requires {table_name} to use InnoDB")


def _require_no_triggers(table_name: str, mode: str) -> None:
    """Reject triggers that could mutate the immutable snapshot storage."""
    if mode == "sqlite":
        row = (
            op.get_bind()
            .execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"),
                {"table": table_name},
            )
            .first()
        )
    else:
        row = (
            op.get_bind()
            .execute(
                text(
                    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = :table"
                ),
                {"table": table_name},
            )
            .first()
        )
    if row is not None:
        raise RuntimeError(f"revision 0021 refuses {table_name} with triggers")


def _require_table_shape(mode: str) -> None:
    """Validate both tables exactly before accepting a completed prior DDL."""
    bind = op.get_bind()
    inspector = inspect(bind)
    for table_name in (_HEADER, _CHUNKS):
        if not inspector.has_table(table_name):
            raise RuntimeError(f"revision 0021 requires {table_name} to exist")
        _require_columns(inspector, table_name, mode)
        _require_primary_key(inspector, table_name, mode)
        _require_checks(inspector, table_name, mode)
        if inspector.get_unique_constraints(table_name):
            raise RuntimeError(f"revision 0021 refuses unexpected {table_name} unique constraints")
        if inspector.get_indexes(table_name):
            raise RuntimeError(f"revision 0021 refuses unexpected {table_name} indexes")
        if table_name == _HEADER and inspector.get_foreign_keys(table_name):
            raise RuntimeError("revision 0021 refuses unexpected header foreign keys")
        if mode == "sqlite":
            _require_sqlite_key_collations(table_name)
        else:
            _require_mysql_collations_and_engine(table_name)
        _require_no_triggers(table_name, mode)
    _require_foreign_key(inspector)


def _require_table_presence() -> tuple[bool, bool]:
    """Return whether each target table exists without changing the schema."""
    inspector = inspect(op.get_bind())
    return inspector.has_table(_HEADER), inspector.has_table(_CHUNKS)


def upgrade() -> None:
    """Create the immutable snapshot header and chunks after explicit migration."""
    mode = _require_supported_target()
    _require_revision_marker()
    header_exists, chunks_exist = _require_table_presence()
    if header_exists or chunks_exist:
        if not (header_exists and chunks_exist):
            raise RuntimeError(
                "revision 0021 found a partial snapshot schema; preserve it for offline inspection"
            )
        _require_table_shape(mode)
        return

    header_ddl, chunks_ddl = _sqlite_ddl() if mode == "sqlite" else _mysql_ddl()
    op.execute(text(header_ddl))
    op.execute(text(chunks_ddl))
    _require_table_shape(mode)


def downgrade() -> None:
    """Refuse automatic rollback so immutable identity history cannot be deleted."""
    raise RuntimeError(
        "revision 0021 downgrade is refused; snapshot history is immutable and must be preserved"
    )


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
