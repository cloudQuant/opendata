"""AC-11 的入口级 HTTP 面：真路由 + SQLite 仓库替身（design §10.1）。

``tests/test_data_query_api.py`` 把不需要仓库的路径（枚举 422、404、401、
OpenAPI 参数表）留在门禁里，把需要 MySQL 专有 DDL 的路径标成 ``e2e``。
中间那一段两边都没碰：``fields`` 白名单在 HTTP 上真的返回 400、注入串
真的只是一个绑定值、``source`` 不是这条域注册过的腿就返回 400、CSV 导出
真的走了公式转义、``adjust`` 真的按因子表在位与否给 200/400/501、
``diff-report`` 真的把差异行交出来。

这一模块补的是那一段。替身是 **真 SQLAlchemy 引擎**打在 SQLite 上：
``_inspect_columns`` 里 ``sqlalchemy.inspect(connection)`` 需要一个真的
dialect，鸭子类型的假 engine 会被它的 ``except Exception: return []`` 收成
空列，于是每个请求都按「表不存在」的 404 路径走，判据面就废了。表结构从
``opendata.pipeline.ddl`` 的生成器读出来而不是在这里手抄：``fields`` 白名单
是按表的真实列校验的，抄一份三列的假表会让 ``fields=`` 因为仓库里根本没有
的理由返回 200 或 400。

留在 ``e2e`` 的只有 SQLite 给不了的方言面（分区裁剪、EXPLAIN、MySQL 专有
ods DDL）。
"""

import csv
import io
from datetime import date, datetime, timedelta

import pytest
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
    event,
    func,
    insert,
    pool,
    select,
    table,
    text,
)

from opendata.api.data_query import DIFF_TABLE, FACTOR_TABLE, get_warehouse_engine
from opendata.data.domains import dwd_table, ods_table
from opendata.data.providers import register_providers
from opendata.main import app
from opendata.pipeline.ddl import DWD_TRACE_COLUMNS, contract_columns
from opendata.pipeline.query import MAX_PAGE_SIZE

pytestmark = pytest.mark.integration

QUERY_URL = "/api/v1/data/equity/stock_daily"
EXPORT_URL = "/api/v1/data/equity/stock_daily/export"
DIFF_URL = "/api/v1/data/domains/stock_daily/diff-report"

#: ``build_data_select`` 在没有区间时用 ``date.today()`` 回填 365 天窗口。
#: 5 天与 400 天恒在/恒出该窗口，跨午夜跑也不改判据的意思。
RECENT = date.today() - timedelta(days=5)
BEFORE_WINDOW = date.today() - timedelta(days=400)

#: 会被电子表格当公式执行的符号前缀（design §10.1 / OWASP）。
FORMULA_SYMBOLS = ("=cmd|'/C calc'!A0", "+1+1", "@SUM(1+1)", "\t=hybrid")

#: The merged daily table the whole module reads and exports.
BARS_TABLE = dwd_table("stock_daily")
FINANCIAL_TABLE = dwd_table("financial_statement")
THS_ODS_TABLE = ods_table("stock_daily", "ths")
AKSHARE_ODS_TABLE = ods_table("stock_daily", "akshare")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sqla_type(sql_type: str):
    """The SQLite column type standing in for one generated MySQL type."""
    if sql_type.startswith(("varchar", "char")):
        return String
    if sql_type == "double":
        return Float
    if sql_type == "date":
        return Date
    if sql_type == "datetime":
        return DateTime
    return Integer  # bigint / tinyint(1)


def _bars_table(md: MetaData) -> Table:
    """``dwd_stock_daily`` with the columns the DDL generator declares."""
    return Table(
        BARS_TABLE,
        md,
        *[
            Column(
                column.name,
                _sqla_type(column.sql_type),
                nullable=column.nullable,
                primary_key=column.name in ("symbol", "trade_date"),
            )
            for column in [*contract_columns("stock_daily"), *DWD_TRACE_COLUMNS]
        ],
    )


def _financial_statement_table(md: MetaData) -> Table:
    """Small DWD financial table with the report and announcement dates."""
    return Table(
        FINANCIAL_TABLE,
        md,
        Column("symbol", String(64), primary_key=True),
        Column("statement_type", String(32), nullable=False),
        Column("report_period", Date, primary_key=True),
        Column("announce_date", Date),
        Column("item", String(64), nullable=False),
        Column("value", Float),
        Column("revision", Integer, nullable=False),
        Column("source", String(32), nullable=False),
        Column("_merged_at", DateTime, nullable=False),
        Column("_diff_flag", Integer, nullable=False),
        Column("_as_of", Date),
    )


def _ods_table(md: MetaData, source: str, *, include_date: bool = True) -> Table:
    """A raw daily ODS table with its native source columns and primary key."""
    if source == "ths":
        business_columns = [Column("thscode", String(64), primary_key=True)]
        if include_date:
            business_columns.append(Column("trade_date", Date, primary_key=True))
        business_columns.extend(
            [
                Column("open_price", Float),
                Column("high_price", Float),
                Column("low_price", Float),
                Column("close_price", Float),
                Column("volume", Float),
                Column("turnover", Float),
            ]
        )
    elif source == "akshare":
        business_columns = [Column("股票代码", String(64), primary_key=True)]
        if include_date:
            business_columns.append(Column("日期", Date, primary_key=True))
        business_columns.extend(
            [
                Column("开盘", Float),
                Column("最高", Float),
                Column("最低", Float),
                Column("收盘", Float),
                Column("成交量", Float),
                Column("成交额", Float),
            ]
        )
    else:
        raise ValueError(f"unsupported ODS test source {source!r}")
    return Table(
        ods_table("stock_daily", source),
        md,
        *business_columns,
        Column("_source", String(32), nullable=False),
        Column("_fetched_at", DateTime, nullable=False),
        Column("_batch_id", String(36), nullable=False),
    )


def _raw_ods_row(source: str, symbol: str, day: date) -> dict[str, object]:
    """A source-shaped stock row; AKShare volume remains in its raw lots unit."""
    metadata = {
        "_source": source,
        "_fetched_at": datetime(2026, 9, 26, 8, 0, 0),
        "_batch_id": f"{source}-{symbol}-{day.isoformat()}",
    }
    if source == "ths":
        return {
            "thscode": symbol,
            "trade_date": day,
            "open_price": 21.0,
            "high_price": 24.0,
            "low_price": 20.0,
            "close_price": 23.5,
            "volume": 1200.0,
            "turnover": 2400.0,
            **metadata,
        }
    return {
        "股票代码": symbol,
        "日期": day,
        "开盘": 21.0,
        "最高": 24.0,
        "最低": 20.0,
        "收盘": 23.5,
        "成交量": 12.0,
        "成交额": 2400.0,
        **metadata,
    }


