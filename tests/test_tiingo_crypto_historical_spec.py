"""Witness for the declared tiingo ``CryptoHistorical`` model: census row
``OBB2-tiingo-CryptoHistorical``, whose five needs are ``nested_array_explode``,
``client_side_sort``, ``param_value_mapping``, ``date_format`` and ``relative_date_default``
(``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-tiingo.json``; upstream contract
``providers/tiingo/openbb_tiingo/models/crypto_historical.py``, cited below as
``crypto_historical.py``).

Every case drives the generated fetcher through the engine's transport seam with a written document
under the cited response shape -- a bare array of ticker objects, each carrying its own nested
``priceData`` array (:165-181) -- and a token planted in the environment. No case touches a socket
or a file, and none flips a data-rights status: the context comes from the engine's own
``fixture_context``, the same bounded synthetic grant its siblings use.

The document is the cited contract, not an observed body. It publishes two tickers: ``BTCUSD`` with
three bars deliberately written OUT of date order (2025-01-03, 2025-01-01, 2025-01-02) and
``ETHUSD`` with two (2025-01-02, 2025-01-01). Five child items over two parent objects is the grain
the explode is judged on, and the shuffled dates are what the client-side sort is judged on.

The counterfact arms and the readings each one is named for, all of them measured, not asserted
prose:
* explode -- clean arm ``len(rows) == 5`` (one row per ``priceData`` item); with
  ``row_expansion=()`` the same body resolves to ``2`` records, so the ticker object would become
  the row.
* client_side_sort -- clean arm ``rows[0].date == date(2025, 1, 1)`` and
  ``rows[4].date == date(2025, 1, 3)``; with ``order_by=None`` the published order survives, so the
  same cell reads ``date(2025, 1, 3)`` and the sequence flips.
* param_value_mapping -- ``interval="7d"`` reaches the wire as ``resampleFreq=7day``; with the
  value map stripped the wire carries the caller's own ``7d``.
* relative_date_default -- an omitted window sends ``startDate`` one year back from today; with the
  bounds stripped ``startDate`` is absent from the request altogether.
Refusal arms: a ticker object that publishes no ``priceData`` key, a bar that publishes no
``close``, and an interval outside the declared enum -- none of the three may answer with rows.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.row_expansion import (
    MISSING_POINTER,
    RowExpansionError,
)
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    fixture_context,
)
from opendata.data.providers.tiingo._source import SOURCE
from opendata.data.providers.tiingo.provider import PROVIDER
from opendata.data.providers.tiingo.specs import (
    CRYPTO_HISTORICAL,
    DECLARED_MODELS,
    NOT_DECLARABLE,
    UPSTREAM_ROWS,
)

PRICES_URL = "https://api.tiingo.com/tiingo/crypto/prices"

#: The five child items the document publishes, written out of date order on purpose: this is
#: what ``client_side_sort`` (:183) is measured against.
DOCUMENT = [
    {
        "ticker": "BTCUSD",
        "priceData": [
            {
                "date": "2025-01-03",
                "open": 97000.0,
                "high": 99000.0,
                "low": 96500.0,
                "close": 98000.0,
                "volume": 210.0,
                "volumeNotional": 20500000.0,
                "tradesDone": 480,
            },
            {
                "date": "2025-01-01",
                "open": 95000.0,
                "high": 96500.0,
                "low": 94000.0,
                "close": 96000.5,
                "volume": 180.5,
                "volumeNotional": 17400000.25,
                "tradesDone": 431,
            },
            {
                "date": "2025-01-02",
                "open": 96000.5,
                "high": 97500.0,
                "low": 95500.0,
                "close": 97000.0,
                "volume": 0,
                "volumeNotional": None,
                "tradesDone": 0,
            },
        ],
    },
    {
        "ticker": "ETHUSD",
        "priceData": [
            {
                "date": "2025-01-02",
                "open": 3400.25,
                "high": 3500.0,
                "low": 3350.0,
                "close": 3450.0,
                "volume": 5200.0,
                "volumeNotional": 17940000.0,
                "tradesDone": 900,
            },
            {
                "date": "2025-01-01",
                "open": 3300.0,
                "high": 3420.5,
                "low": 3280.0,
                "close": 3400.25,
                "volume": 4800.0,
                "volumeNotional": 16200000.5,
                "tradesDone": 812,
            },
        ],
    },
]


@pytest.fixture(autouse=True)
def _tiingo_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local-only token; the synthetic transport never reaches a socket with it."""
    monkeypatch.setenv("TIINGO_TOKEN", "test-token")


