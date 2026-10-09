"""C85 unit coverage for :mod:`opendata.pipeline.freshness` fail-closed legs.

Every warehouse read is driven by a hand-written fake engine that records the
statement text and replays one canned driver value per read: no connection is
opened, no DDL is issued, no network is touched. The alert-matrix legs assert
on the rendered message text and the severity classification, because a
mis-classified page is exactly the failure this module exists to prevent.
"""

from __future__ import annotations

import importlib
from collections import namedtuple
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Annotated, Optional

import pytest
from pydantic import BaseModel

import opendata.pipeline.freshness as freshness
from opendata.data.domains import DomainSpec
from opendata.pipeline.freshness import (
    AlertSettings,
    DiskUsage,
    FreshnessReport,
    PipelineFailure,
    check_freshness,
    dwd_freshness,
    evaluate_alerts,
    ods_freshness,
    partition_plans_for,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

EXPECTED = date(2024, 1, 31)

PartitionRow = namedtuple("PartitionRow", "name description")

#: ``information_schema`` metadata for a table whose horizon stops at 2026.
GAP_ROWS = (
    PartitionRow("p2025", "'2026-01-01'"),
    PartitionRow("pmax", "MAXVALUE"),
)

#: Metadata for a table already covering ``current_year + years_ahead``.
COVERED_ROWS = (
    PartitionRow("p2026", "'2027-01-01'"),
    PartitionRow("p2027", "'2028-01-01'"),
    PartitionRow("p2028", "'2029-01-01'"),
    PartitionRow("pmax", "MAXVALUE"),
)


class _FakeResult:
    """Replay of one canned driver response."""

    def __init__(self, *, scalar: object = None, rows: Sequence[object] = ()) -> None:
        self._scalar = scalar
        self._rows = list(rows)

    def scalar(self) -> object:
        return self._scalar

    def all(self) -> list[object]:
        return list(self._rows)


class _FakeConnection:
    """Statement recorder that never talks to a database."""

    def __init__(self, engine: _FakeEngine) -> None:
        self.engine = engine

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, statement: object, params: Mapping[str, object] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        self.engine.statements.append((sql, dict(params or {})))
        table = str((params or {}).get("table", ""))
        if sql.startswith("SELECT COUNT(*) FROM information_schema.PARTITIONS"):
            return _FakeResult(scalar=self.engine.partition_counts.get(table, 0))
        if sql.startswith("SELECT PARTITION_NAME AS name"):
            return _FakeResult(rows=self.engine.partition_rows.get(table, ()))
        if sql.startswith("SELECT MAX("):
            return _FakeResult(scalar=self.engine.latest)
        raise AssertionError(f"freshness issued an unexpected statement: {sql}")


class _FakeEngine:
    """Stand-in warehouse engine: records SQL, replays values, opens nothing."""

    def __init__(
        self,
        *,
        latest: object = None,
        partition_counts: Mapping[str, int] | None = None,
        partition_rows: Mapping[str, Sequence[PartitionRow]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.latest = latest
        self.partition_counts = dict(partition_counts or {})
        self.partition_rows = dict(partition_rows or {})
        self.error = error
        self.statements: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def connect(self) -> _FakeConnection:
        if self.error is not None:
            raise self.error
        yield _FakeConnection(self)

    def disposed_statements(self) -> list[str]:
        return [sql for sql, _ in self.statements]


class _NoDateContract(BaseModel):
    """Contract whose fields carry no day-level date at all."""

    symbol: str
    volume: int


class _DateContract(BaseModel):
    """Contract with a plain day-level date column."""

    trade_date: date
    symbol: str


class _TimestampContract(BaseModel):
    """Contract whose only temporal field is a datetime, not a date."""

    symbol: str
    updated_at: datetime


class _AnnotatedDateContract(BaseModel):
    """Contract whose freshness column is wrapped in ``Annotated``."""

    trade_date: Annotated[date, "trading day"]
    symbol: str


class _OptionalDateContract(BaseModel):
    """Contract with both ``Optional[date]`` and ``date | None`` spellings."""

    trade_date: Optional[date]  # noqa: UP045  # the typing.Union spelling is the leg under test
    symbol: str


class _UnionDateContract(BaseModel):
    """Contract whose first date field is a PEP 604 union."""

    note: str | None
    trade_date: date | None
    symbol: str


class _DateTimeUnionContract(BaseModel):
    """Contract whose only temporal field is an optional timestamp."""

    updated_at: datetime | None


def _spec(
    *,
    declared: bool = True,
    time_field: str | None = "trade_date",
    temporal_kind: str = "series",
) -> DomainSpec:
    """Build one isolated domain spec without touching ``domains.yaml``."""
    if not declared:
        return DomainSpec(
            display_name="c85 probe",
            rest_path="c85/probe",
            contract="Bar",
            priority="P0",
        )
    return DomainSpec(
        display_name="c85 probe",
        rest_path="c85/probe",
        contract="Bar",
        priority="P0",
        temporal_kind=temporal_kind,
        time_field=time_field,
        natural_key=("symbol",),
        filter_dims=(),
        storage_mode="upsert",
        permissions=("query",),
    )


def _wire_domain(monkeypatch, spec: DomainSpec, model: type[BaseModel]) -> None:
    """Point the module's domain lookups at one hand-built spec/model pair."""
    monkeypatch.setattr(freshness, "require_domain", lambda domain: spec)
    monkeypatch.setattr(freshness, "contract_model", lambda domain: model)


class TestDeferredTypeImports:
    def test_type_only_imports_are_resolvable_at_runtime(self):
        """The ``TYPE_CHECKING`` block must not rot into an unresolvable import.

        With ``from __future__ import annotations`` the hints are strings, so a
        wrong module path in that block stays invisible until something calls
        :func:`typing.get_type_hints`. Reloading with the flag forced true both
        executes those imports and lets the hints be resolved.
        """
        import typing

        from sqlalchemy import Engine

        original = typing.TYPE_CHECKING
        typing.TYPE_CHECKING = True
        try:
            reloaded = importlib.reload(freshness)
            hints = typing.get_type_hints(reloaded.check_freshness)
            report_type = reloaded.FreshnessReport
        finally:
            typing.TYPE_CHECKING = original
            importlib.reload(freshness)

        assert hints["engine"] is Engine
        assert hints["expected"] is date
        assert hints["source"] == str | None
        assert hints["return"] is report_type


class TestDiskWaterLevel:
    @pytest.mark.parametrize("total_bytes", [0, -1])
    def test_nonpositive_total_capacity_reads_as_zero_ratio(self, total_bytes: int):
        """A volume that reports no capacity must not divide by zero nor page."""
        assert DiskUsage(used_bytes=500, total_bytes=total_bytes).used_ratio == 0.0

    def test_unmeasurable_capacity_produces_no_disk_alert(self):
        alerts = evaluate_alerts(
            settings=AlertSettings(),
            freshness=[],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=9_000, total_bytes=0),
        )

        assert alerts == []

    def test_ratio_just_below_warning_stays_quiet_and_just_above_escalates(self):
        settings = AlertSettings(disk_ratio_warning=0.8, disk_ratio_critical=0.9)

        quiet = evaluate_alerts(
            settings=settings,
            freshness=[],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=799, total_bytes=1000),
        )
        warned = evaluate_alerts(
            settings=settings,
            freshness=[],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=800, total_bytes=1000),
        )
        critical = evaluate_alerts(
            settings=settings,
            freshness=[],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=900, total_bytes=1000),
        )

        assert quiet == []
        assert warned[0].severity == "warning"
        assert warned[0].detail == ("disk usage 80.0% (warning >= 80%, critical >= 90%)")
        assert critical[0].severity == "critical"
        assert critical[0].subject == "warehouse volume"


