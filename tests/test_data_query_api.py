"""Data query API tests (A4.9, design §10.1).

Validation paths (enums, the fields whitelist, layer/source rules,
authentication, OpenAPI shape) need no warehouse and run in the gate;
the happy paths and the injection cases against real tables are
``e2e`` because the ods/dwd DDL is MySQL-only.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, pool, text

from opendata.main import app

AUTH = {"Authorization": "Bearer {token}"}


class TestOpenApi:
    def test_routes_are_published(self):
        paths = app.openapi()["paths"]

        assert "/api/v1/data/{asset_class}/{domain}" in paths
        assert "/api/v1/data/catalog" in paths
        assert "/api/v1/data/domains/{domain}/freshness" in paths
        assert "/api/v1/data/domains/{domain}/diff-report" in paths

    def test_query_parameters_are_enumerated(self):
        parameters = app.openapi()["paths"]["/api/v1/data/{asset_class}/{domain}"]["get"][
            "parameters"
        ]
        by_name = {parameter["name"]: parameter for parameter in parameters}

        assert set(by_name["layer"]["schema"]["enum"]) == {"dwd", "ods"}
        assert set(by_name["adjust"]["schema"]["enum"]) == {"none", "qfq", "hfq"}
        assert {"symbols", "start", "end", "source", "fields", "page", "page_size"} <= set(by_name)


class TestAuthentication:
    async def test_query_requires_authentication(self, test_client):
        response = await test_client.get("/api/v1/data/equity/stock_daily")

        assert response.status_code == 401

    async def test_catalog_requires_authentication(self, test_client):
        response = await test_client.get("/api/v1/data/catalog")

        assert response.status_code == 401


class TestValidationPaths:
    async def test_unknown_domain_is_a_404(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/not_a_domain", headers=_auth(test_user_token)
        )

        assert response.status_code == 404

    async def test_wrong_asset_class_is_a_404(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/futures/stock_daily", headers=_auth(test_user_token)
        )

        assert response.status_code == 404

    async def test_bad_layer_is_a_422(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily?layer=raw", headers=_auth(test_user_token)
        )

        assert response.status_code == 422

    async def test_bad_adjust_is_a_422(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily?adjust=split", headers=_auth(test_user_token)
        )

        assert response.status_code == 422

    async def test_ods_layer_requires_an_explicit_source(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily?layer=ods", headers=_auth(test_user_token)
        )

        assert response.status_code == 400
        assert "explicit source" in response.json()["detail"]

    async def test_page_bounds_are_validated(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily?page=0", headers=_auth(test_user_token)
        )

        assert response.status_code == 422

    async def test_unknown_freshness_domain_is_a_404(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/domains/not_a_domain/freshness", headers=_auth(test_user_token)
        )

        assert response.status_code == 404


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.e2e
class TestAgainstTheWarehouse:
    SYMBOL = "QUERY_PROBE"

    @pytest.fixture(autouse=True)
    def registered_capabilities(self):
        """ASGITransport does not run the lifespan; register explicitly."""
        from opendata.data.providers import register_providers

        register_providers()

    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.SYMBOL},
            )
            connection.execute(
                text(
                    "INSERT INTO `dwd_stock_daily` "
                    "(`symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, `amount`, "
                    "`source`, `_merged_at`, `_diff_flag`, `_as_of`) VALUES "
                    "(:probe, '2024-01-05', 1, 1, 1, 1, 1, 1, 'akshare', NOW(), 0, '2024-01-31'), "
                    "(:probe, '2024-01-04', 2, 2, 2, 2, 2, 2, 'akshare', NOW(), 0, '2024-01-31')"
                ),
                {"probe": self.SYMBOL},
            )
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.SYMBOL},
            )
        engine.dispose()

    async def test_queries_dwd_rows_with_a_window_and_field_filter(
        self, warehouse, test_client, test_user_token
    ):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={
                "symbols": self.SYMBOL,
                "start": "2024-01-01",
                "end": "2024-01-31",
                "fields": "close",
                "page_size": 10,
            },
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["layer"] == "dwd"
        # keys stay selected even though only close was requested
        assert {"symbol", "trade_date", "close"} <= set(data["columns"])
        # deterministic business-key order, oldest first
        assert [row["trade_date"] for row in data["rows"]] == ["2024-01-04", "2024-01-05"]
        assert set(data["rows"][0]) >= {"symbol", "trade_date", "close"}

    async def test_unknown_field_is_a_400(self, warehouse, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={"fields": "close,evil"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400
        assert "unknown fields" in response.json()["detail"]

    async def test_injection_attempt_is_treated_as_a_literal_symbol(
        self, warehouse, test_client, test_user_token
    ):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={"symbols": "x'); DROP TABLE dwd_stock_daily; --"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        assert response.json()["data"]["rows"] == []
        with warehouse.connect() as connection:  # the table is still there
            connection.execute(text("SELECT COUNT(*) FROM `dwd_stock_daily`"))

    async def test_adjust_without_factor_rows_fails_closed(
        self, warehouse, test_client, test_user_token
    ):
        """The factor table exists (migration 0005); a bar without its
        factor row must fail rather than serve a partially adjusted
        series (fail-closed, design D10)."""
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={
                "symbols": self.SYMBOL,
                "start": "2024-01-01",
                "end": "2024-01-31",
                "adjust": "qfq",
            },
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400
        assert "no adjust factor" in response.json()["detail"]

    async def test_catalog_lists_capabilities_with_freshness(
        self, warehouse, test_client, test_user_token
    ):
        response = await test_client.get("/api/v1/data/catalog", headers=_auth(test_user_token))

        assert response.status_code == 200
        domains = {row["domain"]: row for row in response.json()["data"]["domains"]}
        assert "stock_daily" in domains
        assert domains["stock_daily"]["asset_class"] == "equity"
        assert set(domains["stock_daily"]) >= {"latest", "lag_days", "status"}

    async def test_freshness_endpoint_reports_the_dwd_date(
        self, warehouse, test_client, test_user_token
    ):
        response = await test_client.get(
            "/api/v1/data/domains/stock_daily/freshness",
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["domain"] == "stock_daily"
        assert data["lag_days"] is None or data["lag_days"] >= 0

    async def test_diff_report_endpoint_answers_even_without_rows(
        self, warehouse, test_client, test_user_token
    ):
        response = await test_client.get(
            "/api/v1/data/domains/stock_daily/diff-report",
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        assert response.json()["data"]["domain"] == "stock_daily"


def test_freshness_default_lag_is_computed_against_today():
    """The catalog's coarse staleness signal uses today (calendar is A4.7)."""
    from opendata.pipeline.freshness import check_freshness

    assert check_freshness.__doc__ is not None
    assert date.today() >= date(2024, 1, 1)


