"""The catalog's five readings (AC-18|02) on a scratch SQLite warehouse.

The acceptance wording asks ``/api/v1/data/catalog`` to carry 覆盖行数 /
覆盖标的数 / 时间范围 / 各源最近更新 / 新鲜度状态 / 质量标记 per domain.
Before this round the row carried one of them (``status``) and a
capability leg list, and even that one reading was wrong for most domains:
the merged table was measured with the *source* column name (``日期``)
instead of the contract column (``trade_date``), so a table that held rows
answered ``missing``.

These tests run the handler directly rather than through ``app``: the
FastAPI lifespan runs MySQL-only DDL, so an offline HTTP test would either
recreate the production schema or skip. The HTTP shape stays covered by
``tests/test_data_query_api.py`` and ``tests/test_frontend_endpoint_contract.py``.
"""

from datetime import date

import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    pool,
    text,
)

from opendata.api import data_query
from opendata.api.dependencies import Principal
from opendata.data.domains import dwd_table, ods_table
from opendata.data.providers import register_providers
from opendata.models.user import User

EXPECTED = date(2026, 9, 25)


@pytest.fixture(autouse=True)
def registered_capabilities() -> None:
    """The catalog walks the capability registry; it must be populated."""
    register_providers()


@pytest.fixture
def warehouse(monkeypatch: pytest.MonkeyPatch):
    """A SQLite warehouse with one populated domain per quality state."""
    md = MetaData()
    dwd_stock_daily = Table(
        dwd_table("stock_daily"),
        md,
        Column("symbol", String, primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("close", Integer),
        Column("source", String),
        Column("_merged_at", DateTime),
        Column("_diff_flag", Integer, nullable=False),
        Column("_as_of", Date),
    )
    ods_stock_daily_akshare = Table(
        ods_table("stock_daily", "akshare"),
        md,
        Column("股票代码", String, primary_key=True),
        Column("日期", Date, primary_key=True),
        Column("收盘", Integer),
    )
    # stock_action carries the marker column and no differences: "clean".
    dwd_stock_action = Table(
        dwd_table("stock_action"),
        md,
        Column("symbol", String, primary_key=True),
        Column("ex_date", Date, primary_key=True),
        Column("_diff_flag", Integer, nullable=False),
    )
    # index_constituent has no marker column at all: "unmeasured", which is
    # a different claim from "clean" and must not be folded into it.
    dwd_index_constituent = Table(
        dwd_table("index_constituent"),
        md,
        Column("index_symbol", String, primary_key=True),
        Column("symbol", String, primary_key=True),
        Column("as_of", Date, primary_key=True),
    )
    # dq_diff_report exists and carries rows for exactly one domain.
    diff_report = Table(
        data_query.DIFF_TABLE,
        md,
        Column("id", Integer, primary_key=True),
        Column("domain", String),
    )

    engine = create_engine(
        "sqlite://", poolclass=pool.StaticPool, connect_args={"check_same_thread": False}
    )
    md.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(dwd_stock_daily),
            [
                _bar("600000", date(2026, 9, 22), 0),
                _bar("600000", date(2026, 9, 24), 1),
                _bar("000001", date(2026, 9, 23), 0),
                _bar("000002", date(2026, 9, 24), 0),
            ],
        )
        connection.execute(
            insert(ods_stock_daily_akshare),
            [
                {"股票代码": "600000", "日期": date(2026, 9, 22), "收盘": 1},
                {"股票代码": "600000", "日期": date(2026, 9, 23), "收盘": 1},
            ],
        )
        connection.execute(
            insert(dwd_stock_action),
            [
                {"symbol": "600000", "ex_date": date(2026, 9, 1), "_diff_flag": 0},
                {"symbol": "600000", "ex_date": date(2026, 9, 25), "_diff_flag": 0},
            ],
        )
        connection.execute(
            insert(dwd_index_constituent),
            [{"index_symbol": "000300", "symbol": "600000", "as_of": date(2026, 9, 25)}],
        )
        connection.execute(
            insert(diff_report),
            [
                {"id": 1, "domain": "stock_daily"},
                {"id": 2, "domain": "stock_daily"},
            ],
        )

    async def fixed_expected(engine, *, on=None):
        return EXPECTED

    monkeypatch.setattr(data_query, "_expected_data_date", fixed_expected)
    yield engine
    engine.dispose()


def _bar(symbol: str, day: date, flag: int) -> dict:
    return {
        "symbol": symbol,
        "trade_date": day,
        "close": 1,
        "source": "akshare",
        "_merged_at": None,
        "_diff_flag": flag,
        "_as_of": EXPECTED,
    }


