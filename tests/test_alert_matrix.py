"""The alert matrix's production join (AC-18|01): measure → decide → deliver.

``opendata/pipeline/freshness.py`` already decided whether a reading is an
alert, and ``ws_manager`` already delivered frames - what was missing was
the caller, so the 08:30 freshness row was declared, unexecutable, and
never ran: a vanished table produced an ``Alert`` object no channel carried.
These tests pin the join on a scratch SQLite warehouse, including the
counterfactual that a healthy warehouse must send nothing.

SQLite is enough here because the matrix only reads ``MAX(<date column>)``;
the driver difference (MySQL returns a ``date`` for that aggregate, SQLite
the stored text) is pinned by name below, because reading it wrong would
report a populated table as missing.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, event, insert, pool

from opendata.data.domains import dwd_table, ods_table
from opendata.data.providers import register_providers
from opendata.models.pipeline import ShardStatus
from opendata.pipeline import alert_matrix

EXPECTED = date(2026, 9, 25)


@pytest.fixture(autouse=True)
def registered_capabilities() -> None:
    """The matrix walks the capability registry; it must be populated."""
    register_providers()


def _bar_table(metadata: MetaData, name: str, date_column: str) -> Table:
    return Table(
        name,
        metadata,
        Column("symbol", String, primary_key=True),
        Column(date_column, Date, primary_key=True),
    )


def _engine() -> object:
    return create_engine(
        "sqlite://", poolclass=pool.StaticPool, connect_args={"check_same_thread": False}
    )


@pytest.fixture
def lagging_warehouse():
    """stock_daily: dwd one day behind, akshare two, ths' table gone."""
    metadata = MetaData()
    dwd = _bar_table(metadata, dwd_table("stock_daily"), "trade_date")
    ods_akshare = _bar_table(metadata, ods_table("stock_daily", "akshare"), "日期")

    engine = _engine()
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(dwd),
            [
                {"symbol": "600000", "trade_date": date(2026, 9, 23)},
                {"symbol": "600000", "trade_date": date(2026, 9, 24)},
            ],
        )
        connection.execute(insert(ods_akshare), [{"symbol": "600000", "日期": date(2026, 9, 23)}])
    yield engine
    engine.dispose()


@pytest.fixture
def control_db() -> object:
    """An empty ``pipeline_progress``; each test records the runs it means."""
    engine = _control_engine()
    yield engine
    engine.dispose()


@pytest.fixture
def fresh_warehouse():
    """stock_daily and both of its legs reach the expected date."""
    metadata = MetaData()
    dwd = _bar_table(metadata, dwd_table("stock_daily"), "trade_date")
    ods_akshare = _bar_table(metadata, ods_table("stock_daily", "akshare"), "日期")
    ods_ths = _bar_table(metadata, ods_table("stock_daily", "ths"), "trade_date")

    engine = _engine()
    metadata.create_all(engine)
    with engine.begin() as connection:
        for table in (dwd, ods_akshare, ods_ths):
            column = table.primary_key.columns.keys()[1]
            connection.execute(
                insert(table),
                [
                    {"symbol": "600000", column: date(2026, 9, 24)},
                    {"symbol": "600000", column: EXPECTED},
                ],
            )
    yield engine
    engine.dispose()


