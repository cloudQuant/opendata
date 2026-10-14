"""Offline witness for Trading Economics EconomicCalendar.

The row below is written from the official API schema example, then kept independent of the
provider declaration so field aliases and casts are tested rather than mirrored from it. All HTTP
responses use the engine's fixture transport and a synthetic API key; no network or rights grant is
implied by these tests.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from opendata.data.models.tradingeconomics import TradingEconomicsEconomicCalendar
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
)
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers.tradingeconomics._source import SOURCE
from opendata.data.providers.tradingeconomics.models.economic_calendar import (
    TradingEconomicsEconomicCalendarFetcher,
)
from opendata.data.providers.tradingeconomics.provider import PROVIDER
from opendata.data.providers.tradingeconomics.specs import (
    ECONOMIC_CALENDAR,
    GROUP_VALUES,
    IMPORTANCE_VALUES,
)
from opendata.services import provider_model_query

API_URL = "https://api.tradingeconomics.com"

# Independent source-shaped fixture based on the official calendar response table. TE publishes
# CalendarId and DateSpan as decimal text in documented payload examples and Importance as 1/2/3.
CALENDAR_DOCUMENT = [
    {
        "CalendarId": "319282",
        "Date": "2023-04-03T14:00:00",
        "Country": "United States",
        "Category": "Business Confidence",
        "Event": "ISM Manufacturing PMI",
        "Reference": "Mar",
        "ReferenceDate": "2023-03-31T00:00:00",
        "Source": "Institute for Supply Management",
        "SourceURL": "https://www.ismworld.org",
        "Actual": "46.3",
        "Previous": "47.7",
        "Forecast": "47.5",
        "TEForecast": "49",
        "URL": "/united-states/business-confidence",
        "DateSpan": "0",
        "Importance": 3,
        "LastUpdate": "2023-04-03T14:00:00.68",
        "Revised": "",
        "Currency": "$",
        "Unit": "%",
        "Ticker": "USISMIM",
        "Symbol": "USISMIM",
    }
]


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant a synthetic token; test transports never open a socket."""
    monkeypatch.setenv("TRADINGECONOMICS_API_KEY", "fixture-key")


def _fetch(document: object, *, sends: int = 1, **query: object):
    transport = FixedResponseTransport(HttpResponse(200, document))
    fetcher = TradingEconomicsEconomicCalendarFetcher()
    fetcher.http_transport = transport
    rows = fetcher.fetch(
        ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=sends),
        **query,
    )
    return transport.calls, tuple(rows)


class TestDeclaration:
    def test_one_provider_binding_declares_the_complete_output_contract(self) -> None:
        assert SOURCE == "tradingeconomics"
        assert PROVIDER.source == SOURCE
        assert PROVIDER.credentials[0].environment_variable == "TRADINGECONOMICS_API_KEY"
        assert ECONOMIC_CALENDAR.model == "EconomicCalendar"
        assert tuple(column.name for column in ECONOMIC_CALENDAR.columns) == tuple(
            TradingEconomicsEconomicCalendar.model_fields
        )
        assert dict(GROUP_VALUES)["interest_rate"] == "interest rates"
        assert dict(GROUP_VALUES)["labour"] == "labor"
        assert dict(IMPORTANCE_VALUES) == {"low": "1", "medium": "2", "high": "3"}