class TestFreshnessFieldGuards:
    def test_declared_time_field_absent_from_contract_fails_closed(self, monkeypatch):
        """A rename in the contract must be caught, not silently probed."""
        _wire_domain(monkeypatch, _spec(time_field="trade_date"), _NoDateContract)

        with pytest.raises(ValueError, match="declares unknown time_field 'trade_date'"):
            freshness.freshness_field("c85_domain")

    def test_declared_non_date_time_field_is_rejected_without_fallback(self, monkeypatch):
        """A datetime contract field is not day-level freshness; no other field is tried."""
        _wire_domain(monkeypatch, _spec(time_field="updated_at"), _TimestampContract)

        with pytest.raises(ValueError, match="time_field 'updated_at' is not a date field"):
            freshness.freshness_field("c85_domain")

    def test_legacy_contract_without_any_date_field_fails_closed(self, monkeypatch):
        """The historical first-date-field rule has nothing to find here."""
        _wire_domain(monkeypatch, _spec(declared=False), _NoDateContract)

        with pytest.raises(ValueError, match="has no date field to measure freshness with"):
            freshness.freshness_field("c85_domain")

    def test_legacy_contract_still_uses_its_first_date_field(self, monkeypatch):
        class _LegacyBar(BaseModel):
            trade_date: date
            symbol: str

        _wire_domain(monkeypatch, _spec(declared=False), _LegacyBar)

        assert freshness.freshness_field("c85_domain") == "trade_date"

    def test_declared_snapshot_domain_without_a_time_field_fails_closed(self, monkeypatch):
        """An explicit null ``time_field`` is unsupported; no other column is guessed."""
        _wire_domain(
            monkeypatch,
            _spec(time_field=None, temporal_kind="snapshot"),
            _NoDateContract,
        )

        with pytest.raises(ValueError, match="declares no time_field for day-level freshness"):
            freshness.freshness_field("c85_domain")

    def test_declared_day_level_field_is_used_verbatim(self, monkeypatch):
        _wire_domain(monkeypatch, _spec(time_field="trade_date"), _DateContract)

        assert freshness.freshness_field("c85_domain") == "trade_date"

    @pytest.mark.parametrize(
        "model",
        [_DateContract, _AnnotatedDateContract, _OptionalDateContract, _UnionDateContract],
        ids=["plain", "annotated", "typing-optional", "pep604-union"],
    )
    def test_every_documented_date_spelling_is_accepted(self, monkeypatch, model):
        """``Annotated``/``Optional`` wrappers must not hide a real day-level column."""
        _wire_domain(monkeypatch, _spec(time_field="trade_date"), model)

        assert freshness.freshness_field("c85_domain") == "trade_date"

    def test_optional_datetime_is_still_rejected(self, monkeypatch):
        """Wrapping a timestamp in ``| None`` does not make it day-level freshness."""
        _wire_domain(monkeypatch, _spec(time_field="updated_at"), _DateTimeUnionContract)

        with pytest.raises(ValueError, match="time_field 'updated_at' is not a date field"):
            freshness.freshness_field("c85_domain")


