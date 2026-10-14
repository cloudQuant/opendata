"""Offline witnesses for the documented Tiingo/Benzinga Iteration 2 endpoints."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from opendata.data.models.benzinga import BenzingaCompanyNews
from opendata.data.models.econdb import EconDBEconomicIndicator
from opendata.data.models.tiingo import TiingoEquityHistorical
from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import HttpResponse, ProviderEngineError
from opendata.data.providers.benzinga.models.iter2 import (
    BenzingaAnalystSearchFetcher,
    BenzingaCompanyNewsFetcher,
    BenzingaWorldNewsFetcher,
)
from opendata.data.providers.econdb.models.iter2 import (
    EconDBEconomicIndicatorsFetcher,
    EconDBGdpNominalFetcher,
    EconDBGdpRealFetcher,
    EconDBPortVolumeFetcher,
    EconDBYieldCurveFetcher,
)
from opendata.data.providers.tiingo.models.iter2 import (
    TiingoCompanyNewsFetcher,
    TiingoCurrencyHistoricalFetcher,
    TiingoEquityHistoricalFetcher,
    TiingoEtfHistoricalFetcher,
    TiingoTrailingDividendYieldFetcher,
    TiingoWorldNewsFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


class Recorder:
    """Return hand-authored response documents and remember every attempted request."""

    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(self, url, params, *, timeout, source, headers=None):
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "timeout": timeout,
                "source": source,
                "headers": dict(headers or {}),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


TIINGO_NEWS = {
    "id": 123456,
    "title": "A documented market headline",
    "url": "https://news.example.test/story",
    "description": "A brief description.",
    "publishedDate": "2025-01-02T09:30:00Z",
    "crawlDate": "2025-01-02T09:31:00Z",
    "source": "news.example.test",
    "tickers": ["AAPL", "MSFT"],
    "tags": ["technology"],
}

BENZINGA_NEWS = {
    "id": 36444586,
    "author": "Benzinga Insights",
    "created": "Mon, 01 Jan 2024 13:35:14 -0400",
    "updated": "Mon, 01 Jan 2024 13:35:15 -0400",
    "title": "A documented news headline",
    "teaser": "Brief summary",
    "body": "Full article text",
    "url": "https://www.benzinga.com/story",
    "channels": [{"name": "Options"}],
    "stocks": [{"name": "AAPL", "exchange": "NASDAQ"}],
    "tags": [{"name": "BZI-AUOA"}],
}


@pytest.fixture(autouse=True)
def _local_only_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIINGO_TOKEN", "fixture-token-never-sent")
    monkeypatch.setenv("BENZINGA_API_KEY", "fixture-token-never-sent")
    monkeypatch.setenv("ECONDB_API_KEY", "fixture-token-never-sent")


def _fetch(fetcher_type, document, **query):
    transport = Recorder(HttpResponse(200, document))
    fetcher = type(
        f"Fixture{fetcher_type.__name__}",
        (fetcher_type,),
        {"http_transport": transport},
    )()
    rows = fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
    return transport, rows


def _authorized_context(fetcher_type, *, sends: int = 1) -> FetchContext:
    capability = fetcher_type.capability
    hosts = {
        "tiingo": "api.tiingo.com",
        "benzinga": "api.benzinga.com",
        "econdb": "api.econdb.com",
    }
    return _request_context(
        capability.source,
        fetcher_type.canonical_model,
        capability.market,
        hosts[capability.source],
        sends=sends,
    )


class TestTiingoDocumentedShapes:
    def test_equity_historical_daily_response(self) -> None:
        transport, rows = _fetch(
            TiingoEquityHistoricalFetcher,
            [
                {
                    "date": "2025-01-02",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.5,
                    "close": 11.5,
                    "volume": 1200,
                    "adjOpen": 9.9,
                    "adjHigh": 11.8,
                    "adjLow": 9.4,
                    "adjClose": 11.3,
                    "adjVolume": 1215,
                    "divCash": 0.2,
                    "splitFactor": 1.0,
                }
            ],
            symbol="AAPL",
            interval="1d",
        )
        assert len(transport.calls) == 1
        assert transport.calls[0]["url"] == "https://api.tiingo.com/tiingo/daily/AAPL/prices"
        assert transport.calls[0]["params"]["resampleFreq"] == "daily"
        assert rows[0].symbol == "AAPL" and rows[0].date == date(2025, 1, 2)
        assert rows[0].adj_close == 11.3 and rows[0].volume == 1200

    def test_etf_historical_uses_documented_same_eod_shape(self) -> None:
        transport, rows = _fetch(
            TiingoEtfHistoricalFetcher,
            [
                {
                    "date": "2025-01-02",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 700,
                }
            ],
            symbol="SPY",
        )
        assert transport.calls[0]["url"] == "https://api.tiingo.com/tiingo/daily/SPY/prices"
        assert rows[0].symbol == "SPY" and rows[0].close == 101.0

    def test_currency_historical_response(self) -> None:
        transport, rows = _fetch(
            TiingoCurrencyHistoricalFetcher,
            [
                {
                    "date": "2025-01-02T10:00:00Z",
                    "ticker": "EURUSD",
                    "open": 1.03,
                    "high": 1.04,
                    "low": 1.02,
                    "close": 1.035,
                }
            ],
            symbol="EURUSD",
            interval="5m",
        )
        assert transport.calls[0]["url"] == "https://api.tiingo.com/tiingo/fx/EURUSD/prices"
        assert transport.calls[0]["params"]["resampleFreq"] == "5min"
        assert rows[0].symbol == "EURUSD" and rows[0].close == 1.035

    def test_trailing_dividend_yield_response(self) -> None:
        transport, rows = _fetch(
            TiingoTrailingDividendYieldFetcher,
            [{"date": "2025-01-02T00:00:00Z", "trailingDiv1Y": "0.0315"}],
            symbol="AAPL",
        )
        assert transport.calls[0]["url"].endswith(
            "/tiingo/corporate-actions/AAPL/distribution-yield"
        )
        assert rows[0].trailing_dividend_yield == 0.0315

    def test_company_news_response_and_ticker_filter(self) -> None:
        transport, rows = _fetch(TiingoCompanyNewsFetcher, [TIINGO_NEWS], symbol="AAPL")
        assert transport.calls[0]["url"] == "https://api.tiingo.com/tiingo/news"
        assert transport.calls[0]["params"]["tickers"] == "AAPL"
        assert rows[0].symbol == "AAPL" and rows[0].tickers == ["AAPL", "MSFT"]

    def test_world_news_response_without_ticker_query(self) -> None:
        transport, rows = _fetch(TiingoWorldNewsFetcher, [TIINGO_NEWS])
        assert "tickers" not in transport.calls[0]["params"]
        assert rows[0].symbol is None and rows[0].source == "news.example.test"

    @pytest.mark.parametrize(
        "fetcher_type,query",
        [
            (TiingoEquityHistoricalFetcher, {}),
            (TiingoEtfHistoricalFetcher, {}),
            (TiingoCurrencyHistoricalFetcher, {}),
            (TiingoTrailingDividendYieldFetcher, {}),
            (TiingoCompanyNewsFetcher, {}),
        ],
    )
    def test_required_symbol_is_rejected_before_io(self, fetcher_type, query) -> None:
        transport = Recorder(HttpResponse(200, []))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "TIINGO_QUERY_INVALID"
        assert transport.calls == []

    @pytest.mark.parametrize(
        "fetcher_type,query",
        [
            (TiingoEquityHistoricalFetcher, {"symbol": "AAPL"}),
            (TiingoEtfHistoricalFetcher, {"symbol": "SPY"}),
            (TiingoCurrencyHistoricalFetcher, {"symbol": "EURUSD"}),
            (TiingoTrailingDividendYieldFetcher, {"symbol": "AAPL"}),
            (TiingoCompanyNewsFetcher, {"symbol": "AAPL"}),
            (TiingoWorldNewsFetcher, {}),
        ],
    )
    def test_missing_token_fails_before_io(self, fetcher_type, query, monkeypatch) -> None:
        monkeypatch.delenv("TIINGO_TOKEN", raising=False)
        transport = Recorder(HttpResponse(200, []))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "TIINGO_CREDENTIAL_MISSING"
        assert transport.calls == []

    @pytest.mark.parametrize(
        "fetcher_type,query",
        [
            (TiingoEquityHistoricalFetcher, {"symbol": "AAPL"}),
            (TiingoEtfHistoricalFetcher, {"symbol": "SPY"}),
            (TiingoCurrencyHistoricalFetcher, {"symbol": "EURUSD"}),
            (TiingoTrailingDividendYieldFetcher, {"symbol": "AAPL"}),
            (TiingoCompanyNewsFetcher, {"symbol": "AAPL"}),
            (TiingoWorldNewsFetcher, {}),
        ],
    )
    def test_malformed_response_shape_is_rejected(self, fetcher_type, query) -> None:
        transport = Recorder(HttpResponse(200, {"unexpected": []}))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "TIINGO_SHAPE_INVALID"
        assert len(transport.calls) == 1


class TestBenzingaDocumentedShapes:
    def test_analyst_search_nested_metrics_and_epoch_seconds(self) -> None:
        body = {
            "analyst_ratings_analyst": [
                {
                    "firm_id": "FIRM-123456",
                    "firm_name": "Example Research",
                    "id": "ANALYST-123",
                    "name_first": "Ada",
                    "name_full": "Ada Analyst",
                    "name_last": "Analyst",
                    "ratings_accuracy": {
                        "1m_average_return": "10.0",
                        "1m_smart_score": "8.25",
                        "1m_stdev": "3.0",
                        "1m_success_rate": "50.0",
                        "1m_loss_count": 1,
                        "overall_average_return": "7.5",
                        "overall_avg_return_percentile": "80.0",
                        "overall_loss_count": 2,
                        "smart_score": "9.1",
                        "total_ratings": 8,
                        "total_ratings_percentile": "75.0",
                    },
                    "updated": 1704758400,
                }
            ]
        }
        transport, rows = _fetch(BenzingaAnalystSearchFetcher, body)
        assert transport.calls[0]["url"] == (
            "https://api.benzinga.com/api/v2.1/calendar/ratings/analysts"
        )
        assert rows[0].analyst_id == "ANALYST-123"
        assert rows[0].ratings_accuracy.one_month_average_return == 0.1
        assert rows[0].ratings_accuracy.one_month_smart_score == 8.25

    def test_company_news_pages_and_flattens_documented_name_arrays(self) -> None:
        transport, rows = _fetch(BenzingaCompanyNewsFetcher, [BENZINGA_NEWS], symbol="AAPL")
        assert len(transport.calls) == 1
        assert transport.calls[0]["url"] == "https://api.benzinga.com/api/v2/news"
        assert transport.calls[0]["params"]["tickers"] == "AAPL"
        assert rows[0].symbol == "AAPL"
        assert rows[0].stocks == ["AAPL"] and rows[0].channels == ["Options"]

    def test_news_uses_bounded_offset_pages(self) -> None:
        first_page = [
            {
                **BENZINGA_NEWS,
                "id": 36444586 + index,
                "created": f"Mon, 01 Jan 2024 13:{index % 60:02d}:14 -0400",
            }
            for index in range(100)
        ]
        last_page = [{**BENZINGA_NEWS, "id": 36444686}]
        transport = Recorder(HttpResponse(200, first_page), HttpResponse(200, last_page))
        fetcher = type(
            "FixtureFetcher", (BenzingaWorldNewsFetcher,), {"http_transport": transport}
        )()
        rows = fetcher.fetch(ctx=_authorized_context(BenzingaWorldNewsFetcher, sends=2), limit=101)
        assert [call["params"]["page"] for call in transport.calls] == ["0", "100"]
        assert [call["params"]["pageSize"] for call in transport.calls] == ["100", "100"]
        assert len(rows) == 101

    def test_world_news_has_no_ticker_filter(self) -> None:
        transport, rows = _fetch(BenzingaWorldNewsFetcher, [BENZINGA_NEWS], limit=100)
        assert "tickers" not in transport.calls[0]["params"]
        assert rows[0].id == 36444586 and rows[0].tags == ["BZI-AUOA"]

    @pytest.mark.parametrize(
        "fetcher_type,query,body",
        [
            (BenzingaAnalystSearchFetcher, {}, {"wrong_key": []}),
            (BenzingaCompanyNewsFetcher, {"symbol": "AAPL"}, {"wrong_key": []}),
            (BenzingaWorldNewsFetcher, {}, {"wrong_key": []}),
        ],
    )
    def test_malformed_response_shape_is_rejected(self, fetcher_type, query, body) -> None:
        transport = Recorder(HttpResponse(200, body))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "BENZINGA_SHAPE_INVALID"
        assert len(transport.calls) == 1

    @pytest.mark.parametrize(
        "fetcher_type,query",
        [
            (BenzingaCompanyNewsFetcher, {}),
        ],
    )
    def test_required_symbol_is_rejected_before_io(self, fetcher_type, query) -> None:
        transport = Recorder(HttpResponse(200, []))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "BENZINGA_QUERY_INVALID"
        assert transport.calls == []

    @pytest.mark.parametrize(
        "fetcher_type",
        [BenzingaAnalystSearchFetcher, BenzingaCompanyNewsFetcher, BenzingaWorldNewsFetcher],
    )
    def test_missing_api_key_fails_before_io(self, fetcher_type, monkeypatch) -> None:
        monkeypatch.delenv("BENZINGA_API_KEY", raising=False)
        transport = Recorder(HttpResponse(200, []))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        query = {"symbol": "AAPL"} if fetcher_type is BenzingaCompanyNewsFetcher else {}
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "BENZINGA_CREDENTIAL_MISSING"
        assert transport.calls == []


# These documents follow the response examples published in EconDB's official v1 OpenAPI
# schema. The exact CPI / port examples are transcribed from that schema; GDP and yield contents
# below are hand-authored values using its same documented series envelope, not captured data.
ECONOMIC_INDICATOR = {
    "ticker": "CPIUS",
    "description": "United States - Consumer price index",
    "geography": "United States",
    "frequency": "M",
    "dataset": "BLS_CU",
    "units": None,
    "additional_metadata": {},
    "data": {"values": [21.48, 21.62], "dates": ["1947-01-01", "1947-04-01"]},
}

GDP_NOMINAL = {
    "ticker": "GDPUS_NOMINAL",
    "description": "United States - GDP, current prices (nominal)",
    "geography": "United States",
    "frequency": "Q",
    "dataset": "NATIONAL_ACCOUNTS",
    "units": "USD",
    "additional_metadata": {},
    "data": {
        "values": [100.0, 110.0, 150.0],
        "dates": ["2020-01-01", "2020-04-01", "2021-01-01"],
    },
}

GDP_REAL = {
    "ticker": "GDPUS_REAL",
    "description": "United States - Real GDP, chained volume measure",
    "geography": "United States",
    "frequency": "Q",
    "dataset": "NATIONAL_ACCOUNTS",
    "units": "USD at chained 2017 prices",
    "additional_metadata": {},
    "data": {
        "values": [200.0, 210.0, 240.0],
        "dates": ["2020-01-01", "2020-04-01", "2021-01-01"],
    },
}

PORT_ROSTER = [
    {
        "name": "Barcelona",
        "locode": "ES BCN",
        "iso2": "ES",
        "lat": 41.35,
        "lon": 2.16667,
        "timezone": "Europe/Madrid",
        "region": "West Mediterranean",
    }
]

PORT_TRAFFIC = {
    "type": "timeseries",
    "title": "Container volume (TEU, full)",
    "series": [
        {"code": "Imports", "name": "Gross imports", "axis": 0},
        {"code": "Exports", "name": "Gross exports", "axis": 0},
        {"code": "Transshipment", "name": "Transshipment", "axis": 0},
    ],
    "data": [
        {"Date": "2022-02-21", "Exports": 26635.0, "Imports": 34850.0, "Transshipment": 331.5}
    ],
}

PORT_DWELL = {
    "type": "timeseries",
    "title": "Container yard dwell (days)",
    "tooltip": "Average period needed to process cargo",
    "series": [
        {"code": "Imports", "name": "Imports", "axis": 0, "lbreak": True},
        {"code": "Exports", "name": "Exports", "axis": 0, "lbreak": True},
        {"code": "Transshipments", "name": "Transshipments", "axis": 0, "lbreak": True},
        {"code": "Rail imports", "name": "Rail imports", "axis": 0, "dashed": True, "lbreak": True},
    ],
    "data": [
        {
            "Date": "2022-02-21",
            "Imports": 7.238361111111112,
            "Exports": 12.079057054263567,
            "Transshipments": 8.403610210526317,
            "Rail imports": None,
        }
    ],
}

YIELD_FIVE_YEAR = {
    "ticker": "USYIELD5",
    "description": "United States - Treasury yield, 5-year",
    "geography": "United States",
    "frequency": "D",
    "dataset": "TREASURY_YIELDS",
    "units": "Percent",
    "additional_metadata": {},
    "data": {"values": [4.1, 4.2], "dates": ["2025-01-02", "2025-01-03"]},
}

YIELD_TEN_YEAR = {
    "ticker": "USYIELD10",
    "description": "United States - Treasury yield, 10-year",
    "geography": "United States",
    "frequency": "D",
    "dataset": "TREASURY_YIELDS",
    "units": "%",
    "additional_metadata": {},
    "data": {"values": [4.5, 4.6], "dates": ["2025-01-02", "2025-01-03"]},
}


def _assert_typed_roundtrip(rows) -> None:
    assert rows
    for row in rows:
        assert type(row).model_validate_json(row.model_dump_json()) == row


def _request_context(
    source: str,
    model: str,
    market: str,
    allowed_host: str,
    *,
    sends: int = 1,
) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=sends,
        source_attempts=sends,
        allowed_hosts={allowed_host},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=sends,
        source_attempts=sends,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=3, request_budget=budget)


def _econdb_context(model: str, *, sends: int = 1) -> FetchContext:
    return _request_context("econdb", model, "global", "api.econdb.com", sends=sends)


class TestEconDBDocumentedShapes:
    def test_economic_indicator_official_series_shape_and_window(self) -> None:
        transport, rows = _fetch(
            EconDBEconomicIndicatorsFetcher,
            ECONOMIC_INDICATOR,
            symbol="CPIUS",
            country="United States",
            frequency="monthly",
            start_date="1947-01-01",
            end_date="1947-04-01",
            limit=2,
        )
        assert transport.calls[0]["url"] == "https://api.econdb.com/v1/series/CPIUS/"
        assert transport.calls[0]["params"] == {}
        assert transport.calls[0]["headers"] == {
            "Authorization": "Token fixture-token-never-sent",
            "Accept": "application/json",
        }
        assert [row.value for row in rows] == [21.48, 21.62]
        assert rows[0].unit is None and rows[1].country == "United States"
        _assert_typed_roundtrip(rows)

    @pytest.mark.parametrize(
        "fetcher_type,body,query",
        [
            (EconDBEconomicIndicatorsFetcher, {"unexpected": []}, {"symbol": "CPIUS"}),
            (
                EconDBGdpNominalFetcher,
                {**GDP_NOMINAL, "data": {"dates": ["2020-01-01"], "values": []}},
                {"symbol": "GDPUS_NOMINAL"},
            ),
            (
                EconDBGdpRealFetcher,
                {**GDP_REAL, "additional_metadata": []},
                {"symbol": "GDPUS_REAL"},
            ),
        ],
    )
    def test_series_models_refuse_malformed_official_envelopes(
        self, fetcher_type, body, query
    ) -> None:
        transport, rows = self._malformed(fetcher_type, body, query)
        assert rows == []
        assert len(transport.calls) == 1

    @staticmethod
    def _malformed(fetcher_type, body, query):
        transport = Recorder(HttpResponse(200, body))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "ECONDB_SHAPE_INVALID"
        return transport, []

    def test_nominal_gdp_computes_only_periods_with_matching_prior_data(self) -> None:
        _, rows = _fetch(
            EconDBGdpNominalFetcher,
            GDP_NOMINAL,
            symbol="GDPUS_NOMINAL",
            country="United States",
            frequency="quarterly",
        )
        assert len(rows) == 3
        assert rows[1].nominal_growth_qoq == pytest.approx(10.0)
        assert rows[1].nominal_growth_yoy is None
        assert rows[2].nominal_growth_yoy == pytest.approx(50.0)
        assert rows[2].nominal_growth_qoq is None
        _assert_typed_roundtrip(rows)

    def test_real_gdp_retains_units_and_rejects_nominal_semantics(self) -> None:
        _, rows = _fetch(
            EconDBGdpRealFetcher,
            GDP_REAL,
            symbol="GDPUS_REAL",
            country="United States",
        )
        assert rows[1].units == "USD at chained 2017 prices"
        assert rows[1].real_growth_qoq == pytest.approx(5.0)
        _assert_typed_roundtrip(rows)

        transport = Recorder(HttpResponse(200, GDP_NOMINAL))
        fetcher = type("FixtureFetcher", (EconDBGdpRealFetcher,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(EconDBGdpRealFetcher), symbol="GDPUS_NOMINAL")
        assert caught.value.code == "ECONDB_SERIES_SEMANTICS_INVALID"
        assert len(transport.calls) == 1

    def test_port_volume_joins_documented_teu_and_dwell_shapes(self) -> None:
        transport = Recorder(
            HttpResponse(200, PORT_ROSTER),
            HttpResponse(200, PORT_TRAFFIC),
            HttpResponse(200, PORT_DWELL),
        )
        fetcher = type(
            "FixtureFetcher", (EconDBPortVolumeFetcher,), {"http_transport": transport}
        )()
        rows = fetcher.fetch(
            ctx=_authorized_context(EconDBPortVolumeFetcher, sends=3), port_code="ESBCN"
        )
        assert [call["url"] for call in transport.calls] == [
            "https://api.econdb.com/v1/ports/",
            "https://api.econdb.com/v1/ports/ES%20BCN/container-traffic/",
            "https://api.econdb.com/v1/ports/ES%20BCN/dwell-times/",
        ]
        assert len(rows) == 1
        assert rows[0].port_name == "Barcelona" and rows[0].country_code == "ES"
        assert rows[0].country is None
        assert rows[0].import_teu == 34850 and rows[0].export_teu == 26635
        assert rows[0].export_dwell_time == pytest.approx(12.079057054263567)
        assert rows[0].transshipment_teu == pytest.approx(331.5)
        _assert_typed_roundtrip(rows)

    def test_port_volume_refuses_non_integral_teu(self) -> None:
        non_integral_traffic = {
            **PORT_TRAFFIC,
            "data": [{**PORT_TRAFFIC["data"][0], "Exports": 26635.5}],
        }
        transport = Recorder(
            HttpResponse(200, PORT_ROSTER),
            HttpResponse(200, non_integral_traffic),
            HttpResponse(200, PORT_DWELL),
        )
        fetcher = type(
            "FixtureFetcher", (EconDBPortVolumeFetcher,), {"http_transport": transport}
        )()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(
                ctx=_authorized_context(EconDBPortVolumeFetcher, sends=3), port_code="ES BCN"
            )
        assert caught.value.code == "ECONDB_SHAPE_INVALID"
        assert len(transport.calls) == 3

    def test_yield_curve_uses_explicit_series_to_maturity_mapping(self) -> None:
        transport = Recorder(
            HttpResponse(200, YIELD_FIVE_YEAR),
            HttpResponse(200, YIELD_TEN_YEAR),
        )
        fetcher = type(
            "FixtureFetcher", (EconDBYieldCurveFetcher,), {"http_transport": transport}
        )()
        rows = fetcher.fetch(
            ctx=_authorized_context(EconDBYieldCurveFetcher, sends=2),
            country="United States",
            series_ids=["USYIELD5", "USYIELD10"],
            maturities=["5y", "10y"],
            date="2025-01-02",
        )
        assert len(transport.calls) == 2
        assert [row.maturity for row in rows] == ["5Y", "10Y"]
        assert [row.value for row in rows] == [4.1, 4.5]
        assert {row.date for row in rows} == {date(2025, 1, 2)}
        _assert_typed_roundtrip(rows)

    def test_yield_curve_rejects_a_series_id_mismatch(self) -> None:
        transport = Recorder(
            HttpResponse(200, {**YIELD_FIVE_YEAR, "ticker": "OTHER"}),
            HttpResponse(200, YIELD_TEN_YEAR),
        )
        fetcher = type(
            "FixtureFetcher", (EconDBYieldCurveFetcher,), {"http_transport": transport}
        )()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(
                ctx=_authorized_context(EconDBYieldCurveFetcher, sends=2),
                country="United States",
                series_ids=["USYIELD5", "USYIELD10"],
                maturities=["5Y", "10Y"],
            )
        assert caught.value.code == "ECONDB_SHAPE_INVALID"
        assert len(transport.calls) == 2

    @pytest.mark.parametrize(
        "fetcher_type,query",
        [
            (EconDBEconomicIndicatorsFetcher, {}),
            (EconDBGdpNominalFetcher, {}),
            (EconDBGdpRealFetcher, {}),
            (EconDBPortVolumeFetcher, {}),
            (EconDBYieldCurveFetcher, {}),
        ],
    )
    def test_required_inputs_fail_before_io(self, fetcher_type, query) -> None:
        transport = Recorder(HttpResponse(200, {}))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            fetcher.fetch(ctx=_authorized_context(fetcher_type), **query)
        assert caught.value.code == "ECONDB_QUERY_INVALID"
        assert transport.calls == []

    @pytest.mark.parametrize(
        "fetcher_type,query",
        [
            (EconDBEconomicIndicatorsFetcher, {"symbol": "CPIUS"}),
            (EconDBGdpNominalFetcher, {"symbol": "GDPUS_NOMINAL"}),
            (EconDBGdpRealFetcher, {"symbol": "GDPUS_REAL"}),
            (EconDBPortVolumeFetcher, {"port_code": "ES BCN"}),
            (
                EconDBYieldCurveFetcher,
                {
                    "country": "United States",
                    "series_ids": ["USYIELD5", "USYIELD10"],
                    "maturities": ["5Y", "10Y"],
                },
            ),
        ],
    )
    def test_missing_token_fails_before_io(
        self, fetcher_type, query, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ECONDB_API_KEY", raising=False)
        transport = Recorder(HttpResponse(200, {}))
        fetcher = type("FixtureFetcher", (fetcher_type,), {"http_transport": transport})()
        with pytest.raises(ProviderEngineError) as caught:
            if fetcher_type is EconDBPortVolumeFetcher:
                sends = 3
            elif fetcher_type is EconDBYieldCurveFetcher:
                sends = 2
            else:
                sends = 1
            fetcher.fetch(ctx=_authorized_context(fetcher_type, sends=sends), **query)
        assert caught.value.code == "ECONDB_CREDENTIAL_MISSING"
        assert transport.calls == []


def test_provider_model_query_injects_econdb_identity_and_serializes_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = Recorder(HttpResponse(200, ECONOMIC_INDICATOR))
    observed: list[tuple[str | None, str | None]] = []

    class ObservingFetcher(EconDBEconomicIndicatorsFetcher):
        http_transport = transport

        def transform_query(self, **kwargs: object):
            params = super().transform_query(**kwargs)
            observed.append((params.source, params.market))
            return params

    instance = ObservingFetcher()
    capability = instance.capability
    descriptor = SimpleNamespace(
        source="econdb",
        model="EconomicIndicators",
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, instance)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, EconDBEconomicIndicator),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="econdb",
            model="EconomicIndicators",
            query={"symbol": "CPIUS"},
            principal=Principal(),
            ctx=_econdb_context("EconomicIndicators"),
        )
    )
    assert observed == [("econdb", "global")]
    assert response["source"] == "econdb"
    assert response["model"] == "EconomicIndicators"
    assert response["results"] == [
        {
            "date": "1947-01-01",
            "symbol_root": None,
            "symbol": "CPIUS",
            "country": "United States",
            "value": 21.48,
            "unit": None,
            "unit_multiplier": None,
            "scale": None,
            "order": None,
            "level": None,
            "title": "United States - Consumer price index",
            "description": "United States - Consumer price index",
            "country_code": None,
        },
        {
            "date": "1947-04-01",
            "symbol_root": None,
            "symbol": "CPIUS",
            "country": "United States",
            "value": 21.62,
            "unit": None,
            "unit_multiplier": None,
            "scale": None,
            "order": None,
            "level": None,
            "title": "United States - Consumer price index",
            "description": "United States - Consumer price index",
            "country_code": None,
        },
    ]
    assert transport.calls[0]["headers"]["Authorization"] == "Token fixture-token-never-sent"


def test_provider_model_query_injects_tiingo_identity_and_serializes_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bar = {
        "date": "2025-01-02",
        "open": 10.0,
        "high": 12.0,
        "low": 9.5,
        "close": 11.5,
        "volume": 1200,
    }
    transport = Recorder(HttpResponse(200, [bar]))
    observed: list[tuple[str | None, str | None]] = []

    class ObservingFetcher(TiingoEquityHistoricalFetcher):
        http_transport = transport

        def transform_query(self, **kwargs: object):
            params = super().transform_query(**kwargs)
            observed.append((params.source, params.market))
            return params

    instance = ObservingFetcher()
    capability = instance.capability
    descriptor = SimpleNamespace(
        source="tiingo",
        model="EquityHistorical",
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, instance)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, TiingoEquityHistorical),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="tiingo",
            model="EquityHistorical",
            query={"symbol": "AAPL"},
            principal=Principal(),
            ctx=_request_context("tiingo", "EquityHistorical", "us", "api.tiingo.com"),
        )
    )
    assert observed == [("tiingo", "us")]
    expected = TiingoEquityHistorical(
        symbol="AAPL",
        date=date(2025, 1, 2),
        open=10.0,
        high=12.0,
        low=9.5,
        close=11.5,
        volume=1200,
    )
    assert response["source"] == "tiingo"
    assert response["model"] == "EquityHistorical"
    assert response["results"] == [expected.model_dump(mode="json")]


def test_provider_model_query_injects_benzinga_identity_and_serializes_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = Recorder(HttpResponse(200, [BENZINGA_NEWS]))
    observed: list[tuple[str | None, str | None]] = []

    class ObservingFetcher(BenzingaCompanyNewsFetcher):
        http_transport = transport

        def transform_query(self, **kwargs: object):
            params = super().transform_query(**kwargs)
            observed.append((params.source, params.market))
            return params

    instance = ObservingFetcher()
    capability = instance.capability
    descriptor = SimpleNamespace(
        source="benzinga",
        model="CompanyNews",
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, instance)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, BenzingaCompanyNews),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="benzinga",
            model="CompanyNews",
            query={"symbol": "AAPL"},
            principal=Principal(),
            ctx=_request_context("benzinga", "CompanyNews", "us", "api.benzinga.com"),
        )
    )
    assert observed == [("benzinga", "us")]
    expected = BenzingaCompanyNews(
        id=BENZINGA_NEWS["id"],
        author=BENZINGA_NEWS["author"],
        created=datetime.fromisoformat("2024-01-01T13:35:14-04:00"),
        updated=datetime.fromisoformat("2024-01-01T13:35:15-04:00"),
        title=BENZINGA_NEWS["title"],
        teaser=BENZINGA_NEWS["teaser"],
        body=BENZINGA_NEWS["body"],
        url=BENZINGA_NEWS["url"],
        channels=["Options"],
        stocks=["AAPL"],
        tags=["BZI-AUOA"],
        symbol="AAPL",
    )
    assert response["source"] == "benzinga"
    assert response["model"] == "CompanyNews"
    assert response["results"] == [expected.model_dump(mode="json")]