def _principal(scopes: tuple[str, ...] | None = None) -> Principal:
    return Principal(user=User(id=1, username="catalog-probe"), api_key_id=None, scopes=scopes)


async def _catalog(engine, scopes: tuple[str, ...] | None = None) -> dict:
    response = await data_query.data_catalog(principal=_principal(scopes), engine=engine)
    return response.data


class TestFiveReadings:
    """AC-18|02: 覆盖 / 范围 / 各源最近更新 / 新鲜度 / 质量, one row per domain."""

    async def test_a_populated_domain_carries_every_reading(self, warehouse) -> None:
        rows = {row["domain"]: row for row in (await _catalog(warehouse))["domains"]}
        row = rows["stock_daily"]

        assert row["layer"] == "dwd"
        assert row["table"] == "dwd_stock_daily"
        # 覆盖: rows and distinct subjects, from the table's own primary key
        assert row["coverage"]["rows"] == 4
        assert row["coverage"]["symbols"] == 3
        # 时间范围
        assert (row["coverage"]["start"], row["coverage"]["end"]) == (
            "2026-09-22",
            "2026-09-24",
        )
        # 新鲜度状态, measured against the calendar date the caller pinned
        assert row["freshness_field"] == "trade_date"
        assert row["latest"] == "2026-09-24"
        assert row["lag_days"] == 1
        assert row["status"] == "stale"
        # 各源最近更新: the source's own column and its own table
        assert [leg["source"] for leg in row["sources"]] == ["akshare", "ths"]
        assert row["sources"][0]["latest"] == "2026-09-23"
        assert row["sources"][0]["lag_days"] == 2
        # 质量标记
        assert row["quality"]["diff_flagged"] == 1
        assert row["quality"]["diff_report_rows"] == 2
        assert row["quality"]["flag"] == "flagged"

    async def test_the_merged_layer_is_not_measured_with_a_source_column(self, warehouse) -> None:
        """The pre-fix false red: ``dwd_stock_daily`` read with ``日期``.

        A source column name does not exist in the merged table, so the
        aggregate returned nothing and the row answered ``missing`` while
        holding four rows. The dwd reading must use the contract column.
        """
        rows = {row["domain"]: row for row in (await _catalog(warehouse))["domains"]}

        assert rows["stock_daily"]["freshness_field"] == "trade_date"
        assert rows["stock_daily"]["status"] != "missing"
        assert rows["stock_daily"]["coverage"]["rows"] == 4

    async def test_a_clean_domain_says_clean_and_an_unmeasurable_one_says_so(
        self, warehouse
    ) -> None:
        rows = {row["domain"]: row for row in (await _catalog(warehouse))["domains"]}

        assert rows["stock_action"]["quality"] == {
            "diff_flagged": 0,
            "diff_report_rows": 0,
            "flag": "clean",
        }
        assert rows["index_constituent"]["quality"]["flag"] == "unmeasured"
        assert rows["index_constituent"]["quality"]["diff_flagged"] is None
        # "unmeasured" is about the marker column; freshness stays separate.
        assert rows["index_constituent"]["status"] == "fresh"

    async def test_the_totals_describe_the_rows_the_caller_got(self, warehouse) -> None:
        data = await _catalog(warehouse)

        assert data["domains_total"] == len(data["domains"])
        assert data["source_legs_total"] == sum(len(row["sources"]) for row in data["domains"])
        assert data["expected_data_date"] == EXPECTED.isoformat()
        assert {row["domain"] for row in data["domains"]} == set(data_query.registered_legs())


class TestUnmeasurableLegs:
    """What the catalog reports when a reading cannot be taken."""

    async def test_a_leg_with_no_field_mapping_is_unmapped_not_borrowed(self, warehouse) -> None:
        rows = {row["domain"]: row for row in (await _catalog(warehouse))["domains"]}
        legs = {leg["source"]: leg for leg in rows["index_daily"]["sources"]}

        assert legs["akshare"]["status"] == "unmapped"
        assert legs["akshare"]["latest"] is None
        assert legs["akshare"]["lag_days"] is None
        assert "unknown domain" in legs["akshare"]["reason"]
        # The unmapped leg does not borrow the other leg's reading: ths has
        # a mapping, so it is measured - and its table really is absent.
        assert legs["ths"]["status"] == "missing"

    async def test_an_absent_table_reads_missing_without_losing_the_row(self, warehouse) -> None:
        rows = {row["domain"]: row for row in (await _catalog(warehouse))["domains"]}
        row = rows["index_daily"]

        assert row["status"] == "missing"
        assert row["latest"] is None
        assert row["coverage"] is None
        assert row["quality"] is None
        # It is still a domain the caller can see and count.
        assert row["table"] == "dwd_index_daily"
        assert len(row["sources"]) == 2

    async def test_a_missing_diff_report_table_is_not_reported_as_zero(self, warehouse) -> None:
        """Absent detail table and empty detail table are different readings."""
        with warehouse.begin() as connection:
            connection.execute(text(f"DROP TABLE `{data_query.DIFF_TABLE}`"))

        rows = {row["domain"]: row for row in (await _catalog(warehouse))["domains"]}

        assert rows["stock_daily"]["quality"]["diff_report_rows"] is None
        assert rows["stock_daily"]["quality"]["flag"] == "flagged"  # the marker column still reads
        assert rows["stock_action"]["quality"]["flag"] == "clean"


