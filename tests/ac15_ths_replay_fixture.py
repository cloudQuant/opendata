"""Shared isolated-warehouse fixture for the AC15 (THS 旧缺陷接续) legs.

The subject is the OpenData replacement of the legacy THS daily-k import chain
``backfill_ths_history.py -> ThsHistoryRunner -> ths_dump_importer ->
MarketDataStore.commit`` (202/AUDIT D04, D05).  The replacement that carries the
same source, the same ODS table and the same "one bounded history window into
the warehouse" job is :func:`scripts.ops.replay_warehouse_pipeline.run_replay`
with the production :class:`opendata.pipeline.runner.DataPipeline` it builds.

Everything below keeps that production code real.  Only the two storage
adapters that are MySQL-only (``OdsWriter``'s staging upsert and the DWD
writer) are re-implemented against SQLite, exactly as
``tests/test_warehouse_pipeline_replay.py`` already does.  The step-5 watermark
record and the step-6 coverage/``data_tables`` refresh stay the production hooks,
so the ODS / DWD / publication / coverage layers exist as real, writable tables
during a run and "nothing was written" is a measured statement.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opendata.core.database import Base
from opendata.pipeline.affected_keys import SymbolPartitioning
from opendata.pipeline.metadata_refresh import build_pipeline_metadata_hook
from opendata.pipeline.notify import build_notify_hook
from opendata.pipeline.publication_store import PUBLICATION_METADATA
from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window
from opendata.pipeline.watermark import WATERMARK_TABLE
from scripts.ops import replay_warehouse_pipeline as replay

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    import pandas as pd
    from pytest import MonkeyPatch
    from sqlalchemy import Connection, Engine

SOURCE_URL = "mysql+pymysql://source_user:source_secret@warehouse.example:3306/source_db"
TARGET_URL = "mysql+pymysql://target_user:target_secret@127.0.0.1:33565/opendata_c65_replay_data"
METADATA_URL = (
    "mysql+pymysql://metadata_user:metadata_secret@localhost:33565/opendata_c65_replay_main"
)

ODS_TABLE = "ods_stock_daily_ths"
DWD_TABLE = "dwd_stock_daily"
DATASET_ID = "cn-equity-stock-daily"
BATCH_ID = "11111111-1111-4111-8111-111111111111"

_ODS_DDL = f"""
CREATE TABLE {ODS_TABLE} (
    _batch_id VARCHAR(36) NOT NULL,
    _fetched_at DATETIME NOT NULL,
    _source VARCHAR(32) NOT NULL,
    amount REAL NOT NULL,
    close_price REAL NOT NULL,
    date_ms BIGINT,
    high_price REAL NOT NULL,
    low_price REAL NOT NULL,
    open_price REAL NOT NULL,
    thscode VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    turnover REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (thscode, trade_date)
)
"""

_DWD_DDL = f"""
CREATE TABLE {DWD_TABLE} (
    symbol VARCHAR(32) NOT NULL,
    trade_date DATE NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    source VARCHAR(32) NOT NULL,
    _merged_at DATETIME NOT NULL,
    _diff_flag INTEGER NOT NULL,
    _as_of DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
)
"""

_WATERMARK_DDL = f"""
CREATE TABLE {WATERMARK_TABLE} (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id VARCHAR(36) NOT NULL UNIQUE,
    domain VARCHAR(64) NOT NULL,
    source VARCHAR(32) NOT NULL,
    layer VARCHAR(8) NOT NULL,
    window_start DATE,
    window_end DATE,
    rows_written INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
)
"""

#: Every durable layer AC15/D04 names, as ``(database, table)`` pairs.
PROTECTED_TABLES: tuple[tuple[str, str], ...] = (
    ("target", ODS_TABLE),
    ("target", DWD_TABLE),
    ("target", WATERMARK_TABLE),
    ("metadata", "pipeline_progress"),
    ("metadata", "pipeline_step_checkpoints"),
    ("metadata", "pipeline_symbol_windows"),
    ("metadata", "data_tables"),
    ("metadata", "od_publication_heads"),
    ("metadata", "od_publications"),
    ("metadata", "od_published_fact_versions"),
    ("metadata", "od_publication_snapshots"),
    ("metadata", "od_publication_outbox"),
    ("source", ODS_TABLE),
)

#: ``SELECT`` per database that renders the current head of one layer.
_HEAD_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "target",
        "head::batch_watermark",
        "SELECT COALESCE(MAX(`seq`), -1) || '#' || COUNT(*) FROM `batch_watermark`",
    ),
    (
        "metadata",
        "head::publication_sequence",
        "SELECT COALESCE(MAX(`latest_sequence`), -1) || '#' || "
        "COALESCE(MAX(`updated_at_us`), -1) FROM `od_publication_heads`",
    ),
    (
        "metadata",
        "head::coverage_watermark",
        "SELECT COALESCE(MAX(`data_end_date`), 'none') || '#' || "
        "COALESCE(MAX(`row_count`), -1) FROM `data_tables`",
    ),
)

#: Full content of the durable layers, so a rewrite is visible as well as an append.
_DETAIL_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "target",
        "ods_rows",
        f"SELECT `thscode`, `trade_date`, `_batch_id` FROM `{ODS_TABLE}`",  # noqa: S608
    ),
    ("target", "dwd_rows", f"SELECT `symbol`, `trade_date` FROM `{DWD_TABLE}`"),  # noqa: S608
    (
        "metadata",
        "progress_rows",
        "SELECT `pipeline_id`, `shard`, `status`, `rows_written` FROM `pipeline_progress`",
    ),
    (
        "metadata",
        "step_rows",
        "SELECT `pipeline_id`, `step`, `status` FROM `pipeline_step_checkpoints`",
    ),
    (
        "metadata",
        "window_rows",
        "SELECT `pipeline_id`, `source`, `symbol`, `window_start`, `window_end` "
        "FROM `pipeline_symbol_windows`",
    ),
    ("metadata", "coverage_rows", "SELECT `table_name`, `row_count` FROM `data_tables`"),
    (
        "metadata",
        "publication_head_rows",
        "SELECT `dataset_id`, `latest_sequence`, `latest_publication_id`, `watermark_json` "
        "FROM `od_publication_heads`",
    ),
    (
        "metadata",
        "publication_rows",
        "SELECT `dataset_id`, `publication_id`, `record_count` FROM `od_publications`",
    ),
    (
        "metadata",
        "fact_version_rows",
        "SELECT `dataset_id`, `business_key` FROM `od_published_fact_versions`",
    ),
    ("metadata", "snapshot_rows", "SELECT `snapshot_id` FROM `od_publication_snapshots`"),
    ("metadata", "outbox_rows", "SELECT `publication_id` FROM `od_publication_outbox`"),
)


def _flatten(rows: Sequence[object]) -> tuple[tuple[str, ...], ...]:
    """Render one result set as a sorted tuple of string tuples."""
    return tuple(sorted(tuple(str(value) for value in row) for row in rows))


def _seed_publication_and_coverage(engine: Engine) -> None:
    """Land one real publication head and one coverage row so heads are non-zero.

    Without a pre-existing head, "the publication head did not advance" would
    compare a missing value against a missing value.
    """
    stamp = 1_760_000_000_000_000
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO `od_publications` (`publication_id`, `dataset_id`, `source_id`, "
                "`sequence`, `schema_version`, `semantic_version`, `unit`, `adjustment_basis`, "
                "`quality_json`, `provenance_json`, `watermark_json`, `idempotency_key`, "
                "`payload_hash`, `record_count`, `published_at_us`) VALUES "
                "(:pid, :dataset, 'ths', 7, '1.0.0', 'equity-daily-v1', 'CNY', 'none', "
                "'{}', '{}', '{\"trade_date\":\"2024-01-05\"}', 'seed-key', "
                "'0000000000000000000000000000000000000000000000000000000000000000', 3, :stamp)"
            ),
            {"pid": "a" * 32, "dataset": DATASET_ID, "stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO `od_publication_heads` (`dataset_id`, `latest_sequence`, "
                "`latest_publication_id`, `watermark_json`, `updated_at_us`) VALUES "
                '(:dataset, 7, :pid, \'{"trade_date":"2024-01-05"}\', :stamp)'
            ),
            {"dataset": DATASET_ID, "pid": "a" * 32, "stamp": stamp},
        )
        seeded = datetime(2024, 1, 5, tzinfo=timezone.utc)
        connection.execute(
            text(
                "INSERT INTO `data_tables` (`id`, `table_name`, `row_count`, `last_update_time`, "
                "`data_start_date`, `data_end_date`, `created_at`, `updated_at`) VALUES "
                "(1, :name, 3, :stamp, :first, :last, :stamp, :stamp)"
            ),
            {
                "name": DWD_TABLE,
                "stamp": seeded,
                "first": date(2024, 1, 2),
                "last": date(2024, 1, 5),
            },
        )


@dataclass
class ThsReplaySandbox:
    """Three real SQLite databases plus the replay seams that point at them."""

    tmp_path: Path
    window: Window
    symbols: tuple[str, ...]
    days: tuple[date, ...]
    source_path: Path
    target_path: Path
    metadata_path: Path
    source_engine: Engine
    target_engine: Engine
    metadata_engine: Engine
    poison_symbol: str | None = None
    engine_calls: list[str] = field(default_factory=list)

    def _path_of(self, label: str) -> Path:
        return getattr(self, f"{label}_path")

    def _engine_of(self, label: str) -> Engine:
        return getattr(self, f"{label}_engine")

    # ------------------------------------------------------------------ reads

    def snapshot(self) -> dict[str, object]:
        """Re-read every durable layer through brand-new connections.

        A fresh engine per database means a fresh DBAPI connection, so the
        values are what an outside reader sees rather than what the running
        code still holds inside its own transaction - the read-back that tells
        "nothing was written" apart from "nothing was committed yet".
        """
        state: dict[str, object] = {}
        for label in ("source", "target", "metadata"):
            engine = create_engine(f"sqlite:///{self._path_of(label)}")
            try:
                with engine.connect() as connection:
                    for database, name, statement in _HEAD_QUERIES + _DETAIL_QUERIES:
                        if database != label:
                            continue
                        result = connection.execute(text(statement)).all()
                        state[name] = _flatten(result) if "rows" in name else str(result[0][0])
                    for database, table in PROTECTED_TABLES:
                        if database == label and inspect(connection).has_table(table):
                            state[f"count::{database}.{table}"] = int(
                                connection.execute(
                                    text(f"SELECT COUNT(*) FROM `{table}`")  # noqa: S608
                                ).scalar_one()
                            )
            finally:
                engine.dispose()
        return state

    def landed_keys(self, table: str, *, symbol_column: str, date_column: str) -> set[str]:
        """Return the exact ``symbol|iso date`` pairs present in a target table."""
        with self.target_engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT `{symbol_column}`, `{date_column}` FROM `{table}`")  # noqa: S608
            ).all()
        return {f"{symbol}|{day}" for symbol, day in rows}

    def expected_keys(self, symbols: Sequence[str]) -> set[str]:
        """Return the source snapshot's keys for the given raw symbols."""
        return {f"{symbol}|{day}" for symbol in symbols for day in self.days}

    def run(self, **overrides: object) -> dict[str, object]:
        """Invoke ``run_replay`` with the sandbox URLs and the caller's flags.

        The manifest path is a write-mode argument of the tool itself, so it is
        only supplied when the caller asks for ``apply``; leaving it out is what
        makes :meth:`run` with no flags the literal default invocation.
        """
        kwargs: dict[str, object] = {
            "window": self.window,
            "source_url": SOURCE_URL,
            "target_url": TARGET_URL,
            "metadata_url": METADATA_URL,
        }
        kwargs.update(overrides)
        if kwargs.get("apply"):
            kwargs.setdefault("manifest_path", self.tmp_path / "manifest.json")
        return replay.run_replay(**kwargs)  # type: ignore[arg-type]

    # -------------------------------------------------------------- tripwires

    def arm_tripwires(self) -> None:
        """Make any write to a protected layer table abort through SQLite triggers."""
        for label, table in PROTECTED_TABLES:
            engine = self._engine_of(label)
            if not inspect(engine).has_table(table):
                raise AssertionError(f"protected layer table {table} does not exist")
            with engine.begin() as connection:
                for verb in ("INSERT", "UPDATE", "DELETE"):
                    connection.execute(
                        text(
                            f"CREATE TRIGGER ac15_{table}_{verb.lower()} {verb} ON `{table}` "
                            "BEGIN SELECT RAISE(ABORT, 'AC15 dry-run write tripwire'); END"
                        )
                    )

    def disarm_tripwires(self) -> None:
        """Detach every write tripwire."""
        seen: set[tuple[str, str]] = set()
        for label, table in PROTECTED_TABLES:
            if (label, table) in seen:
                continue
            seen.add((label, table))
            with self._engine_of(label).begin() as connection:
                for verb in ("INSERT", "UPDATE", "DELETE"):
                    connection.execute(text(f"DROP TRIGGER IF EXISTS ac15_{table}_{verb.lower()}"))

    def attempt_forbidden_write(self, label: str, table: str) -> str:
        """Try one direct INSERT into a protected table; return the refusal text."""
        try:
            with self._engine_of(label).begin() as connection:
                connection.execute(text(f"INSERT INTO `{table}` DEFAULT VALUES"))
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        raise AssertionError(f"protected table {table} accepted a write")

    # ------------------------------------------------------------------ seams

    def bind(self, monkeypatch: MonkeyPatch) -> None:
        """Point the replay's engine/pipeline seams at the sandbox databases."""
        monkeypatch.setattr(replay, "_new_sync_engine", self._new_sync_engine)
        monkeypatch.setattr(replay, "_new_async_engine", self._new_async_engine)
        monkeypatch.setattr(replay, "_build_pipeline", self._build_pipeline)
        monkeypatch.setattr(replay._benchmark, "_readonly_source_snapshot", self._snapshot)
        event.listen(self.source_engine, "before_cursor_execute", self._guard_source_read)

    def _new_sync_engine(self, url: str) -> Engine:
        if url == SOURCE_URL:
            self.engine_calls.append("source")
            return self.source_engine
        if url == TARGET_URL:
            self.engine_calls.append("target")
            return self.target_engine
        if url == METADATA_URL:
            self.engine_calls.append("metadata")
            return self.metadata_engine
        raise AssertionError("unexpected URL reached the injected engine factory")

    def _new_async_engine(self, _url: object) -> Any:
        self.engine_calls.append("metadata-async")
        return create_async_engine(f"sqlite+aiosqlite:///{self.metadata_path}")

    @contextmanager
    def _snapshot(self, engine: Engine) -> Iterator[Connection]:
        with engine.connect() as connection:
            yield connection

    def _guard_source_read(
        self,
        _dbapi_connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """Abort exactly one symbol's source read while the fault is armed.

        The guard sits on the DBAPI statement boundary, below every line of the
        replay, and recognizes the per-symbol fetch by its bound values.  The
        dialect expands those values to a positional tuple before the event, so
        both shapes are accepted.
        """
        poison = self.poison_symbol
        if poison is None or f"FROM `{ODS_TABLE}` WHERE" not in str(statement):
            return
        values: list[object]
        if isinstance(parameters, dict):
            values = list(parameters.values())
        elif isinstance(parameters, (tuple, list)):
            values = list(parameters)
        else:
            values = [parameters]
        if poison in values:
            raise sa_exc.DBAPIError(
                statement,
                None,  # type: ignore[arg-type]
                parameters,
                RuntimeError(f"injected source read failure for {poison}"),
                None,
            )

    def _build_pipeline(
        self,
        *,
        engine: Engine,
        session_maker: async_sessionmaker,
        fetch_symbol: object,
        symbols: Sequence[str],
        source: str,
        shard_size: int,
        notify: object,
        run_variant: str,
    ) -> DataPipeline:
        assert source == "ths"

        def write_ods(frame: pd.DataFrame) -> int:
            prepared = frame.copy()
            prepared["_source"] = "ths"
            prepared["_fetched_at"] = "2024-02-01 00:00:00"
            prepared["_batch_id"] = BATCH_ID
            columns = list(prepared.columns)
            sql_columns = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join(f":{column}" for column in columns)
            updates = ", ".join(
                f'"{column}" = excluded."{column}"'
                for column in columns
                if column not in {"thscode", "trade_date"}
            )
            statement = text(
                f"INSERT INTO `{ODS_TABLE}` ({sql_columns}) VALUES ({placeholders}) "  # noqa: S608
                'ON CONFLICT ("thscode", "trade_date") DO UPDATE SET '
                f"{updates}"
            )
            with engine.begin() as connection:
                connection.execute(statement, prepared.to_dict("records"))
            return len(prepared)

        async def merge(context: Any) -> None:
            keys = list(context.affected_keys)
            if not keys:
                return
            with engine.begin() as connection:
                for raw_symbol, trade_date in keys:
                    row = connection.execute(
                        text(
                            f"SELECT open_price, high_price, low_price, close_price, volume, "  # noqa: S608
                            f"amount FROM `{ODS_TABLE}` "
                            "WHERE thscode = :symbol AND trade_date = :trade_date"
                        ),
                        {"symbol": raw_symbol, "trade_date": trade_date},
                    ).one()
                    connection.execute(
                        text(
                            f"INSERT INTO `{DWD_TABLE}` "  # noqa: S608
                            "(symbol, trade_date, open, high, low, close, volume, amount, "
                            "source, _merged_at, _diff_flag, _as_of) "
                            "VALUES (:symbol, :trade_date, :open, :high, :low, :close, "
                            ":volume, :amount, 'ths', :merged_at, 0, :as_of) "
                            "ON CONFLICT (symbol, trade_date) DO UPDATE SET "
                            "open=excluded.open, high=excluded.high, low=excluded.low, "
                            "close=excluded.close, volume=excluded.volume, "
                            "amount=excluded.amount, source=excluded.source, "
                            "_merged_at=excluded._merged_at, _diff_flag=excluded._diff_flag, "
                            "_as_of=excluded._as_of"
                        ),
                        {
                            "symbol": str(raw_symbol).split(".", maxsplit=1)[0],
                            "trade_date": trade_date,
                            "open": row[0],
                            "high": row[1],
                            "low": row[2],
                            "close": row[3],
                            "volume": row[4],
                            "amount": row[5],
                            "merged_at": datetime(2024, 2, 1),
                            "as_of": self.window.end,
                        },
                    )

        def load_keys(_context: Any) -> list[tuple[object, object]]:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        f"SELECT thscode, trade_date FROM `{ODS_TABLE}` "  # noqa: S608
                        "WHERE trade_date >= :start AND trade_date <= :end "
                        "ORDER BY thscode, trade_date"
                    ),
                    {"start": self.window.start, "end": self.window.end},
                ).all()
            return [(row[0], row[1]) for row in rows]

        return DataPipeline(
            PipelineSpec(
                domain="stock_daily",
                source=source,
                key=("thscode", "trade_date"),
                symbols=tuple(symbols),
                shard_size=shard_size,
                variant=run_variant,
            ),
            session_maker=session_maker,
            write_ods=write_ods,
            fetch_symbol=fetch_symbol,  # type: ignore[arg-type]
            merge=merge,
            # The replay suppresses external delivery on purpose; the step-5
            # watermark record and the step-6 coverage refresh stay the
            # production hooks so the AC15 layers really can move.
            notify=build_notify_hook(
                engine,
                batch_id=BATCH_ID,
                layer="ods",
                table=ODS_TABLE,
                payload_mode="meta",
            ),
            meta=build_pipeline_metadata_hook(
                engine, session_maker, domain="stock_daily", sources=("ths",)
            ),
            load_affected_keys=load_keys,
            bounded_partitions=SymbolPartitioning(
                symbol_key_index=0,
                normalize_symbol=lambda symbol: str(symbol).split(".", maxsplit=1)[0],
                batch_size=50,
            ),
        )


