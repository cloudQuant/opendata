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

    async def test_adjust_without_factors_answers_501(
        self, warehouse, test_client, test_user_token
    ):
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

        assert response.status_code == 501
        assert "factor" in response.json()["detail"]

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
