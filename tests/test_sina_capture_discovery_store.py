"""Bounded SQLite behavior tests for source-native publication capture discovery."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import create_engine, delete, insert, text, update
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.event import listen
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from opendata.pipeline import publication_store
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    CaptureDiscoveryConsistencyError,
    CaptureDiscoveryCursorError,
    CaptureDiscoveryLimitError,
    CaptureDiscoveryPage,
    CaptureDiscoveryRecordIntegrityError,
    PublicationContractConflictError,
    PublicationRecord,
    PublicationStore,
    PublishedRecordInput,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Connection

CURSOR_KEY = b"test-only-sina-capture-discovery-key-32"
CONTRACT_SHA256 = "a" * 64
INSTRUMENT_ID = "600000.SH"
OBSERVED_AT = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PublicationStore]:
    """Bind publication storage to a fresh SQLite file below pytest's temp root."""
    engine = create_engine(f"sqlite:///{tmp_path / 'capture-discovery.sqlite'}")
    PUBLICATION_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id", "publication_id", "instrument_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id")'
        )
    try:
        yield PublicationStore(engine, cursor_signing_key=CURSOR_KEY)
    finally:
        engine.dispose()


class _FakeMetadataResult:
    """Minimal SQLAlchemy result surface for exact MySQL index metadata tests."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMetadataResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _FakeDialect:
    """Name-only dialect fixture for the pure index guard dispatch."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeIndexMetadataConnection:
    """Return pinned index statistics without opening any external connection."""

    def __init__(
        self,
        rows: list[dict[str, object]],
        column_rows: list[dict[str, object]] | None = None,
        dialect_name: str = "mysql",
    ) -> None:
        self.dialect = _FakeDialect(dialect_name)
        self.rows = rows
        self.column_rows = (
            _valid_mysql_identity_column_rows() if column_rows is None else column_rows
        )
        self.statements: list[str] = []
        self.parameters: list[object] = []

    def execute(
        self,
        statement: object,
        parameters: object = None,
    ) -> _FakeMetadataResult:
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(parameters)
        if "information_schema.STATISTICS" in sql:
            return _FakeMetadataResult(self.rows)
        raise AssertionError("unexpected execute metadata query")

    def exec_driver_sql(
        self,
        statement: str,
        parameters: object = None,
    ) -> _FakeMetadataResult:
        self.statements.append(statement)
        self.parameters.append(parameters)
        if "information_schema.COLUMNS" in statement:
            return _FakeMetadataResult(self.column_rows)
        raise AssertionError("unexpected metadata query")


def _valid_mysql_discovery_index_rows() -> list[dict[str, object]]:
    """Return the exact full-column 0017 index statistics shape."""
    return [
        {
            "INDEX_NAME": "ix_od_fact_capture_discovery",
            "NON_UNIQUE": 1,
            "SEQ_IN_INDEX": position,
            "COLUMN_NAME": column_name,
            "SUB_PART": None,
            "INDEX_TYPE": "BTREE",
            "IS_VISIBLE": "YES",
            "COLLATION": "A",
        }
        for position, column_name in enumerate(
            (
                "dataset_id",
                "publication_id",
                "instrument_id",
                "publication_sequence",
                "is_deleted",
                "observed_at",
                "capture_id",
            ),
            start=1,
        )
    ]


def _valid_mysql_identity_column_rows() -> list[dict[str, object]]:
    """Return migration 0017's binary identity column character metadata."""
    return [
        {
            "COLUMN_NAME": column_name,
            "CHARACTER_SET_NAME": "utf8mb4",
            "COLLATION_NAME": "utf8mb4_0900_bin",
        }
        for column_name in ("dataset_id", "instrument_id", "capture_id")
    ]


def publish_rows(
    store: PublicationStore,
    key: str,
    rows: list[PublishedRecordInput],
) -> PublicationRecord:
    """Create one deterministic synthetic publication with capture selectors."""
    return store.publish(
        dataset_id="sina_fzb_report_observations",
        source_id="akshare.sina",
        schema_version="2",
        semantic_version="2.0",
        unit="source_native",
        adjustment_basis="not_applicable",
        quality={"status": "passed"},
        provenance={"contract_sha256": CONTRACT_SHA256, "fixture": "synthetic"},
        idempotency_key=key,
        records=rows,
    )