class _Recorder:
    """A broadcast channel that captures the frames it accepted."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.frames: list[dict] = []
        self.refuse = refuse

    async def __call__(self, message: dict) -> None:
        if self.refuse:
            raise RuntimeError("no websocket clients")
        self.frames.append(message)


class TestCollection:
    """What the matrix measures, and what it says it could not."""

    async def test_both_layers_are_read_for_a_registered_domain(self, lagging_warehouse) -> None:
        reports, scope = alert_matrix.collect_freshness(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",)
        )

        # stock_daily registers two legs and both carry a field mapping.
        assert scope == alert_matrix.MatrixScope(domains=1, source_legs=2, unmapped_legs=0)
        by_subject = {(report.source, report.field): report for report in reports}
        assert by_subject[(None, "trade_date")].latest == date(2026, 9, 24)
        assert by_subject[(None, "trade_date")].lag_days == 1
        # The ods layer keeps the source's own column name, not the contract's.
        assert by_subject[("akshare", "日期")].lag_days == 2
        assert by_subject[("ths", "trade_date")].status == "missing"
        assert all(report.expected == EXPECTED for report in reports)

    async def test_a_date_column_reads_whatever_the_driver_hands_back(
        self, lagging_warehouse
    ) -> None:
        """SQLite returns ``MAX(col)`` as text, MySQL as a date object.

        Both must produce a reading: a table that holds rows may not report
        ``missing`` because of a type the driver chose.
        """
        reports, _ = alert_matrix.collect_freshness(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",)
        )
        dwd = next(report for report in reports if report.source is None)

        assert dwd.status == "stale"
        assert dwd.latest == date(2026, 9, 24)

    async def test_every_leg_is_measured_or_counted_never_dropped(self, lagging_warehouse) -> None:
        """The invariant behind ``unmapped_legs``.

        A leg with no registered field mapping cannot be read; the scope has
        to account for it, or a silently unreadable surface would look
        healthy. 26 of the 33 registered legs are in that state today.
        """
        reports, scope = alert_matrix.collect_freshness(lagging_warehouse, expected=EXPECTED)
        legs = alert_matrix.registered_legs()

        assert scope.domains == len(legs)
        assert scope.source_legs + scope.unmapped_legs == sum(len(v) for v in legs.values())
        assert scope.unmapped_legs > 0  # measured on the registry, not invented
        assert len(reports) == scope.domains + scope.source_legs

    async def test_an_unregistered_domain_is_refused(self) -> None:
        """No invented domain tiers: the shipped template watched "p0"."""
        with pytest.raises(ValueError, match="not registered"):
            alert_matrix.registered_legs(("p0",))

    async def test_an_absent_table_reports_missing_instead_of_raising(
        self, lagging_warehouse
    ) -> None:
        reports, _ = alert_matrix.collect_freshness(
            lagging_warehouse, expected=EXPECTED, domains=("index_constituent",)
        )

        assert [report.status for report in reports if report.source is None] == ["missing"]

    def test_a_leg_with_no_field_mapping_is_the_measured_majority(self) -> None:
        """Which legs cannot be read, stated as a number rather than a surprise."""
        legs = alert_matrix.registered_legs()
        unmapped = [
            f"{domain}/{source}"
            for domain, sources in legs.items()
            for source in sources
            if not _leg_is_measurable(domain, source)
        ]

        assert sum(len(v) for v in legs.values()) == 33
        assert len(unmapped) == 26
        assert "stock_daily/akshare" not in unmapped
        assert "economy_cpi/fred" in unmapped


def _leg_is_measurable(domain: str, source: str) -> bool:
    from opendata.data.mapping import require_domain_mapping
    from opendata.pipeline.freshness import freshness_field

    try:
        return freshness_field(domain) in require_domain_mapping(source, domain).fields
    except LookupError:
        return False


class TestDelivery:
    """AC-18|01: 缺失 must trigger an alert that reaches a channel."""

    async def test_a_missing_table_alerts_critical_and_reaches_the_channel(
        self, lagging_warehouse
    ) -> None:
        recorder = _Recorder()

        run = await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",), broadcast=recorder
        )

        critical = [frame for frame in recorder.frames if frame["severity"] == "critical"]
        assert [(frame["domain"], frame["source"]) for frame in critical] == [
            ("stock_daily", "ths")
        ]
        assert critical[0]["rule"] == "freshness"
        assert critical[0]["layer"] == "ods"
        assert critical[0]["expected"] == EXPECTED.isoformat()
        assert run.delivered == len(recorder.frames) == len(run.alerts)

    async def test_a_stale_reading_warns_on_the_merged_layer(self, lagging_warehouse) -> None:
        recorder = _Recorder()

        await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",), broadcast=recorder
        )

        dwd_frames = [frame for frame in recorder.frames if frame["layer"] == "dwd"]
        assert [frame["source"] for frame in dwd_frames] == [None]
        assert [frame["severity"] for frame in dwd_frames] == ["warning"]
        assert "1 day(s) behind" in dwd_frames[0]["detail"]

    async def test_a_healthy_warehouse_sends_nothing(self, fresh_warehouse) -> None:
        """The counterfactual: delivery follows the reading, it does not run anyway."""
        recorder = _Recorder()

        run = await alert_matrix.run_alert_matrix(
            fresh_warehouse, expected=EXPECTED, domains=("stock_daily",), broadcast=recorder
        )

        assert run.alerts == ()
        assert recorder.frames == []
        assert run.delivered == 0

    async def test_the_verdict_follows_the_supplied_expectation_not_the_wall_clock(
        self, lagging_warehouse
    ) -> None:
        """Same rows, two expectations, opposite verdicts.

        ``expected`` is the trading calendar's answer (A4.7); nothing in the
        matrix reads today, so a weekend cannot page anyone.
        """
        on_time = _Recorder()
        behind = _Recorder()

        quiet = await alert_matrix.run_alert_matrix(
            lagging_warehouse,
            expected=date(2026, 9, 24),
            domains=("stock_daily",),
            broadcast=on_time,
        )
        loud = await alert_matrix.run_alert_matrix(
            lagging_warehouse,
            expected=date(2026, 10, 20),
            domains=("stock_daily",),
            broadcast=behind,
        )

        assert [report.lag_days for report in loud.reports if report.source is None] == [26]
        assert [alert.severity for alert in loud.alerts if alert.subject.endswith(":dwd")] == [
            "critical"
        ]
        assert behind.frames[0]["expected"] == "2026-10-20"
        # The same dwd table is on time one day earlier: the lag is real,
        # not a fixed red.
        assert [alert.subject for alert in quiet.alerts if alert.subject.endswith(":dwd")] == []

    async def test_a_channel_that_refuses_the_frame_does_not_lose_the_alerts(
        self, lagging_warehouse
    ) -> None:
        recorder = _Recorder(refuse=True)

        run = await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",), broadcast=recorder
        )

        assert run.alerts
        assert run.delivered == 0
        assert recorder.frames == []

    async def test_the_alerts_stay_decided_without_a_channel(self, lagging_warehouse) -> None:
        """``broadcast=None`` is the offline reading: same alerts, nothing sent."""
        without = await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",)
        )
        with_channel = await alert_matrix.run_alert_matrix(
            lagging_warehouse,
            expected=EXPECTED,
            domains=("stock_daily",),
            broadcast=_Recorder(),
        )

        assert [alert.subject for alert in without.alerts] == [
            alert.subject for alert in with_channel.alerts
        ]
        assert without.delivered == 0


class TestFrameShape:
    async def test_the_frame_carries_the_fields_a_subscriber_filters_on(
        self, lagging_warehouse
    ) -> None:
        recorder = _Recorder()

        await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",), broadcast=recorder
        )

        frame = recorder.frames[0]
        assert set(frame) == {
            "type",
            "rule",
            "severity",
            "domain",
            "layer",
            "source",
            "detail",
            "expected",
            "created_at",
        }
        assert frame["type"] == "data.freshness_alert"

    def test_the_merged_layer_keeps_source_none(self) -> None:
        from opendata.pipeline.freshness import Alert

        frame = alert_matrix.freshness_message(
            Alert("freshness", "critical", "stock_daily:dwd", "no data"),
            expected=EXPECTED,
        )

        assert frame["layer"] == "dwd"
        assert frame["source"] is None

    def test_the_frame_serializes_as_json(self) -> None:
        import json

        from opendata.pipeline.freshness import Alert

        frame = alert_matrix.freshness_message(
            Alert("freshness", "warning", "stock_daily:akshare", "lagging"),
            expected=EXPECTED,
        )

        assert json.loads(json.dumps(frame)) == frame


class TestFailureSignal:
    """AC-13|07 rows 1-2: the failure readings come from the shard checkpoint.

    ``pipeline_progress`` is a *shard* table, so a run's verdict has to be
    aggregated before it can be counted: one lost symbol in a batch of
    thousands is a failed window, and five failed shards in one window are
    one failure, not five. Getting either wrong turns a broken leg into a
    healthy reading, which is the same silent-green class the freshness rows
    had before ``alert_matrix`` gave them a caller.
    """

    async def test_one_lost_shard_makes_the_whole_run_a_failure(self, control_db: object) -> None:
        _record_run(
            control_db,
            domain="stock_daily",
            source="ths",
            window_end=date(2026, 9, 25),
            shards=[(ShardStatus.FAILED, "upstream 502"), (ShardStatus.DONE, None)],
        )

        failures, legs = alert_matrix.collect_failures(control_db)

        assert legs == 1
        assert [(f.domain, f.source, f.consecutive_failures) for f in failures] == [
            ("stock_daily", "ths", 1)
        ]
        assert failures[0].last_error == "upstream 502"

    async def test_the_streak_counts_runs_not_shards(self, control_db: object) -> None:
        """Two windows that each lost three symbols are a streak of two."""
        for day, count in ((23, 3), (24, 2)):
            _record_run(
                control_db,
                domain="stock_daily",
                source="ths",
                window_end=date(2026, 9, day),
                shards=[(ShardStatus.FAILED, "timeout")] * count,
            )

        failures, legs = alert_matrix.collect_failures(control_db)

        assert legs == 1
        assert [(f.consecutive_failures, f.last_error) for f in failures] == [(2, "timeout")]

    async def test_a_run_that_worked_again_breaks_the_streak(self, control_db: object) -> None:
        """Recovery is a fact about the newest run, and the leg stays counted."""
        _record_run(
            control_db,
            domain="stock_daily",
            source="ths",
            window_end=date(2026, 9, 24),
            shards=[(ShardStatus.FAILED, "boom")],
        )
        _record_run(
            control_db,
            domain="stock_daily",
            source="ths",
            window_end=date(2026, 9, 25),
            shards=[(ShardStatus.DONE, None)],
        )

        failures, legs = alert_matrix.collect_failures(control_db)

        assert failures == ()
        # legs == 1 is what keeps this a "recovered" reading rather than an
        # empty one; 0 would mean the pipeline never ran at all.
        assert legs == 1

    async def test_a_leg_that_never_worked_is_capped_at_the_window_read(
        self, control_db: object
    ) -> None:
        """:data:`FAILURE_RUN_WINDOW` bounds the number, not the outage."""
        for day in range(1, 13):
            _record_run(
                control_db,
                domain="stock_daily",
                source="akshare",
                window_end=date(2026, 9, day),
                shards=[(ShardStatus.FAILED, "refused")],
            )

        failures, _ = alert_matrix.collect_failures(control_db)

        assert [f.consecutive_failures for f in failures] == [alert_matrix.FAILURE_RUN_WINDOW]

    async def test_the_run_limit_reads_the_newest_windows(self, control_db: object) -> None:
        """A capped read under-reports; it never invents a longer streak."""
        for day in (23, 24, 25):
            _record_run(
                control_db,
                domain="stock_daily",
                source="ths",
                window_end=date(2026, 9, day),
                shards=[(ShardStatus.FAILED, f"day {day}")],
            )

        capped, _ = alert_matrix.collect_failures(control_db, run_limit=2)
        whole, _ = alert_matrix.collect_failures(control_db)

        assert [f.consecutive_failures for f in capped] == [2]
        assert [f.consecutive_failures for f in whole] == [3]

    async def test_the_error_text_is_cut_to_the_alert_budget(self, control_db: object) -> None:
        """A traceback cannot ride into every subscriber's frame."""
        _record_run(
            control_db,
            domain="stock_daily",
            source="ths",
            window_end=date(2026, 9, 25),
            shards=[(ShardStatus.FAILED, "x" * (alert_matrix.FAILURE_ERROR_CHARS * 3))],
        )

        failures, _ = alert_matrix.collect_failures(control_db)

        assert len(failures[0].last_error) == alert_matrix.FAILURE_ERROR_CHARS

    async def test_a_shard_that_failed_without_an_error_still_reports(
        self, control_db: object
    ) -> None:
        """``error`` is nullable: the failure is the fact, the text is a detail."""
        _record_run(
            control_db,
            domain="index_daily",
            source="ths",
            window_end=date(2026, 9, 25),
            shards=[(ShardStatus.FAILED, None)],
        )

        failures, _ = alert_matrix.collect_failures(control_db)

        assert [f.last_error for f in failures] == ["no error recorded"]

    async def test_an_empty_checkpoint_table_has_no_legs_to_score(self, control_db: object) -> None:
        failures, legs = alert_matrix.collect_failures(control_db)

        assert failures == ()
        assert legs == 0


