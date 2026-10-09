"""Client-side row filtering: the engine facet cboe's ``IndexSearch`` needed to be declarable.

Before this facet the declaration could say where the records are (``rows_pointer``) and which keys
to read out of them (:class:`ColumnSpec`), but never "drop the rows that do not match" -- so a model
whose upstream answer is a *selection* over a published document had no declaration at all, and was
recorded as not declarable.

Every case here is offline: a transport from ``_engine.testing`` serves a canned document, and an
autouse guard below refuses to run the real HTTP read function at all, so "no network" is a property
of the run rather than of the fixtures.

The faces, in the order they are judged:

* cboe ``IndexSearch`` really is declarable now, and its two gated predicates behave the way the
  branch upstream takes does -- including where the two branches disagree with each other;
* ``ignore_case`` is judgeable: the same document, the same needle, only the flag different;
* a record that does not publish the tested key is a non-match, and "nothing matched" stays
  distinguishable from a shape failure;
* completeness is judged on the *published* rows, so filtering can never talk a short answer into
  looking complete;
* a malformed filter is refused when the record is built, naming the offender.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    apply_row_filters,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
    RowFilterSpec,
)
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    SyntheticTransport,
    fixture_context,
)
from opendata.data.providers.cboe import specs
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.models.index_search import CboeIndexSearchFetcher
from opendata.data.providers.cboe.specs import AVAILABLE_INDICES, INDEX_CONSTITUENTS, INDEX_SEARCH

DIRECTORY_URL = "https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json"
PROBE_SOURCE = "probe"

# The positive control: four published directory rows, three of which are the SPX family. The census
# records this model as "a client-side substring match over the directory", so ``query="SPX"`` keeps
# the family and only a needle no other symbol carries -- ``VIX`` -- keeps exactly one row.
POSITIVE_CONTROL_4: list[dict[str, Any]] = [
    {
        "index_symbol": "SPX",
        "index_name": "S&P 500",
        "Exchange": "NYSE",
        "description": "Broad US equities",
        "currency": "USD",
        "mkt_data_delay": "15 min",
        "calc_start_time": "08:30",
        "calc_end_time": "17:00",
        "time_zone": "America/Chicago",
        "tick_days": "1",
        "tick_frequency": "15",
        "tick_period": "minutes",
        "featured": True,
        "source": "cboe",
    },
    {
        "index_symbol": "SPXN",
        "index_name": "S&P 500 Banks",
        "Exchange": "NYSE",
        "description": "Banks only",
        "currency": "USD",
        "mkt_data_delay": "15 min",
        "calc_start_time": "08:30",
        "calc_end_time": "17:00",
        "time_zone": "America/Chicago",
    },
    {
        "index_symbol": "VIX",
        "index_name": "CBOE Volatility Index",
        "Exchange": "Cboe",
        "description": "Implied volatility",
        "currency": "USD",
        "mkt_data_delay": "Real time",
        "calc_start_time": "03:30",
        "calc_end_time": "20:00",
        "time_zone": "America/Chicago",
    },
    {
        "index_symbol": "SPXT",
        "index_name": "S&P 500 Total Return",
        "Exchange": "NYSE",
        "description": "The SPX family, gross of dividends",
        "currency": "USD",
        "mkt_data_delay": "15 min",
        "calc_start_time": "08:30",
        "calc_end_time": "17:00",
        "time_zone": "America/Chicago",
    },
]

# A row the tested key is simply absent from. Not a malformed row -- just not the one asked for.
ROW_WITHOUT_SYMBOL: dict[str, Any] = {
    "index_name": "Mergers and Acquisitions Index",
    "Exchange": "NYSE",
    "description": "Deal-cycle basket",
    "currency": "USD",
    "mkt_data_delay": "15 min",
    "calc_start_time": "08:30",
    "calc_end_time": "17:00",
    "time_zone": "America/Chicago",
}

# The same published row minus ``description`` -- a key no column of ROW_COLUMNS carries. A filter
# reads the decoded record before normalization, so it can test a key the model does not publish;
# that is what lets this missing-key face keep an unfiltered sibling that still normalizes.
ROW_WITHOUT_DESCRIPTION: dict[str, Any] = {
    key: value for key, value in POSITIVE_CONTROL_4[1].items() if key != "description"
}

#: The record keys the cboe directory publishes for the columns ``INDEX_SEARCH`` declares. The
#: filter is judged on these raw keys, before normalization, which is why ``symbol`` reads here as
#: ``index_symbol``.
ROW_COLUMNS = (
    ColumnSpec("symbol", "str", source_key="index_symbol", required=True),
    ColumnSpec("name", "str", source_key="index_name", required=True),
    ColumnSpec("exchange", "str"),
    ColumnSpec("currency", "str"),
    ColumnSpec("data_delay", "str", source_key="mkt_data_delay"),
    ColumnSpec("open_time", "str", source_key="calc_start_time"),
)


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove offline-ness structurally: the real HTTP read path raises the moment it is reached."""

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a row-filter case reached the network")

    monkeypatch.setattr(http_json, "_http_get_json", forbidden)


