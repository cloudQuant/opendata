"""Persist each run's expired-lease claim policy.

Revision ID: 0020_run_ledger_claim_policy
Revises: 0019_publication_market_symbol_scope
Create Date: 2026-10-29

Existing rows and legacy inserts retain ``recover_expired`` semantics through
the database default. MySQL applies the column and its enforced CHECK in one
ALTER TABLE statement so an interrupted DDL cannot leave a retryable half
schema. A complete schema with the old Alembic marker is recognized as a
crash after DDL; every other partial shape fails closed. Automatic downgrade
is refused because dropping ``one_shot`` can cause an external side effect to
run again.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, String, Text, column, inspect, select, table, text
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from alembic import op

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector

revision = "0020_run_ledger_claim_policy"
down_revision = "0019_publication_market_symbol_scope"
branch_labels = None
depends_on = None

_TABLE = "od_run_ledger"
_VERSION_TABLE_FALLBACK = "alembic_version_data"
_CLAIM_POLICY = "claim_policy"
_CLAIM_POLICY_DEFAULT = "recover_expired"
_MYSQL_MIN_VERSION = (8, 0, 16)
_MYSQL_COLLATION = "utf8mb4_0900_bin"
# This SQL literal checks a numeric fence value; it is not credential material.
_FENCING_TOKEN_CHECK = "fencing_token >= 0"  # noqa: S105  # nosec B105
_CHECKS = {
    "ck_od_run_ledger_status": (
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')"
    ),
    "ck_od_run_ledger_fencing_token": _FENCING_TOKEN_CHECK,
}
_CLAIM_POLICY_CHECK = "claim_policy IN ('recover_expired', 'one_shot')"
_CLAIM_POLICY_CONSTRAINT = "ck_od_run_ledger_claim_policy"
_INDEX = "ix_od_run_ledger_claim"
_INDEX_COLUMNS = ("status", "lease_expires_at_us", "created_at_us")
_SQLITE_COLLATION_PROBE = "ix_od_run_ledger_claim_policy_probe_0020"
_BASE_COLUMNS: dict[str, tuple[type[Any], int | None, bool]] = {
    "run_key": (String, 255, False),
    "job_snapshot_json": (Text, None, False),
    "status": (String, 16, False),
    "worker": (String, 255, True),
    "lease_id": (String, 32, True),
    "fencing_token": (BigInteger, None, False),
    "lease_expires_at_us": (BigInteger, None, True),
    "created_at_us": (BigInteger, None, False),
    "updated_at_us": (BigInteger, None, False),
    "started_at_us": (BigInteger, None, True),
    "heartbeat_at_us": (BigInteger, None, True),
    "finished_at_us": (BigInteger, None, True),
    "error_text": (Text, None, True),
}
_CLAIM_POLICY_COLUMN: tuple[type[Any], int | None, bool] = (String, 16, False)
_CHECK_TOKEN = re.compile(
    r"\s*(?:_[a-z0-9]+(?=')\s*)?"
    r"('(?:''|[^'])*'|`[a-z_][a-z0-9_$]*`|"
    r'"[a-z_][a-z0-9_$]*"|[a-z_][a-z0-9_$]*|>=|<=|<>|!=|[(),]|[0-9]+)',
    re.IGNORECASE,
)


class _CheckParser:
    """Parse only the small CHECK-expression grammar used by this ledger."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        """Store the already tokenized CHECK expression."""
        self.tokens = tokens
        self.position = 0

    def parse(self) -> tuple[object, ...]:
        """Return a canonical expression tree and reject trailing tokens."""
        expression = self._parse_or()
        if self.position != len(self.tokens):
            raise ValueError("unexpected trailing CHECK tokens")
        return expression

    def _parse_or(self) -> tuple[object, ...]:
        expression = self._parse_and()
        while self._accept("identifier", "or"):
            expression = ("or", expression, self._parse_and())
        return expression

    def _parse_and(self) -> tuple[object, ...]:
        expression = self._parse_primary()
        while self._accept("identifier", "and"):
            expression = ("and", expression, self._parse_primary())
        return expression

    def _parse_primary(self) -> tuple[object, ...]:
        if self._accept("symbol", "("):
            expression = self._parse_or()
            self._require("symbol", ")")
            return expression

        identifier = self._require_kind("identifier")
        if self._accept("identifier", "in"):
            self._require("symbol", "(")
            values = [self._require_kind("string")]
            while self._accept("symbol", ","):
                values.append(self._require_kind("string"))
            self._require("symbol", ")")
            return ("in", identifier, tuple(sorted(values)))
        if self._accept("operator", ">="):
            value = int(self._require_kind("number"))
            return (">=", identifier, value)
        raise ValueError("unsupported CHECK expression")

    def _accept(self, kind: str, value: str) -> bool:
        if self.position >= len(self.tokens) or self.tokens[self.position] != (kind, value):
            return False
        self.position += 1
        return True

    def _require(self, kind: str, value: str) -> None:
        if not self._accept(kind, value):
            raise ValueError("unexpected CHECK expression token")

    def _require_kind(self, kind: str) -> str:
        if self.position >= len(self.tokens) or self.tokens[self.position][0] != kind:
            raise ValueError("unexpected CHECK expression token kind")
        value = self.tokens[self.position][1]
        self.position += 1
        return value


def _tokenize_check(sql: object, mode: str) -> list[tuple[str, str]]:
    """Tokenize a reviewed CHECK expression without changing string semantics."""
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
            tokens.append(("identifier", raw[1:-1].casefold()))
        elif raw.startswith('"'):
            if mode == "mysql":
                raise ValueError("double-quoted MySQL CHECK tokens are not accepted")
            tokens.append(("identifier", raw[1:-1].casefold()))
        elif raw in {"(", ")", ","}:
            tokens.append(("symbol", raw))
        elif raw in {">=", "<=", "<>", "!="}:
            tokens.append(("operator", raw))
        elif raw.isdecimal():
            tokens.append(("number", raw))
        else:
            tokens.append(("identifier", raw.casefold()))
    return tokens


def _canonical_check(sql: object, mode: str) -> tuple[object, ...]:
    """Parse a reflected expression into a small, semantics-preserving tree."""
    return _CheckParser(_tokenize_check(sql, mode)).parse()


def _require_supported_target() -> str:
    """Allow only online SQLite and MySQL 8.0.16+ migration targets."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("revision 0020 requires an online warehouse connection")
    dialect = context.dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0020 supports SQLite and MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0020 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < _MYSQL_MIN_VERSION
    ):
        raise RuntimeError("revision 0020 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _version_table_name(context: object) -> str:
    """Read the configured Alembic data-version table name."""
    value = getattr(context, "version_table", None) or _VERSION_TABLE_FALLBACK
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("revision 0020 requires a valid Alembic version table name")
    return value


def _require_revision_marker(mode: str) -> None:
    """Require one exact 0019 marker before inspecting or changing the ledger."""
    context = op.get_context()
    version_table = _version_table_name(context)
    schema = getattr(context, "version_table_schema", None)
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(version_table, schema=schema):
        raise RuntimeError("revision 0020 requires the configured Alembic version table")
    version_columns = inspector.get_columns(version_table, schema=schema)
    if len(version_columns) != 1 or version_columns[0].get("name") != "version_num":
        raise RuntimeError("revision 0020 requires a version table containing only version_num")
    version_column = version_columns[0]
    version_type = version_column.get("type")
    if (
        not isinstance(version_type, String)
        or getattr(version_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_type.length, int)
        or version_column.get("nullable") is not False
        or version_column.get("default") is not None
    ):
        raise RuntimeError("revision 0020 requires a bounded, non-null VARCHAR version_num")
    if mode == "mysql" and version_type.length < max(len(down_revision), len(revision)):
        raise RuntimeError("revision 0020 requires version_num wide enough for its revision IDs")
    if inspector.get_pk_constraint(version_table, schema=schema).get("constrained_columns") != [
        "version_num"
    ]:
        raise RuntimeError("revision 0020 requires version_num as the sole version-table key")
    marker_table = table(
        version_table,
        column("version_num", version_type),
        schema=schema,
    )
    markers = bind.execute(select(marker_table.c.version_num)).scalars().all()
    if markers != [down_revision]:
        raise RuntimeError(
            "revision 0020 requires exactly one 0019 marker; unexpected or partial state "
            "requires offline verification"
        )


def _expected_columns(include_claim_policy: bool) -> dict[str, tuple[type[Any], int | None, bool]]:
    """Return the literal revision-0010 shape with an optional new column."""
    result = dict(_BASE_COLUMNS)
    if include_claim_policy:
        result[_CLAIM_POLICY] = _CLAIM_POLICY_COLUMN
    return result


def _matches_type(
    actual: object,
    name: str,
    expected: type[Any],
    length: int | None,
    mode: str,
) -> bool:
    """Compare one reflected column with the frozen ledger column type."""
    if mode == "mysql" and name in {"job_snapshot_json", "error_text"}:
        type_matches = isinstance(actual, MEDIUMTEXT)
    else:
        type_matches = isinstance(actual, expected)
    if not type_matches:
        return False
    return length is None or getattr(actual, "length", None) == length


def _is_default(value: object, expected: str) -> bool:
    """Accept only the driver's plain or single-quoted rendering of a literal."""
    return str(value).strip() in {expected, f"'{expected}'"}


def _require_columns(inspector: Inspector, mode: str, include_claim_policy: bool) -> None:
    """Require the exact expected ledger columns, defaults, and nullability."""
    expected = _expected_columns(include_claim_policy)
    columns = inspector.get_columns(_TABLE)
    by_name = {str(item.get("name")): item for item in columns}
    if len(by_name) != len(columns) or set(by_name) != set(expected):
        raise RuntimeError("revision 0020 found an unexpected od_run_ledger column set")

    for name, (expected_type, expected_length, nullable) in expected.items():
        item = by_name[name]
        if (
            not _matches_type(item.get("type"), name, expected_type, expected_length, mode)
            or item.get("nullable") is not nullable
            or item.get("computed")
            or item.get("identity")
        ):
            raise RuntimeError(f"revision 0020 found an unexpected {name} column definition")
        default = item.get("default")
        if name == _CLAIM_POLICY and include_claim_policy:
            if not _is_default(default, _CLAIM_POLICY_DEFAULT):
                raise RuntimeError("revision 0020 requires the legacy claim-policy server default")
        elif default is not None:
            raise RuntimeError(f"revision 0020 refuses an unexpected default on {name}")


def _require_mysql_collations(mode: str, include_claim_policy: bool) -> None:
    """Require exact MySQL identity collations for status and policy values."""
    if mode != "mysql":
        return
    expected_names = {"run_key", "status", "worker", "lease_id"}
    if include_claim_policy:
        expected_names.add(_CLAIM_POLICY)
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
            ),
            {"table": _TABLE},
        )
        .mappings()
        .all()
    )
    actual = {str(row.get("COLUMN_NAME")): row for row in rows}
    if not expected_names.issubset(actual):
        raise RuntimeError("revision 0020 could not verify MySQL ledger string collations")
    if any(
        str(actual[name].get("CHARACTER_SET_NAME", "")).casefold() != "utf8mb4"
        or str(actual[name].get("COLLATION_NAME", "")).casefold() != _MYSQL_COLLATION
        for name in expected_names
    ):
        raise RuntimeError("revision 0020 requires exact utf8mb4_0900_bin ledger collations")


