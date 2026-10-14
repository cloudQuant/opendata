"""AC2-19 financial PIT storage, source time, coverage, and conflict tests."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError

from opendata.pipeline.dwd_merge import DwdWriter
from opendata.pipeline.financial_pit import (
    FinancialCoverageInput,
    FinancialPitConflict,
    FinancialPitCoverageGap,
    FinancialPitError,
    FinancialPitQueryTooBroad,
    FinancialPitUnsupported,
    FinancialVersionInput,
    parse_as_of,
    prepare_financial_version,
)
from opendata.services.financial_pit import (
    _insert_ignore,
    declare_financial_pit_coverage,
    ingest_financial_versions,
    ingest_financial_versions_in_transaction,
    query_financial_as_of,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


@pytest.fixture(scope="module")
def financial_registry():
    from opendata.data.providers.catalog import register_provider
    from opendata.data.registry import ProviderRegistry

    registry = ProviderRegistry()
    register_provider("ths", registry)
    register_provider("akshare", registry)
    return registry


@pytest.fixture(autouse=True)
def _use_registered_financial_sources(monkeypatch, financial_registry):
    monkeypatch.setattr("opendata.data.registry.get_registry", lambda: financial_registry)


@pytest.fixture
def pit_engine():
    engine = create_engine("sqlite:///:memory:")
    _upgrade_pit_schema(engine)
    yield engine
    engine.dispose()


def _upgrade_pit_schema(engine) -> None:
    path = Path(__file__).parents[1] / "alembic_data/versions/20261012-0008_financial_pit.py"
    spec = importlib.util.spec_from_file_location("financial_pit_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            module.upgrade()


def _statement(
    *,
    value: float = 10.0,
    revision: int = 1,
    symbol: str = "600000",
    item: str = "net_profit",
    announce_date: date = date(2024, 4, 1),
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "statement_type": "income",
        "report_period": date(2024, 3, 31),
        "announce_date": announce_date,
        "item": item,
        "value": value,
        "revision": revision,
    }


def _version(
    *,
    value: float = 10.0,
    revision: int = 1,
    source_revision: str | None = None,
    symbol: str = "600000",
    item: str = "net_profit",
    precision: str = "timestamp",
    published_at: datetime | None = datetime(2024, 4, 1, 12, tzinfo=timezone.utc),
    published_on: date | None = None,
    zone: str = "Asia/Shanghai",
    observed_at: datetime = datetime(2026, 10, 12, 1, tzinfo=timezone.utc),
) -> FinancialVersionInput:
    announce_date = (
        published_on
        if precision == "date"
        else (published_at.astimezone(ZoneInfo(zone)).date() if published_at else date(2024, 4, 1))
    )
    payload = _statement(
        value=value,
        revision=revision,
        symbol=symbol,
        item=item,
        announce_date=announce_date,
    )
    return FinancialVersionInput(
        domain="financial_statement",
        source="ths",
        market="CN",
        payload=payload,
        source_revision=source_revision or f"native-rev-{revision}",
        revision_order=revision,
        public_time_precision=precision,
        published_at=published_at if precision == "timestamp" else None,
        published_on=published_on if precision == "date" else None,
        source_timezone=zone,
        observed_at=observed_at,
        provenance="https://source.example/release/2024Q1",
    )


def _coverage(
    *,
    symbol: str = "600000",
    source: str = "ths",
    market: str = "CN",
    domain: str = "financial_statement",
    timezone_name: str = "Asia/Shanghai",
    report_start: date = date(2024, 3, 31),
    report_end: date = date(2024, 3, 31),
    public_from: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc),
    complete_through: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    evidence_uri: str = "https://source.example/history-completeness/2024Q1",
) -> FinancialCoverageInput:
    return FinancialCoverageInput(
        domain=domain,
        source=source,
        market=market,
        symbol=symbol,
        report_period_start=report_start,
        report_period_end=report_end,
        public_history_from=public_from,
        complete_through=complete_through,
        source_timezone=timezone_name,
        evidence_uri=evidence_uri,
    )


def _query(
    engine,
    *,
    as_of: str | datetime = "2024-04-02T00:00:00+00:00",
    source: str = "ths",
    market: str = "CN",
    symbols: tuple[str, ...] = ("600000",),
    fields: tuple[str, ...] = (
        "symbol",
        "statement_type",
        "report_period",
        "announce_date",
        "item",
        "value",
        "revision",
        "source",
        "_merged_at",
        "_diff_flag",
        "_as_of",
    ),
):
    return query_financial_as_of(
        engine,
        domain="financial_statement",
        source=source,
        market=market,
        symbols=symbols,
        report_period_start=date(2024, 3, 31),
        report_period_end=date(2024, 3, 31),
        as_of=as_of,
        selected_fields=fields,
    )


def _declare(engine, **kwargs) -> str:
    return declare_financial_pit_coverage(engine, _coverage(**kwargs))


class _FakeMysqlDriverError(Exception):
    def __init__(self, error_number: int) -> None:
        self.errno = error_number
        super().__init__(error_number, "simulated MySQL constraint error")


class _FakeSavepoint:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class _FakeMysqlResult:
    def __init__(self, row: dict[str, object] | None = None, rowcount: int = 0) -> None:
        self.row = row
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self.row


class _FakeMysqlRaceConnection:
    """Exercise MySQL driver and current-read branches without a DB connection."""

    def __init__(self, winner: dict[str, object]) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self.winner = winner
        self.statements: list[str] = []
        self.savepoints: list[_FakeSavepoint] = []
        self.version_insert_raised = False

    def begin_nested(self) -> _FakeSavepoint:
        savepoint = _FakeSavepoint()
        self.savepoints.append(savepoint)
        return savepoint

    def execute(self, statement, params):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("SELECT"):
            return _FakeMysqlResult(self.winner if "FOR UPDATE" in sql else None)
        if "financial_pit_versions" in sql and not self.version_insert_raised:
            self.version_insert_raised = True
            raise IntegrityError(
                sql,
                params,
                _FakeMysqlDriverError(1062),
            )
        return _FakeMysqlResult(rowcount=0)


def test_two_publications_restatement_and_replay_are_asof_visible(pit_engine):
    _declare(pit_engine)
    first = _version(value=10.0, revision=1)
    second = _version(
        value=11.0,
        revision=2,
        published_at=datetime(2024, 4, 3, 9, tzinfo=timezone.utc),
        observed_at=datetime(2026, 10, 12, 2, tzinfo=timezone.utc),
    )

    assert ingest_financial_versions(pit_engine, [first]).inserted == 1
    assert ingest_financial_versions(pit_engine, [first]).idempotent == 1
    assert ingest_financial_versions(pit_engine, [second]).inserted == 1

    before_publication = _query(pit_engine, as_of="2024-04-01T11:59:59+00:00")
    after_first = _query(pit_engine, as_of="2024-04-02T00:00:00+00:00")
    before_restatement = _query(pit_engine, as_of="2024-04-03T08:59:59+00:00")
    after_restatement = _query(pit_engine, as_of="2024-04-04T00:00:00+00:00")

    assert before_publication["rows"] == []
    assert after_first["rows"][0]["value"] == 10.0
    assert before_restatement["rows"][0]["revision"] == 1
    assert after_restatement["rows"][0]["revision"] == 2
    assert after_restatement["rows"][0]["value"] == 11.0
    assert after_restatement["rows"][0]["_as_of"] is None
    assert after_restatement["pit"]["as_of_precision"] == "timestamp"


def test_date_precision_is_visible_from_next_source_local_midnight(pit_engine):
    _declare(pit_engine, timezone_name="America/New_York")
    date_record = _version(
        value=12.0,
        precision="date",
        published_at=None,
        published_on=date(2024, 2, 10),
        zone="America/New_York",
    )
    # Align the statement period to this row's period, which remains 2024-03-31.
    ingest_financial_versions(pit_engine, [date_record])

    last_microsecond = _query(pit_engine, as_of="2024-02-11T04:59:59.999999+00:00")
    next_local_midnight = _query(pit_engine, as_of="2024-02-11T05:00:00+00:00")
    publication_day = _query(pit_engine, as_of="2024-02-10")
    following_day = _query(pit_engine, as_of="2024-02-11")

    assert last_microsecond["rows"] == []
    assert next_local_midnight["rows"][0]["value"] == 12.0
    assert publication_day["rows"] == []
    assert following_day["rows"][0]["value"] == 12.0


def test_unknown_publication_time_is_stored_but_never_claimed_as_pit(pit_engine):
    _declare(pit_engine)
    unknown = _version(
        precision="unknown",
        published_at=None,
        published_on=None,
    )
    result = ingest_financial_versions(pit_engine, [unknown])
    assert result.inserted == 1

    with pytest.raises(
        FinancialPitUnsupported,
        match="PIT_UNSUPPORTED.*lack a proven public timestamp",
    ):
        _query(pit_engine)


def test_missing_coverage_is_unsupported_and_wrong_source_or_market_never_crosses(pit_engine):
    ingest_financial_versions(pit_engine, [_version()])
    with pytest.raises(FinancialPitCoverageGap) as missing:
        _query(pit_engine)
    assert missing.value.code == "PIT_COVERAGE_GAP"

    _declare(pit_engine)
    with pytest.raises(FinancialPitCoverageGap):
        _query(pit_engine, source="akshare")
    with pytest.raises(FinancialPitUnsupported, match="not a registered capability"):
        _query(pit_engine, market="US")
    with pytest.raises(FinancialPitCoverageGap):
        _query(pit_engine, symbols=("600001",))


def test_coverage_candidate_scan_has_a_fixed_bound(pit_engine):
    from opendata.pipeline.financial_pit import prepare_financial_coverage

    with pit_engine.begin() as connection:
        for index in range(65):
            values = prepare_financial_coverage(
                _coverage(
                    public_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    complete_through=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    evidence_uri=(
                        f"https://source.example/history-completeness/2024Q1?entry={index}"
                    ),
                )
            )
            connection.execute(
                text(
                    "INSERT INTO financial_pit_coverage (coverage_id, domain, source, market, "
                    "symbol, report_period_start, report_period_end, public_history_from_utc, "
                    "complete_through_utc, source_timezone, evidence_uri, evidence_digest) "
                    "VALUES (:coverage_id, :domain, :source, :market, :symbol, "
                    ":report_period_start, :report_period_end, :public_history_from_utc, "
                    ":complete_through_utc, :source_timezone, :evidence_uri, :evidence_digest)"
                ),
                values,
            )
    with pytest.raises(FinancialPitQueryTooBroad, match="coverage candidates exceed 64"):
        _query(pit_engine)


def test_same_revision_conflict_commits_evidence_and_blocks_pit(pit_engine):
    _declare(pit_engine)
    first = _version(value=10.0, revision=1)
    conflicting = _version(value=999.0, revision=1)
    assert ingest_financial_versions(pit_engine, [first]).inserted == 1
    result = ingest_financial_versions(pit_engine, [conflicting])

    assert len(result.conflicts) == 1
    with pit_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT COUNT(*) FROM financial_pit_conflicts")
        ).scalar_one()
        versions = connection.execute(
            text("SELECT payload_digest FROM financial_pit_versions")
        ).all()
    assert stored == 1
    assert len(versions) == 1
    repeated = ingest_financial_versions(pit_engine, [conflicting])
    assert len(repeated.conflicts) == 1
    with pit_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM financial_pit_conflicts")).scalar_one()
            == 1
        )
    with pytest.raises(FinancialPitConflict, match="persisted content conflicts"):
        _query(pit_engine)


@pytest.mark.parametrize("incoming_value", [10.0, 11.0], ids=("same-payload", "different-payload"))
def test_mysql_duplicate_race_uses_current_read_and_classifies_winner(
    incoming_value,
):
    from opendata.pipeline.financial_pit import prepare_financial_version

    winner = prepare_financial_version(_version(value=10.0)).values
    connection = _FakeMysqlRaceConnection(
        {
            "payload_digest": winner["payload_digest"],
            "payload_json": winner["payload_json"],
            "provenance": winner["provenance"],
        }
    )

    result = ingest_financial_versions_in_transaction(
        connection,
        [_version(value=incoming_value)],
    )

    assert any(
        "FROM financial_pit_versions WHERE version_id = :version_id FOR UPDATE" in sql
        for sql in connection.statements
    )
    assert all("ON DUPLICATE KEY UPDATE" not in sql for sql in connection.statements)
    assert connection.savepoints[0].rolled_back is True
    if incoming_value == 10.0:
        assert result.inserted == 0
        assert result.idempotent == 1
        assert result.conflicts == ()
        assert len(connection.savepoints) == 1
    else:
        assert result.inserted == 0
        assert result.idempotent == 0
        assert result.conflicts == (str(winner["version_id"]),)
        assert connection.savepoints[1].committed is True


class _NoDatabaseInteraction:
    """Fail immediately if identifier validation touches a connection."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected database interaction: {name}")