def _diffs_table(md: MetaData) -> Table:
    """``dq_diff_report`` as alembic 0002 declares it.

    One sampled difference per row, keyed by ``(batch_id, biz_key, field)``
    so re-running a batch updates the same rows instead of duplicating them.
    """
    return Table(
        DIFF_TABLE,
        md,
        Column("batch_id", String(64), primary_key=True),
        Column("domain", String(64), nullable=False),
        Column("source_a", String(32), nullable=False),
        Column("source_b", String(32), nullable=False),
        Column("biz_key", String(255), primary_key=True),
        Column("field", String(64), primary_key=True),
        Column("value_a", String(255)),
        Column("value_b", String(255)),
        Column("deviation", Float),
        Column("verdict", String(16), nullable=False),
        Column("checked_at", DateTime, nullable=False),
    )


def _factors_table(md: MetaData, *, affine: bool = False) -> Table:
    """A legacy factor table, optionally extended with affine coefficients."""
    columns = [
        Column("symbol", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("qfq_factor", Float, nullable=False),
        Column("hfq_factor", Float, nullable=False),
    ]
    if affine:
        columns.extend(
            [
                Column("qfq_scale", Float),
                Column("qfq_offset", Float),
                Column("hfq_scale", Float),
                Column("hfq_offset", Float),
                Column("adjustment_version", String(32)),
                Column("legacy_source", String(64)),
            ]
        )
    return Table(FACTOR_TABLE, md, *columns)


def _bar(
    symbol: str,
    day: date,
    *,
    open_: float = 1.0,
    close: float = 2.0,
    source: str = "ths",
) -> dict:
    return {
        "symbol": symbol,
        "trade_date": day,
        "open": open_,
        "high": max(open_, close),
        "low": min(open_, close),
        "close": close,
        "volume": 1000.0,
        "amount": 2000.0,
        "source": source,
        "_merged_at": datetime(2026, 9, 26, 8, 0, 0),
        "_diff_flag": 0,
        "_as_of": day,
    }


def _factor(symbol: str, day: date, *, qfq: float, hfq: float) -> dict:
    return {"symbol": symbol, "trade_date": day, "qfq_factor": qfq, "hfq_factor": hfq}


def _affine_factor(
    symbol: str,
    day: date,
    *,
    qfq: float,
    hfq: float,
    qfq_scale: float | None,
    qfq_offset: float | None,
    hfq_scale: float | None,
    hfq_offset: float | None,
    version: str | None = "affine-v1",
    legacy_source: str | None = "corporate-action-ratio-v1",
) -> dict:
    return {
        **_factor(symbol, day, qfq=qfq, hfq=hfq),
        "qfq_scale": qfq_scale,
        "qfq_offset": qfq_offset,
        "hfq_scale": hfq_scale,
        "hfq_offset": hfq_offset,
        "adjustment_version": version,
        "legacy_source": legacy_source,
    }


def _diff(
    biz_key: str, batch: str, field: str, *, verdict: str, checked: datetime, value_b: str
) -> dict:
    return {
        "batch_id": batch,
        "domain": "stock_daily",
        "source_a": "ths",
        "source_b": "akshare",
        "biz_key": biz_key,
        "field": field,
        "value_a": "170.05",
        "value_b": value_b,
        "deviation": 5.88e-05,
        "verdict": verdict,
        "checked_at": checked,
    }


@pytest.fixture(autouse=True)
def registered_capabilities() -> None:
    """ASGITransport does not run the lifespan; the registry drives the routing."""
    register_providers()


@pytest.fixture
def warehouse(test_client):
    """Stand up a warehouse stand-in and point the data API at it.

    Calling the fixture creates the two tables every reading needs plus the
    ones the caller asks for, and returns the engine so a test can read back
    the same database the handler read. Leaving ``factors`` out is exactly
    what the warehouse looked like before the adjustment domain landed.
    """
    engines: list = []

    def build(
        *,
        bars=(),
        diffs=(),
        factors=None,
        affine_factors: bool = False,
        ods_rows=None,
        missing_ods_dates=(),
        financial_rows=(),
    ):
        md = MetaData()
        tables = {"bars": _bars_table(md), "diffs": _diffs_table(md)}
        if financial_rows:
            tables["financial"] = _financial_statement_table(md)
        ods_tables = {
            source: _ods_table(md, source, include_date=source not in missing_ods_dates)
            for source in (ods_rows or {})
        }
        tables.update({f"ods:{source}": table for source, table in ods_tables.items()})
        engine = create_engine(
            "sqlite://", poolclass=pool.StaticPool, connect_args={"check_same_thread": False}
        )
        md.create_all(engine)
        if factors is not None:
            tables["factors"] = _factors_table(md, affine=affine_factors)
            tables["factors"].create(engine)
        with engine.begin() as connection:
            for name, rows in (("bars", bars), ("diffs", diffs), ("factors", factors or ())):
                if rows:
                    connection.execute(insert(tables[name]), list(rows))
            if financial_rows:
                connection.execute(insert(tables["financial"]), list(financial_rows))
            for source, rows in (ods_rows or {}).items():
                if rows:
                    connection.execute(insert(ods_tables[source]), list(rows))
        app.dependency_overrides[get_warehouse_engine] = lambda: engine
        engines.append(engine)
        return engine

    yield build

    app.dependency_overrides.pop(get_warehouse_engine, None)
    for engine in engines:
        engine.dispose()


def _stored_rows(engine) -> int:
    """Rows still in the stand-in warehouse: the proof that nothing executed."""
    count = select(func.count()).select_from(table(BARS_TABLE))
    with engine.connect() as connection:
        return int(connection.execute(count).one()[0])


class TestQuerySchema:
    """AC-11|01：默认 ``layer=dwd`` / ``adjust=none``，响应 schema 与契约一致，分页可走。"""

    async def test_the_defaults_are_the_merged_unadjusted_view(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT)])

        response = await test_client.get(
            QUERY_URL, params={"symbols": "600519"}, headers=_auth(test_user_token)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True and body["message"] == "success"
        # 调用方只给了一个符号：dwd 合并层、auto 路由、不复权。
        assert (body["data"]["layer"], body["data"]["source"], body["data"]["adjust"]) == (
            "dwd",
            "auto",
            "none",
        )
        assert (body["data"]["domain"], body["data"]["asset_class"]) == ("stock_daily", "equity")

    async def test_a_row_carries_exactly_the_selected_contract_columns(
        self, warehouse, test_client, test_user_token
    ):
        engine = warehouse(bars=[_bar("600519", RECENT, close=3.5)])

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "fields": "close,open"},
            headers=_auth(test_user_token),
        )

        data = response.json()["data"]
        # 业务键不请求也在投影里（否则一行无法被解释），顺序是表的顺序。
        assert data["columns"] == ["symbol", "trade_date", "open", "close"]
        assert list(data["rows"][0]) == data["columns"]
        assert data["count"] == len(data["rows"]) == 1
        assert data["rows"][0]["close"] == 3.5
        assert data["rows"][0]["trade_date"] == RECENT.isoformat()
        assert _stored_rows(engine) == 1

    async def test_pagination_walks_the_business_key_without_repeating_a_row(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar(symbol, RECENT) for symbol in ("600519", "000001", "600000")])

        pages = []
        for page in (1, 2, 3, 4):
            response = await test_client.get(
                QUERY_URL,
                params={"page": page, "page_size": 1},
                headers=_auth(test_user_token),
            )
            pages.append([row["symbol"] for row in response.json()["data"]["rows"]])

        assert pages == [["000001"], ["600000"], ["600519"], []]

    async def test_an_explicit_window_is_the_date_column_between_its_bounds(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT), _bar("600519", BEFORE_WINDOW)])

        response = await test_client.get(
            QUERY_URL,
            params={
                "symbols": "600519",
                "start": (RECENT - timedelta(days=1)).isoformat(),
                "end": (RECENT + timedelta(days=1)).isoformat(),
            },
            headers=_auth(test_user_token),
        )

        assert [row["trade_date"] for row in response.json()["data"]["rows"]] == [
            RECENT.isoformat()
        ]


