"""Warehouse-free unit tests for the bounded ODS-to-DWD backfill.

Nothing here creates an engine, imports a connector or opens a socket: the
warehouse is a recording fake connection, the reflected ODS table is a plain
SQLAlchemy ``Table`` built in memory (so the emitted SELECT can be compiled and
asserted as text), the metadata inspector is a fake, and the preflight key
spool is a fake of the ``sqlite3`` seam. Assertions are made on the emitted SQL
text, the bound parameters, the counters and the exact guard messages.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import numpy as np
import pytest
from sqlalchemy import Column, Date, Float, MetaData, String
from sqlalchemy import Table as SqlTable

import opendata.pipeline.dwd_backfill as backfill
from opendata.data.mapping import DomainMapping, require_domain_mapping
from opendata.pipeline.dwd_backfill import (
    DwdBackfillError,
    _canonical_key_signature,
    _inspect_tables,
    _preflight_collisions,
    _Progress,
    _row_contract_key,
    backfill_dwd,
)

STAMP = datetime(2024, 2, 1, 8, 30, tzinfo=timezone.utc)
SOURCE_TABLE = "ods_stock_daily_ths"
TARGET_TABLE = "dwd_stock_daily"
ODS_COLUMNS = (
    "thscode",
    "trade_date",
    "date_ms",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)
SOURCE_PK = ("thscode", "trade_date")
BUSINESS_KEY = ("symbol", "trade_date")
INSERT_SQL = "INSERT INTO seen_keys (key_signature) VALUES (?)"
SPOOL_SETUP = (
    "PRAGMA cache_size = -2048",
    "PRAGMA temp_store = FILE",
    "CREATE TABLE seen_keys (key_signature TEXT PRIMARY KEY)",
)


def ods_row(symbol="600519.SH", trade_date=date(2024, 1, 2), **overrides):
    """One raw ODS record in the source spelling the ths mapping declares."""
    row = {
        "thscode": symbol,
        "trade_date": trade_date,
        "date_ms": 1704124800000,
        "open_price": 9.0,
        "high_price": 11.0,
        "low_price": 8.0,
        "close_price": 10.0,
        "volume": 1000.0,
        "turnover": 10000.0,
    }
    row.update(overrides)
    return row


class FakeResult:
    """The ``Result`` seam :func:`_read_page` consumes."""

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]

    def mappings(self):
        return self

    def all(self):
        return [dict(row) for row in self.rows]

    def fetchall(self):
        return [dict(row) for row in self.rows]


class FakeTransaction:
    """A transaction that records entry and whether it was rolled back."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.transactions_started += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.connection.transaction_rolled_back = exc_type is not None
        return False


class RecordingConnection:
    """A fake DB-API/SQLAlchemy connection: it records SQL and replays pages."""

    def __init__(self, pages=()):
        self.pending = list(pages)
        self.statements = []
        self.closed = False
        self.isolation_options = []
        self.transactions_started = 0
        self.transaction_rolled_back = None

    def execute(self, statement, parameters=None):
        compiled = statement.compile()
        self.statements.append((str(compiled), dict(compiled.params)))
        if not self.pending:
            return FakeResult([])
        page = self.pending.pop(0)
        if isinstance(page, BaseException):
            raise page
        return FakeResult(page)

    def cursor(self):
        return self

    def fetchall(self):
        return []

    def begin(self):
        return FakeTransaction(self)

    def execution_options(self, **options):
        self.isolation_options.append(options)
        return self

    def close(self):
        self.closed = True

    @property
    def texts(self):
        return [text for text, _ in self.statements]

    @property
    def params(self):
        return [bound for _, bound in self.statements]


class FakeDialect:
    def __init__(self, name):
        self.name = name


class FakeEngine:
    """An engine seam that only hands out the recording connection."""

    def __init__(self, connection, dialect_name="mysql"):
        self.connection = connection
        self.dialect = FakeDialect(dialect_name)
        self.connects = 0

    def connect(self):
        self.connects += 1
        return self.connection


class RecordingWriter:
    """The key-upsert seam: records every submitted chunk."""

    def __init__(self, reported_rows=None):
        self.reported_rows = reported_rows
        self.calls = []

    def write(self, frame, *, table, key):
        self.calls.append((table, tuple(key), frame))
        return len(frame) if self.reported_rows is None else self.reported_rows

    @property
    def tables(self):
        return [call[0] for call in self.calls]

    @property
    def keys(self):
        return [call[1] for call in self.calls]