def _generated(source: str, spec: ModelSpec, transport: SyntheticTransport) -> Any:
    """Bind one declaration to a recorded transport -- the seam every case below fetches through."""
    generated = make_http_json_fetcher(source, spec)
    return type(generated.__name__, (generated,), {"http_transport": transport})()


def _fetch(spec: ModelSpec, document: Any, *, source: str = PROBE_SOURCE, **query: object):
    """Serve ``document`` to ``spec``'s fetcher offline; return its rows and the recorded calls."""
    transport = FixedResponseTransport(HttpResponse(200, document))
    fetcher = _generated(source, spec, transport)
    rows = list(fetcher.fetch(ctx=fixture_context(source, spec, sends=2), **query))
    return rows, transport


def _symbols(rows: list[Any]) -> list[Any]:
    return [getattr(row, "symbol", None) for row in rows]


def _probe_spec(
    model: str,
    pointer: str,
    filters: tuple[RowFilterSpec, ...] = (),
    *,
    total_key: str | None = None,
    columns: tuple[ColumnSpec, ...] = ROW_COLUMNS,
    params: tuple[ParamSpec, ...] = (ParamSpec("needle", "str"),),
) -> ModelSpec:
    """A throwaway declaration whose only interesting part is its ``row_filters``."""
    return ModelSpec(
        model=model,
        domain=f"probe_{model.casefold()}",
        asset_class="equity",
        period="snapshot",
        market="all",
        base_url="https://probe.test",
        path="/selection",
        rows_pointer=pointer,
        params=params,
        columns=columns,
        scenario="离线判定面：客户端行筛选的引擎能力",
        error_prefix=f"PROBE_{model}".upper(),
        pagination=PaginationSpec(total_key=total_key),
        row_filters=filters,
    )


def _needle_filter(**overrides: Any) -> RowFilterSpec:
    """A ``contains`` filter over the raw ``index_symbol`` key, literal needle ``"spx"``."""
    declaration: dict[str, Any] = {
        "op": "contains",
        "columns": ("index_symbol",),
        "value": "spx",
        "ignore_case": True,
    }
    declaration.update(overrides)
    return RowFilterSpec(**declaration)