def discover_captures(
    store: PublicationStore,
    publication: PublicationRecord,
    *,
    instrument_id: str,
    page_size: int = 100,
    cursor: str | None = None,
    access_scope: str = "default",
) -> CaptureDiscoveryPage:
    """Call discovery with the preflight publication and reviewed fixture pin."""
    return store.list_capture_discovery(
        publication.dataset_id,
        publication.publication_id,
        expected_publication=publication,
        expected_contract_sha256=CONTRACT_SHA256,
        instrument_id=instrument_id,
        page_size=page_size,
        cursor=cursor,
        access_scope=access_scope,
    )


def capture_row(
    capture_id: str,
    row_number: int,
    *,
    observed_at: datetime | None = OBSERVED_AT,
    instrument_id: str = INSTRUMENT_ID,
) -> PublishedRecordInput:
    """Build one synthetic row whose source observation metadata is explicit."""
    return PublishedRecordInput(
        business_key=f"2026:{capture_id[:8]}:{row_number}",
        payload={
            "instrument_id": instrument_id,
            "capture_id": capture_id,
            "report_period": f"2026-Q{row_number}",
        },
        observed_at=observed_at,
        instrument_id=instrument_id,
        capture_id=capture_id,
    )


def test_discovery_deduplicates_capture_and_pages_by_source_time_then_id(
    store: PublicationStore,
) -> None:
    """Duplicate rows collapse by capture while preserving deterministic order."""
    capture_a = "a" * 64
    capture_b = "b" * 64
    capture_c = "c" * 64
    later = OBSERVED_AT + timedelta(minutes=1)
    publication = publish_rows(
        store,
        "captures-page-order",
        [
            capture_row(capture_a, 1, observed_at=OBSERVED_AT),
            capture_row(capture_a, 2, observed_at=OBSERVED_AT),
            capture_row(capture_b, 1, observed_at=later),
            capture_row(capture_c, 1, observed_at=later),
        ],
    )

    first = discover_captures(
        store,
        publication,
        instrument_id=INSTRUMENT_ID,
        page_size=2,
        access_scope="reader-1",
    )
    assert [(item.capture_id, item.observed_at) for item in first.items] == [
        (capture_b, later),
        (capture_c, later),
    ]
    assert first.next_cursor is not None

    second = discover_captures(
        store,
        publication,
        instrument_id=INSTRUMENT_ID,
        page_size=2,
        cursor=first.next_cursor,
        access_scope="reader-1",
    )
    assert [(item.capture_id, item.observed_at) for item in second.items] == [
        (capture_a, OBSERVED_AT)
    ]
    assert second.next_cursor is None


def test_discovery_is_scoped_to_rows_written_by_the_exact_publication(
    store: PublicationStore,
) -> None:
    """A later pin lists its batch only and does not claim historical completeness."""
    old_capture = "1" * 64
    new_capture = "2" * 64
    first = publish_rows(store, "capture-old", [capture_row(old_capture, 1)])
    second = publish_rows(store, "capture-new", [capture_row(new_capture, 1)])

    page = discover_captures(
        store,
        second,
        instrument_id=INSTRUMENT_ID,
    )

    assert [item.capture_id for item in page.items] == [new_capture]
    assert page.items[0].observed_at == OBSERVED_AT
    assert first.publication_id != second.publication_id


def test_discovery_empty_instrument_is_not_missing_publication(store: PublicationStore) -> None:
    """An instrument absent from a batch yields an empty page, not no publication."""
    capture_id = "3" * 64
    publication = publish_rows(
        store,
        "capture-other-instrument",
        [capture_row(capture_id, 1, instrument_id="000001.SZ")],
    )

    page = discover_captures(
        store,
        publication,
        instrument_id=INSTRUMENT_ID,
    )

    assert page.items == ()
    assert page.next_cursor is None