def _fetch(spec, transport, sends=1, **query):
    """Drive one declaration over one scripted transport and return ``(calls, rows)``."""
    cls = make_http_json_fetcher(SOURCE, spec)
    fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
    rows = list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=sends), **query))
    return transport.calls, rows


def _run(spec=None, document=None, **query):
    """Drive one declared model over one hand-written document with a single scripted response."""
    declaration = CRYPTO_HISTORICAL if spec is None else spec
    body = DOCUMENT if document is None else document
    transport = FixedResponseTransport(HttpResponse(200, body))
    return _fetch(declaration, transport, **query)


def _send(spec=None, **query):
    """Return the query dict of the single request one declaration put on the wire."""
    calls, _ = _run(spec, symbol=query.pop("symbol", "BTCUSD"), **query)
    assert len(calls) == 1, "ONE request for every ticker (:126-138) is part of the contract"
    return calls[0]["params"]


def _stripped(
    spec,
    *,
    parameter_name=None,
    drop_value_map=False,
    drop_order=False,
    drop_expansion=False,
):
    """Return ``spec`` with exactly one declared facet removed -- the break half of an arm."""
    if drop_expansion:
        return replace(spec, row_expansion=())
    if drop_order:
        return replace(spec, order_by=None)
    params = []
    for parameter in spec.params:
        if parameter_name is not None and parameter.name == parameter_name:
            params.append(replace(parameter, relative_default=None))
        elif drop_value_map:
            params.append(replace(parameter, value_map=()))
        else:
            params.append(parameter)
    return replace(spec, params=tuple(params))