def _require_sqlite_collations(mode: str, include_claim_policy: bool) -> None:
    """Probe SQLite's effective comparison rules without retaining an index."""
    if mode != "sqlite":
        return
    bind = op.get_bind()
    existing = bind.execute(
        text("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = :name"),
        {"name": _SQLITE_COLLATION_PROBE},
    ).first()
    if existing is not None:
        raise RuntimeError("revision 0020 found a stale SQLite ledger collation probe")

    columns = ["status"]
    if include_claim_policy:
        columns.append(_CLAIM_POLICY)
    created = False
    validation_error: BaseException | None = None
    try:
        op.create_index(
            _SQLITE_COLLATION_PROBE,
            _TABLE,
            columns,
            unique=False,
            sqlite_where=text("0"),
        )
        created = True
        parts = (
            bind.execute(
                text(
                    'SELECT seqno, name, "desc" AS descending, coll, "key" AS is_key '
                    "FROM pragma_index_xinfo(:index_name) ORDER BY seqno"
                ),
                {"index_name": _SQLITE_COLLATION_PROBE},
            )
            .mappings()
            .all()
        )
        key_parts = [row for row in parts if row.get("is_key") == 1]
        if (
            tuple(row.get("name") for row in key_parts) != tuple(columns)
            or any(str(row.get("coll", "")).casefold() != "binary" for row in key_parts)
            or any(row.get("descending") not in (0, False) for row in key_parts)
        ):
            raise RuntimeError("revision 0020 requires SQLite BINARY enum comparisons")
    except BaseException as error:
        validation_error = error
        raise
    finally:
        if created:
            try:
                op.drop_index(_SQLITE_COLLATION_PROBE, table_name=_TABLE)
            except Exception as cleanup_error:
                if validation_error is not None:
                    raise RuntimeError(
                        "revision 0020 SQLite collation validation failed and its probe "
                        "could not be removed"
                    ) from cleanup_error
                raise RuntimeError(
                    "revision 0020 could not remove its SQLite collation probe"
                ) from cleanup_error