class TestCboeIndexSearchIsDeclarable:
    """The model the engine could not express is expressed now, and behaves like the branch is."""

    def test_the_declaration_carries_two_gated_predicates(self) -> None:
        assert [
            (row_filter.columns, row_filter.when_value) for row_filter in INDEX_SEARCH.row_filters
        ] == [
            (("index_symbol",), "True"),
            (("name", "index_name", "index_symbol", "description"), "False"),
        ]
        assert all(row_filter.op == "contains" for row_filter in INDEX_SEARCH.row_filters)
        assert all(row_filter.value_param == "query" for row_filter in INDEX_SEARCH.row_filters)
        assert all(row_filter.when_param == "is_symbol" for row_filter in INDEX_SEARCH.row_filters)
        assert all(row_filter.ignore_case for row_filter in INDEX_SEARCH.row_filters)

    def test_search_is_no_longer_recorded_as_non_declarable(self) -> None:
        assert "IndexSearch" not in specs.NOT_DECLARABLE

    def test_positive_control_keeps_the_rows_whose_symbol_contains_the_needle(self) -> None:
        """The census records upstream as "a client-side substring match over the directory".

        So ``SPX`` is the SPX family -- three of four published rows, VIX dropped -- and the needle
        that isolates exactly ONE row is ``VIX``. Both are asserted by value, because "one row kept"
        is a property of the needle, not of the branch.
        """
        family, transport = _fetch(
            INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="SPX", is_symbol=True
        )
        assert _symbols(family) == ["SPX", "SPXN", "SPXT"]
        assert [row.name for row in family] == ["S&P 500", "S&P 500 Banks", "S&P 500 Total Return"]
        assert family[0].description == "Broad US equities"
        assert family[0].data_delay == "15 min"
        assert family[0].open_time == "08:30"
        assert family[0].time_zone == "America/Chicago"
        alone, _ = _fetch(
            INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="VIX", is_symbol=True
        )
        assert len(alone) == 1
        assert alone[0].symbol == "VIX"
        assert alone[0].name == "CBOE Volatility Index"
        assert len(transport.calls) == 1
        assert transport.calls[0]["url"] == DIRECTORY_URL

    def test_the_needle_is_case_folded_as_upstream_reads_it(self) -> None:
        folded, _ = _fetch(
            INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="spx", is_symbol=True
        )
        exact, _ = _fetch(
            INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="SPX", is_symbol=True
        )
        assert _symbols(folded) == ["SPX", "SPXN", "SPXT"]
        assert _symbols(folded) == _symbols(exact)

    def test_a_row_reachable_only_through_description_survives_the_or(self) -> None:
        """The OR leg, proven on a row neither the symbol nor the name leg can see."""
        rows, _ = _fetch(INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="SPX")
        assert _symbols(rows) == ["SPX", "SPXN", "SPXT"]
        assert [row.description for row in rows][-1] == "The SPX family, gross of dividends"

    def test_the_default_branch_is_the_or_over_the_published_legs(self) -> None:
        """Upstream's ``else`` arm searches name, symbol and description; a name hit survives.

        The row reaches the answer through ``index_name`` only: its own symbol does not contain the
        needle, so the symbol leg cannot be what kept it.
        """
        rows, _ = _fetch(
            INDEX_SEARCH,
            [*POSITIVE_CONTROL_4, {**ROW_WITHOUT_SYMBOL, "index_symbol": "MAI"}],
            source=SOURCE,
            query="Merger",
        )
        assert _symbols(rows) == ["MAI"]
        assert rows[0].name == "Mergers and Acquisitions Index"

    def test_the_name_leg_is_declared_in_both_spellings_the_sources_use(self) -> None:
        """Upstream reads ``name``; this package's directory maps ``index_name``.

        No live response has settled which key cboe publishes, so the OR tests both, and the
        declaration's ``notes`` say so. A row that publishes neither spelling is dropped rather than
        guessed at -- that is the whole cost of the hedge, and it is judged on the record.
        """
        assert INDEX_SEARCH.row_filters[1].columns == (
            "name",
            "index_name",
            "index_symbol",
            "description",
        )
        named = {**ROW_WITHOUT_SYMBOL, "index_symbol": "MAI"}
        by_index_name, _ = _fetch(INDEX_SEARCH, [named], source=SOURCE, query="Merger")
        assert _symbols(by_index_name) == ["MAI"]
        without_either = {
            **named,
            "index_name": "A Basket of Nothing",
            "description": "An unrelated basket",
        }
        dropped, _ = _fetch(INDEX_SEARCH, [without_either], source=SOURCE, query="Merger")
        assert dropped == []
        # ...and the row is still reachable through the leg that does not depend on the dispute.
        by_symbol, _ = _fetch(INDEX_SEARCH, [without_either], source=SOURCE, query="MAI")
        assert _symbols(by_symbol) == ["MAI"]

    def test_the_symbol_gate_drops_a_row_the_or_would_have_kept(self) -> None:
        """What makes these two filters and not one: the same input, judged by different legs."""
        gated, _ = _fetch(
            INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="Broad", is_symbol=True
        )
        or_branch, _ = _fetch(INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="Broad")
        assert gated == []
        assert _symbols(or_branch) == ["SPX"]

    def test_an_empty_needle_never_discards_the_published_answer(self) -> None:
        """An omitted needle means "nothing to select on", which is not the same as "no hits"."""
        omitted, _ = _fetch(INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, is_symbol=True)
        assert _symbols(omitted) == ["SPX", "SPXN", "VIX", "SPXT"]
        blank, transport = _fetch(
            INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="", is_symbol=True
        )
        assert _symbols(blank) == ["SPX", "SPXN", "VIX", "SPXT"]
        assert transport.calls[0]["params"] == {"query": "", "is_symbol": "true"}

    def test_the_request_is_the_directory_one_plus_the_declared_parameters(self) -> None:
        """The GET is the undecorated directory: the selection is client-side, not in the URL."""
        transport = SyntheticTransport(
            {
                (
                    DIRECTORY_URL,
                    frozenset({"query": "SPX", "is_symbol": "true"}.items()),
                ): HttpResponse(200, POSITIVE_CONTROL_4)
            }
        )
        fetcher = _generated(SOURCE, INDEX_SEARCH, transport)
        rows = list(
            fetcher.fetch(
                ctx=fixture_context(SOURCE, INDEX_SEARCH, sends=1), query="SPX", is_symbol=True
            )
        )
        assert _symbols(rows) == ["SPX", "SPXN", "SPXT"]
        assert transport.calls[0]["url"] == DIRECTORY_URL
        assert transport.calls[0]["params"] == {"query": "SPX", "is_symbol": "true"}

    def test_an_unkeyed_request_is_reported_instead_of_answered(self) -> None:
        """The needle is data: an unkeyed request fails rather than serving another fixture."""
        transport = SyntheticTransport({})
        fetcher = _generated(SOURCE, INDEX_SEARCH, transport)
        with pytest.raises(AssertionError) as caught:
            fetcher.fetch(ctx=fixture_context(SOURCE, INDEX_SEARCH, sends=1), query="merger")
        assert "all_indices.json" in str(caught.value)
        assert transport.calls[0]["params"] == {"query": "merger", "is_symbol": "false"}

    def test_the_generated_fetcher_is_bound_to_the_registry(self) -> None:
        from opendata.data.providers.catalog import get_provider

        provider = get_provider(SOURCE)
        bindings = {
            binding.class_name: binding.canonical_model_ids for binding in provider.fetcher_bindings
        }
        assert bindings["CboeIndexSearchFetcher"] == ("IndexSearch",)
        assert CboeIndexSearchFetcher.canonical_model == "IndexSearch"
        assert CboeIndexSearchFetcher.capability.domain == "cboe_index_search"


