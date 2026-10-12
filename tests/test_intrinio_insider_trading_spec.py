"""C98's witness for the declared intrinio ``InsiderTrading`` model: census row
``OBB2-intrinio-InsiderTrading``, whose four needs are ``nested_array_explode``,
``sentinel_value_nulling``, ``field_rename_map`` and ``relative_date_default``.

Every case drives the generated fetcher through the engine's transport seam with a written
document under the cited row key (``transaction_filings``) and an API key planted in the
environment.
No case touches a socket or a file, and none flips a data-rights status: the context comes from the
engine's own ``fixture_context``, the same bounded synthetic grant its siblings use.

The document is the cited contract, not an observed body -- no body of this endpoint has ever been
captured in this repo, exactly as for ``HISTORICAL_DIVIDENDS``. It carries two filings: the first
publishes two transactions (one with real values, including a ``0`` share count and three ``False``
relation flags, and one whose every transaction key is the empty string), and the second publishes
one transaction whose keys are all the empty string too. That is the shape upstream's
``model_validator`` (insider_trading.py:109-117) was written for: the spellings become ``None``
while a published ``0`` or ``False`` stays the value it is.

Each facet gets a counterfact arm that would fail if the facet were removed from the declaration:
the explode (``row_expansion=()`` answers two rows, not three), the parent copy (a plan with no
``parent_fields`` refuses the row for want of ``symbol``), the sentinel (the same body read without
sentinels is a shape failure, not a null), the relative bound (an unbound ``start_date`` sends no
window at all) and paging (the same two-page script read by a paging variant asks twice).
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
    NestedArrayExpansion,
    RowExpansionError,
)
from opendata.data.providers._engine.spec import PaginationSpec
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    SequencedResponseTransport,
    fixture_context,
)
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.provider import PROVIDER
from opendata.data.providers.intrinio.specs import (
    DECLARED_MODELS,
    INSIDER_TRADING,
    NOT_DECLARABLE,
    UPSTREAM_ROWS,
)

FILINGS_URL = "https://api-v2.intrinio.com/companies/AAPL/insider_transaction_filings"

#: The transaction-level keys of one filing: every one of them is optional upstream, and the
#: sentinel case reads them all back as null.
TRANSACTION_KEYS = (
    "transaction_date",
    "ownership_type",
    "officer_title",
    "transaction_type_code",
    "acquisition_disposition_code",
    "security_title",
    "total_shares_owned",
    "amount_of_shares",
    "transaction_price",
    "conversion_exercise_price",
    "deemed_execution_date",
    "exercise_date",
    "expiration_date",
    "underlying_security_title",
    "underlying_shares",
    "nature_of_ownership",
    "director",
    "officer",
    "ten_percent_owner",
    "other_relation",
    "derivative_transaction",
    "report_line_number",
)

#: One transaction whose every published key is the empty string -- the shape upstream's
#: before-validator exists to null (insider_trading.py:109-117).
EMPTY_TRANSACTION = dict.fromkeys(TRANSACTION_KEYS, "")

#: The first filing's real transaction: a ``0`` share count and three ``False`` flags are values,
#: not spellings, and must survive the sentinel rewrite untouched.
REAL_TRANSACTION = {
    "transaction_date": "2024-02-27",
    "ownership_type": "D",
    "officer_title": "Chief Executive Officer",
    "transaction_type_code": "P - Open Market",
    "acquisition_disposition_code": "A",
    "security_title": "Common Stock",
    "total_shares_owned": 999804.0,
    "amount_of_shares": 0,
    "transaction_price": 182.33,
    "conversion_exercise_price": None,
    "nature_of_ownership": "DT:Deemed",
    "director": False,
    "officer": True,
    "ten_percent_owner": False,
    "other_relation": False,
    "derivative_transaction": False,
    "report_line_number": 7,
}

#: Two filings, the first exploding into two rows and the second into one. The ``owner`` object is
#: where the filing publishes the insider's own CIK and name (:174-175).
DOCUMENT = {
    "transaction_filings": [
        {
            "filing_date": "2024-03-01",
            "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/x.htm",
            "issuer_ticker": "AAPL",
            "issuer_cik": "0000320193",
            "issuer_company": "Apple Inc.",
            "owner": {"owner_cik": "0001114892", "owner_name": "Cook Timothy D"},
            "transactions": [REAL_TRANSACTION, dict(EMPTY_TRANSACTION)],
        },
        {
            "filing_date": "2023-08-01",
            "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/y.htm",
            "issuer_ticker": "AAPL",
            "issuer_cik": "0000320193",
            "issuer_company": "Apple Inc.",
            "owner": {"owner_cik": "0001510762", "owner_name": "Adams Katherine L"},
            "transactions": [dict(EMPTY_TRANSACTION)],
        },
    ]
}

#: One filing carrying one real transaction, so a paging counterfact has a short second page to
#: stop on.
SECOND_TRANSACTION_PAGE = {
    "transaction_filings": [
        {
            "filing_date": "2022-05-04",
            "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/z.htm",
            "issuer_ticker": "AAPL",
            "issuer_cik": "0000320193",
            "issuer_company": "Apple Inc.",
            "owner": {"owner_cik": "0000890100", "owner_name": "Wagner Janne"},
            "transactions": [dict(REAL_TRANSACTION, transaction_date="2022-05-02")],
        }
    ]
}


class _Missing:
    """Marker for a fixture that must leave a key out rather than publish a null."""


MISSING = _Missing()


def replace_document(*, transactions_key):
    """Return the two-filing document with the second filing's child array rewritten."""
    filings = [dict(DOCUMENT["transaction_filings"][0])]
    second = {
        key: value
        for key, value in DOCUMENT["transaction_filings"][1].items()
        if key != "transactions"
    }
    if transactions_key is not MISSING:
        second["transactions"] = transactions_key
    filings.append(second)
    return {"transaction_filings": filings}