class TestFinancialQueryFilters:
    async def test_report_period_and_announcement_date_filter_dwd_and_export(
        self, warehouse, test_client, test_user_token
    ):
        rows = [
            {
                "symbol": symbol,
                "statement_type": "income",
                "report_period": report_period,
                "announce_date": announce_date,
                "item": "net_profit",
                "value": value,
                "revision": 1,
                "source": "ths",
                "_merged_at": datetime(2026, 9, 26, 8, 0, 0),
                "_diff_flag": 0,
                "_as_of": report_period,
            }
            for symbol, report_period, announce_date, value in (
                ("AAA", RECENT, RECENT + timedelta(days=1), 1.0),
                ("BBB", RECENT, RECENT + timedelta(days=2), 2.0),
                (
                    "CCC",
                    RECENT - timedelta(days=20),
                    RECENT + timedelta(days=1),
                    3.0,
                ),
            )
        ]
        warehouse(financial_rows=rows)
        url = "/api/v1/data/equity/financial_statement"
        params = {
            "period": RECENT.isoformat(),
            "report_date": (RECENT + timedelta(days=1)).isoformat(),
            "start": (RECENT - timedelta(days=30)).isoformat(),
            "end": (RECENT + timedelta(days=3)).isoformat(),
            "fields": "announce_date,item,value",
        }

        response = await test_client.get(url, params=params, headers=_auth(test_user_token))
        export = await test_client.get(
            f"{url}/export", params=params, headers=_auth(test_user_token)
        )

        assert response.status_code == 200
        assert [(row["symbol"], row["item"]) for row in response.json()["data"]["rows"]] == [
            ("AAA", "net_profit")
        ]
        assert export.status_code == 200
        csv_rows = list(csv.DictReader(io.StringIO(export.text)))
        assert [(row["symbol"], row["item"]) for row in csv_rows] == [("AAA", "net_profit")]

    async def test_financial_filters_are_rejected_for_nonfinancial_domains(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT)])

        for filter_params in ({"period": RECENT.isoformat()}, {"report_date": RECENT.isoformat()}):
            for url in (QUERY_URL, EXPORT_URL):
                response = await test_client.get(
                    url, params=filter_params, headers=_auth(test_user_token)
                )
                assert response.status_code == 400
                assert "only for financial domains" in response.json()["detail"]

    async def test_invalid_window_is_rejected_before_json_or_csv_starts(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT)])
        params = {
            "start": (RECENT + timedelta(days=1)).isoformat(),
            "end": RECENT.isoformat(),
        }

        for url in (QUERY_URL, EXPORT_URL):
            response = await test_client.get(url, params=params, headers=_auth(test_user_token))
            assert response.status_code == 400
            assert "after end" in response.json()["detail"]