@pytest.mark.parametrize(
    "conflict_column",
    ["conflict_id) VALUES ('attacker'); --", "missing_column", ""],
    ids=["sql-injection-text", "not-in-insert-columns", "empty"],
)
def test_insert_ignore_rejects_invalid_conflict_column_before_database_access(
    conflict_column: str,
) -> None:
    with pytest.raises(RuntimeError, match="internal PIT SQL conflict identifier is invalid"):
        _insert_ignore(
            cast("Connection", _NoDatabaseInteraction()),
            "financial_pit_versions",
            "version_id, payload_digest",
            {"version_id": "v1", "payload_digest": "digest"},
            conflict_column=conflict_column,
        )


def test_mysql_plain_insert_uses_savepoint_and_ignores_rowcount_for_success():
    class FakeConnection:
        dialect = SimpleNamespace(name="mysql")

        def __init__(self) -> None:
            self.savepoint = _FakeSavepoint()
            self.statement = ""

        def begin_nested(self):
            return self.savepoint

        def execute(self, statement, params):
            self.statement = str(statement)
            return _FakeMysqlResult(rowcount=0)

    connection = FakeConnection()
    result = _insert_ignore(
        connection,
        "financial_pit_versions",
        "version_id, payload_digest",
        {"version_id": "version", "payload_digest": "digest"},
        conflict_column="version_id",
    )

    assert result is True
    assert connection.savepoint.committed is True
    assert "ON DUPLICATE KEY UPDATE" not in connection.statement
    assert connection.statement.startswith("INSERT INTO financial_pit_versions")


