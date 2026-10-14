"""Offline contract tests for Benzinga's documented Ratings records."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from types import SimpleNamespace

import pytest

from opendata.data.models.benzinga import BenzingaPriceTarget
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
)
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers.benzinga._source import SOURCE
from opendata.data.providers.benzinga.models.price_target import BenzingaPriceTargetFetcher
from opendata.data.providers.benzinga.provider import PROVIDER
from opendata.data.providers.benzinga.specs import ACTION_SPELLINGS, DECLARED_MODELS, PRICE_TARGET
from opendata.services import provider_model_query

RATINGS_URL = "https://api.benzinga.com/api/v2.1/calendar/ratings"
RATINGS_PAGE_URL = "https://www.benzinga.com/quote/MNDY/analyst-ratings"

# Hand-transcribed from Benzinga's public Ratings response example, with one source-supported
# secondary URL deliberately different to ensure url_analyst reads url_calendar.
OFFICIAL_RATING = {
    "action_company": "Maintains",
    "action_pt": "Lowers",
    "adjusted_pt_current": "260.00",
    "adjusted_pt_prior": "300.00",
    "analyst": "Jefferies",
    "analyst_id": "57fe0358ac6af40001334e62",
    "analyst_name": "Riley Example",
    "currency": "USD",
    "date": "2024-02-01",
    "exchange": "NASDAQ",
    "id": "rating-123",
    "importance": 3,
    "name": "Monday.Com",
    "notes": "",
    "pt_current": "260.0000",
    "pt_prior": "300.00",
    "rating_current": "Buy",
    "rating_prior": "Hold",
    "ticker": "MNDY",
    "time": "14:52:07",
    "updated": 1706800000,
    "url": "https://www.benzinga.com/story/rating-123",
    "url_calendar": RATINGS_PAGE_URL,
    "url_news": "https://www.benzinga.com/stock-articles/MNDY/analyst-ratings",
    # This name is not in the documented Ratings field list and must not become a public value.
    "price_when_posted": 257.5,
}
DOCUMENT = {"ratings": [OFFICIAL_RATING]}


@pytest.fixture(autouse=True)
def _benzinga_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fixture-only credential for the scripted transport."""
    monkeypatch.setenv("BENZINGA_API_KEY", "fixture-token-not-a-secret")


def _fetcher(transport: FixedResponseTransport) -> BenzingaPriceTargetFetcher:
    """Bind one fixed response to a fetcher instance without touching the network."""
    fixture_type = type(
        "FixtureBenzingaPriceTargetFetcher",
        (BenzingaPriceTargetFetcher,),
        {"http_transport": transport},
    )
    return fixture_type()


def _run(
    document: object = DOCUMENT, **query: object
) -> tuple[FixedResponseTransport, list[BenzingaPriceTarget]]:
    transport = FixedResponseTransport(HttpResponse(200, document))
    rows = list(
        _fetcher(transport).fetch(
            ctx=fixture_context(SOURCE, PRICE_TARGET, sends=1),
            **query,
        )
    )
    return transport, rows