class TestCensusRowIsDeclared:
    """The row the census put in shipped_cover now has a package to carry the declaration."""

    def test_the_model_is_declared_and_no_longer_refused(self) -> None:
        assert CRYPTO_HISTORICAL.model == "CryptoHistorical"
        assert "CryptoHistorical" in DECLARED_MODELS
        assert "CryptoHistorical" not in NOT_DECLARABLE
        assert not set(DECLARED_MODELS) & set(NOT_DECLARABLE), "declared *and* refused"
        assert set(UPSTREAM_ROWS) == set(DECLARED_MODELS) | set(NOT_DECLARABLE), "a silent drop"
        assert len(DECLARED_MODELS) == 7, "all seven Tiingo census rows have provider bindings"

    def test_the_one_binding_is_the_one_declaration(self) -> None:
        """``DECLARED_MODELS`` stays derived: the registry ids are the records' own model names."""
        bound = {
            model_id
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert bound == set(DECLARED_MODELS) == set(UPSTREAM_ROWS)
        assert PROVIDER.source == SOURCE == "tiingo"

    def test_the_five_census_needs_are_all_carried_by_fields_that_exist(self) -> None:
        expansion = CRYPTO_HISTORICAL.row_expansion[0]
        assert CRYPTO_HISTORICAL.path == "/tiingo/crypto/prices"
        assert CRYPTO_HISTORICAL.rows_pointer == "", "the document itself is the ticker array"
        assert expansion.child_array_pointer == "priceData"
        assert {mapping.column for mapping in expansion.parent_fields} == {"ticker"}
        assert CRYPTO_HISTORICAL.order_by.column == "date"
        assert CRYPTO_HISTORICAL.order_by.direction == "asc"
        assert CRYPTO_HISTORICAL.order_by.limit is None, ":183 sorts with no cap"
        aliases = {
            column.name: column.source_key
            for column in CRYPTO_HISTORICAL.columns
            if column.source_key
        }
        assert aliases["transactions"] == "tradesDone"
        assert aliases["volume_notional"] == "volumeNotional"
        assert aliases["symbol"] == "ticker"
        bounds = {
            parameter.name: parameter.relative_default for parameter in CRYPTO_HISTORICAL.params
        }
        assert bounds["start_date"].years == -1 and bounds["start_date"].days == 0
        assert bounds["end_date"].years == 0 and bounds["end_date"].months == 0
        assert CRYPTO_HISTORICAL.pagination.kind == "none", "no paging loop on this endpoint"


class TestNestedPriceDataExplode:
    """POSITIVE: one row per ``priceData`` item, the ticker of its parent object copied onto it."""

    def test_two_tickers_of_three_and_two_bars_answer_five_rows_in_one_request(self) -> None:
        calls, rows = _run(symbol="BTCUSD,ETHUSD")
        assert len(calls) == 1
        assert calls[0]["url"] == PRICES_URL
        assert calls[0]["params"]["tickers"] == "BTCUSD,ETHUSD"
        assert calls[0]["params"]["token"] == "test-token"
        assert len(rows) == 5

    def test_the_first_sorted_row_carries_its_number_and_its_date(self) -> None:
        """The row-level reading: BTCUSD's 2025-01-01 bar, close and tradesDone and date face."""
        _, rows = _run(symbol="BTCUSD,ETHUSD")
        assert rows[0].symbol == "BTCUSD"
        assert rows[0].date == date(2025, 1, 1)
        assert rows[0].close == 96000.5
        assert rows[0].low == 94000.0
        assert rows[0].transactions == 431
        assert rows[0].volume_notional == 17400000.25

    def test_the_date_face_is_ascending_over_both_tickers(self) -> None:
        """client_side_sort face, read as the sequence of five published dates."""
        _, rows = _run(symbol="BTCUSD,ETHUSD")
        assert [row.date for row in rows] == [
            date(2025, 1, 1),
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 1, 2),
            date(2025, 1, 3),
        ]
        assert [row.symbol for row in rows] == ["BTCUSD", "ETHUSD", "BTCUSD", "ETHUSD", "BTCUSD"]

    def test_a_published_zero_and_none_stay_themselves(self) -> None:
        """A ``volume: 0`` bar is a value, and the ``volumeNotional: null`` bar reads as None."""
        _, rows = _run(symbol="BTCUSD,ETHUSD")
        zero_volume = rows[2]
        assert zero_volume.date == date(2025, 1, 2)
        assert zero_volume.volume == 0.0
        assert zero_volume.transactions == 0
        assert zero_volume.volume_notional is None

    def test_the_row_model_publishes_the_contract_names_not_the_record_names(self) -> None:
        dumped = _run(symbol="BTCUSD")[1][0].model_dump()
        assert set(dumped) == {column.name for column in CRYPTO_HISTORICAL.columns}
        for retired in ("priceData", "ticker", "tradesDone", "volumeNotional"):
            assert retired not in dumped
        assert dumped["transactions"] == 431

    def test_the_explode_is_what_sets_the_row_grain(self) -> None:
        """COUNTERFACT: with no expansion stage the same body reads one row per TICKER."""
        exploded = http_json.resolve_rows(
            DOCUMENT, CRYPTO_HISTORICAL.rows_pointer, CRYPTO_HISTORICAL
        )
        assert len(exploded) == 5
        unexploded = _stripped(CRYPTO_HISTORICAL, drop_expansion=True)
        published = http_json.resolve_rows(DOCUMENT, unexploded.rows_pointer, unexploded)
        assert len(published) == 2, "the ticker object, not the bar, became the row"

    def test_an_order_declared_without_the_parent_copy_cannot_build_a_row(self) -> None:
        """COUNTERFACT: strip the parent copy and the required ``symbol`` column has no source."""
        stage = CRYPTO_HISTORICAL.row_expansion[0]
        orphaned = replace(
            CRYPTO_HISTORICAL,
            row_expansion=(replace(stage, parent_fields=()),),
        )
        with pytest.raises(ProviderEngineError) as caught:
            _run(orphaned, symbol="BTCUSD")
        assert str(caught.value.code) == "TIINGO_SHAPE_INVALID"
        assert "symbol" in caught.value.missing


