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

from datetime import date

import pytest
from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, insert, pool

from opendata.data.domains import dwd_table, ods_table
from opendata.data.providers import register_providers
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