class TestDateAnnotationRecognition:
    """``_is_date_type`` on raw annotations.

    Pydantic strips ``Annotated[...]`` metadata into ``FieldInfo.metadata``
    before ``freshness_field`` ever sees the annotation, so the wrapper legs
    documented in ``freshness_field`` have to be asserted on the resolver
    itself.
    """

    @pytest.mark.parametrize(
        "annotation",
        [
            date,
            Annotated[date, "trading day"],
            Annotated[date, "a", "b"],
            Optional[date],  # noqa: UP045  # the typing.Union spelling is the leg under test
            date | None,
        ],
    )
    def test_day_level_spellings_are_recognised(self, annotation):
        assert freshness._is_date_type(annotation) is True

    @pytest.mark.parametrize(
        "annotation",
        [
            datetime,
            datetime | None,
            Annotated[datetime, "fetched at"],
            str | None,
            int,
            type(None),
            object(),
        ],
    )
    def test_everything_that_is_not_a_day_is_refused(self, annotation):
        """A timestamp or a text column must never be read as day-level freshness."""
        assert freshness._is_date_type(annotation) is False


class TestWarehouseReads:
    """The ods/dwd wrappers through a recorded fake engine: no DDL, no connection."""

    def test_ods_check_reads_the_mapping_source_column(self):
        engine = _FakeEngine(latest="2024-01-05")

        report = ods_freshness(
            engine,
            "stock_daily",
            "akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
        )

        assert report.field == "日期"  # the ods layer keeps the source's naming
        assert report.source == "akshare"
        assert report.latest == date(2024, 1, 5)
        assert report.lag_days == 26
        assert report.status == "stale"
        assert engine.statements == [("SELECT MAX(`日期`) FROM `ods_stock_daily_akshare`", {})]

    def test_dwd_check_reads_the_contract_column(self):
        engine = _FakeEngine(latest=date(2024, 1, 31))

        report = dwd_freshness(engine, "stock_daily", expected=EXPECTED)

        assert report.source is None
        assert report.field == "trade_date"
        assert report.latest == EXPECTED
        assert report.status == "fresh"
        assert engine.statements == [("SELECT MAX(`trade_date`) FROM `dwd_stock_daily`", {})]