class TestOfficialRatingsMapping:
    def test_official_fields_and_epoch_seconds_keep_typed_meaning(self) -> None:
        transport, rows = _run(symbol="MNDY")

        assert len(transport.calls) == 1
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, BenzingaPriceTarget)
        assert row.symbol == "MNDY"
        assert row.published_date == date(2024, 2, 1)
        assert row.published_time == time(14, 52, 7)
        assert row.published_time.tzinfo is None
        assert row.last_updated == datetime(2024, 2, 1, 15, 6, 40, tzinfo=timezone.utc)
        assert row.exchange == "NASDAQ"
        assert row.analyst_name == "Riley Example"
        assert row.analyst_firm == "Jefferies"
        assert row.currency == "USD"
        assert row.rating_current == "Buy"
        assert row.price_target == 260.0
        assert row.price_target_previous == 300.0
        assert row.adj_price_target == 260.0
        assert row.previous_adj_price_target == 300.0
        assert row.price_when_posted is None

    def test_urls_keep_their_documented_roles_and_wire_query_is_bounded(self) -> None:
        transport, rows = _run(
            symbol="MNDY",
            action="upgrades",
            date="2024-02-01",
            limit=25,
        )
        request = transport.calls[0]
        assert request["url"] == RATINGS_URL
        assert request["params"]["parameters[tickers]"] == "MNDY"
        assert request["params"]["parameters[action]"] == "Upgrades"
        assert request["params"]["parameters[date]"] == "2024-02-01"
        assert request["params"]["pagesize"] == "25"
        assert request["params"]["token"] == "fixture-token-not-a-secret"
        assert "source" not in request["params"] and "market" not in request["params"]
        assert rows[0].url_analyst == RATINGS_PAGE_URL
        assert rows[0].url_analyst != OFFICIAL_RATING["url"]
        assert rows[0].url_news == OFFICIAL_RATING["url_news"]

    def test_blank_nulls_zero_and_public_field_set_are_preserved(self) -> None:
        raw = {
            **OFFICIAL_RATING,
            "importance": 0,
            "notes": "",
            "url_news": "",
            "url_calendar": "",
        }
        _, rows = _run({"ratings": [raw]}, symbol="MNDY")
        dumped = rows[0].model_dump()
        assert rows[0].importance == 0
        assert rows[0].notes is None
        assert rows[0].url_news is None
        assert rows[0].url_analyst is None
        assert rows[0].price_when_posted is None
        assert set(dumped) == set(BenzingaPriceTarget.model_fields)

    def test_custom_adapter_owns_row_contract_while_provider_binding_stays_unique(self) -> None:
        assert "PriceTarget" in DECLARED_MODELS
        matching = [
            binding
            for binding in PROVIDER.fetcher_bindings
            if "PriceTarget" in binding.canonical_model_ids
        ]
        assert len(matching) == 1
        assert matching[0].module == "opendata.data.providers.benzinga.models.price_target"
        assert matching[0].class_name == "BenzingaPriceTargetFetcher"
        assert BenzingaPriceTargetFetcher.row_model is BenzingaPriceTarget
        assert not hasattr(BenzingaPriceTargetFetcher, "model_spec")
        assert ACTION_SPELLINGS[4] == ("upgrades", "Upgrades")

    @pytest.mark.parametrize("symbol", ["BRK.B", "BRK-B", "1234", "MNDY,BRK.B"])
    def test_valid_symbol_forms_are_checked_without_rewriting(self, symbol: str) -> None:
        transport, rows = _run(symbol=symbol)

        assert len(rows) == 1
        assert transport.calls[0]["params"]["parameters[tickers]"] == symbol

    def test_none_symbol_remains_optional(self) -> None:
        transport, rows = _run(symbol=None)

        assert len(rows) == 1
        assert "parameters[tickers]" not in transport.calls[0]["params"]


