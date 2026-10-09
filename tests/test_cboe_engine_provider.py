"""The cboe pilot: one real provider driven entirely by declarations, verified offline.

This is the end-to-end proof of the declarative engine: the fetchers are generated from
``ModelSpec`` data, reach the registry through the ordinary provider descriptor, and are judged
without a socket. It also fixes the two behaviours the pilot exposed as necessary and were not in
the first engine draft -- path templating from a validated parameter, and a transport seam on the
generated fetcher.
"""

from __future__ import annotations

import pytest

from opendata.data.providers._engine.http_json import HttpResponse, make_http_json_fetcher
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.testing import SyntheticTransport, fixture_context
from opendata.data.providers.catalog import get_provider, register_provider
from opendata.data.providers.cboe import specs
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.models.available_indices import (
    CboeAvailableIndicesFetcher,
)
from opendata.data.providers.cboe.models.index_constituents import (
    CboeIndexConstituentsFetcher,
)
from opendata.data.providers.cboe.specs import AVAILABLE_INDICES, INDEX_CONSTITUENTS
from opendata.data.registry import ProviderRegistry

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
        assert provider.credentials == ()
        assert len(provider.fetcher_bindings) == 3

    def test_registry_publishes_every_declared_model(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("cboe", registry)
        domains = {c.domain for c in capabilities}
        assert domains == {
            "cboe_available_indices",
            "cboe_index_constituent_quotes",
            "cboe_index_search",
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
        result = fetcher.fetch(ctx=fixture_context(SOURCE, AVAILABLE_INDICES, sends=1))
        rows = list(result)
        assert len(rows) == 1
        assert rows[0].symbol == "SPX"
        assert rows[0].name == "S&P 500"
        assert rows[0].data_delay == "15 min"
        assert set(rows[0].model_dump()) == {column.name for column in AVAILABLE_INDICES.columns}
        assert transport.calls[0]["url"] == DIRECTORY_URL
        assert transport.calls[0]["params"] == {}


class TestTemplatedPathModel:
    """A model that addresses its resource in the URL rather than the query string."""

    def _transport(self) -> SyntheticTransport:
        return SyntheticTransport(
            {(CONSTITUENTS_URL, frozenset()): HttpResponse(200, CONSTITUENTS_DOCUMENT)}
        )

    def _fetch(self, transport: SyntheticTransport, symbol: str = "BUK100P"):
        fetcher = _with_transport(CboeIndexConstituentsFetcher, transport)
        return fetcher.fetch(
            ctx=fixture_context(SOURCE, INDEX_CONSTITUENTS, sends=1), symbol=symbol
        )

    def test_symbol_is_addressed_in_the_path_not_the_query(self) -> None:
        transport = self._transport()
        rows = list(self._fetch(transport))
        assert transport.calls[0]["url"] == CONSTITUENTS_URL
        assert transport.calls[0]["params"] == {}
        assert rows[0].last_price == 148.2
        assert rows[0].change_percent == 47.5
        assert rows[0].asset_type == "Equity"

    def test_envelope_facts_do_not_overwrite_the_record_symbol(self) -> None:
        transport = self._transport()
        rows = list(self._fetch(transport))
        assert rows[0].symbol == "AZL.L"

    def test_envelope_fills_a_column_the_record_does_not_publish(self) -> None:
        transport = self._transport()
        rows = list(self._fetch(transport))
        assert "currency" not in CONSTITUENT_RECORD
        assert rows[0].currency == "GBP"


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
            )
        assert "PARAM_UNENCODABLE" in str(caught.value)
        assert transport.calls == []

    def test_a_missing_value_is_refused_before_any_send(self) -> None:
        spec = self._spec("/quotes/{symbol}.json")
        transport = SyntheticTransport({})
        fetcher = _with_transport(make_http_json_fetcher("templated", spec), transport)
        with pytest.raises(Exception) as caught:
            fetcher.fetch(ctx=fixture_context("templated", spec, sends=1))
        assert "QUERY_INVALID" in str(caught.value)
        assert transport.calls == []


def test_pilot_records_the_non_declarable_remainder_by_name() -> None:
    """The honest denominator: eight of eleven still need composition a declaration cannot express.

    ``IndexSearch`` left this set when the engine grew ``row_filters``. Every cboe upstream model
    is named exactly once across the declared three and the remaining eight, so a model cannot
    leave the remainder without arriving in the declared column -- the two sets are checked
    against each other, not just against their own literals.
    """
    assert set(specs.NOT_DECLARABLE) == {
        "EquityHistorical",
        "EtfHistorical",
        "EquityQuote",
        "EquitySearch",
        "FuturesCurve",
        "IndexHistorical",
        "IndexSnapshots",
        "OptionsChains",
    }
    assert len(specs.NOT_DECLARABLE) == 8
    declared = {AVAILABLE_INDICES.model, INDEX_CONSTITUENTS.model, specs.INDEX_SEARCH.model}
    assert len(declared) == 3
    assert declared.isdisjoint(specs.NOT_DECLARABLE)
    assert len(declared | set(specs.NOT_DECLARABLE)) == 11