class TestDocumentedRoutes:
    def test_country_group_date_route_normalizes_a_typed_serializable_event(self) -> None:
        calls, rows = _fetch(
            CALENDAR_DOCUMENT,
            country="United States",
            group="business",
            start_date=date(2023, 4, 1),
            end_date=date(2023, 4, 30),
        )
        assert len(calls) == 1
        assert calls[0]["url"] == (
            f"{API_URL}/calendar/country/United%20States/group/business/2023-04-01/2023-04-30"
        )
        assert calls[0]["params"] == {"c": "fixture-key", "f": "json"}
        assert len(rows) == 1 and type(rows[0]) is TradingEconomicsEconomicCalendar
        row = rows[0]
        assert row.date == datetime(2023, 4, 3, 14, 0, tzinfo=timezone.utc)
        assert row.reference_date == date(2023, 3, 31)
        assert row.calendar_id == 319282
        assert row.date_span == 0
        assert row.importance == "high"
        assert row.consensus == "47.5"
        assert row.forecast == "49"
        assert row.last_updated == datetime(2023, 4, 3, 14, 0, 0, 680000)
        assert row.model_dump(mode="json")["reference_date"] == "2023-03-31"

    def test_integer_calendar_id_is_encoded_as_documented_path_segment(self) -> None:
        calls, rows = _fetch(CALENDAR_DOCUMENT, calendar_id=319282)
        assert calls[0]["url"] == f"{API_URL}/calendar/calendarid/319282"
        assert calls[0]["params"] == {"c": "fixture-key", "f": "json"}
        assert rows[0].calendar_id == 319282

    def test_multiple_calendar_ids_remain_a_single_comma_separated_path(self) -> None:
        calls, _ = _fetch(CALENDAR_DOCUMENT, calendar_id="319282,319283")
        assert calls[0]["url"] == f"{API_URL}/calendar/calendarid/319282,319283"

    def test_long_calendar_id_list_is_split_below_the_url_limit(self) -> None:
        identifiers = ",".join(str(value) for value in range(1, 101))
        calls, _ = _fetch(
            CALENDAR_DOCUMENT,
            sends=4,
            calendar_id=identifiers,
        )
        assert len(calls) > 1
        assert all(len(call["url"]) + 1 + len(urlencode(call["params"])) <= 260 for call in calls)
        assert all("/calendar/calendarid/" in call["url"] for call in calls)

    def test_country_importance_uses_the_documented_numeric_query_spelling(self) -> None:
        calls, rows = _fetch(CALENDAR_DOCUMENT, country="United States", importance="medium")
        assert calls[0]["url"] == f"{API_URL}/calendar/country/United%20States"
        assert calls[0]["params"] == {
            "c": "fixture-key",
            "f": "json",
            "importance": "2",
        }
        assert rows[0].importance == "high", "response importance is event metadata, not the filter"

    def test_country_date_and_importance_route_is_documented(self) -> None:
        calls, _ = _fetch(
            CALENDAR_DOCUMENT,
            country="All",
            start_date=date(2016, 12, 2),
            end_date=date(2016, 12, 6),
            importance="high",
        )
        assert calls[0]["url"] == (f"{API_URL}/calendar/country/All/2016-12-02/2016-12-06")
        assert calls[0]["params"] == {
            "c": "fixture-key",
            "f": "json",
            "importance": "3",
        }

    def test_group_date_route_and_default_snapshot_route_are_both_supported(self) -> None:
        grouped_calls, _ = _fetch(
            CALENDAR_DOCUMENT,
            group="interest_rate",
            start_date=date(2016, 3, 1),
            end_date=date(2018, 3, 3),
        )
        assert grouped_calls[0]["url"] == (
            f"{API_URL}/calendar/group/interest%20rates/2016-03-01/2018-03-03"
        )
        labour_calls, _ = _fetch(CALENDAR_DOCUMENT, group="labour")
        assert labour_calls[0]["url"] == f"{API_URL}/calendar/group/labor"
        snapshot_calls, _ = _fetch(CALENDAR_DOCUMENT)
        assert snapshot_calls[0]["url"] == f"{API_URL}/calendar"


class TestPreflightRefusals:
    def test_one_sided_date_window_is_rejected_without_http(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ValidationError):
            fetcher.fetch(
                ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1),
                start_date=date(2023, 4, 1),
            )
        assert transport.calls == []

    def test_generated_validation_does_not_retain_caller_values(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ValidationError) as exc_info:
            fetcher.fetch(
                ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1),
                unexpected="sensitive-value",
            )
        assert "sensitive-value" not in str(exc_info.value)
        assert all(error["input"] is None for error in exc_info.value.errors())
        assert transport.calls == []

    def test_reversed_date_window_is_rejected_without_http(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ValidationError):
            fetcher.fetch(
                ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1),
                start_date=date(2023, 5, 1),
                end_date=date(2023, 4, 1),
            )
        assert transport.calls == []

    @pytest.mark.parametrize(
        "query",
        [
            {"calendar_id": "31/9282"},
            {"calendar_id": "0"},
            {"calendar_id": "319282", "country": "United States"},
            {"group": "business", "importance": "high"},
            {
                "country": "United States",
                "group": "business",
                "importance": "high",
            },
            {"unexpected": "value"},
        ],
    )
    def test_unsupported_or_invalid_input_is_refused_before_http(
        self, query: dict[str, object]
    ) -> None:
        transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ValidationError):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1), **query)
        assert transport.calls == []

    def test_missing_api_key_is_rejected_before_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRADINGECONOMICS_API_KEY")
        transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_CREDENTIAL_MISSING"):
            fetcher.fetch(
                ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1),
                country="United States",
            )
        assert transport.calls == []