def test_mysql_non_duplicate_integrity_error_is_rolled_back_and_propagated():
    class FakeConnection:
        dialect = SimpleNamespace(name="mysql")

        def __init__(self) -> None:
            self.savepoint = _FakeSavepoint()

        def begin_nested(self):
            return self.savepoint

        def execute(self, statement, params):
            raise IntegrityError(
                str(statement),
                params,
                _FakeMysqlDriverError(1452),
            )

    connection = FakeConnection()
    with pytest.raises(IntegrityError):
        _insert_ignore(
            connection,
            "financial_pit_versions",
            "version_id, payload_digest",
            {"version_id": "version", "payload_digest": "digest"},
            conflict_column="version_id",
        )
    assert connection.savepoint.rolled_back is True


def test_future_revision_conflict_does_not_poison_earlier_asof(pit_engine):
    _declare(pit_engine)
    first = _version(
        value=10.0,
        revision=1,
        source_revision="release-1",
        published_at=datetime(2024, 4, 1, 12, tzinfo=timezone.utc),
    )
    future = _version(
        value=20.0,
        revision=2,
        source_revision="release-2",
        published_at=datetime(2024, 4, 5, 12, tzinfo=timezone.utc),
    )
    future_conflict = _version(
        value=21.0,
        revision=2,
        source_revision="release-2",
        published_at=datetime(2024, 4, 5, 12, tzinfo=timezone.utc),
    )
    ingest_financial_versions(pit_engine, [first, future])
    result = ingest_financial_versions(pit_engine, [future_conflict])

    assert len(result.conflicts) == 1
    early = _query(pit_engine, as_of="2024-04-02T00:00:00+00:00")
    assert early["rows"][0]["value"] == 10.0
    with pytest.raises(FinancialPitConflict, match="persisted content conflicts"):
        _query(pit_engine, as_of="2024-04-06T00:00:00+00:00")


