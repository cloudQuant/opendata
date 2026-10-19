"""The cboe pilot: one real provider driven entirely by declarations, verified offline.

This is the end-to-end proof of the declarative engine: the fetchers are generated from
``ModelSpec`` data, reach the registry through the ordinary provider descriptor, and are judged
without a socket. It also fixes the two behaviours the pilot exposed as necessary and were not in
the first engine draft -- path templating from a validated parameter, and a transport seam on the
generated fetcher.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time
from types import SimpleNamespace

import pytest

from opendata.data.providers._engine.http_json import HttpResponse, make_http_json_fetcher
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    SyntheticTransport,
    fixture_context,
)
from opendata.data.providers.catalog import get_provider, register_provider
from opendata.data.providers.cboe import specs
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.models.available_indices import (
    CboeAvailableIndicesFetcher,
)
from opendata.data.providers.cboe.models.index_constituents import (
    CboeIndexConstituentsFetcher,
)
from opendata.data.providers.cboe.models.index_search import CboeIndexSearchFetcher
from opendata.data.providers.cboe.specs import (
    AVAILABLE_INDICES,
    INDEX_CONSTITUENTS,
    INDEX_SEARCH,
)
from opendata.data.registry import ProviderRegistry
from opendata.services import provider_model_query

DIRECTORY_URL = "https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json"
CONSTITUENTS_URL = (
    "https://cdn.cboe.com/api/global/european_indices/constituent_quotes/BUK100P.json"
)

DIRECTORY_RECORD = {
    "index_symbol": "SPX",
    "index_name": "S&P 500",
    "exchange": "Cboe",
    "currency": "USD",
    "description": "Broad US equities",
    "mkt_data_delay": "15 min",
    "calc_start_time": "08:30",
    "calc_end_time": "17:00",
    "time_zone": "America/Chicago",
    "tick_days": "1",
    "tick_frequency": "15",
    "tick_period": "minutes",
    "featured": True,
    "source": "cboe",
}

CONSTITUENT_RECORD = {
    "symbol": "AZL.L",
    "name": "AstraZeneca",
    "type": "Equity",
    "current_price": 148.2,
    "open": 147.0,
    "high": 149.0,
    "low": 146.5,
    "close": 148.0,
    "volume": 3_400_000,
    "prev_day_close": 147.5,
    "price_change": 0.7,
    "price_change_percent": 47.5,
    "tick": "U",
    "last_trade_time": "2026-10-08T15:32:11",
}

CONSTITUENTS_DOCUMENT = {
    "symbol": "BUK100P",
    "name": "FTSE UK 100 Principal",
    "currency": "GBP",
    "data": [CONSTITUENT_RECORD],
}


def _with_transport(cls: type, transport: SyntheticTransport):
    """Instantiate a generated fetcher class against a recorded transport."""
    return type(cls.__name__, (cls,), {"http_transport": transport})()


class TestProviderRegistration:
    """cboe is a real provider now, not a reserved catalog identity."""

    def test_catalog_no_longer_reserves_cboe(self) -> None:
        provider = get_provider("cboe")
        assert "reserved" not in provider.description.lower()
        assert len(provider.credentials) == 1
        assert provider.credentials[0].environment_variable == "CBOE_LIVEVOL_ACCESS_TOKEN"
        assert provider.credentials[0].required_for_health is False
        assert len(provider.fetcher_bindings) == 11

    def test_registry_publishes_every_declared_model(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("cboe", registry)
        domains = {c.domain for c in capabilities}
        assert domains == {
            "cboe_available_indices",
            "cboe_equity_search",
            "cboe_equity_historical",
            "cboe_etf_historical",
            "cboe_equity_quote",
            "cboe_options_chains",
            "cboe_index_constituent_quotes",
            "cboe_index_search",
            "cboe_index_historical",
            "cboe_index_snapshots",
            "cboe_futures_curve",
        }
        # Two cross-faces so a silent drop cannot hide in the literal: no two bindings publish the
        # same domain, and the published count equals the descriptor's binding count.
        assert len(capabilities) == len(domains) == len(get_provider("cboe").fetcher_bindings)
        assert all(c.source == "cboe" for c in capabilities)
        assert not any(c.verified for c in capabilities)


class TestStaticPathModel:
    """A model whose whole response document is the record list."""

    def test_fetch_returns_declared_columns_only(self) -> None:
        transport = SyntheticTransport(
            {(DIRECTORY_URL, frozenset()): HttpResponse(200, [DIRECTORY_RECORD])}
        )
        fetcher = _with_transport(CboeAvailableIndicesFetcher, transport)
        result = fetcher.fetch(
            ctx=fixture_context(SOURCE, AVAILABLE_INDICES, sends=1),
            source=SOURCE,
            market=AVAILABLE_INDICES.market,
        )
        rows = list(result)
        assert len(rows) == 1
        assert rows[0].symbol == "SPX"
        assert rows[0].name == "S&P 500"
        assert rows[0].data_delay == 15
        assert rows[0].open_time == time(8, 30)
        assert rows[0].close_time == time(17, 0)
        assert set(rows[0].model_dump()) == {column.name for column in AVAILABLE_INDICES.columns}
        assert transport.calls[0]["url"] == DIRECTORY_URL
        assert transport.calls[0]["params"] == {}

    def test_zero_delay_and_explicit_null_times_are_preserved(self) -> None:
        record = {
            **DIRECTORY_RECORD,
            "mkt_data_delay": "0 min",
            "calc_start_time": None,
            "calc_end_time": None,
        }
        fetcher = _with_transport(
            CboeAvailableIndicesFetcher,
            FixedResponseTransport(HttpResponse(200, [record])),
        )
        rows = list(
            fetcher.fetch(
                ctx=fixture_context(SOURCE, AVAILABLE_INDICES, sends=1),
                source=SOURCE,
                market=AVAILABLE_INDICES.market,
            )
        )
        assert rows[0].data_delay == 0
        assert rows[0].open_time is None
        assert rows[0].close_time is None

    def test_available_indices_allows_nullable_name_but_requires_symbol(self) -> None:
        record = {**DIRECTORY_RECORD, "index_name": None}
        fetcher = _with_transport(
            CboeAvailableIndicesFetcher,
            FixedResponseTransport(HttpResponse(200, [record])),
        )
        rows = list(
            fetcher.fetch(
                ctx=fixture_context(SOURCE, AVAILABLE_INDICES, sends=1),
                source=SOURCE,
                market=AVAILABLE_INDICES.market,
            )
        )
        assert rows[0].name is None

        from opendata.data.providers._engine.http_json import ProviderEngineError

        missing_symbol = {**DIRECTORY_RECORD, "index_symbol": None}
        broken_fetcher = _with_transport(
            CboeAvailableIndicesFetcher,
            FixedResponseTransport(HttpResponse(200, [missing_symbol])),
        )
        with pytest.raises(ProviderEngineError, match="CBOE_SHAPE_INVALID"):
            list(
                broken_fetcher.fetch(
                    ctx=fixture_context(SOURCE, AVAILABLE_INDICES, sends=1),
                    source=SOURCE,
                    market=AVAILABLE_INDICES.market,
                )
            )

    @pytest.mark.parametrize(
        "bad_fields",
        [
            {"mkt_data_delay": "delayed"},
            {"calc_start_time": "25:70"},
        ],
    )
    def test_directory_refuses_unparseable_public_contract_values(
        self, bad_fields: dict[str, object]
    ) -> None:
        from opendata.data.providers._engine.http_json import ProviderEngineError

        fetcher = _with_transport(
            CboeAvailableIndicesFetcher,
            FixedResponseTransport(HttpResponse(200, [{**DIRECTORY_RECORD, **bad_fields}])),
        )
        with pytest.raises(ProviderEngineError, match="CBOE_SHAPE_INVALID"):
            list(
                fetcher.fetch(
                    ctx=fixture_context(SOURCE, AVAILABLE_INDICES, sends=1),
                    source=SOURCE,
                    market=AVAILABLE_INDICES.market,
                )
            )


class TestTemplatedPathModel:
    """A model that addresses its resource in the URL rather than the query string."""

    def _transport(self) -> SyntheticTransport:
        return SyntheticTransport(
            {(CONSTITUENTS_URL, frozenset()): HttpResponse(200, CONSTITUENTS_DOCUMENT)}
        )

    def _fetch(self, transport: SyntheticTransport, symbol: str = "BUK100P"):
        fetcher = _with_transport(CboeIndexConstituentsFetcher, transport)
        return fetcher.fetch(
            ctx=fixture_context(SOURCE, INDEX_CONSTITUENTS, sends=1),
            symbol=symbol,
            source=SOURCE,
            market=INDEX_CONSTITUENTS.market,
        )

    def test_symbol_is_addressed_in_the_path_not_the_query(self) -> None:
        transport = self._transport()
        rows = list(self._fetch(transport))
        assert transport.calls[0]["url"] == CONSTITUENTS_URL
        assert transport.calls[0]["params"] == {}
        assert rows[0].last_price == 148.2
        assert rows[0].change_percent == 47.5
        assert rows[0].asset_type == "Equity"
        assert rows[0].last_trade_time == datetime(2026, 10, 8, 15, 32, 11)

    def test_envelope_facts_do_not_overwrite_the_record_symbol(self) -> None:
        transport = self._transport()
        rows = list(self._fetch(transport))
        assert rows[0].symbol == "AZL.L"

    def test_envelope_fills_a_column_the_record_does_not_publish(self) -> None:
        transport = self._transport()
        rows = list(self._fetch(transport))
        assert "currency" not in CONSTITUENT_RECORD
        assert rows[0].currency == "GBP"

    def test_explicit_null_last_trade_time_is_preserved(self) -> None:
        document = {
            **CONSTITUENTS_DOCUMENT,
            "data": [{**CONSTITUENT_RECORD, "last_trade_time": None}],
        }
        fetcher = _with_transport(
            CboeIndexConstituentsFetcher,
            FixedResponseTransport(HttpResponse(200, document)),
        )
        rows = list(
            fetcher.fetch(
                ctx=fixture_context(SOURCE, INDEX_CONSTITUENTS, sends=1),
                symbol="BUK100P",
                source=SOURCE,
                market=INDEX_CONSTITUENTS.market,
            )
        )
        assert rows[0].last_trade_time is None

    def test_unparseable_last_trade_time_is_refused(self) -> None:
        from opendata.data.providers._engine.http_json import ProviderEngineError

        document = {
            **CONSTITUENTS_DOCUMENT,
            "data": [{**CONSTITUENT_RECORD, "last_trade_time": "not-a-timestamp"}],
        }
        fetcher = _with_transport(
            CboeIndexConstituentsFetcher,
            FixedResponseTransport(HttpResponse(200, document)),
        )
        with pytest.raises(ProviderEngineError, match="CBOE_SHAPE_INVALID"):
            list(
                fetcher.fetch(
                    ctx=fixture_context(SOURCE, INDEX_CONSTITUENTS, sends=1),
                    symbol="BUK100P",
                    source=SOURCE,
                    market=INDEX_CONSTITUENTS.market,
                )
            )

    def test_constituent_requires_nonnull_symbol(self) -> None:
        from opendata.data.providers._engine.http_json import ProviderEngineError

        document = {
            **CONSTITUENTS_DOCUMENT,
            "data": [{**CONSTITUENT_RECORD, "symbol": None}],
        }
        fetcher = _with_transport(
            CboeIndexConstituentsFetcher,
            FixedResponseTransport(HttpResponse(200, document)),
        )
        with pytest.raises(ProviderEngineError, match="CBOE_SHAPE_INVALID"):
            list(
                fetcher.fetch(
                    ctx=fixture_context(SOURCE, INDEX_CONSTITUENTS, sends=1),
                    symbol="BUK100P",
                    source=SOURCE,
                    market=INDEX_CONSTITUENTS.market,
                )
            )


def test_index_search_rejects_nullable_public_identity_fields() -> None:
    from opendata.data.providers._engine.http_json import ProviderEngineError

    for broken in (
        {**DIRECTORY_RECORD, "index_symbol": None},
        {**DIRECTORY_RECORD, "index_name": None},
    ):
        transport = FixedResponseTransport(HttpResponse(200, [broken]))
        fetcher = _with_transport(CboeIndexSearchFetcher, transport)
        with pytest.raises(ProviderEngineError, match="CBOE_SHAPE_INVALID"):
            list(
                fetcher.fetch(
                    ctx=fixture_context(SOURCE, INDEX_SEARCH, sends=1),
                    query="Broad",
                    is_symbol=False,
                    source=SOURCE,
                    market=INDEX_SEARCH.market,
                )
            )


@pytest.mark.parametrize(
    ("model", "fetcher_class", "spec", "query", "url", "params", "document", "expected"),
    [
        (
            "IndexSearch",
            CboeIndexSearchFetcher,
            INDEX_SEARCH,
            {"query": "SPX", "is_symbol": True},
            DIRECTORY_URL,
            {"query": "SPX", "is_symbol": "true"},
            [DIRECTORY_RECORD],
            {
                "symbol": "SPX",
                "name": "S&P 500",
                "currency": "USD",
                "description": "Broad US equities",
                "data_delay": 15,
                "open_time": "08:30:00",
                "close_time": "17:00:00",
                "time_zone": "America/Chicago",
                "tick_days": "1",
                "tick_frequency": "15",
                "tick_period": "minutes",
            },
        ),
        (
            "IndexConstituents",
            CboeIndexConstituentsFetcher,
            INDEX_CONSTITUENTS,
            {"symbol": "BUK100P"},
            CONSTITUENTS_URL,
            {},
            CONSTITUENTS_DOCUMENT,
            {
                "symbol": "AZL.L",
                "name": "AstraZeneca",
                "currency": "GBP",
                "security_type": None,
                "last_price": 148.2,
                "open": 147.0,
                "high": 149.0,
                "low": 146.5,
                "close": 148.0,
                "volume": 3_400_000,
                "prev_close": 147.5,
                "change": 0.7,
                "change_percent": 47.5,
                "tick": "U",
                "last_trade_time": "2026-10-08T15:32:11",
                "asset_type": "Equity",
            },
        ),
    ],
)
def test_service_injects_identity_and_serializes_typed_cboe_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_class: type,
    spec: ModelSpec,
    query: dict[str, object],
    url: str,
    params: dict[str, str],
    document: object,
    expected: dict[str, object],
) -> None:
    registry = ProviderRegistry()
    register_provider("cboe", registry)
    transport = SyntheticTransport({(url, frozenset(params.items())): HttpResponse(200, document)})
    monkeypatch.setattr(fetcher_class, "http_transport", transport)
    captured_query: dict[str, object] = {}
    original_transform_query = fetcher_class.transform_query

    def capture_query(fetcher: object, **kwargs: object) -> object:
        captured_query.update(kwargs)
        return original_transform_query(fetcher, **kwargs)

    monkeypatch.setattr(fetcher_class, "transform_query", capture_query)
    principal = SimpleNamespace(allows_domain=lambda domain: domain.startswith("cboe_"))
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source=SOURCE,
            model=model,
            query=query,
            principal=principal,
            ctx=fixture_context(SOURCE, spec, sends=1),
        )
    )

    assert captured_query["source"] == SOURCE
    assert captured_query["market"] == spec.market
    assert response["source"] == SOURCE
    assert response["model"] == model
    assert response["results"] == [expected]
    assert json.dumps(response["results"], allow_nan=False)
    assert transport.calls[0]["url"] == url
    assert transport.calls[0]["params"] == params


class TestDeclarationRejectsMalformedTemplate:
    """A path placeholder must name a declared parameter and resolve to one safe segment."""

    def _spec(self, path: str) -> ModelSpec:
        return ModelSpec(
            model="Templated",
            domain="templated",
            asset_class="equity",
            period="snapshot",
            market="all",
            base_url="https://templated.test",
            path=path,
            columns=(ColumnSpec("symbol", "str", required=True),),
            params=(ParamSpec("symbol", "str", required=True),),
            scenario="模板路径的声明校验",
            error_prefix="TEMPLATED",
        )

    def test_unknown_placeholder_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="not a declared parameter"):
            self._spec("/{ticker}.json")

    def test_brace_without_a_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            self._spec("/quotes/{1bad}.json")

    @pytest.mark.parametrize("unsafe", ["../../etc/passwd", "BUK/100P", "BUK?x=1", "BUK 100P"])
    def test_an_unsafe_segment_is_refused_before_any_send(self, unsafe: str) -> None:
        transport = SyntheticTransport({})
        fetcher = _with_transport(
            make_http_json_fetcher("templated", self._spec("/quotes/{symbol}.json")), transport
        )
        with pytest.raises(Exception) as caught:
            fetcher.fetch(
                ctx=fixture_context("templated", self._spec("/quotes/{symbol}.json"), sends=1),
                symbol=unsafe,
                source="templated",
                market="all",
            )
        assert "PARAM_UNENCODABLE" in str(caught.value)
        assert transport.calls == []

    def test_a_missing_value_is_refused_before_any_send(self) -> None:
        spec = self._spec("/quotes/{symbol}.json")
        transport = SyntheticTransport({})
        fetcher = _with_transport(make_http_json_fetcher("templated", spec), transport)
        with pytest.raises(Exception) as caught:
            fetcher.fetch(
                ctx=fixture_context("templated", spec, sends=1),
                source="templated",
                market="all",
            )
        assert "QUERY_INVALID" in str(caught.value)
        assert transport.calls == []


def test_pilot_records_no_model_without_reviewed_source_protocols() -> None:
    """Keep the fixed eleven-model count while naming the now-empty protocol gap.

    IndexSnapshots left this set in wave 37 (the official ``all_us_indices.json`` panel, captured
    unauthenticated with ``HTTP/2 200``), and FuturesCurve left it in wave 39: Cboe's own public
    settlement CSV answers ``HTTP/2 200`` with a per-expiry curve and honours ``dt=`` for the
    settlement day, captured in ``docs/evidence/C144/wave39-unauthenticated-access-20261019``. The
    refusal count fell 1 -> 0 and the hand-written count rose 6 -> 7, so the sum stays 11.
    """
    assert set(specs.NOT_DECLARABLE) == set()
    assert len(specs.NOT_DECLARABLE) == 0
    declared = {
        AVAILABLE_INDICES.model,
        INDEX_CONSTITUENTS.model,
        specs.INDEX_SEARCH.model,
        specs.EQUITY_SEARCH.model,
    }
    assert len(declared) == 4
    assert declared.isdisjoint(specs.NOT_DECLARABLE)
    handwritten = {
        "EquityHistorical",
        "EtfHistorical",
        "EquityQuote",
        "IndexHistorical",
        "IndexSnapshots",
        "FuturesCurve",
        "OptionsChains",
    }
    assert declared.isdisjoint(handwritten)
    assert handwritten.isdisjoint(specs.NOT_DECLARABLE)
    assert len(declared | handwritten | set(specs.NOT_DECLARABLE)) == 11
