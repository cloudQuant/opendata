"""Declared row ordering and top-n: the engine facet ``rows.order_limit`` asked for.

Before this facet a declaration could say where the records are (``rows_pointer``), which of them
the model is about (:class:`RowFilterSpec`) and what to take out of each one
(:class:`ColumnSpec`), but never "sort them by this column and keep the first N". A search ranked
by relevance, or a holdings list cut to its largest weights, therefore had no declaration at all
and was recorded as engine work.

Every case here is offline: a transport from ``_engine.testing`` serves a canned document and an
autouse guard refuses to run the real HTTP read function at all, so "no network" is a property of
the run rather than of the fixtures.

The faces, in the order they are judged:

* the sequence really changes -- for a document whose published order is neither ascending nor
  descending, an ascending read and a descending read both differ from the published order, so a
  stage that was silently skipped cannot pass either case;
* ties keep published order in both directions;
* the cap is applied to what the *selection* kept, and a cap larger than the record count is legal;
* ``limit_param`` takes the cap from a validated parameter, the way a needle comes from
  ``value_param``, including what an omitted or unreadable parameter means;
* a record that does not publish the sort key, or publishes one this column cannot compare, is last
  rather than dropped or refused;
* the count can never reach the declared-total reconciliation;
* an ordering that cannot mean anything is refused when the record is built, naming the offender.
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
    apply_row_order,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
    RowFilterSpec,
    RowOrderSpec,
)
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    fixture_context,
)

PROBE_SOURCE = "probe"

# Published in an order that is neither ascending nor descending, so an ascending read and a
# descending read each differ from what the source handed over: the counterfact depends on it.
# ``BRK`` publishes no ``weight`` at all, and ``GE``/``XOM`` tie at 3.4 -- the two faces the
# semantics had to decide, carried by the same rows every other case reads.
HOLDINGS: list[dict[str, Any]] = [
    {"ticker": "MSFT", "security_name": "Microsoft", "weight": 6.2},
    {"ticker": "AAPL", "security_name": "Apple", "weight": 7.1},
    {"ticker": "GE", "security_name": "General Electric", "weight": 3.4},
    {"ticker": "XOM", "security_name": "Exxon", "weight": 3.4},
    {"ticker": "BRK", "security_name": "Berkshire"},
]

#: The record keys the probe document publishes. ``weight`` is a declared column, so the declaration
#: can be checked against it; ``score`` is not, so it cannot.
ROW_COLUMNS = (
    ColumnSpec("symbol", "str", source_key="ticker", required=True),
    ColumnSpec("name", "str", source_key="security_name", required=True),
    ColumnSpec("weight", "float", source_key="weight"),
)

PUBLISHED = ["MSFT", "AAPL", "GE", "XOM", "BRK"]
ASCENDING = ["GE", "XOM", "MSFT", "AAPL", "BRK"]
DESCENDING = ["AAPL", "MSFT", "GE", "XOM", "BRK"]


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove offline-ness structurally: the real HTTP read path raises the moment it is reached."""

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a row-order case reached the network")

    monkeypatch.setattr(http_json, "_http_get_json", forbidden)


def _generated(source: str, spec: ModelSpec, transport: FixedResponseTransport) -> Any:
    """Bind one declaration to a recorded transport -- the seam every case below fetches through."""
    generated = make_http_json_fetcher(source, spec)
    return type(generated.__name__, (generated,), {"http_transport": transport})()


def _fetch(spec: ModelSpec, document: Any, *, source: str = PROBE_SOURCE, **query: object):
    """Serve ``document`` to ``spec``'s fetcher offline; return its rows and the recorded calls."""
    transport = FixedResponseTransport(HttpResponse(200, document))
    fetcher = _generated(source, spec, transport)
    rows = list(fetcher.fetch(ctx=fixture_context(source, spec, sends=2), **query))
    return rows, transport


def _tickers(rows: list[Any]) -> list[Any]:
    return [getattr(row, "symbol", None) for row in rows]


def _probe_spec(
    model: str,
    order: RowOrderSpec | None = None,
    *,
    filters: tuple[RowFilterSpec, ...] = (),
    total_key: str | None = None,
    params: tuple[ParamSpec, ...] = (ParamSpec("top", "int"),),
    pagination: PaginationSpec | None = None,
) -> ModelSpec:
    """A throwaway declaration whose only interesting part is its ``order_by``."""
    return ModelSpec(
        model=model,
        domain=f"probe_{model.casefold()}",
        asset_class="equity",
        period="snapshot",
        market="all",
        base_url="https://probe.test",
        path="/holdings",
        columns=ROW_COLUMNS,
        params=params,
        rows_pointer="records",
        scenario="离线判定面：行排序与 top-n 的引擎能力",
        error_prefix=f"PROBE_{model}".upper(),
        pagination=pagination or PaginationSpec(total_key=total_key),
        row_filters=filters,
        order_by=order,
    )