class TestScopeFiltering:
    """The rewrite must not widen what a scoped key sees."""

    async def test_a_key_only_sees_the_domains_it_scoped(self, warehouse) -> None:
        data = await _catalog(warehouse, scopes=("stock_daily",))

        assert [row["domain"] for row in data["domains"]] == ["stock_daily"]
        assert data["domains_total"] == 1

    async def test_a_scope_selects_exactly_the_domains_it_names(self, warehouse) -> None:
        data = await _catalog(warehouse, scopes=("stock_action", "index_constituent"))

        assert {row["domain"] for row in data["domains"]} == {
            "stock_action",
            "index_constituent",
        }
        # Narrowing happens before the readings: the filtered-out domain's
        # measurements are not taken, not hidden afterwards.
        assert all(row["coverage"] is not None for row in data["domains"])

    async def test_a_scope_that_matches_no_domain_sees_nothing(self, warehouse) -> None:
        data = await _catalog(warehouse, scopes=("not_a_domain",))

        assert data["domains"] == []
        assert data["source_legs_total"] == 0


class TestTheFreshnessDoor:
    """AC-18|01's read side: one domain's 最新数据日期与滞后天数, with its baseline.

    The door is what a caller uses instead of the whole catalog, so it has to
    state the same reading - including the date the lag was measured against.
    A bare ``lag_days`` is not checkable by the party reading it.
    """

    async def _freshness(self, engine, domain: str, source: str | None = None) -> dict:
        response = await data_query.domain_freshness(
            domain, principal=_principal(), source=source, engine=engine
        )
        return response.data

    async def test_the_dwd_door_names_its_baseline(self, warehouse) -> None:
        data = await self._freshness(warehouse, "stock_daily")

        assert data["domain"] == "stock_daily"
        assert data["source"] is None
        assert data["field"] == "trade_date"
        assert data["latest"] == "2026-09-24"
        assert data["lag_days"] == 1
        assert data["status"] == "stale"
        assert data["expected_data_date"] == EXPECTED.isoformat()
        # Recomputable from what the payload ships, not from the wall clock.
        assert (
            date.fromisoformat(data["expected_data_date"]) - date.fromisoformat(data["latest"])
        ).days == data["lag_days"]

    async def test_the_two_doors_agree_on_the_same_domain(self, warehouse) -> None:
        data = await self._freshness(warehouse, "stock_daily")
        catalog = await _catalog(warehouse)
        row = next(item for item in catalog["domains"] if item["domain"] == "stock_daily")

        assert (data["field"], data["latest"], data["lag_days"], data["status"]) == (
            row["freshness_field"],
            row["latest"],
            row["lag_days"],
            row["status"],
        )
        assert data["expected_data_date"] == catalog["expected_data_date"]

    async def test_the_ods_door_measures_that_sources_own_table(self, warehouse) -> None:
        data = await self._freshness(warehouse, "stock_daily", source="akshare")

        # The merged layer holds 2026-09-24; this leg's own table stops at
        # 2026-09-23 in a column called 日期. Reading the merged answer here
        # would report a leg that does not exist.
        assert data["field"] == "日期"
        assert data["latest"] == "2026-09-23"
        assert data["lag_days"] == 2
        assert data["expected_data_date"] == EXPECTED.isoformat()

    async def test_a_leg_with_no_table_still_reports_the_baseline(self, warehouse) -> None:
        response = await data_query.domain_freshness(
            "stock_daily", principal=_principal(), source="ths", engine=warehouse
        )

        # `ths` has no ods table in this warehouse: the early return is a
        # different payload shape (no field, no lag), and a caller that only
        # checks `status` must still see what it was measured against.
        assert response.message == "no data"
        assert response.data["status"] == "missing"
        assert response.data["latest"] is None
        assert response.data["expected_data_date"] == EXPECTED.isoformat()