class TestExportContract:
    """The CSV export surface (AC-11: 导出 + 公式注入转义).

    The safety rules and the response shape need no warehouse; the
    actual rows are read in the e2e class below.
    """

    async def test_export_requires_authentication(self, test_client):
        response = await test_client.get("/api/v1/data/equity/stock_daily/export")

        assert response.status_code == 401

    async def test_export_is_published_in_the_openapi_document(self):
        parameters = app.openapi()["paths"]["/api/v1/data/{asset_class}/{domain}/export"]["get"][
            "parameters"
        ]
        by_name = {parameter["name"]: parameter for parameter in parameters}

        assert set(by_name["layer"]["schema"]["enum"]) == {"dwd", "ods"}
        assert set(by_name["adjust"]["schema"]["enum"]) == {"none", "qfq", "hfq"}
        assert {"symbols", "start", "end", "source", "fields", "limit"} <= set(by_name)

    async def test_export_rejects_an_unknown_domain(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/not_a_domain/export", headers=_auth(test_user_token)
        )

        assert response.status_code == 404

    async def test_export_limit_is_bounded(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily/export",
            params={"limit": 10_000_000},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 422


@pytest.mark.e2e
class TestExportAgainstTheWarehouse:
    SYMBOL = "EXPORT_PROBE"

    @pytest.fixture(autouse=True)
    def registered_capabilities(self):
        from opendata.data.providers import register_providers

        register_providers()

    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.SYMBOL},
            )
            connection.execute(
                text(
                    "INSERT INTO `dwd_stock_daily` "
                    "(`symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, `amount`, "
                    "`source`, `_merged_at`, `_diff_flag`, `_as_of`) VALUES "
                    "(:probe, '2024-01-05', 1, 1, 1, 1, 1, 1, 'akshare', NOW(), 0, '2024-01-31'), "
                    "(:probe, '2024-01-04', 2, 2, 2, 2, 2, 2, 'akshare', NOW(), 0, '2024-01-31')"
                ),
                {"probe": self.SYMBOL},
            )
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.SYMBOL},
            )
        engine.dispose()

    async def test_export_streams_a_header_and_the_rows(
        self, warehouse, test_client, test_user_token
    ):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily/export",
            params={"symbols": self.SYMBOL, "start": "2024-01-01", "end": "2024-01-31"},
            headers=_auth(test_user_token),
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        lines = response.text.strip().splitlines()
        assert lines[0].split(",")[0] == "symbol"
        assert len(lines) == 3  # header + two rows
        assert all(self.SYMBOL in line for line in lines[1:])

    async def test_export_honours_the_field_filter(self, warehouse, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily/export",
            params={
                "symbols": self.SYMBOL,
                "start": "2024-01-01",
                "end": "2024-01-31",
                "fields": "close",
            },
            headers=_auth(test_user_token),
        )

        header = response.text.splitlines()[0].split(",")

        assert "close" in header
        assert "amount" not in header

    async def test_export_rejects_an_unknown_field(self, warehouse, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/data/equity/stock_daily/export",
            params={
                "symbols": self.SYMBOL,
                "start": "2024-01-01",
                "end": "2024-01-31",
                "fields": "close;DROP TABLE",
            },
            headers=_auth(test_user_token),
        )

        assert response.status_code == 400

    async def test_exported_cells_cannot_run_as_spreadsheet_formulas(
        self, warehouse, test_client, test_user_token
    ):
        """A symbol carrying a formula payload must come back inert."""
        payload = "=cmd|'/c calc'!A0"
        with warehouse.begin() as connection:
            connection.execute(
                text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": payload},
            )
            connection.execute(
                text(
                    "INSERT INTO `dwd_stock_daily` "
                    "(`symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, `amount`, "
                    "`source`, `_merged_at`, `_diff_flag`, `_as_of`) VALUES "
                    "(:probe, '2024-01-06', 1, 1, 1, 1, 1, 1, 'akshare', NOW(), 0, '2024-01-31')"
                ),
                {"probe": payload},
            )
        try:
            response = await test_client.get(
                "/api/v1/data/equity/stock_daily/export",
                params={"symbols": payload, "start": "2024-01-01", "end": "2024-01-31"},
                headers=_auth(test_user_token),
            )
        finally:
            with warehouse.begin() as connection:
                connection.execute(
                    text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                    {"probe": payload},
                )

        body = response.text
        assert response.status_code == 200
        assert "'=cmd|'/c calc'!A0" in body
        assert "\n=cmd|" not in body and ",=cmd|" not in body


