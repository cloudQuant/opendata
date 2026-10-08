"""Behavior tests for registered REST path aliases and canonical resolution."""

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from opendata.api import data_query
from opendata.api.dependencies import Principal
from opendata.data.capability import Capability
from opendata.data.domains import DomainSpec
from opendata.main import app
from opendata.models.user import User

_DWD_COLUMNS = [
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "_merged_at",
    "_diff_flag",
    "_as_of",
]


class _Registry:
    """Registry stub with source legs but no provider invocation capability."""

    def __init__(self, capabilities: list[Capability]) -> None:
        self._capabilities = capabilities

    def capabilities(self) -> list[Capability]:
        """Return deterministic registered route capabilities."""
        return list(self._capabilities)


def _capability(domain: str, source: str = "ths", asset_class: str = "equity") -> Capability:
    """Build one local, verified registry capability."""
    return Capability(
        asset_class=asset_class,
        domain=domain,
        period="1D",
        market="cn",
        source=source,
        verified=True,
    )


def _registry() -> _Registry:
    """Return the route's registered stock legs without loading providers."""
    return _Registry([_capability("stock_daily", "ths"), _capability("stock_daily", "akshare")])


def _principal(scopes: tuple[str, ...] | None = None) -> Principal:
    """Build a caller with optional API-key domain scopes."""
    return Principal(
        user=User(id=1, username="path-alias-probe"),
        api_key_id=1 if scopes is not None else None,
        scopes=scopes,
    )


def _patch_route_reads(monkeypatch: pytest.MonkeyPatch):
    """Replace warehouse reflection and reads while retaining real query building."""
    inspected_tables: list[str] = []
    executed: list[tuple[str, dict[str, object]]] = []
    adjusted_domains: list[str] = []

    async def table_columns(_engine: object, table: str) -> list[str]:
        inspected_tables.append(table)
        return list(_DWD_COLUMNS)

    def fetch_rows(_engine: object, sql: str, params: dict) -> list[dict[str, object]]:
        executed.append((sql, params))
        return [{"symbol": "600519.SH", "trade_date": date.today().isoformat(), "close": 10.0}]

    async def adjusted_rows(
        _engine: object, domain: str, rows: list[dict], method: str
    ) -> list[dict]:
        adjusted_domains.append(domain)
        return [{**rows[0], "close": 11.0}]

    monkeypatch.setattr(data_query, "get_registry", _registry)
    monkeypatch.setattr(data_query, "_table_columns", table_columns)
    monkeypatch.setattr(data_query, "_fetch_rows", fetch_rows)
    monkeypatch.setattr(data_query, "_adjusted_rows", adjusted_rows)
    app.dependency_overrides[data_query.get_warehouse_engine] = lambda: object()
    return inspected_tables, executed, adjusted_domains