class FakeIntegrityError(Exception):
    """Stands in for ``sqlite3.IntegrityError`` so no database is opened."""


class FakeSpool:
    """The preflight key spool: records every statement it is handed."""

    def __init__(self, fail_after=None):
        self.statements = []
        self.seen = []
        self.commits = 0
        self.fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters=()):
        if self.fail_after is not None and len(self.statements) >= self.fail_after:
            raise RuntimeError("preflight spool disk failure")
        self.statements.append((statement, tuple(parameters)))
        if statement.startswith("INSERT"):
            signature = parameters[0]
            if signature in self.seen:
                raise FakeIntegrityError(f"UNIQUE constraint failed: {signature}")
            self.seen.append(signature)

    def commit(self):
        self.commits += 1


class FakeSqlite3:
    """The ``sqlite3`` seam of the preflight spool."""

    IntegrityError = FakeIntegrityError

    def __init__(self, fail_after=None):
        self.spool = FakeSpool(fail_after)
        self.paths = []

    def connect(self, path, **kwargs):
        self.paths.append(str(path))
        return self.spool


class FakeInspector:
    """The SQLAlchemy inspector seam: table and column shapes only."""

    def __init__(self, tables):
        self.tables = tables

    def get_table_names(self):
        return sorted(self.tables)

    def get_columns(self, table):
        return [{"name": name} for name in self.tables[table]["columns"]]

    def get_pk_constraint(self, table):
        return {"constrained_columns": self.tables[table]["pk"]}