def _require_checks(
    inspector: Inspector,
    mode: str,
    include_claim_policy: bool,
) -> None:
    """Require the exact named CHECK set and enforced MySQL definitions."""
    expected = dict(_CHECKS)
    if include_claim_policy:
        expected[_CLAIM_POLICY_CONSTRAINT] = _CLAIM_POLICY_CHECK
    checks = inspector.get_check_constraints(_TABLE)
    names = [item.get("name") for item in checks]
    if (
        len(names) != len(expected)
        or any(not isinstance(name, str) or not name for name in names)
        or len(set(names)) != len(names)
    ):
        raise RuntimeError("revision 0020 found an unexpected od_run_ledger CHECK set")
    actual = {str(item["name"]): item.get("sqltext") for item in checks}
    if set(actual) != set(expected):
        raise RuntimeError("revision 0020 found an unexpected od_run_ledger CHECK set")
    for name, expression in expected.items():
        _require_check_definition(name, actual[name], expression, mode)

    if mode == "mysql":
        rows = (
            op.get_bind()
            .execute(
                text(
                    "SELECT CONSTRAINT_NAME, ENFORCED FROM information_schema.TABLE_CONSTRAINTS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                    "AND CONSTRAINT_TYPE = 'CHECK'"
                ),
                {"table": _TABLE},
            )
            .mappings()
            .all()
        )
        enforced = {str(row.get("CONSTRAINT_NAME")): row.get("ENFORCED") for row in rows}
        if set(enforced) != set(expected) or any(
            str(value).upper() != "YES" for value in enforced.values()
        ):
            raise RuntimeError("revision 0020 requires every ledger CHECK to be enforced")