class TestDiskAndPartitionSignals:
    """AC-13|07 rows 3-4: the two signals that are not database reads."""

    async def test_the_water_level_names_the_volume_it_measured(self, tmp_path) -> None:
        from opendata.pipeline.alert_matrix import collect_disk

        disk, measured = collect_disk(tmp_path / "warehouse" / "data")

        # ``data_dir`` is a relative default that may never have been created,
        # so the probe walks up: a reading that names its own subject is the
        # difference between "the volume is fine" and "nothing was measured".
        assert measured == str(tmp_path)
        assert disk is not None
        assert 0.0 <= disk.used_ratio <= 1.0

    async def test_no_path_is_no_claim(self) -> None:
        from opendata.pipeline.alert_matrix import collect_disk

        assert collect_disk(None) == (None, None)

    async def test_a_full_volume_alerts_critical(self, monkeypatch, tmp_path) -> None:
        recorder = _Recorder()

        monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(1000, 950))
        run = await alert_matrix.run_alert_matrix(
            _engine(),
            expected=EXPECTED,
            domains=("stock_daily",),
            disk_path=tmp_path,
            broadcast=recorder,
        )

        disk_alerts = [a for a in run.alerts if a.rule == "disk_water"]
        assert [(a.severity, a.subject) for a in disk_alerts] == [("critical", "warehouse volume")]
        assert run.scope.disk_path == str(tmp_path)
        disk_frames = [f for f in recorder.frames if f["rule"] == "disk_water"]
        assert [f["type"] for f in disk_frames] == ["data.disk_water_alert"]
        assert "95.0%" in disk_frames[0]["detail"]

    async def test_a_missing_year_alerts_without_the_matrix_touching_ddl(
        self, monkeypatch, lagging_warehouse
    ) -> None:
        statements = _spy_statements(lagging_warehouse)
        _patch_partition_states(
            monkeypatch, {"dwd_stock_daily": _yearly_partitions_through(date(2027, 1, 1))}
        )

        plans, partitioned = alert_matrix.collect_partitions(
            lagging_warehouse, current_year=2026, years_ahead=2, domains=("stock_daily",)
        )

        assert partitioned == 1
        assert plans == {"dwd_stock_daily": [("p2027", date(2028, 1, 1))]}
        # Reported, never repaired: the engine it reads is the production one.
        assert [s for s in statements if "ALTER" in s.upper() or "REORGANIZE" in s.upper()] == []

    async def test_the_partition_face_never_asks_for_a_repair(
        self, monkeypatch, lagging_warehouse
    ) -> None:
        """A call to ``ensure`` here would be DDL against the production warehouse."""
        from opendata.pipeline.partitions import PartitionMaintainer

        def refuse(self: object, *args: object, **kwargs: object) -> object:
            raise AssertionError("the alert matrix must not apply partitions")

        _patch_partition_states(
            monkeypatch, {"dwd_stock_daily": _yearly_partitions_through(date(2027, 1, 1))}
        )
        monkeypatch.setattr(PartitionMaintainer, "ensure", refuse)
        monkeypatch.setattr(PartitionMaintainer, "apply", refuse, raising=False)

        plans, _ = alert_matrix.collect_partitions(
            lagging_warehouse, current_year=2026, domains=("stock_daily",)
        )

        assert plans

    async def test_an_unpartitioned_table_is_not_reported_as_complete(
        self, monkeypatch, lagging_warehouse
    ) -> None:
        _patch_partition_states(monkeypatch, {})

        plans, partitioned = alert_matrix.collect_partitions(
            lagging_warehouse, current_year=2026, domains=("stock_daily",)
        )

        assert plans == {}
        # 0 here is the reading that says "this surface knows no partitions";
        # an empty plan with a positive count would say "every partition exists".
        assert partitioned == 0

    async def test_a_table_whose_horizon_reaches_is_quiet(
        self, monkeypatch, lagging_warehouse
    ) -> None:
        _patch_partition_states(
            monkeypatch, {"dwd_stock_daily": _yearly_partitions_through(date(2028, 1, 1))}
        )

        plans, partitioned = alert_matrix.collect_partitions(
            lagging_warehouse, current_year=2026, domains=("stock_daily",)
        )

        assert plans == {}
        assert partitioned == 1