def test_discovery_fails_if_pinned_publication_disappears(store: PublicationStore) -> None:
    """A preflight pin removed before the store read is a contract conflict."""
    publication = publish_rows(
        store,
        "capture-publication-disappeared",
        [capture_row("3" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.execute(
            delete(PUBLICATIONS).where(PUBLICATIONS.c.publication_id == publication.publication_id)
        )
    with pytest.raises(PublicationContractConflictError, match="disappeared"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("source_id", "another.source"),
        ("schema_version", "3"),
        ("semantic_version", "3.0"),
        ("adjustment_basis", "different"),
        ("payload_hash", "b" * 64),
        (
            "provenance_json",
            json.dumps(
                {"contract_sha256": "c" * 64, "fixture": "synthetic"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    ],
)
def test_discovery_revalidates_preflight_publication_and_contract(
    store: PublicationStore,
    field_name: str,
    replacement: str,
) -> None:
    """Source, schema, or contract metadata drift fails before reading facts."""
    publication = publish_rows(
        store,
        f"capture-contract-drift-{field_name}",
        [capture_row("d" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.execute(
            update(PUBLICATIONS)
            .where(PUBLICATIONS.c.publication_id == publication.publication_id)
            .values({field_name: replacement})
        )

    with pytest.raises(PublicationContractConflictError, match="changed after preflight"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


def test_discovery_rejects_invalid_preflight_pin_arguments(store: PublicationStore) -> None:
    """The store requires a typed pin and its exact resolved contract fingerprint."""
    publication = publish_rows(
        store,
        "capture-invalid-preflight-pin",
        [capture_row("e" * 64, 1)],
    )
    with pytest.raises(TypeError, match="PublicationRecord"):
        store.list_capture_discovery(
            publication.dataset_id,
            publication.publication_id,
            expected_publication=cast("PublicationRecord", object()),
            expected_contract_sha256=CONTRACT_SHA256,
            instrument_id=INSTRUMENT_ID,
        )
    with pytest.raises(PublicationContractConflictError, match="pin is inconsistent"):
        store.list_capture_discovery(
            publication.dataset_id,
            publication.publication_id,
            expected_publication=publication,
            expected_contract_sha256="b" * 64,
            instrument_id=INSTRUMENT_ID,
        )


def test_discovery_rejects_invalid_instrument_and_page_size(
    store: PublicationStore,
) -> None:
    """Unbounded or malformed query selectors fail before any fact scan."""
    publication = publish_rows(
        store,
        "capture-invalid-request-selectors",
        [capture_row("f" * 64, 1)],
    )
    with pytest.raises(ValueError, match="instrument_id is invalid"):
        discover_captures(store, publication, instrument_id="600 000.SH")
    with pytest.raises(ValueError, match="page_size"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID, page_size=0)


def test_cursor_rejects_scope_and_page_contract_changes(store: PublicationStore) -> None:
    """A signed cursor cannot cross caller scope, page size, or instrument."""
    publication = publish_rows(
        store,
        "capture-cursor-scope",
        [capture_row("4" * 64, 1), capture_row("5" * 64, 2)],
    )
    first = discover_captures(
        store,
        publication,
        instrument_id=INSTRUMENT_ID,
        page_size=1,
        access_scope="reader-1",
    )
    assert first.next_cursor is not None
    tampered_cursor = first.next_cursor[:-1] + ("B" if first.next_cursor[-1] != "B" else "C")

    with pytest.raises(CaptureDiscoveryCursorError, match="request scope"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
            page_size=1,
            cursor=first.next_cursor,
            access_scope="reader-2",
        )
    with pytest.raises(CaptureDiscoveryCursorError, match="request scope"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
            page_size=2,
            cursor=first.next_cursor,
            access_scope="reader-1",
        )
    with pytest.raises(CaptureDiscoveryCursorError, match="request scope"):
        discover_captures(
            store,
            publication,
            instrument_id="000001.SZ",
            page_size=1,
            cursor=first.next_cursor,
            access_scope="reader-1",
        )
    other_publication = publish_rows(
        store,
        "capture-cursor-other-publication",
        [capture_row("d" * 64, 1)],
    )
    with pytest.raises(CaptureDiscoveryCursorError, match="request scope"):
        discover_captures(
            store,
            other_publication,
            instrument_id=INSTRUMENT_ID,
            page_size=1,
            cursor=first.next_cursor,
            access_scope="reader-1",
        )
    with pytest.raises(CaptureDiscoveryCursorError, match="signature"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
            page_size=1,
            cursor=tampered_cursor,
            access_scope="reader-1",
        )


def test_discovery_rejects_malformed_and_expired_cursors(
    store: PublicationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor parse and expiry failures never fall through to a fresh page."""
    publication = publish_rows(
        store,
        "capture-cursor-expiry",
        [capture_row("1" * 64, 1), capture_row("2" * 64, 2)],
    )
    with pytest.raises(CaptureDiscoveryCursorError, match="malformed"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
            cursor="not-a-cursor",
        )

    monkeypatch.setattr(
        publication_store,
        "CAPTURE_DISCOVERY_CURSOR_TTL",
        timedelta(seconds=-1),
    )
    first = discover_captures(
        store,
        publication,
        instrument_id=INSTRUMENT_ID,
        page_size=1,
    )
    assert first.next_cursor is not None
    with pytest.raises(CaptureDiscoveryCursorError, match="expired"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
            page_size=1,
            cursor=first.next_cursor,
        )


@pytest.mark.parametrize(
    "rows",
    [
        [capture_row("6" * 64, 1, observed_at=None)],
        [
            capture_row("7" * 64, 1, observed_at=OBSERVED_AT),
            capture_row("7" * 64, 2, observed_at=OBSERVED_AT + timedelta(seconds=1)),
        ],
    ],
)
def test_discovery_rejects_missing_or_inconsistent_observation_time(
    store: PublicationStore,
    rows: list[PublishedRecordInput],
) -> None:
    """The store never invents one observation time from malformed capture rows."""
    publication = publish_rows(store, f"capture-invalid-{len(rows)}", rows)

    with pytest.raises(CaptureDiscoveryRecordIntegrityError):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
        )


@pytest.mark.parametrize(
    "stored_timestamp",
    [
        OBSERVED_AT.isoformat(),
        (OBSERVED_AT + timedelta(hours=8)).isoformat(),
    ],
)
def test_discovery_rejects_noncanonical_utc_timestamp_in_storage(
    store: PublicationStore,
    stored_timestamp: str,
) -> None:
    """A noncanonical or non-UTC string does not replace canonical UTC."""
    publication = publish_rows(
        store,
        "capture-invalid-time-encoding",
        [capture_row("8" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.execute(
            update(PUBLISHED_FACT_VERSIONS)
            .where(PUBLISHED_FACT_VERSIONS.c.publication_id == publication.publication_id)
            .values(observed_at=stored_timestamp)
        )

    with pytest.raises(CaptureDiscoveryRecordIntegrityError, match="timestamp is invalid"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
        )


def test_discovery_rejects_invalid_stored_capture_selector(store: PublicationStore) -> None:
    """A malformed persisted capture identifier cannot become a candidate."""
    publication = publish_rows(
        store,
        "capture-invalid-selector-storage",
        [capture_row("9" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.execute(
            update(PUBLISHED_FACT_VERSIONS)
            .where(PUBLISHED_FACT_VERSIONS.c.publication_id == publication.publication_id)
            .values(capture_id="invalid")
        )

    with pytest.raises(CaptureDiscoveryRecordIntegrityError, match="selector is invalid"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


@pytest.mark.parametrize(
    ("row_limit", "error_type", "message"),
    [
        (1, CaptureDiscoveryLimitError, "row budget"),
        (10, CaptureDiscoveryRecordIntegrityError, "stored publication row count"),
    ],
)
def test_discovery_rejects_injected_rows_beyond_its_publication_bounds(
    store: PublicationStore,
    monkeypatch: pytest.MonkeyPatch,
    row_limit: int,
    error_type: type[ValueError],
    message: str,
) -> None:
    """Fetched rows are bounded and cannot exceed their pinned publication count."""
    publication = publish_rows(
        store,
        f"capture-injected-row-{row_limit}",
        [capture_row("a" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.execute(
            insert(PUBLISHED_FACT_VERSIONS).values(
                dataset_id=publication.dataset_id,
                business_key=f"capture-injected:{row_limit}",
                revision=1,
                publication_id=publication.publication_id,
                publication_sequence=publication.sequence,
                instrument_id=INSTRUMENT_ID,
                capture_id="b" * 64,
                business_date=None,
                event_at=None,
                observed_at=OBSERVED_AT.isoformat(),
                source_published_at=None,
                available_at=None,
                record_json="{}",
                record_hash="0" * 64,
                is_deleted=False,
            )
        )
    monkeypatch.setattr(publication_store, "MAX_CAPTURE_DISCOVERY_ROWS", row_limit)

    with pytest.raises(error_type, match=message):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


def test_discovery_rejects_malformed_publication_metadata(store: PublicationStore) -> None:
    """Malformed persisted provenance fails closed instead of bypassing pin checks."""
    publication = publish_rows(
        store,
        "capture-malformed-publication-metadata",
        [capture_row("c" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.execute(
            update(PUBLICATIONS)
            .where(PUBLICATIONS.c.publication_id == publication.publication_id)
            .values(provenance_json="{malformed")
        )

    with pytest.raises(
        CaptureDiscoveryRecordIntegrityError,
        match="publication metadata could not be verified",
    ):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


def test_discovery_maps_fact_query_database_failure_to_consistency_error(
    store: PublicationStore,
) -> None:
    """A SQL failure during capture retrieval is not returned as an empty page."""
    publication = publish_rows(
        store,
        "capture-fact-query-failure",
        [capture_row("d" * 64, 1)],
    )

    def fail_fact_query(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """Inject a SQLAlchemy failure only at the capture fact query."""
        del connection, cursor, parameters, context, executemany
        normalized_statement = statement.lstrip().upper()
        if (
            normalized_statement.startswith("SELECT")
            and "FROM OD_PUBLISHED_FACT_VERSIONS" in normalized_statement
        ):
            raise OperationalError(statement, {}, RuntimeError("synthetic read failure"))

    listen(store._engine, "before_cursor_execute", fail_fact_query)
    with pytest.raises(CaptureDiscoveryConsistencyError, match="read consistently"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


def test_discovery_rejects_sqlite_dirty_read_connections(tmp_path: Path) -> None:
    """Capture discovery requires an explicit SQLite snapshot without dirty reads."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture-dirty-read.sqlite'}",
        poolclass=NullPool,
    )

    def enable_dirty_reads(dbapi_connection: object, _: object) -> None:
        """Configure only this test-owned SQLite connection before use."""
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA read_uncommitted=1")
        cursor.close()

    listen(engine, "connect", enable_dirty_reads)
    PUBLICATION_METADATA.create_all(engine)
    dirty_store = PublicationStore(engine, cursor_signing_key=CURSOR_KEY)
    try:
        publication = publish_rows(
            dirty_store,
            "capture-dirty-read",
            [capture_row("e" * 64, 1)],
        )
        with pytest.raises(
            CaptureDiscoveryConsistencyError,
            match="stable capture discovery snapshot",
        ):
            discover_captures(dirty_store, publication, instrument_id=INSTRUMENT_ID)
    finally:
        engine.dispose()


def test_discovery_fails_instead_of_returning_a_partial_over_budget_publication(
    store: PublicationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured work cap rejects the full batch before any page is returned."""
    publication = publish_rows(
        store,
        "capture-over-budget",
        [capture_row("9" * 64, 1), capture_row("a" * 64, 2)],
    )
    monkeypatch.setattr(publication_store, "MAX_CAPTURE_DISCOVERY_ROWS", 1)

    with pytest.raises(CaptureDiscoveryLimitError, match="row budget"):
        discover_captures(
            store,
            publication,
            instrument_id=INSTRUMENT_ID,
        )


def test_discovery_fails_closed_when_sqlite_0017_index_is_missing(
    store: PublicationStore,
) -> None:
    """The index guard runs before reading publication facts when the index is absent."""
    publication = publish_rows(
        store,
        "capture-index-missing",
        [capture_row("b" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.exec_driver_sql('DROP INDEX "ix_od_fact_capture_discovery"')

    with pytest.raises(CaptureDiscoveryConsistencyError, match="SQLite index shape"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


@pytest.mark.parametrize(
    "replacement_ddl",
    [
        (
            'CREATE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id", "instrument_id", "publication_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id")'
        ),
        (
            'CREATE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id" COLLATE NOCASE, "publication_id", "instrument_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id")'
        ),
        (
            'CREATE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id" DESC, "publication_id", "instrument_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id")'
        ),
        (
            'CREATE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id", "publication_id", "instrument_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id") '
            'WHERE "is_deleted" = 0'
        ),
        (
            'CREATE UNIQUE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id", "publication_id", "instrument_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id")'
        ),
    ],
    ids=("wrong-order", "wrong-collation", "descending", "partial", "unique"),
)
def test_discovery_rejects_sqlite_index_name_with_wrong_effective_semantics(
    store: PublicationStore,
    replacement_ddl: str,
) -> None:
    """A matching name cannot hide wrong key order, comparison, or index semantics."""
    publication = publish_rows(
        store,
        "capture-index-shape",
        [capture_row("c" * 64, 1)],
    )
    with store._engine.begin() as connection:
        connection.exec_driver_sql('DROP INDEX "ix_od_fact_capture_discovery"')
        connection.exec_driver_sql(replacement_ddl)

    with pytest.raises(CaptureDiscoveryConsistencyError, match="SQLite index"):
        discover_captures(store, publication, instrument_id=INSTRUMENT_ID)


def test_mysql_index_guard_accepts_the_exact_visible_0017_key() -> None:
    """The MySQL metadata guard accepts all seven full ascending key columns."""
    connection = _FakeIndexMetadataConnection(_valid_mysql_discovery_index_rows())

    publication_store._require_capture_discovery_index(cast("Connection", connection))

    assert len(connection.statements) == 2
    assert "information_schema.STATISTICS" in connection.statements[0]
    assert "IS_VISIBLE" in connection.statements[0]
    assert connection.parameters[0] == {"index_name": "ix_od_fact_capture_discovery"}
    assert "information_schema.COLUMNS" in connection.statements[1]


def test_mysql_index_metadata_query_compiles_named_bind_for_mysql_dbapi() -> None:
    """The index guard's bind is compiled by SQLAlchemy for the MySQL DBAPI."""
    compiled = text(publication_store._MYSQL_CAPTURE_DISCOVERY_INDEX_METADATA_SQL).compile(
        dialect=mysql.dialect()
    )
    normalized_sql = " ".join(str(compiled).upper().split())

    assert "INDEX_NAME = %S" in normalized_sql
    assert set(compiled.params) == {"index_name"}


@pytest.mark.parametrize(
    ("position", "field", "replacement"),
    [
        (0, "CHARACTER_SET_NAME", "utf8mb3"),
        (1, "COLLATION_NAME", "utf8mb4_0900_ai_ci"),
        (2, "COLLATION_NAME", "utf8mb4_bin"),
    ],
    ids=("wrong-character-set", "case-insensitive-instrument", "wrong-capture-collation"),
)
def test_mysql_index_guard_rejects_identity_column_collation_drift(
    position: int,
    field: str,
    replacement: str,
) -> None:
    """Identity key equality retains the exact case-sensitive 0017 collation."""
    columns = _valid_mysql_identity_column_rows()
    columns[position][field] = replacement
    connection = _FakeIndexMetadataConnection(
        _valid_mysql_discovery_index_rows(),
        column_rows=columns,
    )

    with pytest.raises(CaptureDiscoveryConsistencyError, match="binary MySQL identity"):
        publication_store._require_capture_discovery_index(cast("Connection", connection))


def test_mysql_index_guard_rejects_missing_identity_column_metadata() -> None:
    """Missing information_schema column rows fail closed after valid index rows."""
    connection = _FakeIndexMetadataConnection(
        _valid_mysql_discovery_index_rows(),
        column_rows=[],
    )

    with pytest.raises(CaptureDiscoveryConsistencyError, match="binary MySQL identity"):
        publication_store._require_capture_discovery_index(cast("Connection", connection))


@pytest.mark.parametrize(
    ("position", "field", "replacement"),
    [
        (0, "COLUMN_NAME", "publication_id"),
        (4, "SUB_PART", 12),
        (3, "COLLATION", "D"),
        (2, "IS_VISIBLE", "NO"),
        (5, "INDEX_TYPE", "HASH"),
        (6, "NON_UNIQUE", 0),
        (1, "SEQ_IN_INDEX", 1),
    ],
    ids=(
        "wrong-order",
        "prefix",
        "descending",
        "invisible",
        "non-btree",
        "unique",
        "bad-position",
    ),
)
def test_mysql_index_guard_rejects_unreviewed_metadata(
    position: int,
    field: str,
    replacement: object,
) -> None:
    """Invisible, shortened, reordered, or non-BTREE keys fail closed."""
    rows = _valid_mysql_discovery_index_rows()
    rows[position][field] = replacement
    connection = _FakeIndexMetadataConnection(rows)

    with pytest.raises(CaptureDiscoveryConsistencyError, match="MySQL index shape"):
        publication_store._require_capture_discovery_index(cast("Connection", connection))


def test_mysql_index_guard_rejects_extra_key_parts() -> None:
    """An extra key part is rejected even when the expected prefix is present."""
    rows = _valid_mysql_discovery_index_rows()
    rows.append({**rows[-1], "SEQ_IN_INDEX": 8})
    connection = _FakeIndexMetadataConnection(rows)

    with pytest.raises(CaptureDiscoveryConsistencyError, match="MySQL index shape"):
        publication_store._require_capture_discovery_index(cast("Connection", connection))


def test_capture_discovery_rejects_unverified_dialects() -> None:
    """PostgreSQL's generic repeatable-read support cannot bypass the index contract."""
    connection = _FakeIndexMetadataConnection([], dialect_name="postgresql")

    with pytest.raises(CaptureDiscoveryConsistencyError, match="SQLite or MySQL index"):
        publication_store._require_capture_discovery_index(cast("Connection", connection))
    assert connection.statements == []


def test_capture_discovery_sql_forces_mysql_index_and_exact_equality_prefix() -> None:
    """Compiled SQL forces 0017 on MySQL and retains only the five indexed selectors."""
    statement = publication_store._capture_discovery_fact_statement(
        dataset="sina_fzb_report_observations",
        publication_key="a" * 32,
        sequence=42,
        instrument=INSTRUMENT_ID,
        dialect_name="mysql",
    )
    mysql_compiled = statement.compile(dialect=mysql.dialect())
    mysql_sql = str(mysql_compiled)
    normalized_mysql_sql = " ".join(mysql_sql.upper().split())
    where_clause = normalized_mysql_sql.split(" WHERE ", maxsplit=1)[1].split(
        " LIMIT ", maxsplit=1
    )[0]

    assert "FORCE INDEX (ix_od_fact_capture_discovery)" in mysql_sql
    assert publication_store.MAX_CAPTURE_DISCOVERY_ROWS + 1 in mysql_compiled.params.values()
    assert "OBSERVED_AT" not in where_clause
    for column_name in (
        "dataset_id",
        "publication_id",
        "instrument_id",
        "publication_sequence",
    ):
        assert re.search(rf"\b{column_name.upper()}\s*=\s*%S\b", where_clause)
    assert re.search(r"\bIS_DELETED\s+IS\s+FALSE\b", where_clause)

    sqlite_statement = publication_store._capture_discovery_fact_statement(
        dataset="sina_fzb_report_observations",
        publication_key="a" * 32,
        sequence=42,
        instrument=INSTRUMENT_ID,
        dialect_name="sqlite",
    )
    sqlite_compiled = sqlite_statement.compile(dialect=sqlite.dialect())
    sqlite_sql = str(sqlite_compiled)
    assert "FORCE INDEX" not in sqlite_sql
    assert publication_store.MAX_CAPTURE_DISCOVERY_ROWS + 1 in sqlite_compiled.params.values()