def _require_check_definition(name: str, actual: object, expected: str, mode: str) -> None:
    """Reject a CHECK whose parsed predicate differs from its frozen contract."""
    try:
        matches = _canonical_check(actual, mode) == _canonical_check(expected, mode)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"revision 0020 found an unexpected definition for {name}") from error
    if not matches:
        raise RuntimeError(f"revision 0020 found an unexpected definition for {name}")


def _require_index(inspector: Inspector) -> None:
    """Require the existing claim-order index and no additional indexes."""
    indexes = inspector.get_indexes(_TABLE)
    if len(indexes) != 1:
        raise RuntimeError("revision 0020 found an unexpected od_run_ledger index set")
    index = indexes[0]
    options = index.get("dialect_options", {})
    if (
        index.get("name") != _INDEX
        or tuple(index.get("column_names") or ()) != _INDEX_COLUMNS
        or not (
            index.get("unique") is False
            or (type(index.get("unique")) is int and index.get("unique") == 0)
        )
        or not isinstance(options, dict)
        or set(options) - {"sqlite_where", "mysql_length"}
        or options.get("sqlite_where") is not None
        or options.get("mysql_length") not in (None, {})
    ):
        raise RuntimeError("revision 0020 found an unexpected od_run_ledger claim index")


def _require_table_shape(mode: str, include_claim_policy: bool) -> None:
    """Validate every reflected ledger object before accepting or changing it."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("revision 0020 requires od_run_ledger to exist")
    _require_columns(inspector, mode, include_claim_policy)
    if inspector.get_pk_constraint(_TABLE).get("constrained_columns") != ["run_key"]:
        raise RuntimeError("revision 0020 requires run_key as the sole ledger primary key")
    if inspector.get_foreign_keys(_TABLE) or inspector.get_unique_constraints(_TABLE):
        raise RuntimeError("revision 0020 refuses unexpected ledger foreign or unique constraints")
    _require_checks(inspector, mode, include_claim_policy)
    _require_index(inspector)
    _require_mysql_collations(mode, include_claim_policy)
    _require_sqlite_collations(mode, include_claim_policy)

    if mode == "mysql":
        options = inspector.get_table_options(_TABLE)
        engine = options.get("mysql_engine") if isinstance(options, dict) else None
        if not isinstance(engine, str) or engine.casefold() != "innodb":
            raise RuntimeError("revision 0020 requires od_run_ledger to use InnoDB")
        triggers = bind.execute(
            text(
                "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = :table"
            ),
            {"table": _TABLE},
        ).first()
    else:
        triggers = bind.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"),
            {"table": _TABLE},
        ).first()
    if triggers is not None:
        raise RuntimeError("revision 0020 refuses od_run_ledger tables with triggers")


def _add_column_ddl(mode: str) -> str:
    """Build one dialect-specific DDL statement for column and CHECK together."""
    if mode == "sqlite":
        return (
            'ALTER TABLE "od_run_ledger" ADD COLUMN "claim_policy" VARCHAR(16) '
            "NOT NULL DEFAULT 'recover_expired' CONSTRAINT "
            '"ck_od_run_ledger_claim_policy" CHECK '
            "(\"claim_policy\" IN ('recover_expired', 'one_shot'))"
        )
    if mode == "mysql":
        return (
            "ALTER TABLE `od_run_ledger` "
            "ADD COLUMN `claim_policy` VARCHAR(16) CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_0900_bin NOT NULL DEFAULT 'recover_expired', "
            "ADD CONSTRAINT `ck_od_run_ledger_claim_policy` CHECK "
            "(`claim_policy` IN ('recover_expired', 'one_shot')) ENFORCED"
        )
    raise RuntimeError("revision 0020 cannot build DDL for this database dialect")


def _require_row_values(mode: str, include_claim_policy: bool) -> None:
    """Reject any persisted status or policy outside its exact enum."""
    if mode == "sqlite":
        if include_claim_policy:
            query = text(
                "SELECT 1 FROM od_run_ledger WHERE "
                "status IS NULL OR status COLLATE BINARY NOT IN "
                "('queued', 'running', 'succeeded', 'failed', 'cancelled') OR "
                "claim_policy IS NULL OR claim_policy COLLATE BINARY NOT IN "
                "('recover_expired', 'one_shot') LIMIT 1"
            )
        else:
            query = text(
                "SELECT 1 FROM od_run_ledger WHERE status IS NULL OR "
                "status COLLATE BINARY NOT IN "
                "('queued', 'running', 'succeeded', 'failed', 'cancelled') LIMIT 1"
            )
    elif mode == "mysql":
        if include_claim_policy:
            query = text(
                "SELECT 1 FROM od_run_ledger WHERE "
                "status IS NULL OR CAST(status AS BINARY) NOT IN "
                "(CAST('queued' AS BINARY), CAST('running' AS BINARY), "
                "CAST('succeeded' AS BINARY), CAST('failed' AS BINARY), "
                "CAST('cancelled' AS BINARY)) OR claim_policy IS NULL OR "
                "CAST(claim_policy AS BINARY) NOT IN "
                "(CAST('recover_expired' AS BINARY), CAST('one_shot' AS BINARY)) LIMIT 1"
            )
        else:
            query = text(
                "SELECT 1 FROM od_run_ledger WHERE status IS NULL OR "
                "CAST(status AS BINARY) NOT IN "
                "(CAST('queued' AS BINARY), CAST('running' AS BINARY), "
                "CAST('succeeded' AS BINARY), CAST('failed' AS BINARY), "
                "CAST('cancelled' AS BINARY)) LIMIT 1"
            )
    else:
        raise RuntimeError("revision 0020 cannot validate this database dialect")
    row = op.get_bind().execute(query).first()
    if row is not None:
        detail = "status or claim_policy" if include_claim_policy else "status"
        raise RuntimeError(f"revision 0020 found a persisted {detail} outside its exact enum")


def upgrade() -> None:
    """Add a defaulted, exact claim policy while preserving every old ledger row."""
    mode = _require_supported_target()
    _require_revision_marker(mode)
    inspector = inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        raise RuntimeError("revision 0020 requires od_run_ledger to exist")
    column_names = {str(item.get("name")) for item in inspector.get_columns(_TABLE)}
    if _CLAIM_POLICY in column_names:
        # MySQL may have committed the single atomic ALTER before the process
        # failed to update Alembic's marker. Accept only the exact complete DDL.
        _require_table_shape(mode, include_claim_policy=True)
        _require_row_values(mode, include_claim_policy=True)
        return

    _require_table_shape(mode, include_claim_policy=False)
    _require_row_values(mode, include_claim_policy=False)
    op.execute(_add_column_ddl(mode))
    _require_table_shape(mode, include_claim_policy=True)
    _require_row_values(mode, include_claim_policy=True)


def downgrade() -> None:
    """Refuse rollback because it would erase one-shot execution semantics."""
    raise RuntimeError(
        "revision 0020 downgrade is refused; dropping claim_policy can re-run one-shot work"
    )


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