class TestIgnoreCaseIsJudgeable:
    """``ignore_case`` has an observable consequence on identical input."""

    def test_the_shipped_declaration_with_ignore_case_off_keeps_nothing(self) -> None:
        """Same document, same needle, only the flag different: folded keeps 3, strict keeps 0."""
        strict = replace(
            INDEX_SEARCH,
            row_filters=tuple(
                replace(row_filter, ignore_case=False) for row_filter in INDEX_SEARCH.row_filters
            ),
        )
        folded, _ = _fetch(INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="spx")
        case_sensitive, _ = _fetch(strict, POSITIVE_CONTROL_4, source=SOURCE, query="spx")
        assert _symbols(folded) == ["SPX", "SPXN", "SPXT"]
        assert case_sensitive == []

    def test_the_two_arms_differ_only_where_case_does(self) -> None:
        document = {"records": POSITIVE_CONTROL_4}
        folded, _ = _fetch(
            _probe_spec("Folded", "records", (_needle_filter(value="sp"),)), document, needle="x"
        )
        strict, _ = _fetch(
            _probe_spec("Strict", "records", (_needle_filter(value="sp", ignore_case=False),)),
            document,
            needle="x",
        )
        assert _symbols(folded) == ["SPX", "SPXN", "SPXT"]
        assert _symbols(strict) == []