def build_sandbox(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    symbols: Sequence[str] = ("000001.SZ", "000002.SZ", "600000.SH", "600519.SH", "688981.SH"),
    days: Sequence[date] | None = None,
) -> ThsReplaySandbox:
    """Create the three isolated databases and bind the replay seams to them."""
    window_days = tuple(days or [date(2024, 1, day) for day in (2, 3, 4, 5)])
    sorted_symbols = tuple(sorted(symbols))
    source_path = tmp_path / "source.sqlite"
    target_path = tmp_path / "target.sqlite"
    metadata_path = tmp_path / "metadata.sqlite"

    source_engine = create_engine(f"sqlite:///{source_path}")
    with source_engine.begin() as connection:
        connection.execute(text(_ODS_DDL))
        connection.execute(
            text(
                f"INSERT INTO `{ODS_TABLE}` (thscode, trade_date, open_price, high_price, "  # noqa: S608
                "low_price, close_price, volume, turnover, amount, date_ms, _source, "
                "_fetched_at, _batch_id) VALUES (:symbol, :day, :value, :value, :value, :value, "
                "100, 1000, 1000, NULL, 'ths', '2024-01-01 00:00:00', "
                "'00000000-0000-4000-8000-000000000000')"
            ),
            [
                {
                    "symbol": symbol,
                    "day": day,
                    "value": float(index + 1) + float(day.day),
                }
                for index, symbol in enumerate(sorted_symbols)
                for day in window_days
            ],
        )

    target_engine = create_engine(f"sqlite:///{target_path}")
    with target_engine.begin() as connection:
        connection.execute(text(_ODS_DDL))
        connection.execute(text(_DWD_DDL))
        connection.execute(text(_WATERMARK_DDL))

    metadata_engine = create_engine(f"sqlite:///{metadata_path}")
    Base.metadata.create_all(metadata_engine)
    PUBLICATION_METADATA.create_all(metadata_engine)
    _seed_publication_and_coverage(metadata_engine)

    sandbox = ThsReplaySandbox(
        tmp_path=tmp_path,
        window=Window(start=window_days[0], end=window_days[-1]),
        symbols=sorted_symbols,
        days=window_days,
        source_path=source_path,
        target_path=target_path,
        metadata_path=metadata_path,
        source_engine=source_engine,
        target_engine=target_engine,
        metadata_engine=metadata_engine,
    )
    sandbox.bind(monkeypatch)
    return sandbox


__all__ = [
    "BATCH_ID",
    "DATASET_ID",
    "DWD_TABLE",
    "METADATA_URL",
    "ODS_TABLE",
    "PROTECTED_TABLES",
    "SOURCE_URL",
    "TARGET_URL",
    "ThsReplaySandbox",
    "build_sandbox",
]
