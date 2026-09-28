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
    func,
    insert,
    pool,
    select,
    table,
    text,
)

from opendata.api.data_query import DIFF_TABLE, FACTOR_TABLE, get_warehouse_engine
from opendata.data.domains import dwd_table
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


def _factors_table(md: MetaData) -> Table:
    """``dwd_stock_adjust``: the four columns ``_factor_rows`` selects."""
    return Table(
        FACTOR_TABLE,
        md,
        Column("symbol", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("qfq_factor", Float, nullable=False),
        Column("hfq_factor", Float, nullable=False),
    )


def _bar(symbol: str, day: date, *, open_: float = 1.0, close: float = 2.0) -> dict:
    return {
        "symbol": symbol,
        "trade_date": day,
        "open": open_,
        "high": max(open_, close),
        "low": min(open_, close),
        "close": close,
        "volume": 1000.0,
        "amount": 2000.0,
        "source": "ths",
        "_merged_at": datetime(2026, 9, 26, 8, 0, 0),
        "_diff_flag": 0,
        "_as_of": day,
    }


def _factor(symbol: str, day: date, *, qfq: float, hfq: float) -> dict:
    return {"symbol": symbol, "trade_date": day, "qfq_factor": qfq, "hfq_factor": hfq}


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

    def build(*, bars=(), diffs=(), factors=None):
        md = MetaData()
        tables = {"bars": _bars_table(md), "diffs": _diffs_table(md)}
        engine = create_engine(
            "sqlite://", poolclass=pool.StaticPool, connect_args={"check_same_thread": False}
        )
        md.create_all(engine)
        if factors is not None:
            tables["factors"] = _factors_table(md)
            tables["factors"].create(engine)
        with engine.begin() as connection:
            for name, rows in (("bars", bars), ("diffs", diffs), ("factors", factors or ())):
                if rows:
                    connection.execute(insert(tables[name]), list(rows))
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


class TestServerSideAdjust:
    """AC-11|02 的服务端半：qfq/hfq 由 Bar + 因子表合成，缺因子就 fail closed。"""

    async def test_qfq_scales_the_prices_and_leaves_the_volume_alone(
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

        row = response.json()["data"]["rows"][0]
        assert (row["open"], row["high"], row["low"], row["close"]) == (1.0, 2.0, 1.0, 2.0)
        # D10：复权是价格口径，量额不变。
        assert (row["volume"], row["amount"]) == (1000.0, 2000.0)

    async def test_hfq_scales_by_the_other_leg_of_the_same_factor_row(
        self, warehouse, test_client, test_user_token
    ):
        warehouse(
            bars=[_bar("600519", RECENT, open_=2.0, close=4.0)],
            factors=[_factor("600519", RECENT, qfq=0.5, hfq=2.0)],
        )

        response = await test_client.get(
            QUERY_URL,
            params={"symbols": "600519", "adjust": "hfq"},
            headers=_auth(test_user_token),
        )

        row = response.json()["data"]["rows"][0]
        assert (row["open"], row["close"]) == (4.0, 8.0)

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