def _weight_order(**overrides: Any) -> RowOrderSpec:
    """An ascending order over the published ``weight`` key, with no cap unless one is given."""
    declaration: dict[str, Any] = {"column": "weight"}
    declaration.update(overrides)
    return RowOrderSpec(**declaration)


class TestSequenceChanges:
    """The point of the facet: rows come out in the declared order, not the source's."""

    def test_ascending_reorders_the_published_rows(self) -> None:
        rows, _ = _fetch(_probe_spec("Asc", _weight_order()), {"records": HOLDINGS})
        assert _tickers(rows) == ASCENDING

    def test_descending_reverses_the_values_not_the_list(self) -> None:
        rows, _ = _fetch(
            _probe_spec("Desc", _weight_order(direction="desc")),
            {"records": HOLDINGS},
        )
        assert _tickers(rows) == DESCENDING

    def test_counterfact_a_skipped_stage_cannot_pass_either_direction(self) -> None:
        """The published order is neither reading, so returning the input is a visible failure.

        Both arms read the same document and are compared against the un-ordered sibling: if the
        stage were dropped from the pipeline every assertion below would still hold, and this one
        would not.
        """
        document = {"records": HOLDINGS}
        un_ordered, _ = _fetch(_probe_spec("Unordered", None), document)
        ascending, _ = _fetch(_probe_spec("CounterAsc", _weight_order()), document)
        descending, _ = _fetch(
            _probe_spec("CounterDesc", _weight_order(direction="desc")), document
        )
        assert _tickers(un_ordered) == PUBLISHED
        assert _tickers(ascending) != _tickers(un_ordered)
        assert _tickers(descending) != _tickers(un_ordered)
        assert _tickers(ascending) != _tickers(descending)

    def test_the_declaration_is_the_only_thing_that_moved_the_rows(self) -> None:
        """Same transport, same document, one field different: ``replace`` isolates the facet."""
        ordered = _probe_spec("Replaced", _weight_order(direction="desc", limit=2))
        plain = replace(ordered, order_by=None)
        capped, _ = _fetch(ordered, {"records": HOLDINGS})
        kept, _ = _fetch(plain, {"records": HOLDINGS})
        assert _tickers(capped) == ["AAPL", "MSFT"]
        assert _tickers(kept) == PUBLISHED

    def test_ties_keep_published_order_in_both_directions(self) -> None:
        """``reverse=True`` preserves stability, so GE/XOM do not swap when the column flips."""
        ascending, _ = _fetch(_probe_spec("TieAsc", _weight_order()), {"records": HOLDINGS})
        descending, _ = _fetch(
            _probe_spec("TieDesc", _weight_order(direction="desc")), {"records": HOLDINGS}
        )
        assert _tickers(ascending) == ASCENDING
        assert _tickers(descending) == DESCENDING
        assert [row.weight for row in ascending[:2]] == [3.4, 3.4]

    def test_a_text_column_orders_as_text(self) -> None:
        """The same kernel reads a name column: the class is the first published cell's."""
        rows, _ = _fetch(
            _probe_spec("ByText", _weight_order(column="security_name")), {"records": HOLDINGS}
        )
        assert _tickers(rows) == ["AAPL", "BRK", "XOM", "GE", "MSFT"]


class TestCapIsApplied:
    """The ``limit`` half, including the faces a naive implementation gets wrong."""

    def test_a_literal_cap_keeps_the_first_n(self) -> None:
        rows, _ = _fetch(
            _probe_spec("Capped", _weight_order(direction="desc", limit=2)), {"records": HOLDINGS}
        )
        assert _tickers(rows) == ["AAPL", "MSFT"]

    def test_a_cap_larger_than_the_answer_keeps_every_row(self) -> None:
        """99 asked of 5 published: slicing, not a count that must be satisfied."""
        rows, _ = _fetch(
            _probe_spec("Roomy", _weight_order(direction="desc", limit=99)), {"records": HOLDINGS}
        )
        assert _tickers(rows) == DESCENDING

    def test_no_cap_declared_keeps_every_row_in_order(self) -> None:
        rows, _ = _fetch(_probe_spec("SortOnly", _weight_order()), {"records": HOLDINGS})
        assert _tickers(rows) == ASCENDING
        assert len(rows) == len(HOLDINGS)

    def test_the_cap_is_taken_from_a_declared_parameter(self) -> None:
        spec = _probe_spec("ParamCap", _weight_order(direction="desc", limit_param="top"))
        two, _ = _fetch(spec, {"records": HOLDINGS}, top=2)
        one, _ = _fetch(spec, {"records": HOLDINGS}, top=1)
        assert _tickers(two) == ["AAPL", "MSFT"]
        assert _tickers(one) == ["AAPL"]

    def test_an_unreadable_or_omitted_parameter_caps_nothing(self) -> None:
        """A caller who did not say, or said something unparseable, gets the ordered answer."""
        spec = _probe_spec("ParamNone", _weight_order(limit_param="top"))
        omitted, _ = _fetch(spec, {"records": HOLDINGS})
        zero, _ = _fetch(spec, {"records": HOLDINGS}, top=0)
        assert _tickers(omitted) == ASCENDING
        assert _tickers(zero) == ASCENDING
        textual = _probe_spec(
            "ParamText",
            _weight_order(limit_param="size"),
            params=(ParamSpec("size", "str"),),
        )
        three, _ = _fetch(textual, {"records": HOLDINGS}, size="3")
        junk, _ = _fetch(textual, {"records": HOLDINGS}, size="many")
        assert _tickers(three) == ASCENDING[:3]
        assert _tickers(junk) == ASCENDING