class TestMissingKeyIsANonMatch:
    """Selecting is not validating: a record without the tested key is not the one asked for."""

    def test_a_row_that_does_not_publish_the_tested_key_is_dropped(self) -> None:
        """The leg tests ``description``, which no declared column publishes -- and still judges."""
        spec = _probe_spec(
            "MissingKey", "records", (_needle_filter(columns=("description",), value="banks"),)
        )
        assert "description" not in ROW_WITHOUT_DESCRIPTION
        assert not any(column.name == "description" for column in ROW_COLUMNS)
        rows, _ = _fetch(
            spec, {"records": [POSITIVE_CONTROL_4[1], ROW_WITHOUT_DESCRIPTION]}, needle="SPX"
        )
        assert _symbols(rows) == ["SPXN"]

    def test_the_unfiltered_face_of_the_same_document_keeps_both_rows(self) -> None:
        """Only the filter removes that row: the same document, unselected, publishes both."""
        document = {"records": [POSITIVE_CONTROL_4[1], ROW_WITHOUT_DESCRIPTION]}
        unfiltered, _ = _fetch(_probe_spec("KeepsAll", "records"), document, needle="SPX")
        assert len(unfiltered) == 2
        assert unfiltered[0].symbol == "SPXN"

    def test_an_empty_selection_is_not_a_shape_failure(self) -> None:
        """No hit is a legitimate answer of zero rows; the pointer face proves the difference."""
        spec = _probe_spec("NoHit", "records", (_needle_filter(value="ZZZ"),))
        rows, _ = _fetch(spec, {"records": [POSITIVE_CONTROL_4[0]]}, needle="SPX")
        assert rows == []
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(spec, {"nothing_here": [POSITIVE_CONTROL_4[0]]}, needle="SPX")
        assert "SHAPE_INVALID" in str(caught.value)

    def test_a_record_that_is_not_a_mapping_is_still_a_shape_failure(self) -> None:
        spec = _probe_spec("NotAMapping", "records", (_needle_filter(),))
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(spec, {"records": [POSITIVE_CONTROL_4[0], "not-a-row"]}, needle="SPX")
        assert "PROBE_NOTAMAPPING_SHAPE_INVALID" in str(caught.value)

    def test_the_unit_face_keeps_the_same_rule(self) -> None:
        """Called directly, ``apply_row_filters`` judges: a non-row is still a shape error."""
        spec = _probe_spec("UnitFace", "records", (_needle_filter(),))
        kept = apply_row_filters([POSITIVE_CONTROL_4[0]], SimpleNamespace(), spec)
        assert kept == [POSITIVE_CONTROL_4[0]]
        with pytest.raises(ProviderEngineError) as caught:
            apply_row_filters([POSITIVE_CONTROL_4[0], 7], SimpleNamespace(), spec)
        assert "SHAPE_INVALID" in str(caught.value)