class TestMatrixScopeHonesty:
    """What the matrix could not measure stays in the result, not in a default."""

    async def test_inputs_left_out_read_as_none_rather_than_zero(self, lagging_warehouse) -> None:
        run = await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",)
        )

        assert run.scope.failure_legs is None
        assert run.scope.partitioned_tables == 0
        assert run.scope.disk_path is None
        assert run.scope.as_dict()["failure_legs"] is None

    async def test_the_control_engine_turns_the_failure_rows_on(self, lagging_warehouse) -> None:
        control = _control_engine()
        # Three failed windows, not three failed shards: the escalation is
        # about a leg that has not worked since, which is a run-level fact.
        for day in (22, 23, 24):
            _record_run(
                control,
                domain="stock_daily",
                source="ths",
                window_end=date(2026, 9, day),
                shards=[(ShardStatus.FAILED, "refused")],
            )

        without = await alert_matrix.run_alert_matrix(
            lagging_warehouse, expected=EXPECTED, domains=("stock_daily",)
        )
        with_control = await alert_matrix.run_alert_matrix(
            lagging_warehouse,
            expected=EXPECTED,
            domains=("stock_daily",),
            control_engine=control,
        )

        assert [a.rule for a in without.alerts] == ["freshness"] * 3
        assert [a.rule for a in with_control.alerts] == [
            "freshness",
            "freshness",
            "freshness",
            "pipeline_failure",
            "consecutive_failures",
        ]
        assert with_control.scope.failure_legs == 1
        assert without.scope.failure_legs is None


