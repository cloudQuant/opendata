"""Guard and SQLite-runner tests for the existing-ODS warehouse replay tool."""

from __future__ import annotations

import asyncio
import json
import stat
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from opendata.core.database import Base
from opendata.pipeline.affected_keys import SymbolPartitioning
from opendata.pipeline.runner import DataPipeline, PipelineOutcome, PipelineSpec, Window
from scripts.ops import replay_warehouse_pipeline as replay

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence
    from pathlib import Path
    from typing import Any

    import pandas as pd
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

WINDOW = Window(start=date(2024, 1, 1), end=date(2024, 1, 31))
SOURCE_URL = "mysql+pymysql://source_user:source_secret@warehouse.example:3306/source_db"
TARGET_URL = "mysql+pymysql://target_user:target_secret@127.0.0.1:33565/opendata_c65_replay_data"
METADATA_URL = (
    "mysql+pymysql://metadata_user:metadata_secret@localhost:33565/opendata_c65_replay_main"
)
ODS_DDL = """
CREATE TABLE ods_stock_daily_ths (
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
DWD_DDL = """
CREATE TABLE dwd_stock_daily (
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
SOURCE_ROWS = (
    ("000001.SZ", "2024-01-02", 1.0),
    ("000001.SZ", "2024-01-03", 1.1),
    ("600519.SH", "2024-01-02", 10.0),
    ("600519.SH", "2024-01-04", 10.1),
)


class SchemaQueryResult:
    """Small result object for the SELECT-only MySQL reflection seam test."""

    def __init__(
        self,
        *,
        scalar: str | None = None,
        rows: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self) -> str | None:
        return self.scalar

    def fetchall(self) -> list[tuple[str, ...]]:
        return self.rows


class MySQLSchemaMetadataConnection:
    """Capture metadata SQL while returning a fixed pre-migrated schema."""

    class Dialect:
        name = "mysql"

    dialect = Dialect()

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(
        self, statement: object, _parameters: dict[str, object] | None = None
    ) -> SchemaQueryResult:
        sql = str(statement)
        self.statements.append(sql)
        normalized = sql.casefold()
        if "information_schema.tables" in normalized:
            return SchemaQueryResult(scalar="ods_stock_daily_ths")
        if "information_schema.columns" in normalized:
            return SchemaQueryResult(
                rows=[
                    ("_batch_id", "varchar(36)", "NO"),
                    ("thscode", "varchar(32)", "NO"),
                    ("trade_date", "date", "NO"),
                ]
            )
        if "information_schema.key_column_usage" in normalized:
            return SchemaQueryResult(rows=[("thscode",), ("trade_date",)])
        raise AssertionError("MySQL schema reflection issued an unexpected statement")


class PipelineLifecycleDouble:
    """Capture the pipeline loop and optionally fail before returning."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.loop: asyncio.AbstractEventLoop | None = None

    async def run(self, window: Window, *, resume: bool) -> PipelineOutcome:
        assert window == WINDOW
        assert resume is True
        self.loop = asyncio.get_running_loop()
        if self.error is not None:
            raise self.error
        return PipelineOutcome(pipeline_id="lifecycle-test")


class AsyncEngineLifecycleDouble:
    """Capture cleanup's loop and optionally surface a disposal failure."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.loop: asyncio.AbstractEventLoop | None = None
        self.disposed = False

    async def dispose(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.disposed = True
        if self.error is not None:
            raise self.error


class ReplayHarness:
    """Route replay dependencies to SQLite while keeping the production runner."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.source_path = tmp_path / "source.sqlite"
        self.target_path = tmp_path / "target.sqlite"
        self.metadata_path = tmp_path / "metadata.sqlite"
        self._initialize_databases()
        monkeypatch.setattr(replay, "_new_sync_engine", self._new_sync_engine)
        monkeypatch.setattr(replay, "_new_async_engine", self._new_async_engine)
        monkeypatch.setattr(replay, "_build_pipeline", self._build_pipeline)
        monkeypatch.setattr(replay._benchmark, "_readonly_source_snapshot", self._snapshot)

    def _initialize_databases(self) -> None:
        source_engine = create_engine(f"sqlite:///{self.source_path}")
        target_engine = create_engine(f"sqlite:///{self.target_path}")
        metadata_engine = create_engine(f"sqlite:///{self.metadata_path}")
        try:
            with source_engine.begin() as connection:
                connection.execute(text(ODS_DDL))
                for symbol, trade_date, close in SOURCE_ROWS:
                    connection.execute(
                        text(
                            "INSERT INTO ods_stock_daily_ths "
                            "(thscode, trade_date, open_price, high_price, low_price, close_price, "
                            "volume, turnover, amount, date_ms, _source, _fetched_at, _batch_id) "
                            "VALUES (:symbol, :trade_date, :close, :close, :close, :close, "
                            "100, 1000, 1000, NULL, 'ths', '2024-01-01 00:00:00', "
                            "'00000000-0000-4000-8000-000000000000')"
                        ),
                        {"symbol": symbol, "trade_date": trade_date, "close": close},
                    )
            with target_engine.begin() as connection:
                connection.execute(text(ODS_DDL))
                connection.execute(text(DWD_DDL))
            Base.metadata.create_all(metadata_engine)
        finally:
            source_engine.dispose()
            target_engine.dispose()
            metadata_engine.dispose()

    def _new_sync_engine(self, url: str) -> Engine:
        if url == SOURCE_URL:
            return create_engine(f"sqlite:///{self.source_path}")
        if url == TARGET_URL:
            return create_engine(f"sqlite:///{self.target_path}")
        if url == METADATA_URL:
            return create_engine(f"sqlite:///{self.metadata_path}")
        raise AssertionError("unexpected URL reached the injected engine factory")

    def _new_async_engine(self, _url: object) -> AsyncEngine:
        return create_async_engine(f"sqlite+aiosqlite:///{self.metadata_path}")

    @staticmethod
    @contextmanager
    def _snapshot(engine: Engine) -> Iterator[object]:
        with engine.connect() as connection:
            yield connection

    def _build_pipeline(
        self,
        *,
        engine: Engine,
        session_maker: async_sessionmaker[AsyncSession],
        fetch_symbol: Callable[[str, Window], pd.DataFrame],
        symbols: Sequence[str],
        source: str,
        shard_size: int,
        notify: Callable[[object], Awaitable[object]],
        run_variant: str,
    ) -> DataPipeline:
        assert source == "ths"

        def write_ods(frame: pd.DataFrame) -> int:
            prepared = frame.copy()
            prepared["_source"] = "ths"
            prepared["_fetched_at"] = "2024-02-01 00:00:00"
            prepared["_batch_id"] = "00000000-0000-4000-8000-000000000000"
            columns = list(prepared.columns)
            sql_columns = ", ".join(f'"{column}"' for column in columns)
            parameters = ", ".join(f":{column}" for column in columns)
            updates = ", ".join(
                f'"{column}" = excluded."{column}"'
                for column in columns
                if column not in {"thscode", "trade_date"}
            )
            statement = text(
                "INSERT INTO ods_stock_daily_ths "  # noqa: S608
                f"({sql_columns}) VALUES ({parameters}) "
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
                            "SELECT open_price, high_price, low_price, close_price, volume, "
                            "amount FROM ods_stock_daily_ths "
                            "WHERE thscode = :symbol AND trade_date = :trade_date"
                        ),
                        {"symbol": raw_symbol, "trade_date": trade_date},
                    ).one()
                    connection.execute(
                        text(
                            "INSERT INTO dwd_stock_daily "
                            "(symbol, trade_date, open, high, low, close, volume, amount, "
                            "source, _merged_at, _diff_flag, _as_of) "
                            "VALUES (:symbol, :trade_date, :open, :high, :low, :close, "
                            ":volume, :amount, 'ths', :merged_at, 0, :as_of) "
                            "ON CONFLICT (symbol, trade_date) DO UPDATE SET "
                            "open=excluded.open, high=excluded.high, low=excluded.low, "
                            "close=excluded.close, volume=excluded.volume, amount=excluded.amount, "
                            "source=excluded.source, _merged_at=excluded._merged_at, "
                            "_diff_flag=excluded._diff_flag, _as_of=excluded._as_of"
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
                            "as_of": WINDOW.end,
                        },
                    )

        def load_keys(_context: Any) -> list[tuple[object, object]]:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT thscode, trade_date FROM ods_stock_daily_ths "
                        "WHERE trade_date >= :start AND trade_date <= :end "
                        "ORDER BY thscode, trade_date"
                    ),
                    {"start": WINDOW.start, "end": WINDOW.end},
                ).all()
            return [(row[0], row[1]) for row in rows]

        async def metadata(_context: Any) -> dict[str, bool]:
            return {"refreshed": True}

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
            fetch_symbol=fetch_symbol,
            merge=merge,
            notify=notify,
            meta=metadata,
            load_affected_keys=load_keys,
            bounded_partitions=SymbolPartitioning(
                symbol_key_index=0,
                normalize_symbol=lambda symbol: str(symbol).split(".", maxsplit=1)[0],
                batch_size=50,
            ),
        )


def _run(
    *,
    window: Window = WINDOW,
    apply: bool = True,
    resume: bool = False,
    manifest_path: Path,
    shard_size: int = 1,
    interrupt_after_shards: int | None = None,
    progress_records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return replay.run_replay(
        window=window,
        source_url=SOURCE_URL,
        target_url=TARGET_URL,
        metadata_url=METADATA_URL,
        apply=apply,
        resume=resume,
        manifest_path=manifest_path if apply else None,
        shard_size=shard_size,
        interrupt_after_shards=interrupt_after_shards,
        progress=progress_records.append if progress_records is not None else None,
    )


def test_url_guards_require_isolated_replay_schemas_and_distinct_metadata() -> None:
    replay._validate_urls(SOURCE_URL, TARGET_URL, METADATA_URL)
    rejected = (
        (
            SOURCE_URL,
            "mysql+pymysql://u:p@remote.example:33565/opendata_c65_replay_data",
            METADATA_URL,
        ),
        (
            SOURCE_URL,
            TARGET_URL,
            "mysql+pymysql://u:p@127.0.0.1:3306/opendata_c65_replay_main",
        ),
        (
            SOURCE_URL,
            "mysql+pymysql://u:p@127.0.0.1:33565/opendata_c65_replay_main",
            METADATA_URL,
        ),
        (
            "mysql+pymysql://u:p@127.0.0.1:33565/opendata_c65_replay_data",
            TARGET_URL,
            METADATA_URL,
        ),
    )
    for source_url, target_url, metadata_url in rejected:
        with pytest.raises(ValueError):
            replay._validate_urls(source_url, target_url, metadata_url)


def test_mysql_schema_reflection_uses_only_information_schema_selects() -> None:
    connection = MySQLSchemaMetadataConnection()

    schema = replay._table_schema(cast("Connection", connection), "ods_stock_daily_ths")

    assert schema.columns == (
        ("_batch_id", "varchar(36)", False),
        ("thscode", "varchar(32)", False),
        ("trade_date", "date", False),
    )
    assert schema.primary_key == ("thscode", "trade_date")
    assert len(connection.statements) == 3
    assert all(
        statement.lstrip().casefold().startswith("select") for statement in connection.statements
    )
    assert all("show" not in statement.casefold() for statement in connection.statements)
    assert all("describe" not in statement.casefold() for statement in connection.statements)


def test_pipeline_disposes_async_engine_on_its_execution_loop() -> None:
    pipeline = PipelineLifecycleDouble()
    engine = AsyncEngineLifecycleDouble()
    disposal_started: list[bool] = []

    outcome = asyncio.run(
        replay._run_pipeline_with_disposal(
            cast("DataPipeline", pipeline),
            cast("AsyncEngine", engine),
            window=WINDOW,
            resume=True,
            on_disposal_start=lambda: disposal_started.append(True),
        )
    )

    assert outcome.pipeline_id == "lifecycle-test"
    assert pipeline.loop is not None
    assert engine.disposed is True
    assert engine.loop is pipeline.loop
    assert disposal_started == [True]


@pytest.mark.parametrize(
    "error",
    (RuntimeError("pipeline failed"), replay.ControlledReplayInterrupt()),
)
def test_pipeline_failure_and_interrupt_still_dispose_on_the_same_loop(
    error: BaseException,
) -> None:
    pipeline = PipelineLifecycleDouble(error)
    engine = AsyncEngineLifecycleDouble()
    disposal_started: list[bool] = []

    with pytest.raises(type(error)):
        asyncio.run(
            replay._run_pipeline_with_disposal(
                cast("DataPipeline", pipeline),
                cast("AsyncEngine", engine),
                window=WINDOW,
                resume=True,
                on_disposal_start=lambda: disposal_started.append(True),
            )
        )

    assert pipeline.loop is not None
    assert engine.disposed is True
    assert engine.loop is pipeline.loop
    assert disposal_started == [True]


def test_pipeline_cleanup_failure_is_propagated() -> None:
    pipeline = PipelineLifecycleDouble()
    engine = AsyncEngineLifecycleDouble(RuntimeError("dispose failed"))

    with pytest.raises(RuntimeError, match="dispose failed"):
        asyncio.run(
            replay._run_pipeline_with_disposal(
                cast("DataPipeline", pipeline),
                cast("AsyncEngine", engine),
                window=WINDOW,
                resume=True,
                on_disposal_start=lambda: None,
            )
        )

    assert pipeline.loop is not None
    assert engine.disposed is True
    assert engine.loop is pipeline.loop


def test_fingerprint_scalar_encoding_keeps_supported_sql_types_distinct() -> None:
    values = (
        None,
        date(2024, 1, 2),
        datetime(2024, 1, 2, 3, 4, 5, 6),
        Decimal("1.00"),
        1.0,
        1,
        True,
        "1",
        b"1",
    )
    encoded = [replay._typed_fingerprint_value(value) for value in values]
    assert len(encoded) == len(set(encoded))


def test_cli_requires_task_specific_environment_urls_and_has_no_url_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (replay.SOURCE_URL_ENV, replay.TARGET_URL_ENV, replay.METADATA_URL_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=replay.SOURCE_URL_ENV):
        replay._main_urls()
    parsed = replay._parser().parse_args(["--start", "2024-01-01", "--end", "2024-01-31"])
    assert not hasattr(parsed, "source_url")
    assert not hasattr(parsed, "target_url")
    assert not hasattr(parsed, "metadata_url")


def test_first_apply_rejects_nonempty_ods_without_creating_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ReplayHarness(tmp_path, monkeypatch)
    with create_engine(f"sqlite:///{harness.target_path}").begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ods_stock_daily_ths "
                "(thscode, trade_date, open_price, high_price, low_price, close_price, "
                "volume, turnover, amount, _source, _fetched_at, _batch_id) "
                "VALUES ('000001.SZ', '2024-01-02', 1, 1, 1, 1, 1, 1, 1, "
                "'ths', '2024-01-01 00:00:00', '00000000-0000-4000-8000-000000000000')"
            )
        )

    manifest = tmp_path / "replay.json"
    with pytest.raises(ValueError, match="empty ODS and DWD"):
        _run(manifest_path=manifest)
    assert not manifest.exists()


def test_controlled_interrupt_records_only_real_done_shards_then_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ReplayHarness(tmp_path, monkeypatch)
    manifest = tmp_path / "replay.json"
    records: list[dict[str, object]] = []

    interrupted = _run(
        manifest_path=manifest,
        interrupt_after_shards=1,
        progress_records=records,
    )

    assert interrupted["status"] == "interrupted", interrupted
    assert interrupted["shards_total"] == 2
    assert interrupted["shards_done_total"] == 1
    assert interrupted["shards_failed"] == 0
    assert interrupted["source_api_calls"] == 0
    assert interrupted["native_writes"] == 0
    assert any(record["record"] == "interrupted" for record in records)
    manifest_text = manifest.read_text(encoding="utf-8")
    saved = json.loads(manifest_text)
    assert saved["done_shards"] == [0]
    assert "source_secret" not in manifest_text
    assert "metadata_secret" not in manifest_text
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600

    metadata_engine = create_engine(f"sqlite:///{harness.metadata_path}")
    try:
        with metadata_engine.connect() as metadata:
            rows = metadata.execute(
                text(
                    "SELECT shard, status FROM pipeline_progress "
                    "WHERE pipeline_id = :pipeline_id ORDER BY shard"
                ),
                {"pipeline_id": interrupted["pipeline_id"]},
            ).all()
    finally:
        metadata_engine.dispose()
    assert [(int(shard), str(status).casefold()) for shard, status in rows] == [
        (0, "done"),
        (1, "running"),
    ]

    resumed = _run(manifest_path=manifest, resume=True)
    assert resumed["status"] == "complete"
    assert resumed["full_coverage_verified"] is True
    assert resumed["shards_resumed"] == 1
    assert resumed["shards_done_total"] == 2
    assert resumed["expected_window_rows"] == 4
    assert resumed["target_ods_rows_exact"] == 4
    assert resumed["target_dwd_rows_exact"] == 4
    assert resumed["target_ods_symbols"] == 2
    assert resumed["target_dwd_symbols"] == 2
    assert resumed["target_ods_first_date"] == "2024-01-02"
    assert resumed["target_dwd_last_date"] == "2024-01-04"
    assert resumed["hook_steps"] == {"merge": "done", "meta": "done", "notify": "done"}


def test_resume_refuses_a_changed_window_and_leaves_manifest_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ReplayHarness(tmp_path, monkeypatch)
    manifest = tmp_path / "replay.json"
    _run(manifest_path=manifest, interrupt_after_shards=1)
    original = manifest.read_bytes()

    with pytest.raises(replay.ReplayManifestError, match="inputs no longer match"):
        _run(
            manifest_path=manifest,
            resume=True,
            window=Window(start=WINDOW.start, end=date(2024, 2, 1)),
        )
    assert manifest.read_bytes() == original


def test_resume_refuses_source_value_drift_with_unchanged_keys_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ReplayHarness(tmp_path, monkeypatch)
    manifest = tmp_path / "replay.json"
    interrupted = _run(manifest_path=manifest, interrupt_after_shards=1)
    assert interrupted["status"] == "interrupted"
    saved_manifest = manifest.read_bytes()

    source_engine = create_engine(f"sqlite:///{harness.source_path}")
    try:
        with source_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE ods_stock_daily_ths SET close_price = close_price + 0.25 "
                    "WHERE thscode = '000001.SZ' AND trade_date = '2024-01-02'"
                )
            )
    finally:
        source_engine.dispose()

    with pytest.raises(replay.ReplayManifestError, match="inputs no longer match"):
        _run(manifest_path=manifest, resume=True)
    assert manifest.read_bytes() == saved_manifest
    with create_engine(f"sqlite:///{harness.target_path}").connect() as target:
        assert target.execute(text("SELECT COUNT(*) FROM ods_stock_daily_ths")).scalar_one() == 2


def test_success_reports_exact_window_counts_and_internal_notification_suppression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ReplayHarness(tmp_path, monkeypatch)
    manifest = tmp_path / "complete.json"
    records: list[dict[str, object]] = []

    report = _run(manifest_path=manifest, progress_records=records)

    assert report["status"] == "complete", report
    assert report["mode"] == "existing-ods-replay"
    assert report["source_observation"] == "existing_ods_snapshot_only"
    assert report["coverage_scope"] == "source_ods_window_only_not_current_market"
    assert report["full_coverage_verified"] is True
    assert report["source_rows_exact"] == 4
    assert report["expected_window_rows"] == 4
    assert report["target_ods_window_rows_exact"] == 4
    assert report["target_dwd_window_rows_exact"] == 4
    assert report["native_writes"] == 0
    assert report["source_api_calls"] == 0
    assert report["notification_delivery"] == "suppressed_internal_replay"
    assert report["hook_steps"] == {"merge": "done", "meta": "done", "notify": "done"}
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    fingerprint = manifest_data["window_fingerprint"]
    assert fingerprint["algorithm"] == "sha256-typed-length-prefixed-v1"
    assert len(fingerprint["sha256"]) == 64
    assert fingerprint["rows_exact"] == 4
    assert set(fingerprint["by_symbol_sha256"]) == {"000001.SZ", "600519.SH"}
    assert all(len(value) == 64 for value in fingerprint["by_symbol_sha256"].values())
    assert set(manifest_data["runtime_code_sha256"]) == set(replay._RUNTIME_FILE_PATHS)
    assert any(record["record"] == "fetch" for record in records)
    assert any(record["record"] == "shard" for record in records)
    assert records[-1]["record"] == "final"


def test_resume_rejects_actual_runtime_file_hash_drift(tmp_path: Path) -> None:
    runtime_file = tmp_path / "runner.py"
    runtime_key = "opendata/pipeline/runner.py"
    actual_runtime_file = replay._REPOSITORY_ROOT / runtime_key
    runtime_file.write_bytes(actual_runtime_file.read_bytes())
    recorded_fingerprints = replay._runtime_code_fingerprints({runtime_key: runtime_file})
    assert recorded_fingerprints == replay._runtime_code_fingerprints(
        {runtime_key: actual_runtime_file}
    )
    manifest: dict[str, object] = {
        "runtime_code_sha256": recorded_fingerprints,
        "done_shards": [0],
    }

    runtime_file.write_bytes(runtime_file.read_bytes() + b"\n# semantic drift\n")
    current_fingerprints = replay._runtime_code_fingerprints({runtime_key: runtime_file})
    assert recorded_fingerprints[runtime_key] != current_fingerprints[runtime_key]
    with pytest.raises(replay.ReplayManifestError, match="inputs no longer match"):
        replay._require_manifest_identity(
            manifest,
            {"runtime_code_sha256": current_fingerprints},
            total_shards=1,
        )