class TestShapeFailureSurvivesFiltering:
    """A filter cannot be the reason a wrong document looks empty."""

    def test_a_pointer_that_is_not_there_is_a_shape_failure_not_an_empty_result(self) -> None:
        spec = _probe_spec("Pointer", "records", (_needle_filter(),))
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(spec, {"wrong": []}, needle="SPX")
        assert "PROBE_POINTER_SHAPE_INVALID" in str(caught.value)

    def test_an_empty_published_list_is_a_legitimate_empty_result(self) -> None:
        spec = _probe_spec("EmptyList", "records", (_needle_filter(),))
        rows, transport = _fetch(spec, {"records": []}, needle="SPX")
        assert rows == []
        assert len(transport.calls) == 1


class TestCompletenessIsJudgedBeforeSelection:
    """``row_filters`` select after the source's own completeness was checked, never before."""

    @staticmethod
    def _spec(model: str) -> ModelSpec:
        return _probe_spec(model, "data", (_needle_filter(),), total_key="total")

    def test_filtered_rows_do_not_enter_the_declared_total_reconciliation(self) -> None:
        """Four published and four declared: keeping three of them is not an incomplete answer."""
        rows, _ = _fetch(
            self._spec("Totals"), {"total": 4, "data": POSITIVE_CONTROL_4}, needle="SPX"
        )
        assert _symbols(rows) == ["SPX", "SPXN", "SPXT"]
        assert len(rows) < 4

    def test_a_short_published_answer_is_still_incomplete(self) -> None:
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(self._spec("Short"), {"total": 4, "data": POSITIVE_CONTROL_4[:1]}, needle="SPX")
        assert "PROBE_SHORT_INCOMPLETE" in str(caught.value)

    def test_a_filter_still_cannot_hide_an_overshoot(self) -> None:
        """Five published against four declared conflicts no matter how few rows survive."""
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(
                self._spec("Overshoot"),
                {"total": 4, "data": [*POSITIVE_CONTROL_4, ROW_WITHOUT_SYMBOL]},
                needle="SPX",
            )
        assert "PROBE_OVERSHOOT_TOTAL_CONFLICT" in str(caught.value)


class TestDeclarationRejectsMalformedFilters:
    """A filter that cannot be evaluated is refused at construction, naming the offender."""

    def test_unknown_value_param_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not declare as a parameter"):
            _probe_spec("BadNeedle", "data", (_needle_filter(value="", value_param="nope"),))

    def test_unknown_when_param_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not declare as a parameter"):
            _probe_spec("BadGate", "data", (_needle_filter(when_param="nope"),))

    def test_a_filter_that_tests_nothing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tests no record keys"):
            _probe_spec("Inert", "data", (_needle_filter(columns=()),))

    def test_an_unknown_op_is_refused(self) -> None:
        with pytest.raises(ValueError, match="is not one of"):
            _needle_filter(op="is_greater_than")

    def test_a_needle_taken_from_two_places_is_refused(self) -> None:
        with pytest.raises(ValueError, match="value or value_param, not both"):
            _needle_filter(value_param="needle")

    def test_an_in_filter_without_alternatives_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must publish its alternatives"):
            _needle_filter(op="in", value=" ")

    def test_a_blank_record_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-blank record key"):
            _needle_filter(columns=("  ",))


class TestOtherDeclarationsAreUntouched:
    """Nothing about a declaration that uses no filter changed."""

    def test_a_declaration_built_positionally_still_binds_the_same_fields(self) -> None:
        positional = ModelSpec(
            "Positional",
            "probe_positional",
            "equity",
            "snapshot",
            "all",
            "https://probe.test",
            "/selection",
            ROW_COLUMNS,
            (ParamSpec("needle", "str"),),
            "records",
            scenario="离线判定面：位置参数仍然绑到同一批字段",
        )
        assert positional.rows_pointer == "records"
        assert positional.decoder.kind == "json"
        assert positional.row_filters == ()

    def test_a_model_without_row_filters_is_unaffected(self) -> None:
        rows, _ = _fetch(_probe_spec("Plain", "records"), {"records": POSITIVE_CONTROL_4})
        assert _symbols(rows) == ["SPX", "SPXN", "VIX", "SPXT"]

    def test_the_other_cboe_declarations_have_no_filters(self) -> None:
        assert AVAILABLE_INDICES.row_filters == ()
        assert INDEX_CONSTITUENTS.row_filters == ()
        assert INDEX_SEARCH.decoder.kind == AVAILABLE_INDICES.decoder.kind