class TestClientSideSort:
    """The sequence itself is the answer, so the sort arm is read on the first and last cell."""

    def test_the_clean_arm_reads_the_earliest_bar_first(self) -> None:
        _, rows = _run(symbol="BTCUSD,ETHUSD")
        assert rows[0].date == date(2025, 1, 1)
        assert rows[0].close == 96000.5
        assert rows[4].date == date(2025, 1, 3)
        assert rows[4].close == 98000.0

    def test_without_the_declared_order_the_published_sequence_survives(self) -> None:
        """COUNTERFACT: ``order_by=None`` answers 2025-01-03 first, not 2025-01-01."""
        unordered = _stripped(CRYPTO_HISTORICAL, drop_order=True)
        _, rows = _run(unordered, symbol="BTCUSD,ETHUSD")
        assert rows[0].date == date(2025, 1, 3)
        assert rows[0].close == 98000.0
        assert [row.date for row in rows] != [
            date(2025, 1, 1),
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 1, 2),
            date(2025, 1, 3),
        ]


class TestParamValueMappingAndWindow:
    """``interval`` -> ``resampleFreq`` (:128-135) and the one-year relative window (:103-108)."""

    def test_the_interval_is_remapped_to_the_endpoint_spelling(self) -> None:
        sent = _send(interval="7d")
        assert sent["resampleFreq"] == "7day"
        assert "interval" not in sent

    def test_the_minute_hour_and_default_arms_remap_too(self) -> None:
        assert _send(interval="90m")["resampleFreq"] == "90min"
        assert _send(interval="2h")["resampleFreq"] == "2hour"
        assert _send()["resampleFreq"] == "1day", "1d is upstream's own default (:51-53)"

    def test_an_unmapped_interval_spelling_would_reach_the_wire_unmapped(self) -> None:
        """COUNTERFACT: without ``value_map`` the caller's ``7d`` is what resampleFreq carries."""
        unmapped = _stripped(CRYPTO_HISTORICAL, drop_value_map=True)
        assert _send(unmapped, interval="7d")["resampleFreq"] == "7d"

    def test_an_omitted_window_sends_one_year_back_and_today(self) -> None:
        today = date.today()
        sent = _send()
        start = date.fromisoformat(sent["startDate"])
        assert (start.year, start.month, start.day) == (today.year - 1, today.month, today.day)
        assert sent["endDate"] == today.isoformat()

    def test_a_named_start_date_beats_the_relative_bound(self) -> None:
        assert _send(start_date="2024-01-02")["startDate"] == "2024-01-02"

    def test_an_unbounded_declaration_sends_no_window_at_all(self) -> None:
        """COUNTERFACT: drop the relative bound and the request answers wider than upstream."""
        unbounded = _stripped(CRYPTO_HISTORICAL, parameter_name="start_date")
        assert "startDate" not in _send(unbounded)

    def test_the_exchanges_list_reaches_the_request_as_published(self) -> None:
        sent = _send(exchanges=["COINBASE", "KRAKEN"])
        assert "COINBASE" in sent["exchanges"]
        assert "KRAKEN" in sent["exchanges"]


class TestRefusals:
    """REFUSAL arms: input this declaration does not claim may not answer with rows."""

    def test_a_ticker_object_without_a_price_data_array_is_a_shape_failure(self) -> None:
        document = [{"ticker": "BTCUSD", "metaData": {"name": "BTC"}}]
        with pytest.raises(RowExpansionError) as caught:
            _run(document=document, symbol="BTCUSD")
        assert caught.value.reason == MISSING_POINTER
        assert caught.value.pointer == "priceData"

    def test_a_bar_that_publishes_no_close_is_refused_not_nulled(self) -> None:
        document = [{"ticker": "BTCUSD", "priceData": [{"date": "2025-01-01", "open": 1.0}]}]
        with pytest.raises(RowExpansionError) as caught:
            _run(document=document, symbol="BTCUSD")
        assert caught.value.reason == MISSING_POINTER
        assert caught.value.pointer == "close"

    def test_an_interval_outside_the_declared_enum_never_reaches_the_wire(self) -> None:
        """Upstream's :135 fallback arm would silently send ``1day``; this declaration refuses."""
        transport = FixedResponseTransport(HttpResponse(200, DOCUMENT))
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(CRYPTO_HISTORICAL, transport, symbol="BTCUSD", interval="1w")
        assert str(caught.value.code).startswith("TIINGO_")
        assert transport.calls == [], "a refused parameter must not go out under another spelling"