class TestOrderingFollowsSelection:
    """The stage orders what survived filtering, and cannot reach the completeness judgement."""

    def test_a_row_the_filter_dropped_is_not_sorted_back_in(self) -> None:
        """The largest weight is also the one excluded, so a filter-after-order would show it."""
        drops_apple = RowFilterSpec(op="not_equals", columns=("ticker",), value="AAPL")
        spec = _probe_spec(
            "AfterFilter",
            _weight_order(direction="desc", limit=3),
            filters=(drops_apple,),
        )
        rows, _ = _fetch(spec, {"records": HOLDINGS})
        assert _tickers(rows) == ["MSFT", "GE", "XOM"]

    def test_the_cap_never_enters_the_declared_total_reconciliation(self) -> None:
        """Five published against five declared is complete even though two rows are kept."""
        spec = _probe_spec("Totals", _weight_order(direction="desc", limit=2), total_key="total")
        rows, _ = _fetch(spec, {"total": 5, "records": HOLDINGS})
        assert _tickers(rows) == ["AAPL", "MSFT"]
        assert len(rows) < 5

    def test_a_short_published_answer_is_still_incomplete(self) -> None:
        spec = _probe_spec("Short", _weight_order(limit=1), total_key="total")
        with pytest.raises(ProviderEngineError) as caught:
            _fetch(spec, {"total": 5, "records": HOLDINGS[:3]})
        assert "PROBE_SHORT_INCOMPLETE" in str(caught.value)


class TestUnrankableRecordsComeLast:
    """No sort cell, or one this column cannot compare: last, kept, and never a shape failure."""

    def test_a_row_without_the_sort_key_is_last_in_both_directions(self) -> None:
        ascending, _ = _fetch(_probe_spec("MissingAsc", _weight_order()), {"records": HOLDINGS})
        descending, _ = _fetch(
            _probe_spec("MissingDesc", _weight_order(direction="desc")), {"records": HOLDINGS}
        )
        assert ascending[-1].symbol == "BRK"
        assert descending[-1].symbol == "BRK"
        assert ascending[-1].weight is None

    def test_a_cap_can_cut_off_the_unrankable_run(self) -> None:
        """The rankable rows come first, so a top-4 here drops BRK rather than promoting it."""
        rows, _ = _fetch(
            _probe_spec("CutLast", _weight_order(direction="desc", limit=4)), {"records": HOLDINGS}
        )
        assert _tickers(rows) == ["AAPL", "MSFT", "GE", "XOM"]

    def test_a_cell_of_another_class_cannot_be_ordered_against_the_column(self) -> None:
        """Unit face: a text cell in a numeric column joins the trailing run, and nothing raises."""
        spec = _probe_spec("Mixed", _weight_order())
        records = [
            {"ticker": "MSFT", "weight": 6.2},
            {"ticker": "ODD", "weight": "high"},
            {"ticker": "AAPL", "weight": 7.1},
            {"ticker": "FLAG", "weight": True},
            {"ticker": "NEST", "weight": [1, 2]},
        ]
        ordered = apply_row_order(records, SimpleNamespace(), spec)
        assert [record["ticker"] for record in ordered] == ["MSFT", "AAPL", "ODD", "FLAG", "NEST"]

    def test_a_column_whose_every_cell_is_unrankable_keeps_the_published_order(self) -> None:
        """Nothing compares, so the stable answer is the source's own sequence -- and no error."""
        spec = _probe_spec("AllUnrankable", _weight_order())
        records = [{"ticker": "A"}, {"ticker": "B"}]
        assert apply_row_order(records, SimpleNamespace(), spec) == records

    def test_the_unit_face_applies_the_declared_direction_and_cap(self) -> None:
        spec = _probe_spec("UnitFace", _weight_order(direction="desc", limit=2))
        ordered = apply_row_order(HOLDINGS, SimpleNamespace(top=9), spec)
        assert [record["ticker"] for record in ordered] == ["AAPL", "MSFT"]

    def test_a_record_that_is_not_a_mapping_is_still_a_shape_failure(self) -> None:
        spec = _probe_spec("NotAMapping", _weight_order())
        with pytest.raises(ProviderEngineError) as caught:
            apply_row_order([HOLDINGS[0], "not-a-row"], SimpleNamespace(), spec)
        assert "PROBE_NOTAMAPPING_SHAPE_INVALID" in str(caught.value)

    def test_a_declaration_without_order_by_is_passed_through_untouched(self) -> None:
        spec = _probe_spec("PassThrough", None)
        records: list[Any] = [{"ticker": "B"}, 7, {"ticker": "A"}]
        assert apply_row_order(records, SimpleNamespace(), spec) == records