class TestRegisteredPathResolution:
    """Exact domain aliases resolve before access checks and query planning."""

    def test_alias_and_canonical_path_resolve_to_the_same_registered_identity(self):
        alias = data_query._resolve_data_path(
            "stock", "daily", capabilities=_registry().capabilities()
        )
        canonical = data_query._resolve_data_path(
            "equity", "stock_daily", capabilities=_registry().capabilities()
        )

        assert alias == canonical
        assert (alias.domain, alias.asset_class) == ("stock_daily", "equity")

    def test_ambiguous_rest_path_fails_closed(self):
        duplicate_specs = {
            name: DomainSpec(display_name=name, rest_path="stock/daily", contract="Bar")
            for name in ("first_domain", "second_domain")
        }
        capabilities = [
            _capability("first_domain"),
            _capability("second_domain"),
        ]

        with pytest.raises(HTTPException) as exc_info:
            data_query._resolve_data_path(
                "stock",
                "daily",
                domain_specs=duplicate_specs,
                capabilities=capabilities,
            )

        assert exc_info.value.status_code == 404

    async def test_alias_scope_is_checked_on_canonical_id_before_warehouse_reads(self, monkeypatch):
        inspected_tables, _, _ = _patch_route_reads(monkeypatch)

        with pytest.raises(HTTPException) as exc_info:
            await data_query.query_domain_data(
                asset_class="stock",
                domain="daily",
                principal=_principal(("futures_daily",)),
                symbols=None,
                start=None,
                end=None,
                source="auto",
                layer="dwd",
                adjust="none",
                fields=None,
                period=None,
                report_date=None,
                page=1,
                page_size=100,
                engine=object(),
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "API key is not scoped for domain 'stock_daily'"
        assert inspected_tables == []

    async def test_wrong_canonical_asset_class_keeps_scope_check_before_validation(
        self, monkeypatch
    ):
        inspected_tables, _, _ = _patch_route_reads(monkeypatch)

        with pytest.raises(HTTPException) as exc_info:
            await data_query.query_domain_data(
                asset_class="crypto",
                domain="stock_daily",
                principal=_principal(("futures_daily",)),
                symbols=None,
                start=None,
                end=None,
                source="auto",
                layer="dwd",
                adjust="none",
                fields=None,
                period=None,
                report_date=None,
                page=1,
                page_size=100,
                engine=object(),
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "API key is not scoped for domain 'stock_daily'"
        assert inspected_tables == []


class TestPathAliasHttpBehavior:
    """The actual REST routes keep canonical query and export behavior."""

    async def test_canonical_and_alias_queries_share_projected_filtered_sorted_adjusted_plan(
        self, test_client, test_user_token, monkeypatch
    ):
        inspected_tables, executed, adjusted_domains = _patch_route_reads(monkeypatch)
        headers = {"Authorization": f"Bearer {test_user_token}"}
        today = date.today()
        params = {
            "symbols": "600519.SH",
            "start": (today - timedelta(days=5)).isoformat(),
            "end": today.isoformat(),
            "source": "ths",
            "fields": "close",
            "adjust": "qfq",
            "page": 1,
            "page_size": 10,
        }

        canonical = await test_client.get(
            "/api/v1/data/equity/stock_daily", params=params, headers=headers
        )
        alias = await test_client.get("/api/v1/data/stock/daily", params=params, headers=headers)

        assert (canonical.status_code, alias.status_code) == (200, 200)
        assert canonical.json()["data"] == alias.json()["data"]
        response_data = alias.json()["data"]
        assert (response_data["domain"], response_data["asset_class"]) == (
            "stock_daily",
            "equity",
        )
        assert response_data["columns"] == ["symbol", "trade_date", "close"]
        assert response_data["source"] == "ths"
        assert response_data["rows"][0]["close"] == 11.0
        assert response_data["rows"] == [
            {"symbol": "600519.SH", "trade_date": today.isoformat(), "close": 11.0}
        ]
        assert inspected_tables == ["dwd_stock_daily", "dwd_stock_daily"]
        assert len(executed) == 4
        canonical_preflight, canonical_query, alias_preflight, alias_query = executed
        assert canonical_preflight == alias_preflight
        assert canonical_query == alias_query

        preflight_sql, _preflight_params = canonical_preflight
        query_sql, query_params = canonical_query
        assert "ORDER BY" in preflight_sql
        assert preflight_sql.split(" FROM ", maxsplit=1)[0] == "SELECT `symbol`, `trade_date`"
        assert query_sql.split(" FROM ", maxsplit=1)[0] == (
            "SELECT `symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, `amount`"
        )
        assert "ORDER BY `symbol`, `trade_date` LIMIT" in query_sql
        assert query_params["source_filter"] == "ths"
        query_values = {str(value) for value in query_params.values()}
        assert {
            "600519.SH",
            (today - timedelta(days=5)).isoformat(),
            today.isoformat(),
        }.issubset(query_values)
        assert adjusted_domains == ["stock_daily", "stock_daily"]

    async def test_alias_export_uses_canonical_table_and_filename(
        self, test_client, test_user_token, monkeypatch
    ):
        inspected_tables, executed, _ = _patch_route_reads(monkeypatch)
        headers = {"Authorization": f"Bearer {test_user_token}"}
        params = {"source": "ths", "fields": "close", "limit": 1}

        canonical = await test_client.get(
            "/api/v1/data/equity/stock_daily/export", params=params, headers=headers
        )
        alias = await test_client.get(
            "/api/v1/data/stock/daily/export", params=params, headers=headers
        )

        assert (canonical.status_code, alias.status_code) == (200, 200)
        assert canonical.text == alias.text
        assert 'filename="equity_stock_daily.csv"' in alias.headers["content-disposition"]
        assert inspected_tables == ["dwd_stock_daily", "dwd_stock_daily"]
        assert executed[0] == executed[1]

    async def test_missing_alias_table_returns_canonical_table_404_before_read(
        self, test_client, test_user_token, monkeypatch
    ):
        inspected_tables, executed, _ = _patch_route_reads(monkeypatch)

        async def missing_table(_engine: object, table: str) -> list[str]:
            inspected_tables.append(table)
            return []

        monkeypatch.setattr(data_query, "_table_columns", missing_table)
        response = await test_client.get(
            "/api/v1/data/stock/daily",
            params={"source": "ths"},
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "table 'dwd_stock_daily' is not available"
        assert inspected_tables == ["dwd_stock_daily"]
        assert executed == []

    async def test_alias_freshness_uses_canonical_domain_and_table(
        self, test_client, test_user_token, monkeypatch
    ):
        _patch_route_reads(monkeypatch)
        expected = date.today() - timedelta(days=1)
        calls: list[tuple[str, str, str | None]] = []

        async def fixed_expected(_engine: object, *, on: date | None = None) -> date:
            return expected

        async def fixed_freshness(
            _engine: object,
            domain: str,
            table: str,
            *,
            source: str | None = None,
            expected: date,
        ) -> dict[str, object]:
            calls.append((domain, table, source))
            return {"field": "trade_date", "latest": expected, "lag_days": 0, "status": "fresh"}

        monkeypatch.setattr(data_query, "_expected_data_date", fixed_expected)
        monkeypatch.setattr(data_query, "_freshness", fixed_freshness)
        headers = {"Authorization": f"Bearer {test_user_token}"}
        canonical = await test_client.get(
            "/api/v1/data/domains/stock_daily/freshness", headers=headers
        )
        alias = await test_client.get("/api/v1/data/domains/stock/daily/freshness", headers=headers)

        assert (canonical.status_code, alias.status_code) == (200, 200)
        assert canonical.json()["data"] == alias.json()["data"]
        assert alias.json()["data"]["domain"] == "stock_daily"
        assert calls == [
            ("stock_daily", "dwd_stock_daily", None),
            ("stock_daily", "dwd_stock_daily", None),
        ]

    async def test_unknown_alias_and_invalid_source_fail_before_table_reflection(
        self, test_client, test_user_token, monkeypatch
    ):
        inspected_tables, _, _ = _patch_route_reads(monkeypatch)
        headers = {"Authorization": f"Bearer {test_user_token}"}
        unknown = await test_client.get("/api/v1/data/stock/not_registered", headers=headers)
        wrong_class = await test_client.get("/api/v1/data/futures/stock_daily", headers=headers)
        invalid_source = await test_client.get(
            "/api/v1/data/stock/daily", params={"source": "unregistered"}, headers=headers
        )

        assert unknown.status_code == 404
        assert wrong_class.status_code == 404
        assert invalid_source.status_code == 400
        assert inspected_tables == []