class TestRawOdsQueryAndExport:
    """ODS endpoints expose source-native columns, values, and key order."""

    async def test_ths_and_akshare_queries_and_exports_use_native_fields(
        self, warehouse, test_client, test_user_token
    ):
        ods_rows = {
            "ths": [
                _raw_ods_row("ths", "600519.SH", RECENT),
                _raw_ods_row("ths", "600519.SH", RECENT - timedelta(days=1)),
                _raw_ods_row("ths", "000001.SZ", RECENT),
            ],
            "akshare": [
                _raw_ods_row("akshare", "600519", RECENT),
                _raw_ods_row("akshare", "600519", RECENT - timedelta(days=1)),
                _raw_ods_row("akshare", "000001", RECENT),
            ],
        }
        warehouse(ods_rows=ods_rows)
        cases = (
            (
                "ths",
                "600519.SH",
                "close_price",
                ["thscode", "trade_date", "close_price"],
                "thscode",
                "close_price",
                "600519.SH",
                "23.5",
            ),
            (
                "akshare",
                "600519",
                "收盘,成交量",
                ["股票代码", "日期", "收盘", "成交量"],
                "股票代码",
                "成交量",
                "600519",
                "12.0",
            ),
        )

        for (
            source,
            symbol,
            fields,
            expected_columns,
            symbol_column,
            value_column,
            raw_symbol,
            raw_value,
        ) in cases:
            params = {
                "layer": "ods",
                "source": source,
                "symbols": symbol,
                "start": RECENT.isoformat(),
                "end": RECENT.isoformat(),
                "fields": fields,
            }
            response = await test_client.get(
                QUERY_URL, params=params, headers=_auth(test_user_token)
            )

            assert response.status_code == 200
            data = response.json()["data"]
            assert data["columns"] == expected_columns
            assert data["count"] == 1
            row = data["rows"][0]
            assert row[symbol_column] == raw_symbol
            assert row[value_column] == float(raw_value)
            assert row[expected_columns[1]] == RECENT.isoformat()

            export = await test_client.get(
                f"{QUERY_URL}/export",
                params={**params, "limit": 10},
                headers=_auth(test_user_token),
            )
            assert export.status_code == 200
            csv_rows = list(csv.reader(io.StringIO(export.text)))
            assert csv_rows[0] == expected_columns
            assert csv_rows[1][0] == raw_symbol
            assert csv_rows[1][1] == RECENT.isoformat()
            assert csv_rows[1][expected_columns.index(value_column)] == raw_value

    async def test_ods_symbols_are_bound_native_literals(
        self, warehouse, test_client, test_user_token
    ):
        engine = warehouse(
            ods_rows={
                "ths": [_raw_ods_row("ths", "600519.SH", RECENT)],
            }
        )
        injected = "600519.SH' OR 1=1 --"

        response = await test_client.get(
            QUERY_URL,
            params={"layer": "ods", "source": "ths", "symbols": injected},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        assert response.json()["data"]["rows"] == []
        with engine.connect() as connection:
            count = select(func.count()).select_from(table(THS_ODS_TABLE))
            assert connection.execute(count).scalar_one() == 1

    async def test_ods_rejects_contract_fields_adjustment_and_pivot_sources(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(ods_rows={"ths": [_raw_ods_row("ths", "600519.SH", RECENT)]})
        invalid_field = await test_client.get(
            QUERY_URL,
            params={"layer": "ods", "source": "ths", "fields": "close"},
            headers=_auth(test_user_token),
        )
        adjustment = await test_client.get(
            QUERY_URL,
            params={"layer": "ods", "source": "ths", "adjust": "qfq"},
            headers=_auth(test_user_token),
        )
        pivot = await test_client.get(
            "/api/v1/data/equity/financial_statement",
            params={"layer": "ods", "source": "akshare"},
            headers=_auth(test_user_token),
        )

        assert invalid_field.status_code == 400
        assert "unknown fields" in invalid_field.json()["detail"]
        assert adjustment.status_code == 400
        assert "raw ODS" in adjustment.json()["detail"]
        assert pivot.status_code == 400
        assert "one-to-one" in pivot.json()["detail"]

        filtered_pivot = await test_client.get(
            "/api/v1/data/equity/financial_statement",
            params={"layer": "ods", "source": "akshare", "period": RECENT.isoformat()},
            headers=_auth(test_user_token),
        )
        assert filtered_pivot.status_code == 400
        assert "one-to-one" in filtered_pivot.json()["detail"]

    async def test_ods_missing_its_mapped_time_column_returns_a_clear_400(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(ods_rows={"ths": []}, missing_ods_dates=("ths",))

        response = await test_client.get(
            QUERY_URL,
            params={"layer": "ods", "source": "ths"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400
        assert "schema does not match" in response.json()["detail"]


class TestDwdSourceProvenance:
    async def test_explicit_source_filters_dwd_rows_and_auto_stays_merged(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            bars=[
                _bar("600519", RECENT, source="ths"),
                _bar("000001", RECENT, source="akshare"),
            ]
        )
        symbols = "000001,600519"

        merged = await test_client.get(
            QUERY_URL, params={"symbols": symbols}, headers=_auth(test_user_token)
        )
        selected = await test_client.get(
            QUERY_URL,
            params={"symbols": symbols, "source": "ths"},
            headers=_auth(test_user_token),
        )
        export = await test_client.get(
            f"{QUERY_URL}/export",
            params={"symbols": symbols, "source": "akshare", "fields": "close"},
            headers=_auth(test_user_token),
        )

        assert [row["symbol"] for row in merged.json()["data"]["rows"]] == ["000001", "600519"]
        assert [row["symbol"] for row in selected.json()["data"]["rows"]] == ["600519"]
        assert export.status_code == 200
        assert export.text.splitlines() == [
            "symbol,trade_date,close",
            f"000001,{RECENT.isoformat()},2.0",
        ]


class TestParameterSafety:
    """AC-11|03：``fields`` 白名单、``source`` 枚举与 ``symbols`` 绑定参数在 HTTP 上的样子。"""

    async def test_a_fields_value_that_is_not_a_column_is_a_400_and_nothing_ran(
        self, warehouse, test_client, test_user_token
    ):
        engine = warehouse(bars=[_bar("600519", RECENT)])

        response = await test_client.get(
            QUERY_URL,
            params={"fields": "open;DROP TABLE dwd_stock_daily"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400
        assert "unknown fields" in response.json()["detail"]
        # 400 不是证据，表还在才是：那串文本自始至终是一个值。
        assert _stored_rows(engine) == 1

    async def test_a_symbol_that_looks_like_sql_stays_a_bound_literal(
        self, warehouse, test_client, test_user_token
    ):
        engine = warehouse(bars=[_bar("600519", RECENT)])
        injected = "600519'); DROP TABLE dwd_stock_daily; --"

        response = await test_client.get(
            QUERY_URL, params={"symbols": injected}, headers=_auth(test_user_token)
        )

        assert response.status_code == 200
        assert response.json()["data"]["rows"] == []  # 被查询的符号，不是被执行的语句
        assert _stored_rows(engine) == 1

    async def test_a_source_that_is_not_a_registered_leg_is_a_400(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT)])

        bogus = await test_client.get(
            QUERY_URL, params={"source": "eastmoney"}, headers=_auth(test_user_token)
        )
        leg = await test_client.get(
            QUERY_URL, params={"source": "ths"}, headers=_auth(test_user_token)
        )
        ods = await test_client.get(
            QUERY_URL,
            params={"layer": "ods", "source": "ths"},
            headers=_auth(test_user_token),
        )
        detail = bogus.json()["detail"]

        assert bogus.status_code == 400
        assert "not a registered leg" in detail
        # 可修的 400：把这条域有的腿名一起交回调用方，而不是只说"非法值"。
        assert set(detail.rsplit(": ", 1)[1].split(", ")) == {"auto", "akshare", "ths"}
        # 注册过的腿过了枚举这道门，下一道门才是它自己的 ods 表。
        assert leg.status_code == 200
        assert ods.status_code == 404

    async def test_export_and_diff_report_are_authenticated_too(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT)])

        anonymous = [
            await test_client.get(EXPORT_URL),
            await test_client.get(DIFF_URL),
        ]

        assert [response.status_code for response in anonymous] == [401, 401]


class TestCsvExport:
    """AC-11|03 的导出半：公式转义发生在 handler 里，不只在单元函数里。"""

    async def test_formula_prefixed_cells_are_neutralized_in_the_export(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar(symbol, RECENT) for symbol in FORMULA_SYMBOLS])

        response = await test_client.get(EXPORT_URL, headers=_auth(test_user_token))

        assert response.status_code == 200
        lines = response.text.splitlines()
        assert lines[0].split(",")[:2] == ["symbol", "trade_date"]
        first_cells = [line.split(",")[0] for line in lines[1:]]
        assert len(first_cells) == len(FORMULA_SYMBOLS)
        for cell in first_cells:
            assert cell.startswith("'"), f"{cell!r} 会被电子表格当公式执行"

    async def test_a_negative_price_stays_a_number_in_the_export(
        self, warehouse, test_client, test_user_token
    ):
        """转义只吃文本：把 ``-1.5`` 引成 ``'-1.5`` 会把价格列变成文本。"""
        warehouse(bars=[_bar("600519", RECENT, open_=-1.5, close=-2.5)])

        response = await test_client.get(
            EXPORT_URL, params={"fields": "open,close"}, headers=_auth(test_user_token)
        )

        assert response.text.splitlines()[1].split(",") == [
            "600519",
            RECENT.isoformat(),
            "-1.5",
            "-2.5",
        ]

    async def test_the_export_is_a_named_attachment_of_the_selected_columns(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT)])

        response = await test_client.get(
            EXPORT_URL, params={"fields": "close"}, headers=_auth(test_user_token)
        )

        assert response.headers["content-type"].startswith("text/csv")
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="equity_stock_daily.csv"'
        )
        assert response.text.splitlines()[0] == "symbol,trade_date,close"

    async def test_a_bad_field_is_answered_400_before_the_body_starts(
        self, warehouse, test_client, test_user_token
    ):
        """白名单在流开始之前跑：一旦开始流式就没有 400 可答了。"""
        warehouse(bars=[_bar("600519", RECENT)])

        response = await test_client.get(
            EXPORT_URL, params={"fields": "close` ; DROP TABLE x"}, headers=_auth(test_user_token)
        )

        assert response.status_code == 400
        assert "unknown fields" in response.json()["detail"]

    async def test_a_partial_final_batch_starts_after_rows_already_written(
        self, warehouse, test_client, test_user_token
    ):
        rows = [_bar(f"S{index:05d}", RECENT) for index in range(10_002)]
        warehouse(bars=rows)

        response = await test_client.get(
            EXPORT_URL, params={"limit": 10_001}, headers=_auth(test_user_token)
        )

        assert response.status_code == 200
        symbols = [row[0] for row in list(csv.reader(io.StringIO(response.text)))[1:]]
        assert len(symbols) == 10_001
        assert len(set(symbols)) == 10_001
        assert symbols == [f"S{index:05d}" for index in range(10_001)]

    async def test_a_streaming_database_failure_interrupts_without_leaking_sql(
        self, warehouse, monkeypatch
    ):
        from opendata.api.data_query import _csv_stream
        from opendata.pipeline.query import DataQuery

        engine = warehouse(bars=[])

        def fail_fetch(*_args, **_kwargs):
            raise RuntimeError("SELECT secret_column FROM secret_table")

        monkeypatch.setattr("opendata.api.data_query._fetch_rows", fail_fetch)
        stream = _csv_stream(
            engine,
            DataQuery(domain="stock_daily"),
            table=BARS_TABLE,
            columns=tuple(column.name for column in contract_columns("stock_daily")),
            key=("symbol", "trade_date"),
            selected=("symbol", "trade_date"),
            time_field=None,
            symbol_field=None,
            period_field=None,
            report_date_field=None,
            limit=1,
        )

        assert await anext(stream) == "symbol,trade_date\r\n"
        with pytest.raises(RuntimeError, match="CSV export failed before completion") as exc_info:
            await anext(stream)
        assert "SELECT" not in str(exc_info.value)