class TestAlertFrames:
    """Each rule of the matrix renders what its own reading is about."""

    def test_every_rule_gets_a_type_a_subscriber_can_route_on(self) -> None:
        from opendata.pipeline.freshness import Alert

        rules = {
            "pipeline_failure": "stock_daily:ths",
            "consecutive_failures": "stock_daily:ths",
            "partition_missing": "dwd_stock_daily",
            "disk_water": "warehouse volume",
        }
        for rule, subject in rules.items():
            frame = alert_matrix.alert_message(
                Alert(rule, "critical", subject, "detail"), expected=EXPECTED
            )
            assert frame["type"] == f"data.{rule}_alert"
            assert frame["rule"] == rule
            assert frame["subject"] == subject
            assert frame["detail"] == "detail"
            assert frame["created_at"].startswith("20")

    def test_a_pipeline_frame_splits_the_leg_the_alert_is_about(self) -> None:
        from opendata.pipeline.freshness import Alert

        frame = alert_matrix.alert_message(
            Alert("pipeline_failure", "warning", "index_daily:akshare", "failed"),
            expected=EXPECTED,
        )

        assert frame["domain"] == "index_daily"
        assert frame["source"] == "akshare"

    def test_a_partition_or_disk_frame_has_no_domain_to_report(self) -> None:
        """Their subject is a table or a volume, so inventing a domain would lie."""
        from opendata.pipeline.freshness import Alert

        for rule in ("partition_missing", "disk_water"):
            frame = alert_matrix.alert_message(Alert(rule, "critical", "x", "d"), expected=EXPECTED)
            assert "domain" not in frame
            assert "expected" not in frame

    def test_freshness_keeps_its_own_richer_frame(self) -> None:
        from opendata.pipeline.freshness import Alert

        frame = alert_matrix.alert_message(
            Alert("freshness", "critical", "stock_daily:ths", "gone"), expected=EXPECTED
        )

        assert frame["type"] == "data.freshness_alert"
        assert frame["layer"] == "ods"
        assert frame["expected"] == EXPECTED.isoformat()