def test_late_arrival_is_selected_by_public_time_not_observation_order(pit_engine):
    _declare(pit_engine)
    later_release = _version(
        value=20,
        revision=2,
        source_revision="rev2",
        published_at=datetime(2024, 4, 4, tzinfo=timezone.utc),
        observed_at=datetime(2026, 10, 12, 1, tzinfo=timezone.utc),
    )
    late_old_release = _version(
        value=10,
        revision=1,
        source_revision="rev1",
        published_at=datetime(2024, 4, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 10, 13, 1, tzinfo=timezone.utc),
    )
    ingest_financial_versions(pit_engine, [later_release])
    ingest_financial_versions(pit_engine, [late_old_release])

    old_view = _query(pit_engine, as_of="2024-04-02T00:00:00+00:00")
    latest_view = _query(pit_engine, as_of="2024-04-05T00:00:00+00:00")
    assert old_view["rows"][0]["revision"] == 1
    assert latest_view["rows"][0]["revision"] == 2
    assert latest_view["rows"][0]["value"] == 20


def test_business_key_source_and_market_are_part_of_version_identity(pit_engine):
    _declare(pit_engine)
    original = _version(value=10, revision=1)
    other_source = FinancialVersionInput(
        **{**original.__dict__, "source": "akshare", "source_revision": "native-rev-1"}
    )
    other_market = FinancialVersionInput(
        **{**original.__dict__, "market": "US", "source_revision": "native-rev-1"}
    )
    other_symbol = FinancialVersionInput(
        **{
            **original.__dict__,
            "payload": _statement(symbol="600001"),
            "source_revision": "native-rev-1",
        }
    )
    other_item = FinancialVersionInput(
        **{
            **original.__dict__,
            "payload": _statement(item="revenue"),
            "source_revision": "native-rev-1",
        }
    )
    _declare(pit_engine, source="akshare")
    other_source = FinancialVersionInput(
        **{**other_source.__dict__, "payload": _statement(value=12)}
    )
    result = ingest_financial_versions(
        pit_engine, [original, other_source, other_symbol, other_item]
    )
    assert result.inserted == 4
    version_ids = {
        prepare_financial_version(version).values["version_id"]
        for version in (original, other_source, other_market, other_symbol, other_item)
    }
    assert len(version_ids) == 5
    assert _query(pit_engine)["rows"][0]["value"] == 10
    assert _query(pit_engine, source="akshare")["rows"][0]["value"] == 12
    with pytest.raises(FinancialPitUnsupported, match="not a registered capability"):
        ingest_financial_versions(pit_engine, [other_market])


