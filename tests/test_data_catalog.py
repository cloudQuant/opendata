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
from typing import Any, get_type_hints

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
from opendata.data.capability import Capability
from opendata.data.domains import dwd_table, load_domains, ods_table
from opendata.data.protocol import Fetcher
from opendata.data.providers import register_providers
from opendata.data.providers.fred.models.search import FredSearchFetcher
from opendata.data.registry import ProviderRegistry, get_registry
from opendata.models.user import User

EXPECTED = date(2026, 9, 25)
PROVIDER_MODEL_DOMAINS = frozenset(
    {
        "fred_search",
        "fred_series",
        "bls_search",
        "bls_series",
        "equity_historical",
        "equity_quote",
    }
)


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


@pytest.fixture
def provider_model_domains_unmapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep metadata-only counterfactuals explicit as DomainSpecs are added."""
    domain_specs = load_domains()
    monkeypatch.setattr(
        data_query,
        "load_domains",
        lambda: {
            domain: spec
            for domain, spec in domain_specs.items()
            if domain not in PROVIDER_MODEL_DOMAINS
        },
    )


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
        legacy_domains = {
            domain for domain, spec in load_domains().items() if not spec.semantics_declared
        }
        expected_domains = set(data_query.registered_legs()) | {
            capability.domain for capability in get_registry().capabilities()
        }

        assert data["domains_total"] == len(data["domains"])
        assert data["source_legs_total"] == sum(len(row["sources"]) for row in data["domains"])
        assert data["expected_data_date"] == EXPECTED.isoformat()
        assert data["domains_total"] == len(expected_domains)
        assert {row["domain"] for row in data["domains"]} == expected_domains
        assert all(
            row["domain_defined"] is True and row["service_state"] == "warehouse"
            for row in data["domains"]
            if row["domain"] in legacy_domains
        )

    async def test_markets_and_registered_callable_details_follow_capabilities(
        self, warehouse
    ) -> None:
        """Market groups and function metadata come from the live registry."""
        data = await _catalog(warehouse)
        capabilities = get_registry().capabilities()
        rows = {row["domain"]: row for row in data["domains"]}
        expected_markets: dict[str, set[str]] = {}
        for capability in capabilities:
            market = capability.market.strip()
            if market:
                expected_markets.setdefault(capability.domain, set()).add(market)

        assert data["markets"] == sorted(
            {market for markets in expected_markets.values() for market in markets}
        )
        for domain, markets in expected_markets.items():
            assert rows[domain]["markets"] == sorted(markets)
            assert {
                (item["market"], item["source"], item["period"])
                for item in rows[domain]["capabilities"]
            } == {
                (cap.market, cap.source, cap.period) for cap in capabilities if cap.domain == domain
            }
        assert set(rows) == set(data_query.registered_legs()) | set(expected_markets)

        multi_market = next(
            domain for domain, markets in expected_markets.items() if len(markets) > 1
        )
        assert rows[multi_market]["markets"] == sorted(expected_markets[multi_market])

        stock_capability = next(
            capability
            for capability in capabilities
            if capability.domain == "stock_daily" and capability.source == "akshare"
        )
        function = next(
            item
            for item in rows["stock_daily"]["capabilities"]
            if item["source"] == stock_capability.source
        )
        fetcher = get_registry().resolve(
            stock_capability.asset_class,
            stock_capability.domain,
            period=stock_capability.period,
            market=stock_capability.market,
            source=stock_capability.source,
        )
        assert function["callable"] == {
            "module": type(fetcher).__module__,
            "name": f"{type(fetcher).__name__}.fetch",
        }
        assert function["endpoint"] == {
            "name": "query_domain_data",
            "method": "GET",
            "path": "/api/v1/data/equity/stock_daily",
            "query_filters": {"source": "akshare", "period": "1D"},
        }
        assert function["model_query_endpoint"] is None
        assert {parameter["name"] for parameter in function["parameters"]} >= {
            "symbol",
            "source",
            "market",
        }

    async def test_undeclared_model_domains_are_metadata_only(
        self, warehouse, provider_model_domains_unmapped
    ) -> None:
        data = await _catalog(warehouse)
        registry = get_registry()
        capabilities = registry.capabilities()
        declared_domains = set(data_query.load_domains())
        model_domains = {capability.domain for capability in capabilities} - declared_domains
        rows = {row["domain"]: row for row in data["domains"]}
        descriptors_by_domain: dict[str, set[str]] = {}
        for descriptor in registry.list_model_descriptors():
            descriptors_by_domain.setdefault(descriptor.domain, set()).add(descriptor.model)

        assert model_domains == {
            "bls_search",
            "bls_series",
            "equity_historical",
            "equity_quote",
            "fred_search",
            "fred_series",
        }
        for domain in model_domains:
            domain_capabilities = [cap for cap in capabilities if cap.domain == domain]
            row = rows[domain]

            assert row["domain_defined"] is False
            assert row["service_state"] == "metadata_only"
            assert row["reason"] == "domain_not_declared"
            assert row["status"] == "unmapped"
            assert row["display_name"] == " / ".join(sorted(descriptors_by_domain[domain]))
            assert row["asset_class"] == domain_capabilities[0].asset_class
            assert row["markets"] == sorted({cap.market for cap in domain_capabilities})
            for field in (
                "layer",
                "table",
                "freshness_field",
                "latest",
                "lag_days",
                "coverage",
                "quality",
            ):
                assert row[field] is None

            capability_rows = row["capabilities"]
            assert {
                (
                    item["asset_class"],
                    item["domain"],
                    item["period"],
                    item["market"],
                    item["source"],
                )
                for item in capability_rows
            } == {
                (cap.asset_class, cap.domain, cap.period, cap.market, cap.source)
                for cap in domain_capabilities
            }
            assert all(item["endpoint"] is None for item in capability_rows)
            assert all(item["model_query_endpoint"] is None for item in capability_rows)
            for capability in domain_capabilities:
                item = next(
                    item
                    for item in capability_rows
                    if item["source"] == capability.source
                    and item["period"] == capability.period
                    and item["market"] == capability.market
                )
                fetcher = registry.resolve(
                    capability.asset_class,
                    capability.domain,
                    period=capability.period,
                    market=capability.market,
                    source=capability.source,
                )
                assert item["verified"] == capability.verified
                assert item["callable"] == {
                    "module": type(fetcher).__module__,
                    "name": f"{type(fetcher).__name__}.fetch",
                }
                query_model = get_type_hints(type(fetcher).transform_query)["return"]
                assert {parameter["name"] for parameter in item["parameters"]} == set(
                    query_model.model_fields
                )
            assert {item["source"] for item in row["sources"]} == {
                cap.source for cap in domain_capabilities
            }
            for leg in row["sources"]:
                matching = [cap for cap in domain_capabilities if cap.source == leg["source"]]
                assert leg["verified"] == all(cap.verified for cap in matching)
                assert leg["table"] is None
                assert leg["status"] == "unmapped"
                assert leg["reason"] == "domain_not_declared"
                assert leg["latest"] is None
                assert leg["lag_days"] is None

    async def test_metadata_only_domains_bypass_warehouse_helpers_and_fetchers(
        self, warehouse, monkeypatch: pytest.MonkeyPatch, provider_model_domains_unmapped
    ) -> None:
        capabilities = get_registry().capabilities()
        unknown_domains = {cap.domain for cap in capabilities} - set(data_query.load_domains())
        assert unknown_domains

        def reject_unknown(domain: str, *, original):
            if domain in unknown_domains:
                raise AssertionError(f"warehouse helper called for {domain}")
            return original(domain)

        original_dwd_table = data_query.dwd_table
        original_display_name = data_query._display_name
        monkeypatch.setattr(
            data_query,
            "dwd_table",
            lambda domain: reject_unknown(domain, original=original_dwd_table),
        )
        monkeypatch.setattr(
            data_query,
            "_display_name",
            lambda domain: reject_unknown(domain, original=original_display_name),
        )

        original_freshness = data_query._freshness

        async def guarded_freshness(engine, domain, *args, **kwargs):
            if domain in unknown_domains:
                raise AssertionError(f"freshness helper called for {domain}")
            return await original_freshness(engine, domain, *args, **kwargs)

        monkeypatch.setattr(data_query, "_freshness", guarded_freshness)

        original_coverage = data_query._coverage_facts

        async def guarded_coverage(engine, domain, *args, **kwargs):
            if domain in unknown_domains:
                raise AssertionError(f"coverage helper called for {domain}")
            return await original_coverage(engine, domain, *args, **kwargs)

        monkeypatch.setattr(data_query, "_coverage_facts", guarded_coverage)

        original_source_leg = data_query._source_leg

        async def guarded_source_leg(engine, domain, *args, **kwargs):
            if domain in unknown_domains:
                raise AssertionError(f"source leg helper called for {domain}")
            return await original_source_leg(engine, domain, *args, **kwargs)

        monkeypatch.setattr(data_query, "_source_leg", guarded_source_leg)

        patched_fetchers: set[type] = set()
        for capability in capabilities:
            if capability.domain not in unknown_domains:
                continue
            fetcher = get_registry().resolve(
                capability.asset_class,
                capability.domain,
                period=capability.period,
                market=capability.market,
                source=capability.source,
            )
            fetcher_type = type(fetcher)
            if fetcher_type in patched_fetchers:
                continue

            def reject_fetch(*_args, _domain=capability.domain, **_kwargs):
                raise AssertionError(f"provider fetch called for {_domain}")

            monkeypatch.setattr(fetcher_type, "fetch", reject_fetch)
            patched_fetchers.add(fetcher_type)

        data = await _catalog(warehouse)
        assert unknown_domains <= {row["domain"] for row in data["domains"]}

    async def test_metadata_only_source_verification_is_conservative_and_order_independent(
        self, warehouse, monkeypatch: pytest.MonkeyPatch, provider_model_domains_unmapped
    ) -> None:
        live_registry = get_registry()
        false_capability = next(
            cap
            for cap in live_registry.capabilities()
            if cap.domain == "fred_search" and cap.source == "fred"
        )
        false_fetcher = live_registry.resolve(
            false_capability.asset_class,
            false_capability.domain,
            period=false_capability.period,
            market=false_capability.market,
            source=false_capability.source,
        )
        true_capability = Capability(
            asset_class=false_capability.asset_class,
            domain=false_capability.domain,
            period="snapshot_verified",
            market=false_capability.market,
            source=false_capability.source,
            verified=True,
            notes=false_capability.notes,
        )

        class VerifiedFredSearchFetcher(FredSearchFetcher):
            capability = true_capability

        true_fetcher = VerifiedFredSearchFetcher()

        def registry_with_order(
            source_fetchers: tuple[Fetcher[Any, Any], Fetcher[Any, Any]],
        ) -> ProviderRegistry:
            registry = ProviderRegistry()
            for capability in live_registry.capabilities():
                if capability.domain == "fred_search" and capability.source == "fred":
                    continue
                registry.register(
                    live_registry.resolve(
                        capability.asset_class,
                        capability.domain,
                        period=capability.period,
                        market=capability.market,
                        source=capability.source,
                    )
                )
            for fetcher in source_fetchers:
                registry.register(fetcher)
            for descriptor in live_registry.list_model_descriptors():
                asset_class, domain, period, market, source = descriptor.capability_identity
                registry.register_model(
                    descriptor.source,
                    descriptor.model,
                    registry.resolve(
                        asset_class,
                        domain,
                        period=period,
                        market=market,
                        source=source,
                    ),
                )
            return registry

        orderings = (
            ((false_fetcher, true_fetcher), [("snapshot", False), ("snapshot_verified", True)]),
            ((true_fetcher, false_fetcher), [("snapshot_verified", True), ("snapshot", False)]),
        )
        for fetchers, expected_order in orderings:
            registry = registry_with_order(fetchers)
            registered = [
                cap
                for cap in registry.capabilities()
                if cap.domain == "fred_search" and cap.source == "fred"
            ]
            assert [(cap.period, cap.verified) for cap in registered] == expected_order

            with monkeypatch.context() as scoped_monkeypatch:
                scoped_monkeypatch.setattr(
                    data_query, "get_registry", lambda _registry=registry: _registry
                )
                data = await _catalog(warehouse)

            row = next(item for item in data["domains"] if item["domain"] == "fred_search")
            assert {item["period"]: item["verified"] for item in row["capabilities"]} == {
                "snapshot": False,
                "snapshot_verified": True,
            }
            assert row["sources"] == [
                {
                    "source": "fred",
                    "verified": False,
                    "table": None,
                    "status": "unmapped",
                    "reason": "domain_not_declared",
                    "latest": None,
                    "lag_days": None,
                }
            ]


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
        assert data["markets"] == data["domains"][0]["markets"]

    async def test_a_scope_for_an_unmapped_model_domain_does_not_leak_siblings(
        self, warehouse, provider_model_domains_unmapped
    ) -> None:
        data = await _catalog(warehouse, scopes=("fred_search",))

        assert [row["domain"] for row in data["domains"]] == ["fred_search"]
        assert data["domains_total"] == 1
        assert data["source_legs_total"] == 1
        row = data["domains"][0]
        assert row["domain_defined"] is False
        assert row["asset_class"] == "macro"
        assert {item["domain"] for item in row["capabilities"]} == {"fred_search"}
        assert {item["source"] for item in row["capabilities"]} == {"fred"}
        assert {item["source"] for item in row["sources"]} == {"fred"}
        assert "bls_search" not in repr(data)
        assert "fred_series" not in repr(data)

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