@pytest.fixture(autouse=True)
def _intrinio_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local-only key; the synthetic transport never reaches a socket with it."""
    monkeypatch.setenv("INTRINIO_API_KEY", "test-key")


def _fetch(spec, transport, sends=1, **query):
    """Drive one declaration over one scripted transport and return ``(calls, rows)``."""
    cls = make_http_json_fetcher(SOURCE, spec)
    fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
    rows = list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=sends), **query))
    return transport.calls, rows


def _run(spec=None, document=None, **query):
    """Drive one declared model over one hand-written document with a single scripted response."""
    declaration = INSIDER_TRADING if spec is None else spec
    body = DOCUMENT if document is None else document
    transport = FixedResponseTransport(HttpResponse(200, body))
    return _fetch(declaration, transport, **query)


def _without_sentinels(spec):
    """Return ``spec`` with every declared null sentinel removed from its columns."""
    return replace(
        spec,
        columns=tuple(
            replace(column, null_sentinel=()) if column.null_sentinel else column
            for column in spec.columns
        ),
    )


class TestCensusRowIsDeclared:
    """The refusal this ledger carried for one round is now a declaration, not a rename."""

    def test_the_model_is_declared_and_no_longer_refused(self) -> None:
        assert INSIDER_TRADING.model == "InsiderTrading"
        assert "InsiderTrading" in DECLARED_MODELS
        assert "InsiderTrading" not in NOT_DECLARABLE
        assert not set(DECLARED_MODELS) & set(NOT_DECLARABLE), "declared *and* refused"
        assert set(UPSTREAM_ROWS) == set(DECLARED_MODELS) | set(NOT_DECLARABLE), "a silent drop"

    def test_the_five_bindings_are_the_five_declarations(self) -> None:
        """``DECLARED_MODELS`` stays derived: the registry ids are the records' own model names."""
        bound = {
            model_id
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert (
            bound
            == set(DECLARED_MODELS)
            == {
                "CalendarIpo",
                "CurrencyPairs",
                "EquitySearch",
                "HistoricalDividends",
                "InsiderTrading",
            }
        )

    def test_the_four_census_needs_are_all_carried_by_fields_that_exist(self) -> None:
        expansion = INSIDER_TRADING.row_expansion[0]
        assert isinstance(expansion, NestedArrayExpansion)
        assert expansion.child_array_pointer == "transactions"
        assert {mapping.column for mapping in expansion.parent_fields} == {
            "issuer_ticker",
            "issuer_cik",
            "issuer_company",
            "filing_date",
            "filing_url",
            "owner_cik",
            "owner_name",
        }
        renamed = {
            column.name: column.source_key
            for column in INSIDER_TRADING.columns
            if column.source_key
        }
        assert renamed["owner_title"] == "officer_title"
        assert renamed["securities_transacted"] == "amount_of_shares"
        assert sum(bool(c.null_sentinel) for c in INSIDER_TRADING.columns) == 28
        bounds = {
            parameter.name: parameter.relative_default for parameter in INSIDER_TRADING.params
        }
        assert bounds["start_date"].years == -5
        assert bounds["end_date"].years == 0 and bounds["end_date"].months == 0
        assert INSIDER_TRADING.pagination.kind == "none"


class TestNestedArrayExplode:
    """POSITIVE: one row per transaction, the filing's own scalars copied onto each of them."""

    def test_two_filings_of_two_and_one_transactions_answer_three_rows(self) -> None:
        calls, rows = _run(symbol="AAPL")
        assert len(calls) == 1
        assert calls[0]["url"] == FILINGS_URL
        assert len(rows) == 3
        assert [row.company_name for row in rows] == ["Apple Inc."] * 3

    def test_every_sub_row_carries_its_own_filing_level_fields(self) -> None:
        _, rows = _run(symbol="AAPL")
        assert [row.owner_name for row in rows] == [
            "Cook Timothy D",
            "Cook Timothy D",
            "Adams Katherine L",
        ]
        assert [row.filing_date for row in rows] == [
            date(2024, 3, 1),
            date(2024, 3, 1),
            date(2023, 8, 1),
        ]
        assert rows[0].symbol == "AAPL"
        assert rows[2].owner_cik == "0001510762"
        assert rows[2].transaction_date is None

    def test_the_row_model_publishes_the_contract_names_not_the_record_names(self) -> None:
        """``field_rename_map``: the six aliased keys and the three filing renames are gone."""
        _, rows = _run(symbol="AAPL")
        dumped = rows[0].model_dump()
        assert set(dumped) == {column.name for column in INSIDER_TRADING.columns}
        for retired in (
            "officer_title",
            "transaction_type_code",
            "acquisition_disposition_code",
            "security_title",
            "total_shares_owned",
            "amount_of_shares",
            "issuer_ticker",
            "issuer_cik",
            "issuer_company",
            "transactions",
        ):
            assert retired not in dumped
        assert dumped["owner_title"] == "Chief Executive Officer"
        assert dumped["transaction_type"] == "P - Open Market"
        assert dumped["security_type"] == "Common Stock"
        assert dumped["securities_owned"] == 999804.0

    def test_the_explode_is_what_sets_the_row_grain(self) -> None:
        """COUNTERFACT: with no expansion stage the same body reads as one record per filing."""
        flat = replace(INSIDER_TRADING, row_expansion=())
        published = http_json.resolve_rows(DOCUMENT, flat.rows_pointer, flat)
        assert len(published) == 2, "the filing, not the transaction, became the row"
        exploded = http_json.resolve_rows(DOCUMENT, INSIDER_TRADING.rows_pointer, INSIDER_TRADING)
        assert len(exploded) == 3

    def test_parent_fields_are_what_copy_the_filing_onto_each_child(self) -> None:
        """COUNTERFACT: a plan that carries no parent fields cannot even build a row."""
        stage = INSIDER_TRADING.row_expansion[0]
        stripped = replace(
            INSIDER_TRADING,
            row_expansion=(replace(stage, parent_fields=()),),
        )
        with pytest.raises(ProviderEngineError) as caught:
            _run(stripped, symbol="AAPL")
        assert str(caught.value.code) == "INTRINIO_SHAPE_INVALID"
        assert "symbol" in caught.value.missing

    def test_a_filing_whose_child_array_is_empty_contributes_no_rows(self) -> None:
        """Absent and empty stay two different answers, as the kernel documents."""
        _, rows = _run(document=replace_document(transactions_key=[]), symbol="AAPL")
        assert len(rows) == 2

    def test_a_filing_that_publishes_no_transactions_array_is_a_shape_failure(self) -> None:
        document = replace_document(transactions_key=MISSING)
        with pytest.raises(RowExpansionError) as caught:
            _run(document=document, symbol="AAPL")
        assert caught.value.reason == MISSING_POINTER
        assert caught.value.pointer == "transactions"


class TestNullSentinel:
    """POSITIVE: every ``''`` of the assembled row is null; a 0 and a False are not."""

    def test_an_all_empty_transaction_reads_as_nulls_not_as_empty_text(self) -> None:
        _, rows = _run(symbol="AAPL")
        empty = rows[2].model_dump()
        for name in (
            "transaction_date",
            "ownership_type",
            "owner_title",
            "transaction_type",
            "acquisition_or_disposition",
            "security_type",
            "securities_owned",
            "securities_transacted",
            "transaction_price",
            "nature_of_ownership",
            "director",
            "officer",
            "derivative_transaction",
            "report_line_number",
        ):
            assert empty[name] is None, name
        assert empty["company_name"] == "Apple Inc."
        assert empty["filing_url"].startswith("https://www.sec.gov/")

    def test_the_empty_transaction_in_the_first_filing_reads_the_same_way(self) -> None:
        _, rows = _run(symbol="AAPL")
        assert rows[1].model_dump() == {
            **rows[0].model_dump(
                include={"symbol", "company_cik", "company_name", "filing_date", "filing_url"}
            ),
            "owner_cik": rows[0].owner_cik,
            "owner_name": rows[0].owner_name,
            **{column.name: None for column in INSIDER_TRADING.columns if not column.required},
        }

    def test_a_published_zero_and_a_published_false_stay_themselves(self) -> None:
        """The sentinel is text-only: 0 shares and a False flag are values, not spellings."""
        _, rows = _run(symbol="AAPL")
        assert rows[0].securities_transacted == 0.0
        assert rows[0].securities_transacted is not None
        assert rows[0].director is False
        assert rows[0].other_relation is False
        assert rows[0].derivative_transaction is False
        assert rows[0].officer is True

    def test_a_sentinel_that_spelled_zero_could_not_swallow_the_zero(self) -> None:
        """COUNTERFACT on the match rule: text is compared to text, never to a published number."""
        widened = replace(
            INSIDER_TRADING,
            columns=tuple(
                replace(column, null_sentinel=("", "0"))
                if column.name == "securities_transacted"
                else column
                for column in INSIDER_TRADING.columns
            ),
        )
        _, rows = _run(widened, symbol="AAPL")
        assert rows[0].securities_transacted == 0.0

    def test_the_same_body_without_the_sentinels_is_a_refusal_not_a_null(self) -> None:
        """COUNTERFACT: remove the facet and the '' reaches the cast, which cannot read it."""
        with pytest.raises(ProviderEngineError) as caught:
            _run(_without_sentinels(INSIDER_TRADING), symbol="AAPL")
        assert str(caught.value.code) == "INTRINIO_SHAPE_INVALID"


class TestRelativeWindow:
    """POSITIVE: an unnamed bound is filled from the request date, five years back."""

    def test_no_caller_bound_sends_the_five_year_window_upstream_sends(self) -> None:
        calls, _ = _run(symbol="AAPL")
        today = date.today()
        sent = calls[0]["params"]
        start = date.fromisoformat(sent["start_date"])
        assert (start.year, start.month, start.day) == (today.year - 5, today.month, today.day)
        assert sent["end_date"] == today.isoformat()

    def test_a_named_start_date_beats_the_relative_bound(self) -> None:
        calls, _ = _run(symbol="AAPL", start_date="2019-01-02")
        assert calls[0]["params"]["start_date"] == "2019-01-02"

    def test_an_unbounded_declaration_sends_no_window_at_all(self) -> None:
        """COUNTERFACT: without ``RelativeDefault`` the request answers wider than upstream."""
        unbounded = replace(
            INSIDER_TRADING,
            params=tuple(
                replace(parameter, relative_default=None)
                if parameter.relative_default is not None
                else parameter
                for parameter in INSIDER_TRADING.params
            ),
        )
        calls, _ = _run(unbounded, symbol="AAPL")
        assert "start_date" not in calls[0]["params"]
        assert "end_date" not in calls[0]["params"]


class TestNoPagingLoop:
    """POSITIVE: ``limit`` is aliased to page_size and caps one page; nothing loops."""

    def test_limit_is_aliased_to_page_size_with_upstreams_default(self) -> None:
        calls, _ = _run(symbol="AAPL")
        sent = calls[0]["params"]
        assert sent["page_size"] == "500"
        assert "limit" not in sent
        assert "symbol" not in sent, "the path consumed it (:152 excludes it from the query)"
        assert sent["sort_by"] == "updated_on"
        assert sent["api_key"] == "test-key"

    def test_a_second_page_is_never_asked_for(self) -> None:
        transport = SequencedResponseTransport(
            HttpResponse(200, DOCUMENT), HttpResponse(200, SECOND_TRANSACTION_PAGE)
        )
        calls, rows = _fetch(INSIDER_TRADING, transport, sends=4, symbol="AAPL")
        assert len(calls) == 1, "page_size caps a page, it does not drive a loop"
        assert len(rows) == 3

    def test_a_paging_variant_over_the_same_script_asks_twice(self) -> None:
        """COUNTERFACT: the loop the model does not declare is a separate facet."""
        paging = replace(
            INSIDER_TRADING,
            pagination=PaginationSpec(kind="page", limit_key="page_size", offset_key="page"),
            params=tuple(
                replace(parameter, default=2) if parameter.name == "limit" else parameter
                for parameter in INSIDER_TRADING.params
            ),
        )
        transport = SequencedResponseTransport(
            HttpResponse(200, DOCUMENT), HttpResponse(200, SECOND_TRANSACTION_PAGE)
        )
        calls, rows = _fetch(paging, transport, sends=4, symbol="AAPL")
        assert len(calls) == 2
        assert len(rows) == 4


class TestReachableRefusals:
    """REFUSAL arms that keep the declaration honest about the shape it claims."""

    def test_a_document_without_the_row_key_refuses(self) -> None:
        with pytest.raises(ProviderEngineError) as caught:
            _run(document={"filings": []}, symbol="AAPL")
        assert str(caught.value.code) == "INTRINIO_SHAPE_INVALID"

    def test_a_filing_that_omits_the_issuer_name_refuses(self) -> None:
        """``company_name`` is the provider's one non-optional field (:59), read off the filing.

        Upstream indexes ``item["issuer_company"]`` in ``transform_data`` (:173), so a filing that
        does not publish it is a shape failure there too; the kernel attributes the same refusal to
        the record key it could not find.
        """
        filings = [dict(DOCUMENT["transaction_filings"][0])]
        filings[0].pop("issuer_company")
        with pytest.raises(RowExpansionError) as caught:
            _run(document={"transaction_filings": filings}, symbol="AAPL")
        assert caught.value.reason == MISSING_POINTER
        assert caught.value.pointer == "issuer_company"

    def test_no_symbol_means_no_request_at_all(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, DOCUMENT))
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(INSIDER_TRADING, transport, sends=1)
        assert str(caught.value.code) == "INTRINIO_QUERY_INVALID"
        assert caught.value.missing == ("symbol",)
        assert transport.calls == []