def test_parser_requires_timezone_for_timestamp_but_preserves_date_precision():
    assert parse_as_of("2024-04-01").kind == "date"
    assert parse_as_of("2024-04-01T12:00:00+08:00").kind == "timestamp"
    with pytest.raises(FinancialPitError, match="timezone offset"):
        parse_as_of("2024-04-01T12:00:00")


def test_dwd_writer_optional_pit_hook_is_same_transaction_and_conflict_skips_latest(
    pit_engine, monkeypatch
):
    metadata = MetaData()
    Table(
        "dwd_financial_statement",
        metadata,
        Column("symbol", String(128), primary_key=True),
        Column("statement_type", String(32), primary_key=True),
        Column("report_period", Date, primary_key=True),
        Column("announce_date", Date),
        Column("item", String(128), primary_key=True),
        Column("value", Float),
        Column("revision", Integer),
        Column("source", String(64)),
        Column("_merged_at", DateTime),
        Column("_diff_flag", Integer),
        Column("_as_of", Date),
    )
    metadata.create_all(pit_engine)
    monkeypatch.setattr(
        "opendata.pipeline.dwd_merge.build_upsert_sql",
        lambda name, columns, key: (
            f"INSERT OR REPLACE INTO {name} ({', '.join(columns)}) VALUES "  # noqa: S608
            f"({', '.join(':' + column for column in columns)})"
        ),
    )
    writer = DwdWriter(pit_engine)
    frame = pd.DataFrame(
        [
            {
                **_statement(),
                "source": "ths",
                "_merged_at": datetime(2026, 10, 12, 1),
                "_diff_flag": 0,
                "_as_of": date(2024, 3, 31),
            }
        ]
    )
    assert (
        writer.write(
            frame,
            table="dwd_financial_statement",
            key=("symbol", "statement_type", "report_period", "item"),
            financial_versions=[_version()],
        )
        == 1
    )
    assert (
        writer.write(
            frame,
            table="dwd_financial_statement",
            key=("symbol", "statement_type", "report_period", "item"),
            financial_versions=[_version()],
        )
        == 1
    )

    conflicted_frame = frame.copy()
    conflicted_frame.loc[0, "value"] = 999.0
    with pytest.raises(FinancialPitConflict, match="conflict persisted"):
        writer.write(
            conflicted_frame,
            table="dwd_financial_statement",
            key=("symbol", "statement_type", "report_period", "item"),
            financial_versions=[_version(value=999.0)],
        )
    with pit_engine.connect() as connection:
        dwd_value = connection.execute(
            text("SELECT value FROM dwd_financial_statement")
        ).scalar_one()
        conflict_count = connection.execute(
            text("SELECT COUNT(*) FROM financial_pit_conflicts")
        ).scalar_one()
    assert dwd_value == 10.0
    assert conflict_count == 1