class TestResponseRefusals:
    def test_non_list_document_is_a_shape_error(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, {"data": CALENDAR_DOCUMENT}))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_SHAPE_INVALID"):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1))
        assert len(transport.calls) == 1

    def test_malformed_row_type_is_a_shape_error(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, ["not an event record"]))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_SHAPE_INVALID"):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1))

    @pytest.mark.parametrize("missing", ["CalendarId", "Date"])
    def test_missing_required_source_columns_are_shape_errors(self, missing: str) -> None:
        document = [dict(CALENDAR_DOCUMENT[0])]
        document[0].pop(missing)
        transport = FixedResponseTransport(HttpResponse(200, document))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_SHAPE_INVALID"):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1))
        assert len(transport.calls) == 1

    def test_unknown_date_span_is_a_shape_error(self) -> None:
        document = [dict(CALENDAR_DOCUMENT[0], DateSpan="2")]
        transport = FixedResponseTransport(HttpResponse(200, document))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_SHAPE_INVALID"):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1))
        assert len(transport.calls) == 1

    def test_response_over_the_documented_row_limit_is_rejected(self) -> None:
        document = [
            dict(CALENDAR_DOCUMENT[0], CalendarId=str(400000 + value)) for value in range(1001)
        ]
        transport = FixedResponseTransport(HttpResponse(200, document))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_ROW_LIMIT"):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1))
        assert len(transport.calls) == 1

    def test_rate_limit_status_is_not_reported_as_empty_calendar(self) -> None:
        transport = FixedResponseTransport(HttpResponse(429, {"message": "rate limited"}))
        fetcher = TradingEconomicsEconomicCalendarFetcher()
        fetcher.http_transport = transport
        with pytest.raises(ProviderEngineError, match="TRADINGECONOMICS_RATE_LIMITED"):
            fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1))
        assert len(transport.calls) == 1


@pytest.mark.parametrize("identity", [{"source": "other"}, {"market": "us"}])
def test_injected_identity_mismatch_is_rejected_before_http(identity: dict[str, object]) -> None:
    transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
    fetcher = TradingEconomicsEconomicCalendarFetcher()
    fetcher.http_transport = transport
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1), **identity)
    assert transport.calls == []


def test_provider_model_query_accepts_pinned_identity_without_sending_it_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FixedResponseTransport(HttpResponse(200, CALENDAR_DOCUMENT))
    observed_queries: list[dict[str, object]] = []

    class ObservingFetcher(TradingEconomicsEconomicCalendarFetcher):
        http_transport = transport

        def transform_query(self, **kwargs: object):
            observed_queries.append(dict(kwargs))
            return super().transform_query(**kwargs)

    fetcher = ObservingFetcher()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source=SOURCE,
        model=ECONOMIC_CALENDAR.model,
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
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, TradingEconomicsEconomicCalendar),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source=SOURCE,
            model=ECONOMIC_CALENDAR.model,
            query={},
            principal=Principal(),
            ctx=fixture_context(SOURCE, ECONOMIC_CALENDAR, sends=1),
        )
    )

    assert observed_queries == [{"source": SOURCE, "market": "all"}]
    assert len(transport.calls) == 1
    assert transport.calls[0]["params"] == {"c": "fixture-key", "f": "json"}
    assert response["source"] == SOURCE
    assert response["model"] == ECONOMIC_CALENDAR.model
    assert response["results"] == [
        TradingEconomicsEconomicCalendar.model_validate(
            {
                "date": datetime(2023, 4, 3, 14, 0, tzinfo=timezone.utc),
                "country": "United States",
                "category": "Business Confidence",
                "event": "ISM Manufacturing PMI",
                "importance": "high",
                "source": "Institute for Supply Management",
                "currency": "$",
                "unit": "%",
                "consensus": "47.5",
                "previous": "47.7",
                "revised": "",
                "actual": "46.3",
                "forecast": "49",
                "reference": "Mar",
                "reference_date": "2023-03-31",
                "calendar_id": 319282,
                "date_span": 0,
                "symbol": "USISMIM",
                "ticker": "USISMIM",
                "te_url": "/united-states/business-confidence",
                "source_url": "https://www.ismworld.org",
                "last_updated": "2023-04-03T14:00:00.68",
            }
        ).model_dump(mode="json")
    ]