class TestRatingsRefusals:
    @pytest.mark.parametrize(
        "query",
        [
            {"symbol": "bad symbol"},
            {"symbol": "MNDY, bad symbol"},
            {"symbol": "MNDY,"},
            {"symbol": ",".join(f"TICKER{index}" for index in range(51))},
            {"symbol": "MNDY", "limit": 0},
            {"symbol": "MNDY", "limit": 1001},
            {"symbol": "MNDY", "limit": True},
            {"symbol": "MNDY", "start_date": "2024-02-02", "end_date": "2024-02-01"},
            {"symbol": "MNDY", "unexpected": "value"},
            {"symbol": "MNDY", "action": "not-a-documented-action"},
        ],
    )
    def test_invalid_queries_fail_before_transport(self, query: dict[str, object]) -> None:
        transport = FixedResponseTransport(HttpResponse(200, DOCUMENT))
        with pytest.raises(ProviderEngineError):
            _fetcher(transport).fetch(ctx=fixture_context(SOURCE, PRICE_TARGET, sends=1), **query)
        assert transport.calls == []

    @pytest.mark.parametrize(
        "symbol",
        [
            "bad symbol",
            "MNDY, bad symbol",
            "MNDY,",
            ",".join(f"TICKER{index}" for index in range(51)),
        ],
    )
    def test_invalid_symbols_have_stable_error_classification_before_transport(
        self, symbol: str
    ) -> None:
        transport = FixedResponseTransport(HttpResponse(200, DOCUMENT))
        with pytest.raises(ProviderEngineError) as error:
            _fetcher(transport).fetch(
                ctx=fixture_context(SOURCE, PRICE_TARGET, sends=1),
                symbol=symbol,
            )

        assert error.value.code == "BENZINGA_QUERY_INVALID"
        assert error.value.invalid == ("symbol",)
        assert transport.calls == []

    def test_missing_token_fails_before_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BENZINGA_API_KEY", raising=False)
        transport = FixedResponseTransport(HttpResponse(200, DOCUMENT))
        with pytest.raises(ProviderEngineError, match="BENZINGA_CREDENTIAL_MISSING"):
            _fetcher(transport).fetch(
                ctx=fixture_context(SOURCE, PRICE_TARGET, sends=1), symbol="MNDY"
            )
        assert transport.calls == []

    @pytest.mark.parametrize(
        "raw",
        [
            {**OFFICIAL_RATING, "updated": True},
            {**OFFICIAL_RATING, "updated": 1706800000.0},
            {**OFFICIAL_RATING, "updated": "1706800000"},
            {**OFFICIAL_RATING, "updated": -1},
            {**OFFICIAL_RATING, "time": "14:52:07+00:00"},
            {**OFFICIAL_RATING, "time": "not-a-time"},
            {**OFFICIAL_RATING, "date": "2024-02-30"},
            {**OFFICIAL_RATING, "pt_current": "NaN"},
            {**OFFICIAL_RATING, "importance": True},
        ],
    )
    def test_malformed_source_cells_are_rejected(self, raw: dict[str, object]) -> None:
        with pytest.raises(ProviderEngineError):
            _run({"ratings": [raw]}, symbol="MNDY")

    @pytest.mark.parametrize("document", [{}, {"ratings": {}}, {"ratings": [None]}])
    def test_wrong_response_envelope_or_row_shape_is_rejected(self, document: object) -> None:
        with pytest.raises(ProviderEngineError):
            _run(document, symbol="MNDY")

    def test_oversized_single_page_is_not_silently_truncated(self) -> None:
        document = {"ratings": [OFFICIAL_RATING] * 1001}
        with pytest.raises(ProviderEngineError):
            _run(document, symbol="MNDY", limit=1000)


def test_provider_model_query_injects_identity_and_serializes_typed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FixedResponseTransport(HttpResponse(200, DOCUMENT))
    observed: list[tuple[str | None, str | None]] = []

    class ObservingFetcher(BenzingaPriceTargetFetcher):
        http_transport = transport

        def transform_query(self, **kwargs: object):
            params = super().transform_query(**kwargs)
            observed.append((getattr(params, "source", None), getattr(params, "market", None)))
            return params

    fetcher = ObservingFetcher()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source=SOURCE,
        model="PriceTarget",
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
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, fetcher)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, BenzingaPriceTarget),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source=SOURCE,
            model="PriceTarget",
            query={"symbol": "MNDY"},
            principal=Principal(),
            ctx=fixture_context(SOURCE, PRICE_TARGET, sends=1),
        )
    )

    assert observed == [(SOURCE, "us")]
    assert response["source"] == SOURCE and response["model"] == "PriceTarget"
    assert response["results"] == [OFFICIAL_RATING_RESULT]
    assert transport.calls[0]["params"]["parameters[tickers]"] == "MNDY"


OFFICIAL_RATING_RESULT = {
    "symbol": "MNDY",
    "published_date": "2024-02-01",
    "published_time": "14:52:07",
    "exchange": "NASDAQ",
    "analyst_name": "Riley Example",
    "analyst_firm": "Jefferies",
    "analyst_id": "57fe0358ac6af40001334e62",
    "company_name": "Monday.Com",
    "currency": "USD",
    "action": "Maintains",
    "action_change": "Lowers",
    "rating_current": "Buy",
    "rating_previous": "Hold",
    "importance": 3,
    "price_target": 260.0,
    "price_target_previous": 300.0,
    "adj_price_target": 260.0,
    "previous_adj_price_target": 300.0,
    "price_when_posted": None,
    "notes": None,
    "url_news": OFFICIAL_RATING["url_news"],
    "url_analyst": RATINGS_PAGE_URL,
    "id": "rating-123",
    "last_updated": "2024-02-01T15:06:40Z",
}