def warehouse_tables(
    *,
    missing_source_columns=(),
    missing_target_columns=(),
    source_pk=SOURCE_PK,
    target_pk=BUSINESS_KEY,
    omit_source=False,
    omit_target=False,
):
    """A reflected-looking warehouse: the two tables, their columns and PKs."""
    source_columns = [name for name in ODS_COLUMNS if name not in missing_source_columns]
    contract_columns = [
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    trace_columns = ["source", "_merged_at", "_diff_flag", "_as_of"]
    target_columns = [
        name for name in (*contract_columns, *trace_columns) if name not in missing_target_columns
    ]
    tables = {}
    if not omit_source:
        tables[SOURCE_TABLE] = {"columns": source_columns, "pk": source_pk}
    if not omit_target:
        tables[TARGET_TABLE] = {"columns": target_columns, "pk": target_pk}
    return tables


def build_ods_table():
    """A real in-memory ``Table`` so statements compile without any engine."""
    return SqlTable(
        SOURCE_TABLE,
        MetaData(),
        Column("thscode", String(64)),
        Column("trade_date", Date()),
        Column("date_ms", Float),
        Column("open_price", Float),
        Column("high_price", Float),
        Column("low_price", Float),
        Column("close_price", Float),
        Column("volume", Float),
        Column("turnover", Float),
    )


class Harness:
    """The seams a :func:`backfill_dwd` run is executed against."""

    def __init__(self, engine, connection, sqlite, writer, reflected, ods):
        self.engine = engine
        self.connection = connection
        self.sqlite = sqlite
        self.writer = writer
        self.reflected = reflected
        self.ods = ods

    def run(self, **overrides):
        kwargs = {
            "domain": "stock_daily",
            "source": "ths",
            "start": date(2024, 1, 2),
            "end": date(2024, 1, 3),
            "page_size": 1,
            "writer": self.writer,
            "merged_at": STAMP,
        }
        kwargs.update(overrides)
        return backfill_dwd(self.engine, **kwargs)


@pytest.fixture
def harness(monkeypatch):
    """Install the warehouse seams (connection, reflection, spool) as fakes."""

    def install(pages=(), *, dialect="mysql", tables=None, mapping=None, spool_statements=None):
        connection = RecordingConnection(pages)
        engine = FakeEngine(connection, dialect)
        writer = RecordingWriter()
        sqlite = FakeSqlite3(spool_statements)
        ods = build_ods_table()
        reflected = []

        def fake_table(name, metadata, **kwargs):
            reflected.append((name, kwargs.get("autoload_with")))
            return ods

        monkeypatch.setattr(backfill, "Table", fake_table)
        monkeypatch.setattr(backfill, "sqlite3", sqlite)
        monkeypatch.setattr(backfill, "inspect", lambda engine: FakeInspector(warehouse_tables()))
        if tables is not None:
            monkeypatch.setattr(
                backfill, "inspect", lambda engine: FakeInspector(warehouse_tables(**tables))
            )
        if mapping is not None:
            monkeypatch.setattr(backfill, "require_domain_mapping", lambda source, domain: mapping)
        return Harness(engine, connection, sqlite, writer, reflected, ods)

    return install


def test_mysql_backfill_pins_isolation_pages_by_composite_key_and_writes(harness) -> None:
    """Full pass: window SQL, keyset continuation, preflight spool, upsert counts."""
    first = ods_row()
    second = ods_row(symbol="000001.SZ", trade_date=date(2024, 1, 3), close_price=12.0)
    h = harness([[first], [second], [], [first], [second], []], dialect="mysql")

    stats = h.run()

    assert h.engine.connects == 1
    assert h.connection.isolation_options == [{"isolation_level": "REPEATABLE READ"}]
    assert h.connection.transactions_started == 1
    assert h.connection.closed is True
    assert h.reflected == [(SOURCE_TABLE, h.engine)]
    assert stats.ods_table == SOURCE_TABLE
    assert stats.dwd_table == TARGET_TABLE

    window_sql, window_params = h.connection.statements[0]
    assert f"FROM {SOURCE_TABLE}" in window_sql
    assert (
        f"WHERE {SOURCE_TABLE}.trade_date >= :trade_date_1 "
        f"AND {SOURCE_TABLE}.trade_date <= :trade_date_2" in window_sql
    )
    assert " OR " not in window_sql
    assert f"ORDER BY {SOURCE_TABLE}.thscode, {SOURCE_TABLE}.trade_date" in window_sql
    assert "LIMIT :param_1" in window_sql
    assert window_params == {
        "trade_date_1": date(2024, 1, 2),
        "trade_date_2": date(2024, 1, 3),
        "param_1": 1,
    }

    continuation_sql, continuation_params = h.connection.statements[1]
    assert (
        f"{SOURCE_TABLE}.thscode > :thscode_1 "
        f"OR {SOURCE_TABLE}.thscode = :thscode_2 "
        f"AND {SOURCE_TABLE}.trade_date > :trade_date_3" in continuation_sql
    )
    assert continuation_params["thscode_1"] == "600519.SH"
    assert continuation_params["trade_date_3"] == date(2024, 1, 2)

    assert stats.preflight_pages == 2
    assert stats.rows_preflighted == 2
    assert stats.pages_read == 2
    assert stats.rows_read == 2
    assert stats.rows_normalized == 2
    assert stats.rows_merged == 2
    assert stats.rows_written == 2
    assert stats.diff_flagged == 0
    assert stats.collision_keys == ()

    assert h.writer.tables == [TARGET_TABLE, TARGET_TABLE]
    assert h.writer.keys == [BUSINESS_KEY, BUSINESS_KEY]
    written = [row for call in h.writer.calls for row in call[2].to_dict("records")]
    assert [row["symbol"] for row in written] == ["600519", "000001"]
    assert [row["trade_date"] for row in written] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert [row["close"] for row in written] == [10.0, 12.0]
    assert [row["source"] for row in written] == ["ths", "ths"]
    assert [row["_diff_flag"] for row in written] == [0, 0]
    assert [row["_as_of"] for row in written] == [STAMP.date(), STAMP.date()]

    spool = h.sqlite.spool
    assert [text for text, _ in spool.statements][:3] == list(SPOOL_SETUP)
    assert all(text == INSERT_SQL for text, _ in spool.statements[3:])
    assert [params[0] for _, params in spool.statements[3:]] == [
        '[["str","600519"],["date","2024-01-02"]]',
        '[["str","000001"],["date","2024-01-03"]]',
    ]
    assert spool.commits == 2
    assert "normalized-keys.sqlite3" in h.sqlite.paths[0]


def test_non_mysql_engine_keeps_the_configured_isolation_level(harness) -> None:
    """The REPEATABLE READ pin is a MySQL-only statement option."""
    h = harness([[ods_row()], [], [ods_row()], []], dialect="postgresql")

    stats = h.run()

    assert h.connection.isolation_options == []
    assert h.connection.closed is True
    assert stats.rows_written == 1


def test_empty_selection_reads_once_per_pass_and_writes_nothing(harness) -> None:
    """An empty window still compiles the bounded SELECT and submits no chunk."""
    h = harness([[], []], dialect="sqlite")

    stats = h.run(page_size=500)

    assert h.connection.texts == h.connection.texts[:2]
    assert (
        h.connection.params
        == [
            {"trade_date_1": date(2024, 1, 2), "trade_date_2": date(2024, 1, 3), "param_1": 500},
        ]
        * 2
    )
    assert (stats.pages_read, stats.rows_read, stats.rows_written) == (0, 0, 0)
    assert h.writer.calls == []


def test_read_failure_after_preflight_reports_committed_progress(harness) -> None:
    """A warehouse error mid-scan becomes a backfill error with honest counters."""
    failure = RuntimeError("warehouse connection lost")
    h = harness([[ods_row()], [], failure])

    with pytest.raises(DwdBackfillError, match="read failed after any earlier pages") as refused:
        h.run()

    assert refused.value.__cause__ is failure
    assert refused.value.stats.rows_preflighted == 1
    assert refused.value.stats.preflight_pages == 1
    assert (refused.value.stats.pages_read, refused.value.stats.rows_written) == (0, 0)
    assert h.writer.calls == []
    assert h.connection.closed is True
    assert h.connection.transaction_rolled_back is True


def test_page_failure_after_committed_pages_is_wrapped(harness) -> None:
    """A writer outage is wrapped, and the submitted rows stay reported."""
    h = harness([[ods_row()], [], [ods_row()], []], dialect="sqlite")
    h.writer.reported_rows = 7

    with pytest.raises(DwdBackfillError, match="page failed after any earlier pages") as refused:
        h.run()

    cause = refused.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert str(cause) == "writer reported 7 rows for a 1-row page"
    stats = refused.value.stats
    assert (stats.rows_merged, stats.rows_written) == (1, 7)
    assert stats.rows_written == 7
    assert h.writer.calls[0][0] == TARGET_TABLE


def test_merge_collision_refuses_the_page_and_reraises_the_backfill_error(harness) -> None:
    """Two rows normalizing to one DWD key are refused instead of silently picked."""
    integer_day = ods_row(trade_date=20240102)
    float_day = ods_row(trade_date=20240102.0)
    h = harness([[integer_day, float_day], [], [integer_day, float_day], []], dialect="sqlite")

    with pytest.raises(DwdBackfillError, match="duplicate normalized DWD keys") as refused:
        h.run(page_size=2)

    stats = refused.value.stats
    assert stats.collision_keys and stats.collision_keys[0].startswith("ths: ")
    assert len(stats.collision_keys) == 1
    assert (stats.pages_read, stats.rows_read, stats.rows_merged) == (1, 2, 0)
    assert stats.rows_written == 0
    assert h.writer.calls == []
    assert h.connection.closed is True


def test_preflight_collision_is_refused_before_any_write(harness) -> None:
    """The spool's unique index rejects an alias pair before the first upsert."""
    alias_pair = [ods_row(), ods_row(symbol="600519")]
    h = harness([alias_pair], dialect="sqlite")

    with pytest.raises(DwdBackfillError, match="ambiguous normalized DWD key") as refused:
        h.run()

    message = str(refused.value)
    assert (
        "ambiguous normalized DWD key ('600519', datetime.date(2024, 1, 2)) in "
        "'ods_stock_daily_ths'; no rows were written. Resolve source rows before rerunning"
    ) in message
    assert refused.value.__cause__ is not None
    stats = refused.value.stats
    assert stats.collision_keys == ("ths: ('600519', datetime.date(2024, 1, 2))",)
    assert (stats.rows_preflighted, stats.rows_written) == (2, 0)
    assert h.writer.calls == []
    assert h.connection.texts == h.connection.texts[:1]


def test_spool_infrastructure_failure_is_reported_as_preflight_failure(harness) -> None:
    """A preflight that cannot spool fails closed with the preflight message."""
    h = harness([[ods_row()], []], dialect="sqlite", spool_statements=3)
    mapping = require_domain_mapping("ths", "stock_daily")
    progress = _Progress()

    with pytest.raises(
        DwdBackfillError, match="DWD backfill preflight failed; no DWD rows were written"
    ) as refused:
        _preflight_collisions(
            h.connection,
            h.ods,
            mapping=mapping,
            time_column="trade_date",
            primary_key=SOURCE_PK,
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
            page_size=1,
            progress=progress,
            domain="stock_daily",
            source="ths",
            source_table=SOURCE_TABLE,
            target_table=TARGET_TABLE,
        )

    assert isinstance(refused.value.__cause__, RuntimeError)
    assert str(refused.value.__cause__) == "preflight spool disk failure"
    assert [text for text, _ in h.sqlite.spool.statements] == list(SPOOL_SETUP)
    assert (progress.preflight_pages, progress.rows_preflighted) == (1, 1)
    assert refused.value.stats.rows_written == 0
    assert h.writer.calls == []


@pytest.mark.parametrize(
    ("source", "start", "end", "page_size", "message"),
    [
        (
            "auto",
            date(2024, 1, 2),
            date(2024, 1, 3),
            10,
            "DWD backfill requires an explicit source; source='auto' is refused",
        ),
        (
            "ths",
            datetime(2024, 1, 1),
            date(2024, 1, 3),
            10,
            "start and end must be date values, not datetimes",
        ),
        ("ths", date(2024, 1, 3), date(2024, 1, 2), 10, "start 2024-01-03 is after end 2024-01-02"),
        ("ths", date(2024, 1, 2), date(2024, 1, 3), 0, "page_size must be positive, got 0"),
        ("ths", date(2024, 1, 2), date(2024, 1, 3), -5, "page_size must be positive, got -5"),
    ],
    ids=("auto-source", "datetime-bound", "reversed-window", "zero-page", "negative-page"),
)
def test_selection_guards_fire_before_any_warehouse_access(source, start, end, page_size, message):
    """Every selection guard runs against a bare object: no engine is ever used."""
    with pytest.raises(ValueError) as refused:
        backfill_dwd(
            object(),
            domain="stock_daily",
            source=source,
            start=start,
            end=end,
            page_size=page_size,
        )

    assert str(refused.value) == message


def test_naive_merge_timestamp_is_refused_before_the_connection_opens(harness) -> None:
    """A naive ``merged_at`` cannot stamp a point-in-time row, so it fails closed."""
    h = harness([[], []], dialect="mysql")

    with pytest.raises(ValueError, match="merged_at must be timezone-aware"):
        h.run(merged_at=datetime(2024, 2, 1, 8, 30))

    assert h.engine.connects == 0
    assert h.connection.statements == []
    assert h.connection.isolation_options == []
    assert h.writer.calls == []


def test_stats_snapshot_is_json_ready_with_iso_bounds_and_tuple_keys(harness) -> None:
    """The returned counters serialize without a custom encoder."""
    h = harness([[ods_row()], [], [ods_row()], []], dialect="sqlite")

    payload = json.loads(json.dumps(h.run().as_dict()))

    assert payload["domain"] == "stock_daily"
    assert payload["source"] == "ths"
    assert payload["ods_table"] == SOURCE_TABLE
    assert payload["dwd_table"] == TARGET_TABLE
    assert payload["start"] == "2024-01-02"
    assert payload["end"] == "2024-01-03"
    assert payload["rows_written"] == 1
    assert payload["collision_keys"] == []


def test_wide_pivot_mapping_is_refused_before_the_warehouse_is_touched() -> None:
    """A pivot leg has no 1:1 ODS shape, so direct backfill is refused."""
    with pytest.raises(ValueError, match="declares a wide-frame pivot") as refused:
        backfill_dwd(
            object(),
            domain="financial_statement",
            source="akshare",
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
        )

    assert "financial_statement/akshare declares a wide-frame pivot" in str(refused.value)
    assert "requires a 1:1 source mapping" in str(refused.value)


def _mapping_without_field(field_name: str) -> DomainMapping:
    """The ths stock_daily mapping with one contract field removed."""
    real = require_domain_mapping("ths", "stock_daily")
    fields = {name: spec for name, spec in real.fields.items() if name != field_name}
    key = tuple(name for name in real.key if name in fields)
    return DomainMapping(
        domain=real.domain,
        key=key,
        fields=fields,
        adjust=real.adjust,
        suspension=real.suspension,
        denominator=real.denominator,
        tolerances=real.tolerances,
    )


def test_mapping_without_the_window_field_is_refused(monkeypatch) -> None:
    """Backfill pages on the window column, so the mapping has to carry it."""
    monkeypatch.setattr(
        backfill,
        "require_domain_mapping",
        lambda source, domain: _mapping_without_field("trade_date"),
    )

    with pytest.raises(ValueError) as refused:
        backfill_dwd(
            object(),
            domain="stock_daily",
            source="ths",
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
        )

    assert "mapping for stock_daily/ths lacks date field 'trade_date'" in str(refused.value)


def test_window_field_outside_the_business_key_is_refused(monkeypatch) -> None:
    """A window column outside the key could not be upserted on."""
    real = require_domain_mapping("ths", "stock_daily")
    keyless = DomainMapping(
        domain=real.domain,
        key=("symbol",),
        fields=real.fields,
        adjust=real.adjust,
        suspension=real.suspension,
        denominator=real.denominator,
        tolerances=real.tolerances,
    )
    monkeypatch.setattr(backfill, "require_domain_mapping", lambda source, domain: keyless)

    with pytest.raises(ValueError) as refused:
        backfill_dwd(
            object(),
            domain="stock_daily",
            source="ths",
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
        )

    assert "date field 'trade_date' is not part of the DWD business key" in str(refused.value)


def test_unregistered_capability_is_refused_without_auto_routing() -> None:
    """A mapped-but-unregistered source fails closed instead of routing auto."""
    with pytest.raises(LookupError) as refused:
        backfill._require_registered_capability("stock_daily", "nosuchsource")

    assert "source 'nosuchsource' has no registered capability for 'stock_daily'" in str(
        refused.value
    )
    assert backfill._require_registered_capability("stock_daily", "ths") is None


@pytest.fixture
def reflect(monkeypatch):
    """Run the reflection guard against a fake warehouse shape."""

    def run(*, tables=None, mapping=None):
        mapping = mapping or require_domain_mapping("ths", "stock_daily")
        inspector = FakeInspector(warehouse_tables(**(tables or {})))
        monkeypatch.setattr(backfill, "inspect", lambda engine: inspector)
        primary_key = _inspect_tables(
            object(),
            source_table=SOURCE_TABLE,
            target_table=TARGET_TABLE,
            source_key=mapping.source_key,
            target_key=tuple(mapping.key),
            time_column=mapping.fields["trade_date"].source_column,
            mapping=mapping,
        )
        return primary_key, inspector

    return run


def test_reflection_returns_the_ods_primary_key_used_for_paging(reflect) -> None:
    """A well-shaped warehouse yields the composite key the pager walks on."""
    primary_key, inspector = reflect()

    assert primary_key == SOURCE_PK
    assert inspector.get_table_names() == [TARGET_TABLE, SOURCE_TABLE]


def test_missing_warehouse_tables_are_listed_and_refused(reflect) -> None:
    """Backfill never creates DDL: both absent tables are named together."""
    with pytest.raises(LookupError) as refused:
        reflect(tables={"omit_source": True, "omit_target": True})

    assert str(refused.value) == (
        "required warehouse tables are missing: ['ods_stock_daily_ths', 'dwd_stock_daily']"
    )


def test_ods_table_missing_a_mapped_column_is_refused(reflect) -> None:
    """The ODS spelling of every mapped field has to exist."""
    with pytest.raises(ValueError) as refused:
        reflect(tables={"missing_source_columns": ("open_price", "turnover")})

    assert str(refused.value) == (
        "ODS table 'ods_stock_daily_ths' lacks mapped columns ['open_price', 'turnover']"
    )


def test_dwd_table_missing_contract_or_trace_column_is_refused(reflect) -> None:
    """The DWD table has to carry the contract and all four trace columns."""
    with pytest.raises(ValueError) as refused:
        reflect(tables={"missing_target_columns": ("amount", "_as_of")})

    assert str(refused.value) == (
        "DWD table 'dwd_stock_daily' lacks contract/trace columns ['_as_of', 'amount']"
    )


def test_keyless_ods_table_is_refused_for_stable_paging(reflect) -> None:
    """Keyset pagination is impossible without an ODS primary key."""
    with pytest.raises(ValueError) as refused:
        reflect(tables={"source_pk": ()})

    assert str(refused.value) == (
        "ODS table 'ods_stock_daily_ths' must have a primary key for stable paging"
    )


def test_ods_primary_key_must_start_with_the_mapped_source_key(reflect) -> None:
    """A reordered key could split one normalized group across a page."""
    with pytest.raises(ValueError) as refused:
        reflect(tables={"source_pk": ("trade_date", "thscode")})

    assert (
        "ODS primary key ('trade_date', 'thscode') must start with mapped source key "
        "('thscode', 'trade_date'); normalized DWD-key groups could otherwise cross a "
        "page silently"
    ) in str(refused.value)


def test_dwd_primary_key_must_equal_the_mapped_business_key(reflect) -> None:
    """The upsert key is the mapped business key, so the table must match it."""
    with pytest.raises(ValueError) as refused:
        reflect(tables={"target_pk": ("symbol",)})

    assert str(refused.value) == (
        "DWD primary key of 'dwd_stock_daily' must match mapped business key "
        "('symbol', 'trade_date')"
    )


class _InconsistentMapping(DomainMapping):
    """A mapping whose declared source key omits the window column.

    ``DomainMapping.source_key`` is derived from ``key``, so a self-consistent
    registered mapping can never drop the window column; the guard is exercised
    with the inconsistent spelling it is written against.
    """

    @property
    def source_key(self):
        return ("thscode",)


def test_window_column_outside_the_ods_business_key_is_refused(reflect) -> None:
    """A window column the key does not carry cannot bound a page."""
    mapping = _InconsistentMapping(
        domain="stock_daily",
        key=BUSINESS_KEY,
        fields=require_domain_mapping("ths", "stock_daily").fields,
        adjust="unadjusted",
        suspension="absent_row",
        denominator="key_union",
    )

    with pytest.raises(ValueError) as refused:
        reflect(mapping=mapping)

    assert str(refused.value) == "mapped window date column must be part of the ODS business key"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ((None,), '[["none",null]]'),
        ((datetime(2024, 1, 2, 3, 4, 5),), '[["datetime","2024-01-02T03:04:05"]]'),
        ((date(2024, 1, 2),), '[["date","2024-01-02"]]'),
        (("600519",), '[["str","600519"]]'),
        ((True,), '[["bool",true]]'),
        ((False,), '[["bool",false]]'),
        ((7,), '[["int",7]]'),
        ((1.5,), '[["float","0x1.8000000000000p+0"]]'),
        ((np.int64(9),), '[["int",9]]'),
        ((np.float64(1.5),), '[["float","0x1.8000000000000p+0"]]'),
        (("600519", date(2024, 1, 2)), '[["str","600519"],["date","2024-01-02"]]'),
    ],
    ids=(
        "none",
        "datetime",
        "date",
        "str",
        "true",
        "false",
        "int",
        "float",
        "numpy-int",
        "numpy-float",
        "composite",
    ),
)
def test_canonical_key_signature_type_tags_every_scalar(key, expected) -> None:
    """Type tags keep ``1``, ``"1"``, ``1.0`` and ``True`` from conflating."""
    assert _canonical_key_signature(key) == expected


def test_canonical_key_signature_refuses_an_unsupported_type() -> None:
    """A key the spool cannot tag fails closed rather than colliding silently."""
    from decimal import Decimal

    with pytest.raises(TypeError) as refused:
        _canonical_key_signature([Decimal("1.5")])

    assert str(refused.value) == "unsupported normalized DWD key type Decimal"


def test_row_contract_key_normalizes_only_the_key_values() -> None:
    """The preflight key is the contract spelling of the raw ODS key."""
    mapping = require_domain_mapping("ths", "stock_daily")

    assert _row_contract_key(ods_row(), mapping) == ("600519", date(2024, 1, 2))
    assert (
        _canonical_key_signature(_row_contract_key(ods_row(), mapping))
        == '[["str","600519"],["date","2024-01-02"]]'
    )
