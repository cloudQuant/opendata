"""The declared ``EquitySearch`` model: one CSV download, decoded offline by the engine itself.

Two witnesses for the row that used to be a refusal. The positive side renders the URL from the
declaration and hands the engine's delimited decoder a CSV body through the transport seam; the
refusal side is the row filter dropping the row it names and a body that is not a table at all.
No case touches a socket or a file.
"""

from __future__ import annotations

import pytest

from opendata.data.providers._engine.decoders import (
    DELIMITER_MISMATCH,
    EMPTY_BODY,
    HEADER_MISMATCH,
)
from opendata.data.providers._engine.http_json import HttpResponse, make_http_json_fetcher
from opendata.data.providers._engine.testing import fixture_context
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import EQUITY_SEARCH

DIRECTORY_URL = "https://www.cboe.com/us/options/symboldir/equity_index_options/"

#: The header upstream renames, leading spaces and all, then two listings.
CSV_BODY = (
    " Stock Symbol,Company Name, DPM Name, Post/Station\n"
    "AAPL,Apple Inc.,MEMX,1234\n"
    "MSFT,Microsoft Corp.,NSDT,5678\n"
)


class RecordingTransport:
    """The engine's only I/O seam, answering with a recorded body."""

    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs) -> HttpResponse:
        url = kwargs.get("url", args[0] if args else None)
        params = kwargs.get("params", args[1] if len(args) > 1 else {})
        self.calls.append({"url": url, "params": dict(params or {})})
        return self.response


def _fetcher(transport: RecordingTransport):
    cls = make_http_json_fetcher(SOURCE, EQUITY_SEARCH)
    return type(cls.__name__, (cls,), {"http_transport": transport})()


def _fetch(transport: RecordingTransport, **query):
    return _fetcher(transport).fetch(ctx=fixture_context(SOURCE, EQUITY_SEARCH, sends=1), **query)


class TestCsvBodyDecodes:
    """POSITIVE: the declaration renders the URL and the CSV body becomes rows."""

    def _rows(self, **query):
        transport = RecordingTransport(HttpResponse(200, None, CSV_BODY))
        rows = list(_fetch(transport, **query))
        return transport, rows

    def test_the_url_is_rendered_from_the_declaration(self) -> None:
        transport, rows = self._rows(query="AAPL", is_symbol=True)
        assert transport.calls[0]["url"] == DIRECTORY_URL
        assert len(rows) == 1

    def test_the_payload_format_constant_is_sent(self) -> None:
        transport, _ = self._rows(query="AAPL", is_symbol=True)
        assert transport.calls[0]["params"]["download"] == "csv"

    def test_the_csv_body_decodes_into_the_published_columns(self) -> None:
        transport, rows = self._rows(query="AAPL", is_symbol=True)
        assert [column.name for column in EQUITY_SEARCH.columns] == [
            "symbol",
            "name",
            "dpm_name",
            "post_station",
        ]
        assert set(rows[0].model_dump()) == {"symbol", "name", "dpm_name", "post_station"}

    def test_a_delimited_row_becomes_the_named_cells(self) -> None:
        _, rows = self._rows(query="MSFT", is_symbol=True)
        assert rows[0].symbol == "MSFT"
        assert rows[0].name == "Microsoft Corp."
        assert rows[0].dpm_name == "NSDT"
        assert rows[0].post_station == "5678"


class TestRefusals:
    """REFUSAL arms: a filter that drops, a needle it cannot answer, bodies that are no table."""

    def test_a_caller_cannot_overwrite_the_payload_format_constant(self) -> None:
        transport = RecordingTransport(HttpResponse(200, None, CSV_BODY))
        with pytest.raises(Exception) as caught:
            list(_fetch(transport, query="AAPL", is_symbol=True, download="json"))
        assert "CBOE_QUERY_INVALID" in str(caught.value)
        assert transport.calls == []

    def test_the_row_filter_drops_the_row_it_names(self) -> None:
        transport = RecordingTransport(HttpResponse(200, None, CSV_BODY))
        rows = list(_fetch(transport, query="apple"))
        assert transport.calls[0]["params"]["download"] == "csv"
        assert [row.symbol for row in rows] == ["AAPL"]

    def test_the_gated_leg_searches_symbols_not_names(self) -> None:
        transport = RecordingTransport(HttpResponse(200, None, CSV_BODY))
        rows = list(_fetch(transport, query="apple", is_symbol=True))
        assert rows == []

    def test_a_body_with_no_delimited_header_refuses(self) -> None:
        transport = RecordingTransport(
            HttpResponse(200, None, "<html><body>not a table</body></html>\n")
        )
        with pytest.raises(Exception) as caught:
            list(_fetch(transport, query="AAPL", is_symbol=True))
        assert DELIMITER_MISMATCH in str(caught.value)
        assert transport.calls[0]["params"]["download"] == "csv"

    def test_a_table_publishing_none_of_the_declared_columns_refuses(self) -> None:
        transport = RecordingTransport(HttpResponse(200, None, "date,value\n2026-01-01,1\n"))
        with pytest.raises(Exception) as caught:
            list(_fetch(transport, query="AAPL", is_symbol=True))
        assert HEADER_MISMATCH in str(caught.value)

    def test_a_blank_body_refuses_before_any_row_is_made(self) -> None:
        transport = RecordingTransport(HttpResponse(200, None, ""))
        with pytest.raises(Exception) as caught:
            list(_fetch(transport, query="AAPL", is_symbol=True))
        assert EMPTY_BODY in str(caught.value)