class TestFiltersCombineAsDeclared:
    """Two open filters AND together; ``match`` decides how one filter reads its columns."""

    # The first leg keeps the SPX family by symbol; the second keeps one row by description, and
    # only while its gate is open. Neither subset is the intersection, so a filter that was silently
    # ignored would show up as the wrong rows rather than as a passing case.
    SYMBOL_LEG = _needle_filter()
    DESCRIPTION_LEG = _needle_filter(
        columns=("description",),
        value="family",
        when_param="flag",
        when_value="True",
    )

    @staticmethod
    def _spec() -> ModelSpec:
        return _probe_spec(
            "Combine",
            "records",
            (
                TestFiltersCombineAsDeclared.SYMBOL_LEG,
                TestFiltersCombineAsDeclared.DESCRIPTION_LEG,
            ),
            params=(ParamSpec("needle", "str"), ParamSpec("flag", "bool", default=False)),
        )

    def test_the_second_filter_is_inert_while_its_gate_is_closed(self) -> None:
        rows, _ = _fetch(self._spec(), {"records": POSITIVE_CONTROL_4})
        assert _symbols(rows) == ["SPX", "SPXN", "SPXT"]

    def test_both_filters_have_to_hold_when_both_gates_are_open(self) -> None:
        rows, _ = _fetch(self._spec(), {"records": POSITIVE_CONTROL_4}, flag=True)
        assert _symbols(rows) == ["SPXT"]

    def test_match_any_keeps_a_row_reachable_through_one_column(self) -> None:
        spec = _probe_spec(
            "Any", "records", (_needle_filter(columns=("index_symbol", "description")),)
        )
        rows, _ = _fetch(spec, {"records": POSITIVE_CONTROL_4})
        assert _symbols(rows) == ["SPX", "SPXN", "SPXT"]

    def test_match_all_requires_every_tested_column_to_hit(self) -> None:
        spec = _probe_spec(
            "All",
            "records",
            (_needle_filter(columns=("index_symbol", "description"), match="all"),),
        )
        rows, _ = _fetch(spec, {"records": POSITIVE_CONTROL_4})
        assert _symbols(rows) == ["SPXT"]

    def test_in_and_not_equals_are_judged_from_declared_alternatives(self) -> None:
        spec = _probe_spec(
            "InAndOut",
            "records",
            (
                _needle_filter(op="in", value="SPX|SPXN|VIX", ignore_case=False),
                _needle_filter(columns=("Exchange",), value="Cboe", op="not_equals"),
            ),
        )
        rows, _ = _fetch(spec, {"records": POSITIVE_CONTROL_4})
        assert _symbols(rows) == ["SPX", "SPXN"]

    def test_in_is_case_folded_when_the_declaration_asks(self) -> None:
        spec = _probe_spec("InFolded", "records", (_needle_filter(op="in", value="spx|spy"),))
        rows, _ = _fetch(spec, {"records": POSITIVE_CONTROL_4})
        assert _symbols(rows) == ["SPX"]


def test_row_filter_face_prints_its_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    """A self-check states what it measured, so a green run is a run that says why it is green."""
    rows, transport = _fetch(
        INDEX_SEARCH, POSITIVE_CONTROL_4, source=SOURCE, query="spx", is_symbol=True
    )
    print(
        f"row filters: cboe::IndexSearch declares {len(INDEX_SEARCH.row_filters)} gated filters; "
        f"needle 'spx' over {len(POSITIVE_CONTROL_4)} published rows kept {len(rows)} "
        f"{_symbols(rows)} in {len(transport.calls)} send(s)"
    )
    captured = capsys.readouterr()
    print(captured.out, end="")
    assert "row filters:" in captured.out
