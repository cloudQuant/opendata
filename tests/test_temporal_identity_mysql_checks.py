"""Keep MySQL CHECK reflection tolerant only of redundant parentheses."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _REPOSITORY_ROOT / "alembic_data" / "versions" / "20261029-0021_temporal_identity_snapshot.py"
)


def _load_migration() -> ModuleType:
    """Load revision 0021 without invoking Alembic or a database connection."""
    spec = importlib.util.spec_from_file_location(
        "temporal_identity_snapshot_mysql_checks", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    """Provide the migration's pure CHECK validation helpers."""
    return _load_migration()


def test_mysql_check_match_accepts_reflection_parentheses_and_length_alias(
    migration: ModuleType,
) -> None:
    """Accept MySQL's reflected LENGTH synonym and redundant grouping."""
    expected = migration._MYSQL_HEADER_CHECKS["ck_od_temporal_identity_snapshots_authority_id"]
    actual = (
        "(((CHAR_LENGTH((`authority_id`)) BETWEEN 1 AND 128)) AND "
        "((LENGTH(`authority_id`) = (CHAR_LENGTH(`authority_id`)))) AND "
        "((LOCATE(CHAR((0)), (`authority_id`)) = (0))))"
    )

    assert migration._check_matches(actual, expected, "mysql")


def test_mysql_chunk_bytes_check_accepts_reflected_length_alias(
    migration: ModuleType,
) -> None:
    """Match the observed MySQL reflection of the OCTET_LENGTH chunk bound."""
    expected = migration._MYSQL_CHUNK_CHECKS["ck_od_temporal_identity_snapshot_chunks_chunk_bytes"]

    assert migration._check_matches(
        "((LENGTH((`chunk_bytes`)) BETWEEN 1 AND 262144))", expected, "mysql"
    )


def test_mysql_check_match_accepts_parenthesized_between_operand(migration: ModuleType) -> None:
    """A reflected scalar wrapper before BETWEEN does not alter the range."""
    expected = migration._MYSQL_HEADER_CHECKS["ck_od_temporal_identity_snapshots_total_bytes"]

    assert migration._check_matches("((total_bytes)) BETWEEN 1 AND 67108864", expected, "mysql")


@pytest.mark.parametrize(
    ("mode", "checks_name"),
    [
        ("mysql", "_MYSQL_HEADER_CHECKS"),
        ("mysql", "_MYSQL_CHUNK_CHECKS"),
        ("sqlite", "_HEADER_CHECKS"),
        ("sqlite", "_CHUNK_CHECKS"),
    ],
)
def test_declared_check_expressions_match_their_reviewed_forms(
    migration: ModuleType,
    mode: str,
    checks_name: str,
) -> None:
    """Keep every current SQLite and MySQL CHECK in the parser's reviewed grammar."""
    checks = getattr(migration, checks_name)

    assert checks
    assert all(
        migration._check_matches(expression, expression, mode) for expression in checks.values()
    )


def test_sqlite_ddl_places_each_named_check_on_its_own_line(migration: ModuleType) -> None:
    """Preserve CHECK names because SQLAlchemy 2.0.39 reflects them line by line."""
    header_ddl, chunks_ddl = migration._sqlite_ddl()
    tables = (
        (header_ddl, migration._HEADER_CHECKS),
        (chunks_ddl, migration._CHUNK_CHECKS),
    )

    for ddl, checks in tables:
        check_lines = [line.strip() for line in ddl.splitlines() if " CHECK (" in line]
        assert [line.split()[1] for line in check_lines] == list(checks)
        assert all(
            line.startswith("CONSTRAINT ") and line.count(" CHECK (") == 1 for line in check_lines
        )


def test_sqlite_ddl_places_named_foreign_key_declaration_on_one_line(
    migration: ModuleType,
) -> None:
    """Keep SQLite's reflected FK name and delete action visible to SQLAlchemy's regex."""
    _, chunks_ddl = migration._sqlite_ddl()
    expected = (
        f"CONSTRAINT {migration._FK_NAME} FOREIGN KEY "
        f'(authority_id, registry_id, reference_version) REFERENCES "{migration._HEADER}" '
        "(authority_id, registry_id, reference_version) ON DELETE RESTRICT"
    )
    foreign_key_lines = [
        line.strip()
        for line in chunks_ddl.splitlines()
        if f"CONSTRAINT {migration._FK_NAME} FOREIGN KEY" in line
    ]

    assert foreign_key_lines == [expected]


def test_sqlite_reflection_accepts_double_quoted_column_names(migration: ModuleType) -> None:
    """SQLite identifier quoting remains supported while MySQL quoting stays strict."""
    expected = migration._HEADER_CHECKS["ck_od_temporal_identity_snapshots_authority_id"]
    actual = (
        'length("authority_id") BETWEEN 1 AND 128 AND '
        'length(CAST("authority_id" AS BLOB)) = length("authority_id")'
    )

    assert migration._check_matches(actual, expected, "sqlite")


def test_sqlite_chunk_digest_reflection_accepts_grouping_parentheses(
    migration: ModuleType,
) -> None:
    """Retain the chunk digest column while normalizing reflected grouping."""
    expected = migration._CHUNK_CHECKS["ck_od_temporal_identity_snapshot_chunks_chunk_sha256"]
    actual = "((typeof(`chunk_sha256`) = 'blob') AND ((length(`chunk_sha256`)) = (32)))"

    assert migration._check_matches(actual, expected, "sqlite")