class TestAllFourKindsAtOnce:
    """The criterion's own list, in one run: 失败 / 连续失败 / 分区缺失 / 磁盘水位."""

    async def test_one_run_delivers_every_kind_the_matrix_decides(
        self, monkeypatch, lagging_warehouse, tmp_path
    ) -> None:
        recorder = _Recorder()
        control = _control_engine()
        for day in (22, 23, 24):
            _record_run(
                control,
                domain="stock_daily",
                source="ths",
                window_end=date(2026, 9, day),
                shards=[(ShardStatus.FAILED, "upstream refused")],
            )
        _patch_partition_states(
            monkeypatch, {"dwd_stock_daily": _yearly_partitions_through(date(2027, 1, 1))}
        )

        monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(1000, 950))
        run = await alert_matrix.run_alert_matrix(
            lagging_warehouse,
            expected=EXPECTED,
            domains=("stock_daily",),
            control_engine=control,
            current_year=2026,
            disk_path=tmp_path,
            broadcast=recorder,
        )

        assert {a.rule for a in run.alerts} == {
            "freshness",
            "pipeline_failure",
            "consecutive_failures",
            "partition_missing",
            "disk_water",
        }
        # One frame per alert, and each frame says which rule it belongs to:
        # a subscriber paging on severity has to be able to tell a missing
        # partition from a vanished table without parsing prose.
        assert {f["rule"] for f in recorder.frames} == {a.rule for a in run.alerts}
        assert run.delivered == len(run.alerts) == len(recorder.frames)
        assert run.scope.as_dict()["partitioned_tables"] == 1
        assert run.scope.as_dict()["failure_legs"] == 1
        assert run.scope.as_dict()["disk_path"] == str(tmp_path)