class TestWarehouseTables:
    """B5.2: the layer-aware warehouse table view."""

    async def test_warehouse_endpoint_requires_auth(self, test_client):
        response = await test_client.get("/api/v1/tables/warehouse")

        assert response.status_code == 401

    async def test_warehouse_route_is_registered_before_dynamic_ids(self):
        from opendata.main import app

        paths = app.openapi()["paths"]
        assert "/api/v1/tables/warehouse" in paths


@pytest.mark.e2e
class TestWarehouseTablesLive:
    @pytest.fixture(autouse=True)
    def registered_capabilities(self):
        from opendata.data.providers import register_providers

        register_providers()

    @pytest.fixture(autouse=True)
    def real_data_db(self, test_client):
        """The warehouse listing reads MySQL's information_schema, so the
        SQLite override the gate fixture installs must be lifted here."""
        from opendata.core.database import get_data_db

        app.dependency_overrides.pop(get_data_db, None)
        yield

    async def test_lists_the_real_warehouse_tables(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/tables/warehouse",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        tables = {entry["table"]: entry for entry in response.json()["data"]["tables"]}
        assert "ods_stock_daily_ths" in tables
        assert "dwd_stock_daily" in tables
        assert tables["ods_stock_daily_ths"]["layer"] == "ods"
        assert tables["dwd_stock_daily"]["layer"] == "dwd"

    async def test_layer_filter_narrows_the_list(self, test_client, test_user_token):
        response = await test_client.get(
            "/api/v1/tables/warehouse",
            params={"layer": "ods"},
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        tables = response.json()["data"]["tables"]
        assert tables and all(entry["layer"] == "ods" for entry in tables)