def test_mysql_byte_length_alias_does_not_accept_character_length(
    migration: ModuleType,
) -> None:
    """Keep byte length distinct from the character-counting function."""
    expected = migration._MYSQL_HEADER_CHECKS["ck_od_temporal_identity_snapshots_authority_id"]
    actual = (
        "CHAR_LENGTH(authority_id) BETWEEN 1 AND 128 AND "
        "CHAR_LENGTH(authority_id) = CHAR_LENGTH(authority_id) AND "
        "LOCATE(CHAR(0), authority_id) = 0"
    )

    assert not migration._check_matches(actual, expected, "mysql")


def test_mysql_byte_length_alias_does_not_accept_changed_range(migration: ModuleType) -> None:
    """Function alias normalization must leave the approved length bounds exact."""
    expected = migration._MYSQL_CHUNK_CHECKS["ck_od_temporal_identity_snapshot_chunks_chunk_bytes"]
    actual = "LENGTH(chunk_bytes) BETWEEN 1 AND 262143"

    assert not migration._check_matches(actual, expected, "mysql")


def test_sqlite_does_not_normalize_mysql_byte_length_alias(migration: ModuleType) -> None:
    """Keep SQLite function names exact even when MySQL treats them as synonyms."""
    expected = migration._CHUNK_CHECKS["ck_od_temporal_identity_snapshot_chunks_chunk_bytes"]
    actual = "typeof(chunk_bytes) = 'blob' AND octet_length(chunk_bytes) BETWEEN 1 AND 262144"

    assert not migration._check_matches(actual, expected, "sqlite")


@pytest.mark.parametrize(
    ("actual", "expected_name"),
    [
        (
            "CHAR_LENGTH(authorıty_id) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(authorıty_id) = CHAR_LENGTH(authorıty_id) AND "
            "LOCATE(CHAR(0), authorıty_id) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authorıty_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authorıty_id`) = CHAR_LENGTH(`authorıty_id`) AND "
            "LOCATE(CHAR(0), `authorıty_id`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`regiſtry_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`regiſtry_id`) = CHAR_LENGTH(`regiſtry_id`) AND "
            "LOCATE(CHAR(0), `regiſtry_id`) = 0",
            "ck_od_temporal_identity_snapshots_registry_id",
        ),
    ],
)
def test_mysql_check_match_rejects_unicode_casefold_aliases(
    migration: ModuleType,
    actual: str,
    expected_name: str,
) -> None:
    """Reject Unicode lookalikes that casefold to a reviewed ASCII identifier."""
    expected = migration._MYSQL_HEADER_CHECKS[expected_name]

    assert not migration._check_matches(actual, expected, "mysql")


@pytest.mark.parametrize(
    ("actual", "expected_name"),
    [
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 127 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 OR "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`)",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`registry_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`registry_id`) = CHAR_LENGTH(`registry_id`) AND "
            "LOCATE(CHAR(0), `registry_id`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) = 0 /* ignored */",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) = 1",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) != 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            'CHAR_LENGTH("authority_id") BETWEEN 1 AND 128 AND '
            'OCTET_LENGTH("authority_id") = CHAR_LENGTH("authority_id") AND '
            'LOCATE(CHAR(0), "authority_id") = 0',
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(UNHEX('00'), `authority_id`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0, 1), `authority_id`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) <= 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) <> 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) =",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`authority_id`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`authority_id`) = CHAR_LENGTH(`authority_id`) AND "
            "LOCATE(CHAR(0), `authority_id`) = 0 AND",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
        (
            "CHAR_LENGTH(`length`) BETWEEN 1 AND 128 AND "
            "OCTET_LENGTH(`length`) = CHAR_LENGTH(`length`) AND "
            "LOCATE(CHAR(0), `length`) = 0",
            "ck_od_temporal_identity_snapshots_authority_id",
        ),
    ],
)
def test_mysql_check_match_rejects_changed_or_unsupported_expressions(
    migration: ModuleType,
    actual: str,
    expected_name: str,
) -> None:
    """Do not normalize changed ranges, boolean logic, scope or SQL syntax."""
    expected = migration._MYSQL_HEADER_CHECKS[expected_name]

    assert not migration._check_matches(actual, expected, "mysql")


def test_check_match_rejects_unsupported_dialect(migration: ModuleType) -> None:
    """Reject dialects whose identifier and literal rules are not reviewed."""
    expected = migration._MYSQL_HEADER_CHECKS["ck_od_temporal_identity_snapshots_authority_id"]

    assert not migration._check_matches(expected, expected, "oracle")


@pytest.mark.parametrize(
    "actual",
    [
        "",
        "(",
        "authority_id",
        "= 0",
    ],
)
def test_check_match_fails_closed_for_incomplete_predicates(
    migration: ModuleType,
    actual: str,
) -> None:
    """Refuse empty, unterminated, operator-free, and scalar-free reflected CHECKs."""
    expected = migration._MYSQL_HEADER_CHECKS["ck_od_temporal_identity_snapshots_authority_id"]

    assert not migration._check_matches(actual, expected, "mysql")


def test_require_checks_rejects_a_missing_named_constraint(migration: ModuleType) -> None:
    """A correct remaining set cannot hide a missing required CHECK constraint."""
    expected = migration._HEADER_CHECKS

    class Inspector:
        def get_check_constraints(self, _table_name: str) -> list[dict[str, str]]:
            return [
                {"name": name, "sqltext": sql}
                for name, sql in expected.items()
                if name != "ck_od_temporal_identity_snapshots_chunk_count"
            ]

    with pytest.raises(RuntimeError, match="unexpected od_temporal_identity_snapshots CHECK set"):
        migration._require_checks(Inspector(), migration._HEADER, "sqlite")