class TestServerSideAdjust:
    """AC-11|02 的服务端半：qfq/hfq 由 Bar + 因子表合成，缺因子就 fail closed。"""

    async def test_qfq_scales_the_prices_and_leaves_the_volume_alone(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            bars=[_bar("600519", RECENT, open_=2.0, close=4.0)],
            factors=[
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=0.5,
                    hfq=2.0,
                    qfq_scale=0.5,
                    qfq_offset=0.0,
                    hfq_scale=2.0,
                    hfq_offset=0.0,
                )
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "qfq"},
            headers=_auth(test_user_token),
        )

        row = response.json()["data"]["rows"][0]
        assert (row["open"], row["high"], row["low"], row["close"]) == (1.0, 2.0, 1.0, 2.0)
        # D10：复权是价格口径，量额不变。
        assert (row["volume"], row["amount"]) == (1000.0, 2000.0)

    async def test_hfq_scales_by_the_other_leg_of_the_same_factor_row(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            bars=[_bar("600519", RECENT, open_=2.0, close=4.0)],
            factors=[
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=0.5,
                    hfq=2.0,
                    qfq_scale=0.5,
                    qfq_offset=0.0,
                    hfq_scale=2.0,
                    hfq_offset=0.0,
                )
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "hfq"},
            headers=_auth(test_user_token),
        )

        row = response.json()["data"]["rows"][0]
        assert (row["open"], row["close"]) == (4.0, 8.0)

    async def test_legacy_factor_table_without_new_columns_keeps_multiplicative_rows(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            bars=[_bar("600519", RECENT, open_=2.0, close=4.0)],
            factors=[_factor("600519", RECENT, qfq=0.5, hfq=2.0)],
        )

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "qfq"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        row = response.json()["data"]["rows"][0]
        assert (row["open"], row["close"]) == (1.0, 2.0)

    async def test_affine_qfq_json_and_csv_apply_cash_offset_and_preserve_nonprice_fields(
        self, warehouse, test_client, test_user_token
    ):
        previous_day = RECENT - timedelta(days=1)
        warehouse(
            bars=[
                _bar("600519", previous_day, open_=100.0, close=120.0),
                _bar("600519", RECENT, open_=90.0, close=100.0),
            ],
            factors=[
                _affine_factor(
                    "600519",
                    previous_day,
                    qfq=0.9,
                    hfq=1.0,
                    qfq_scale=1.0,
                    qfq_offset=-5.0,
                    hfq_scale=1.0,
                    hfq_offset=0.0,
                ),
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=1.0,
                    hfq=10.0 / 9.0,
                    qfq_scale=1.0,
                    qfq_offset=0.0,
                    hfq_scale=1.0,
                    hfq_offset=5.0,
                ),
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={
                "symbols": "600519",
                "adjust": "qfq",
                "fields": "open,high,low,close,volume,amount",
            },
            headers=_auth(test_user_token),
        )
        export = await test_client.get(
            EXPORT_URL,
            params={
                "symbols": "600519",
                "adjust": "qfq",
                "fields": "close,volume,amount",
            },
            headers=_auth(test_user_token),
        )
        raw = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "none"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == export.status_code == raw.status_code == 200
        payload = response.json()["data"]
        assert payload["adjust"] == "qfq"
        assert payload["columns"] == [
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]
        adjusted_rows = payload["rows"]
        assert (adjusted_rows[0]["open"], adjusted_rows[0]["high"]) == (95.0, 115.0)
        assert (adjusted_rows[0]["low"], adjusted_rows[0]["close"]) == (95.0, 115.0)
        assert (adjusted_rows[1]["open"], adjusted_rows[1]["close"]) == (90.0, 100.0)
        assert [(row["volume"], row["amount"]) for row in adjusted_rows] == [
            (1000.0, 2000.0),
            (1000.0, 2000.0),
        ]
        assert "qfq_offset" not in adjusted_rows[0]
        assert "legacy_source" not in adjusted_rows[0]
        assert raw.json()["data"]["rows"][0]["close"] == 120.0
        exported = list(csv.reader(io.StringIO(export.text)))
        assert exported[0] == ["symbol", "trade_date", "close", "volume", "amount"]
        assert exported[1] == ["600519", previous_day.isoformat(), "115.0", "1000.0", "2000.0"]

    async def test_affine_hfq_applies_the_inverse_cash_offset(
        self, warehouse, test_client, test_user_token
    ):
        previous_day = RECENT - timedelta(days=1)
        warehouse(
            bars=[
                _bar("600519", previous_day, open_=100.0, close=120.0),
                _bar("600519", RECENT, open_=90.0, close=100.0),
            ],
            factors=[
                _affine_factor(
                    "600519",
                    previous_day,
                    qfq=0.9,
                    hfq=1.0,
                    qfq_scale=1.0,
                    qfq_offset=-5.0,
                    hfq_scale=1.0,
                    hfq_offset=0.0,
                ),
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=1.0,
                    hfq=10.0 / 9.0,
                    qfq_scale=1.0,
                    qfq_offset=0.0,
                    hfq_scale=1.0,
                    hfq_offset=5.0,
                ),
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "hfq"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        rows = response.json()["data"]["rows"]
        assert (rows[0]["open"], rows[0]["close"]) == (100.0, 120.0)
        assert (rows[1]["open"], rows[1]["close"]) == (95.0, 105.0)
        assert [(row["volume"], row["amount"]) for row in rows] == [
            (1000.0, 2000.0),
            (1000.0, 2000.0),
        ]

    @pytest.mark.parametrize(
        ("adjust", "expected_close"),
        [("qfq", 115.0), ("hfq", 125.0)],
    )
    @pytest.mark.parametrize(
        ("requested_fields", "expected_columns"),
        [
            ("close,volume,amount", ["symbol", "trade_date", "close", "volume", "amount"]),
            ("close", ["symbol", "trade_date", "close"]),
            ("volume", ["symbol", "trade_date", "volume"]),
        ],
    )
    async def test_adjusted_field_subsets_work_in_json_and_csv(
        self,
        warehouse,
        test_client,
        test_user_token,
        adjust,
        expected_close,
        requested_fields,
        expected_columns,
    ):
        warehouse(
            bars=[_bar("600519", RECENT, open_=100.0, close=120.0)],
            factors=[
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=0.9,
                    hfq=1.1,
                    qfq_scale=1.0,
                    qfq_offset=-5.0,
                    hfq_scale=1.0,
                    hfq_offset=5.0,
                )
            ],
            affine_factors=True,
        )
        params = {
            "symbols": "600519",
            "adjust": adjust,
            "fields": requested_fields,
        }

        response = await test_client.get(QUERY_URL, params=params, headers=_auth(test_user_token))
        export = await test_client.get(EXPORT_URL, params=params, headers=_auth(test_user_token))

        assert response.status_code == export.status_code == 200
        payload = response.json()["data"]
        assert payload["columns"] == expected_columns
        assert list(payload["rows"][0]) == expected_columns
        assert set(payload["rows"][0]) == set(expected_columns)
        if "close" in requested_fields.split(","):
            assert payload["rows"][0]["close"] == expected_close
        if "volume" in requested_fields.split(","):
            assert payload["rows"][0]["volume"] == 1000.0
        exported = list(csv.reader(io.StringIO(export.text)))
        assert exported[0] == expected_columns
        assert len(exported[1]) == len(expected_columns)
        if "close" in requested_fields.split(","):
            assert exported[1][expected_columns.index("close")] == str(expected_close)
        if "volume" in requested_fields.split(","):
            assert exported[1][expected_columns.index("volume")] == "1000.0"

    async def test_json_rejects_legacy_and_affine_rows_mixed_for_one_symbol(
        self, warehouse, test_client, test_user_token
    ):
        previous_day = RECENT - timedelta(days=1)
        warehouse(
            bars=[
                _bar("600519", previous_day, open_=100.0, close=120.0),
                _bar("600519", RECENT, open_=90.0, close=100.0),
            ],
            factors=[
                _affine_factor(
                    "600519",
                    previous_day,
                    qfq=0.9,
                    hfq=1.0,
                    qfq_scale=None,
                    qfq_offset=None,
                    hfq_scale=None,
                    hfq_offset=None,
                    version=None,
                    legacy_source="historical-ths",
                ),
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=1.0,
                    hfq=1.1,
                    qfq_scale=1.0,
                    qfq_offset=0.0,
                    hfq_scale=1.0,
                    hfq_offset=5.0,
                ),
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={
                "symbols": "600519",
                "adjust": "qfq",
                "fields": "close",
                "page_size": 1,
            },
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400
        assert "mixed adjustment versions" in response.json()["detail"]

    async def test_csv_rejects_adjustment_version_change_across_batches(
        self, warehouse, test_client, test_user_token, monkeypatch
    ):
        monkeypatch.setattr("opendata.api.data_query.EXPORT_BATCH_ROWS", 1)
        previous_day = RECENT - timedelta(days=1)
        warehouse(
            bars=[
                _bar("600519", previous_day, open_=100.0, close=120.0),
                _bar("600519", RECENT, open_=90.0, close=100.0),
            ],
            factors=[
                _affine_factor(
                    "600519",
                    previous_day,
                    qfq=0.9,
                    hfq=1.0,
                    qfq_scale=None,
                    qfq_offset=None,
                    hfq_scale=None,
                    hfq_offset=None,
                    version="legacy-multiplicative-v1",
                    legacy_source="historical-ths",
                ),
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=1.0,
                    hfq=1.1,
                    qfq_scale=1.0,
                    qfq_offset=0.0,
                    hfq_scale=1.0,
                    hfq_offset=5.0,
                ),
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            EXPORT_URL,
            params={"symbols": "600519", "adjust": "qfq", "fields": "close"},
            headers=_auth(test_user_token),
        )
        assert response.status_code == 400
        assert "mixed adjustment versions" in response.json()["detail"]

    async def test_json_paged_window_ignores_unmatched_and_other_symbol_factors(
        self, warehouse, test_client, test_user_token
    ):
        previous_day = RECENT - timedelta(days=1)
        warehouse(
            bars=[
                _bar("000001", RECENT, open_=20.0, close=22.0),
                _bar("600519", RECENT, open_=100.0, close=120.0),
            ],
            factors=[
                _affine_factor(
                    "000001",
                    RECENT,
                    qfq=0.5,
                    hfq=2.0,
                    qfq_scale=0.5,
                    qfq_offset=0.0,
                    hfq_scale=2.0,
                    hfq_offset=0.0,
                ),
                _affine_factor(
                    "600519",
                    previous_day,
                    qfq=0.9,
                    hfq=1.1,
                    qfq_scale=0.9,
                    qfq_offset=0.0,
                    hfq_scale=1.1,
                    hfq_offset=0.0,
                ),
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=0.9,
                    hfq=1.1,
                    qfq_scale=None,
                    qfq_offset=None,
                    hfq_scale=None,
                    hfq_offset=None,
                    version="legacy-multiplicative-v1",
                    legacy_source="historical-ths",
                ),
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={
                "symbols": "000001,600519",
                "start": RECENT.isoformat(),
                "end": RECENT.isoformat(),
                "adjust": "qfq",
                "page_size": 1,
            },
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        assert len(response.json()["data"]["rows"]) == 1

    async def test_adjustment_reads_only_current_batch_factor_keys_in_sql(
        self, warehouse, test_client, test_user_token, monkeypatch
    ):
        monkeypatch.setattr("opendata.api.data_query.MAX_PAGE_SIZE", 1)
        trade_days = [RECENT - timedelta(days=2), RECENT - timedelta(days=1), RECENT]
        orphan_day = RECENT - timedelta(days=3)
        engine = warehouse(
            bars=[_bar("600519", day, close=20.0 + index) for index, day in enumerate(trade_days)],
            factors=[
                _affine_factor(
                    "600519",
                    day,
                    qfq=1.0,
                    hfq=1.0,
                    qfq_scale=1.0,
                    qfq_offset=0.0,
                    hfq_scale=1.0,
                    hfq_offset=0.0,
                )
                for day in trade_days
            ]
            + [
                _affine_factor(
                    "600519",
                    orphan_day,
                    qfq=1.0,
                    hfq=1.0,
                    qfq_scale=None,
                    qfq_offset=None,
                    hfq_scale=None,
                    hfq_offset=None,
                    version="affine-v9",
                )
            ],
            affine_factors=True,
        )
        factor_queries: list[tuple[str, tuple[str, ...]]] = []

        def record_factor_select(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT * FROM `dwd_stock_adjust`"):
                values = parameters.values() if isinstance(parameters, dict) else parameters
                factor_queries.append((statement, tuple(str(value) for value in values)))

        event.listen(engine, "before_cursor_execute", record_factor_select)
        try:
            response = await test_client.get(
                QUERY_URL,
                params={
                    "symbols": "600519",
                    "start": trade_days[0].isoformat(),
                    "end": trade_days[-1].isoformat(),
                    "adjust": "qfq",
                    "page_size": 1,
                },
                headers=_auth(test_user_token),
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_factor_select)

        assert response.status_code == 200
        assert len(factor_queries) == 4  # three preflight batches and the returned page
        assert all(statement.count("`trade_date` =") == 1 for statement, _ in factor_queries)
        assert all(len(values) == 2 for _, values in factor_queries)
        assert all(values[0] == "600519" for _, values in factor_queries)
        queried_days = {values[1] for _, values in factor_queries}
        assert queried_days == {day.isoformat() for day in trade_days}
        assert orphan_day.isoformat() not in queried_days

    @pytest.mark.parametrize(
        ("version", "qfq_offset", "message"),
        [
            ("affine-v1", None, "requires all four"),
            ("affine-v9", 0.0, "unknown adjustment version"),
        ],
    )
    async def test_incomplete_or_unknown_affine_rows_fail_closed(
        self, warehouse, test_client, test_user_token, version, qfq_offset, message
    ):
        warehouse(
            bars=[_bar("600519", RECENT)],
            factors=[
                _affine_factor(
                    "600519",
                    RECENT,
                    qfq=1.0,
                    hfq=1.0,
                    qfq_scale=1.0,
                    qfq_offset=qfq_offset,
                    hfq_scale=1.0,
                    hfq_offset=0.0,
                    version=version,
                )
            ],
            affine_factors=True,
        )

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "qfq"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400
        assert message in response.json()["detail"]

    async def test_a_bar_without_its_factor_row_is_a_400_not_a_half_adjusted_series(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            bars=[_bar("600519", RECENT), _bar("000001", RECENT)],
            factors=[_factor("600519", RECENT, qfq=0.5, hfq=1.0)],
        )

        response = await test_client.get(
            QUERY_URL, params={"adjust": "qfq"}, headers=_auth(test_user_token)
        )

        assert response.status_code == 400
        assert "no adjust factor" in response.json()["detail"]

    async def test_the_absent_factor_table_is_a_501_that_names_the_missing_table(
        self, warehouse, test_client, test_user_token
    ):
        """因子表落地前（A2.5/A4.1）不能返回一条悄悄算错的序列。"""
        warehouse(bars=[_bar("600519", RECENT)])

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "hfq"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 501
        detail = response.json()["detail"]
        assert FACTOR_TABLE in detail
        assert "not available" in detail

    async def test_an_empty_selection_does_not_claim_an_adjusted_series(
        self, warehouse, test_client, test_user_token
    ):
        """窗口里没有行时没有「已复权」可读：这一格是空，不是 501 也不是假数据。"""
        warehouse(bars=[_bar("600519", RECENT)])

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "NOT_A_SYMBOL", "adjust": "qfq"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        assert response.json()["data"]["rows"] == []


class TestDefaultWindow:
    """AC-11|04 的代码/行为半：不带区间也带着窗口谓词（EXPLAIN 那半另论）。"""

    async def test_an_unbounded_request_still_does_not_reach_outside_the_window(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[_bar("600519", RECENT), _bar("600519", BEFORE_WINDOW)])

        response = await test_client.get(QUERY_URL, headers=_auth(test_user_token))

        assert [row["trade_date"] for row in response.json()["data"]["rows"]] == [
            RECENT.isoformat()
        ]

    def test_the_generated_select_predicates_on_the_partition_key_and_caps_rows(self):
        """窗口的两面都在 SQL 文本上：分区列的谓词 + 行上限，缺一都不是全分区保护的半。"""
        from opendata.pipeline.query import DataQuery, build_data_select

        sql, params = build_data_select(
            DataQuery(domain="stock_daily", layer="dwd", symbols=("600519",)),
            table=dwd_table("stock_daily"),
            columns=tuple(column.name for column in contract_columns("stock_daily")),
            key=("symbol", "trade_date"),
            today=date(2026, 9, 26),
        )

        assert "`trade_date` >= :start" in sql
        assert "`trade_date` <= :end" in sql
        assert "`symbol` IN (:symbol_0)" in sql
        assert "LIMIT :limit" in sql
        assert (params["start"], params["end"]) == (date(2025, 9, 26), date(2026, 9, 26))
        assert params["limit"] == 200  # DEFAULT_PAGE_SIZE 仍是默认行上限


class TestDiffReport:
    """AC-11|05：差异报告可查——行、批次、序、上限，以及表不在时的读法。"""

    async def test_the_report_answers_the_sampled_differences_of_the_domain(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            diffs=[
                _diff(
                    "600519|2026-09-24",
                    "batch-a",
                    "close",
                    verdict="tolerated",
                    checked=datetime(2026, 9, 24, 18, 0, 0),
                    value_b="170.06",
                ),
                _diff(
                    "000001|2026-09-24",
                    "batch-a",
                    "volume",
                    verdict="flagged",
                    checked=datetime(2026, 9, 24, 18, 0, 0),
                    value_b="900",
                ),
                _diff(
                    "600000|2026-09-25",
                    "batch-b",
                    "close",
                    verdict="flagged",
                    checked=datetime(2026, 9, 25, 18, 0, 0),
                    value_b="8.11",
                ),
            ]
        )

        response = await test_client.get(DIFF_URL, headers=_auth(test_user_token))

        data = response.json()["data"]
        assert response.status_code == 200
        assert data["domain"] == "stock_daily"
        assert data["count"] == len(data["rows"]) == 3
        # 最新一批在前，同批按业务键：读的人第一眼就是最近的差。
        assert [
            (row["batch_id"], row["biz_key"], row["field"], row["verdict"]) for row in data["rows"]
        ] == [
            ("batch-b", "600000|2026-09-25", "close", "flagged"),
            ("batch-a", "000001|2026-09-24", "volume", "flagged"),
            ("batch-a", "600519|2026-09-24", "close", "tolerated"),
        ]
        assert set(data["rows"][0]) == {
            "batch_id",
            "source_a",
            "source_b",
            "biz_key",
            "field",
            "value_a",
            "value_b",
            "deviation",
            "verdict",
            "checked_at",
        }
        assert (data["rows"][0]["source_a"], data["rows"][0]["source_b"]) == ("ths", "akshare")
        assert data["rows"][0]["checked_at"].startswith("2026-09-25")

    async def test_batch_id_selects_one_cross_check_run(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            diffs=[
                _diff(
                    "600519|2026-09-24",
                    "batch-a",
                    "close",
                    verdict="tolerated",
                    checked=datetime(2026, 9, 24, 18, 0, 0),
                    value_b="170.06",
                ),
                _diff(
                    "600000|2026-09-25",
                    "batch-b",
                    "close",
                    verdict="flagged",
                    checked=datetime(2026, 9, 25, 18, 0, 0),
                    value_b="8.11",
                ),
            ]
        )

        response = await test_client.get(
            DIFF_URL, params={"batch_id": "batch-a"}, headers=_auth(test_user_token)
        )

        rows = response.json()["data"]["rows"]
        assert [row["batch_id"] for row in rows] == ["batch-a"]
        assert (rows[0]["value_a"], rows[0]["value_b"]) == ("170.05", "170.06")
        assert rows[0]["deviation"] == pytest.approx(5.88e-05)

    async def test_the_limit_is_bounded_like_the_query_page_size(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(bars=[])

        over_and_under = [
            await test_client.get(DIFF_URL, params={"limit": 0}, headers=_auth(test_user_token)),
            await test_client.get(
                DIFF_URL, params={"limit": MAX_PAGE_SIZE + 1}, headers=_auth(test_user_token)
            ),
        ]

        assert [response.status_code for response in over_and_under] == [422, 422]

    async def test_an_unmigrated_report_table_reads_empty_not_as_an_error(
        self, warehouse, test_client, test_user_token
    ):
        """仓里还没迁出 ``dq_diff_report`` 时，这一格是「没有差异可读」而不是 500。"""
        engine = warehouse(bars=[])
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE `{DIFF_TABLE}`"))

        response = await test_client.get(DIFF_URL, headers=_auth(test_user_token))

        assert response.status_code == 200
        assert response.json()["data"] == {"domain": "stock_daily", "rows": [], "count": 0}