def _disk_usage(total: int, used: int) -> object:
    """A ``shutil.disk_usage`` shaped result - it is a named tuple, not a tuple."""
    from collections import namedtuple

    Usage = namedtuple("usage", "total used free")
    return Usage(total=total, used=used, free=total - used)


def _control_engine() -> object:
    """A scratch control database: the shard checkpoint, and nothing else.

    ``pipeline_progress`` is not a warehouse table; reading it from the
    warehouse would find nothing and report every leg as healthy, which is
    why the producer takes its engine separately and these tests do too.
    """
    from opendata.models.pipeline import PipelineProgress

    engine = _engine()
    PipelineProgress.__table__.create(engine)
    return engine


def _record_run(
    engine: object,
    *,
    domain: str,
    source: str,
    window_end: date,
    shards: list[tuple[ShardStatus, str | None]],
) -> str:
    """Insert one run's shard rows; returns the ``pipeline_id`` it wrote."""
    from opendata.models.pipeline import PipelineProgress

    pipeline_id = f"incremental:{domain}:{source}:{window_end.isoformat()}"
    rows = [
        {
            "pipeline_id": pipeline_id,
            "domain": domain,
            "source": source,
            "shard": index,
            "window_start": window_end - timedelta(days=1),
            "window_end": window_end,
            "status": status,
            "rows_written": 0 if status is ShardStatus.FAILED else 12,
            "error": error,
            "updated_at": datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
            + timedelta(minutes=index),
        }
        for index, (status, error) in enumerate(shards)
    ]
    with engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(insert(PipelineProgress.__table__), rows)
    return pipeline_id


def _yearly_partitions_through(upper_bound: date) -> list:
    """Yearly partitions p2024..p<year> plus the MAXVALUE fallback."""
    from opendata.pipeline.partitions import PartitionState

    states = [
        PartitionState(name=f"p{year}", upper_bound=date(year + 1, 1, 1))
        for year in range(2024, upper_bound.year)
    ]
    states.append(PartitionState(name="pmax", upper_bound=None))
    return states


def _patch_partition_states(monkeypatch: pytest.MonkeyPatch, states: dict[str, list]) -> None:
    """Stand in for ``information_schema.PARTITIONS``.

    The real reader is MySQL-only (an unpartitioned InnoDB table reports one
    NULL partition row), so a SQLite fixture cannot answer "is this table
    partitioned" - and the alternative, pointing the tests at a MySQL
    container, would make a unit test of an alert matrix depend on a server.
    """
    from opendata.pipeline.partitions import PartitionMaintainer

    monkeypatch.setattr(PartitionMaintainer, "is_partitioned", lambda self, table: table in states)
    monkeypatch.setattr(
        PartitionMaintainer,
        "state",
        lambda self, table: list(states.get(table, [])),
    )


def _spy_statements(engine: object) -> list[str]:
    """Every statement one engine executes, to prove a face sends no DDL."""
    sent: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sent.append(statement)

    return sent