class TestDeclarationRejectsIllFormedOrders:
    """A stage that cannot mean anything is refused at construction, naming the offender."""

    def test_a_limit_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="keeps no rows"):
            _weight_order(limit=0)

    def test_a_negative_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="keeps no rows"):
            _weight_order(limit=-3)

    def test_a_boolean_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="integer row count"):
            _weight_order(limit=True)

    def test_a_cap_taken_from_two_places_is_refused(self) -> None:
        with pytest.raises(ValueError, match="limit or limit_param, not both"):
            _weight_order(limit=2, limit_param="top")

    def test_an_undirectional_order_is_refused(self) -> None:
        with pytest.raises(ValueError, match="is not one of"):
            _weight_order(direction="descending")

    def test_a_blank_record_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must name one record key"):
            _weight_order(column=" ")

    def test_a_sort_key_no_column_publishes_is_refused(self) -> None:
        """The engine cannot see the source's keys, so it checks this record's own vocabulary."""
        assert "score" not in {column.source_key or column.name for column in ROW_COLUMNS}
        with pytest.raises(ValueError, match="names a record key no column"):
            _probe_spec("UnknownKey", _weight_order(column="score"))

    def test_an_unknown_limit_param_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not declare as a parameter"):
            _probe_spec("UnknownParam", _weight_order(limit_param="count"))

    def test_a_stage_that_is_not_a_row_order_spec_is_refused(self) -> None:
        """The field is typed, and a declaration that hands it anything else is a mistake."""
        with pytest.raises(ValueError, match="must be a RowOrderSpec"):
            replace(_probe_spec("WrongType", _weight_order()), order_by="weight")

    def test_an_ordering_across_pages_is_refused(self) -> None:
        """The stage sees one response's records, so a paged top-n would be a per-page cap."""
        paged = PaginationSpec(kind="offset", limit_key="n", offset_key="o")
        with pytest.raises(ValueError, match="cannot be honoured across"):
            _probe_spec("Paged", _weight_order(direction="desc", limit=2), pagination=paged)

    def test_a_declaration_built_positionally_still_binds_the_same_fields(self) -> None:
        positional = ModelSpec(
            "Positional",
            "probe_positional",
            "equity",
            "snapshot",
            "all",
            "https://probe.test",
            "/holdings",
            ROW_COLUMNS,
            (ParamSpec("top", "int"),),
            "records",
            scenario="离线判定面：位置参数仍然绑到同一批字段",
        )
        assert positional.rows_pointer == "records"
        assert positional.order_by is None
        assert positional.row_filters == ()

    def test_the_filter_facet_is_unchanged_by_the_new_one(self) -> None:
        """Ordering did not move the selection: the same filter still keeps the same rows."""
        prefix_sp = RowFilterSpec(op="prefix", columns=("ticker",), value="SP")
        spec = _probe_spec("FilterOnly", None, filters=(prefix_sp,))
        kept = apply_row_filters([*HOLDINGS, {"ticker": "SPY"}], SimpleNamespace(), spec)
        assert [record["ticker"] for record in kept] == ["SPY"]


def test_row_order_face_prints_its_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    """A self-check states what it measured, so a green run is a run that says why it is green."""
    spec = _probe_spec("Verdict", _weight_order(direction="desc", limit=2))
    rows, transport = _fetch(spec, {"records": HOLDINGS})
    print(
        f"row order: order_by weight desc limit 2 over {len(HOLDINGS)} published rows "
        f"{PUBLISHED} returned {_tickers(rows)} in {len(transport.calls)} send(s)"
    )
    captured = capsys.readouterr()
    print(captured.out, end="")
    assert "row order:" in captured.out
    assert _tickers(rows) == ["AAPL", "MSFT"]