def test_dwd_writer_accepts_contract_subset_with_required_alignment_fields(pit_engine, monkeypatch):
    metadata = MetaData()
    Table(
        "dwd_financial_statement",
        metadata,
        Column("symbol", String(128), primary_key=True),
        Column("statement_type", String(32), primary_key=True),
        Column("report_period", Date, primary_key=True),
        Column("announce_date", Date),
        Column("item", String(128), primary_key=True),
        Column("value", Float),
        Column("revision", Integer),
        Column("source", String(64)),
    )
    metadata.create_all(pit_engine)
    monkeypatch.setattr(
        "opendata.pipeline.dwd_merge.build_upsert_sql",
        lambda name, columns, key: (
            f"INSERT OR REPLACE INTO {name} ({', '.join(columns)}) VALUES "  # noqa: S608
            f"({', '.join(':' + column for column in columns)})"
        ),
    )
    subset = pd.DataFrame(
        [
            {
                "symbol": "600056",
                "statement_type": "income",
                "report_period": date(2024, 3, 31),
                "item": "net_profit",
                "value": 10.0,
                "source": "ths",
            }
        ]
    )

    assert (
        DwdWriter(pit_engine).write(
            subset,
            table="dwd_financial_statement",
            key=("symbol", "statement_type", "report_period", "item"),
            financial_versions=[_version(symbol="600056")],
        )
        == 1
    )
    with pit_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM dwd_financial_statement")).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM financial_pit_versions")).scalar_one()
            == 1
        )


@pytest.mark.parametrize(
    ("table", "version_options", "frame_changes"),
    [
        (
            "dwd_financial_statement",
            {"symbol": "600051"},
            {},
        ),
        (
            "dwd_financial_statement",
            {"value": 20.0},
            {},
        ),
        (
            "dwd_financial_indicator",
            {},
            {},
        ),
        (
            "dwd_financial_statement",
            {"source": "akshare"},
            {},
        ),
    ],
    ids=("business-key", "value", "domain-table", "source"),
)
def test_dwd_writer_rejects_misaligned_pit_batch_before_either_write(
    pit_engine, monkeypatch, table, version_options, frame_changes
):
    metadata = MetaData()
    Table(
        "dwd_financial_statement",
        metadata,
        Column("symbol", String(128), primary_key=True),
        Column("statement_type", String(32), primary_key=True),
        Column("report_period", Date, primary_key=True),
        Column("announce_date", Date),
        Column("item", String(128), primary_key=True),
        Column("value", Float),
        Column("revision", Integer),
        Column("source", String(64)),
        Column("_merged_at", DateTime),
        Column("_diff_flag", Integer),
        Column("_as_of", Date),
    )
    metadata.create_all(pit_engine)
    monkeypatch.setattr(
        "opendata.pipeline.dwd_merge.build_upsert_sql",
        lambda name, columns, key: (
            f"INSERT OR REPLACE INTO {name} ({', '.join(columns)}) VALUES "  # noqa: S608
            f"({', '.join(':' + column for column in columns)})"
        ),
    )
    frame_record = {
        **_statement(symbol="600050"),
        "source": "ths",
        "_merged_at": datetime(2026, 10, 12, 1),
        "_diff_flag": 0,
        "_as_of": date(2024, 3, 31),
    }
    frame_record.update(frame_changes)
    frame = pd.DataFrame([frame_record])
    version = _version(symbol="600050")
    if "source" in version_options:
        version = FinancialVersionInput(**{**version.__dict__, "source": version_options["source"]})
        version_options = {
            name: value for name, value in version_options.items() if name != "source"
        }
    if version_options:
        version = _version(**{"symbol": "600050", **version_options})

    with pytest.raises(FinancialPitError):
        DwdWriter(pit_engine).write(
            frame,
            table=table,
            key=("symbol", "statement_type", "report_period", "item"),
            financial_versions=[version],
        )

    with pit_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM dwd_financial_statement")).scalar_one()
            == 0
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM financial_pit_versions")).scalar_one()
            == 0
        )