class TestDriverValueCoercion:
    """``_as_date`` behaviour seen through the public ``check_freshness`` API."""

    def test_datetime_from_a_typed_driver_becomes_the_latest_date(self):
        engine = _FakeEngine(latest=datetime(2024, 1, 5, 15, 30, 45))

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="ths",
            table="ods_stock_daily_ths",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.latest == date(2024, 1, 5)
        assert report.status == "stale"
        assert report.lag_days == 26
        assert engine.statements == [
            ("SELECT MAX(`trade_date`) FROM `ods_stock_daily_ths`", {}),
        ]

    def test_datetime_equal_to_the_expected_day_counts_as_fresh(self):
        engine = _FakeEngine(latest=datetime(2024, 1, 31, 0, 0, 0))

        report = check_freshness(
            engine,
            domain="stock_daily",
            source=None,
            table="dwd_stock_daily",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.latest == EXPECTED
        assert report.lag_days == 0
        assert report.status == "fresh"

    def test_text_timestamp_uses_its_date_part(self):
        """SQLite returns ``MAX(date)`` as text; a row must not read as missing."""
        engine = _FakeEngine(latest="  2024-01-05 00:00:00  ")

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.latest == date(2024, 1, 5)
        assert report.status == "stale"

    def test_unparsable_string_reports_missing_instead_of_raising(self):
        engine = _FakeEngine(latest="01/31/2024")

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.status == "missing"
        assert report.latest is None
        assert report.lag_days is None

    def test_non_text_typed_value_reports_missing(self):
        """A driver returning an int epoch must fail closed, not guess a date."""
        engine = _FakeEngine(latest=20240131)

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.status == "missing"

    def test_aware_datetime_loses_its_offset_before_comparison(self):
        engine = _FakeEngine(latest=datetime(2024, 1, 5, 23, 30, tzinfo=timezone.utc))

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.latest == date(2024, 1, 5)

    def test_absent_table_is_a_missing_report_not_an_exception(self):
        from sqlalchemy.exc import SQLAlchemyError

        engine = _FakeEngine(error=SQLAlchemyError("(1146) Table doesn't exist"))

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.status == "missing"
        assert report.field == "trade_date"

    def test_empty_table_reads_as_missing(self):
        engine = _FakeEngine(latest=None)

        report = check_freshness(
            engine,
            domain="stock_daily",
            source="akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
            field="trade_date",
        )

        assert report.status == "missing"


class TestOdsMappingGuard:
    def test_mapping_without_the_freshness_field_fails_closed(self, monkeypatch):
        """A declared freshness column the source never delivers must raise."""
        monkeypatch.setattr(freshness, "freshness_field", lambda domain: "no_such_source_column")
        engine = _FakeEngine(latest="2024-01-05")

        with pytest.raises(
            LookupError,
            match="has no field 'no_such_source_column' to measure freshness with",
        ):
            ods_freshness(
                engine,
                "stock_daily",
                "akshare",
                table="ods_stock_daily_akshare",
                expected=EXPECTED,
            )

        assert engine.statements == []

    def test_unknown_source_still_fails_closed(self, monkeypatch):
        monkeypatch.setattr(freshness, "freshness_field", lambda domain: "trade_date")
        engine = _FakeEngine(latest="2024-01-05")

        with pytest.raises(LookupError, match="unknown domain|unknown source"):
            ods_freshness(
                engine,
                "stock_daily",
                "no_such_source",
                table="ods_stock_daily_no_such_source",
                expected=EXPECTED,
            )


class TestPartitionPlansFor:
    def test_unpartitioned_table_is_skipped_and_gapped_table_is_collected(self):
        engine = _FakeEngine(
            partition_counts={"ods_gap": 2, "ods_flat": 0},
            partition_rows={"ods_gap": GAP_ROWS},
        )

        plans = partition_plans_for(
            engine, ["ods_flat", "ods_gap"], current_year=2026, years_ahead=2
        )

        assert plans == {"ods_gap": [("p2026", date(2027, 1, 1)), ("p2027", date(2028, 1, 1))]}
        assert "ods_flat" not in plans
        # Only the two metadata reads ran; maintenance never emitted DDL here.
        assert all(sql.startswith("SELECT") for sql in engine.disposed_statements())
        assert not any(
            sql.upper().startswith(("ALTER", "CREATE", "DROP", "INSERT"))
            for sql in engine.disposed_statements()
        )

    def test_table_with_a_full_horizon_yields_no_entry(self):
        engine = _FakeEngine(
            partition_counts={"ods_covered": 4},
            partition_rows={"ods_covered": COVERED_ROWS},
        )

        assert partition_plans_for(engine, ["ods_covered"], current_year=2026) == {}

    def test_no_tables_means_no_queries_at_all(self):
        engine = _FakeEngine()

        assert partition_plans_for(engine, [], current_year=2026) == {}
        assert engine.statements == []


class TestAlertMessages:
    def test_empty_partition_plan_list_emits_no_gap_alert(self):
        """A planner that found nothing missing must not render an empty name list."""
        alerts = evaluate_alerts(
            settings=AlertSettings(),
            freshness=[],
            failures=[],
            partition_plans={
                "ods_stock_daily_akshare": [],
                "ods_futures_daily_ths": [("p2027", date(2028, 1, 1))],
            },
            disk=None,
        )

        assert [alert.subject for alert in alerts] == ["ods_futures_daily_ths"]
        assert alerts[0].rule == "partition_missing"
        assert alerts[0].severity == "critical"
        assert alerts[0].detail == "missing yearly partitions: p2027"

    def test_stale_alert_text_names_the_latest_and_expected_day(self):
        report = FreshnessReport(
            domain="stock_daily",
            source="akshare",
            field="trade_date",
            latest=date(2024, 1, 29),
            expected=EXPECTED,
            lag_days=2,
            status="stale",
        )

        alerts = evaluate_alerts(
            settings=AlertSettings(),
            freshness=[report],
            failures=[],
            partition_plans={},
            disk=None,
        )

        assert alerts[0].subject == "stock_daily:akshare"
        assert alerts[0].severity == "warning"
        assert alerts[0].detail == "latest 2024-01-29 is 2 day(s) behind 2024-01-31"

    def test_missing_alert_text_and_dwd_subject(self):
        report = FreshnessReport(
            domain="index_constituent",
            source=None,
            field="as_of",
            latest=None,
            expected=EXPECTED,
            lag_days=None,
            status="missing",
        )

        alerts = evaluate_alerts(
            settings=AlertSettings(),
            freshness=[report],
            failures=[PipelineFailure("index_constituent", "ths", 3, "upstream 500")],
            partition_plans={},
            disk=None,
        )

        assert [alert.rule for alert in alerts] == [
            "freshness",
            "pipeline_failure",
            "consecutive_failures",
        ]
        assert alerts[0].subject == "index_constituent:dwd"
        assert alerts[0].detail == "no data found for as_of (missing or empty table)"
        assert alerts[1].detail == "last run failed: upstream 500"
        assert alerts[2].detail == "3 consecutive failures (threshold 3)"

    def test_single_failure_warns_without_escalating_and_serialises_cleanly(self):
        """One failure is below the streak threshold, but must still be reportable."""
        alerts = evaluate_alerts(
            settings=AlertSettings(),
            freshness=[],
            failures=[PipelineFailure("stock_daily", "akshare", 1, "connection reset")],
            partition_plans={},
            disk=None,
        )

        assert len(alerts) == 1
        assert alerts[0].as_dict() == {
            "rule": "pipeline_failure",
            "severity": "warning",
            "subject": "stock_daily:akshare",
            "detail": "last run failed: connection reset",
        }

    def test_fresh_and_healthy_signals_produce_no_alerts(self):
        report = FreshnessReport(
            domain="stock_daily",
            source="akshare",
            field="trade_date",
            latest=EXPECTED,
            expected=EXPECTED,
            lag_days=0,
            status="fresh",
        )

        alerts = evaluate_alerts(
            settings=AlertSettings(stale_lag_days=1),
            freshness=[report],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=1, total_bytes=1000),
        )

        assert alerts == []
