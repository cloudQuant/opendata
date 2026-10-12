"""C98 offline judgement: the two New York Fed rate declarations, facet by facet.

The census rows ``OBB2-federal_reserve-FederalFundsRate`` and
``OBB2-federal_reserve-SOFR`` are declared in
:mod:`opendata.data.providers.federal_reserve.specs` as of C98, and every case here
drives one generated fetcher against an inline document through the same
``FixedResponseTransport`` stub ``tests/test_provider_federal_reserve.py`` uses -- no
socket, no live grant, no recorded capture (none is pinned for these two endpoints, so
each body is synthesized and says so in the spec's notes).

Four facets are judged per model, each with a tamper arm that fails if the facet were
removed from the declaration: the divide-by-100 of the published percent columns
(``column_derives``), the null sentinels matched on record text before any stage, the
request-date ``end_date`` bound (``ParamSpec.relative_default``) beside the absolute
``start_date`` literal, and the ascending date sort (``order_by``). The tamper arms are
two-sided by construction: each re-declares the same spec with one facet taken out and
shows the answer moves in the direction upstream would not allow.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

from opendata.data.providers._engine.http_json import (
    HttpResponse,
    build_query_model,
    encode_query,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import ModelSpec, ParamSpec, RelativeDefault
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers._engine.value_transform import ValueTransformError
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.models.federal_funds_rate import (
    FederalReserveFederalFundsRateFetcher,
)
from opendata.data.providers.federal_reserve.models.sofr import FederalReserveSOFRFetcher
from opendata.data.providers.federal_reserve.specs import (
    DECLARED_GAPS,
    FEDERAL_FUNDS_RATE,
    NOT_DECLARED,
    SOFR,
)

#: One full effective-funds-rate record, cells spelled as decimal text the way the kernel
#: reads them; ``type`` and ``footnoteId`` are published but undeclared, and the row model
#: must not see them (the pops at federal_funds_rate.py:137-138 are no-ops for this engine).
FFR_ROW_A = {
    "effectiveDate": "2024-06-25",
    "percentRate": "4.33",
    "targetRateTo": "5.50",
    "targetRateFrom": "5.25",
    "percentPercentile1": "4.20",
    "percentPercentile25": "4.30",
    "percentPercentile75": "4.40",
    "percentPercentile99": "4.50",
    "volumeInBillions": "185.0",
    "intraDayLow": "4.10",
    "intraDayHigh": "4.60",
    "stdDeviation": "0.05",
    "revisionIndicator": "R",
    "type": "FEDFUNDS",
    "footnoteId": "F",
}
#: The same record with a published integer zero and the '' revision spelling.
FFR_ROW_B = {**FFR_ROW_A, "effectiveDate": "2024-06-26", "percentRate": 0, "revisionIndicator": ""}
#: The remaining revision spellings the upstream validator nulls (:57-61): '' is row B, the
#: quoted form is row C, and 'NA' is one spelling FFR does not declare, so it must survive.
FFR_ROW_C = {**FFR_ROW_A, "effectiveDate": "2024-06-27", "revisionIndicator": "''"}
FFR_ROW_D = {**FFR_ROW_A, "effectiveDate": "2024-06-28", "revisionIndicator": "NA"}
FFR_ROWS = (FFR_ROW_A, FFR_ROW_B, FFR_ROW_C, FFR_ROW_D)
FFR_DOCUMENT = {"refRates": [dict(row) for row in FFR_ROWS]}

#: The nine percent columns upstream divides (federal_funds_rate.py:63-75) as
#: ``(record key, published cell, expected fraction)`` triples.
FFR_SCALED = (
    ("percentRate", "4.33", 0.0433),
    ("targetRateTo", "5.50", 0.055),
    ("targetRateFrom", "5.25", 0.0525),
    ("percentPercentile1", "4.20", 0.042),
    ("percentPercentile25", "4.30", 0.043),
    ("percentPercentile75", "4.40", 0.044),
    ("percentPercentile99", "4.50", 0.045),
    ("intraDayLow", "4.10", 0.041),
    ("intraDayHigh", "4.60", 0.046),
)

#: One SOFR record; ``revisionIndicator`` is published but undeclared here too (sofr.py:105).
SOFR_ROW_A = {
    "effectiveDate": "2024-06-25",
    "percentRate": "5.33",
    "percentPercentile1": "5.10",
    "percentPercentile25": "5.20",
    "percentPercentile75": "5.40",
    "percentPercentile99": "5.50",
    "volumeInBillions": "2350.0",
    "type": "SOFR",
    "footnoteId": "S",
    "revisionIndicator": "R",
}
#: The three spellings the SOFR validator nulls before dividing (sofr.py:46-50), one per row,
#: and the published integer zero that is a value, not a sentinel (the division at :49 keeps 0).
SOFR_EMPTY = {**SOFR_ROW_A, "effectiveDate": "2024-06-26", "percentRate": ""}
SOFR_QUOTE = {**SOFR_ROW_A, "effectiveDate": "2024-06-27", "percentRate": "''"}
SOFR_NA = {**SOFR_ROW_A, "effectiveDate": "2024-06-28", "percentRate": "NA"}
SOFR_ZERO = {**SOFR_ROW_A, "effectiveDate": "2024-06-29", "percentRate": 0}
SOFR_ROWS = (SOFR_ROW_A, SOFR_EMPTY, SOFR_QUOTE, SOFR_NA, SOFR_ZERO)
SOFR_DOCUMENT = {"refRates": [dict(row) for row in SOFR_ROWS]}

#: The five percent columns the SOFR validator's list reaches on published keys (sofr.py:34-44).
SOFR_SCALED = (
    ("percentRate", "5.33", 0.0533),
    ("percentPercentile1", "5.10", 0.051),
    ("percentPercentile25", "5.20", 0.052),
    ("percentPercentile75", "5.40", 0.054),
    ("percentPercentile99", "5.50", 0.055),
)

#: ``(spec, rows, scaled table)`` per model, so every facet case runs the same arms on both.
CASES = (
    (FEDERAL_FUNDS_RATE, FFR_ROWS, FFR_SCALED),
    (SOFR, SOFR_ROWS, SOFR_SCALED),
)
case_ids = ["FederalFundsRate", "SOFR"]
every_case = pytest.mark.parametrize(
    ("spec", "rows", "scaled"), CASES, ids=case_ids, indirect=False
)

#: Which date carries the published integer zero for each model (``FFR_ROW_B`` / ``SOFR_ZERO``).
ZERO_DATE = {"FederalFundsRate": date(2024, 6, 26), "SOFR": date(2024, 6, 29)}


def served(spec: ModelSpec, document: dict) -> tuple:
    """Wire one generated fetcher to a canned JSON document and return it with its recorder."""
    transport = FixedResponseTransport(HttpResponse(200, document))
    fetcher_type = make_http_json_fetcher(SOURCE, spec)
    bound = type(fetcher_type.__name__, (fetcher_type,), {"http_transport": transport})
    return bound(), transport


def rows_of(fetcher, spec: ModelSpec, **kwargs: object) -> list:
    """Fetch through an offline grant and return the normalized rows."""
    return list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1), **kwargs))


def fetched_rows(spec: ModelSpec, document: dict, **kwargs: object) -> list:
    """The rows one declaration produces from one document, without inspecting the wire."""
    fetcher, _transport = served(spec, document)
    return rows_of(fetcher, spec, **kwargs)


def document_of(rows: tuple[dict, ...], *, reverse: bool = False) -> dict:
    """The same records, deep-copied per fetch, newest-first when the case says so."""
    ordered = list(reversed(rows)) if reverse else list(rows)
    return {"refRates": [dict(row) for row in ordered]}


class TestDeclarationFace:
    """What the two records declare, read off the specs themselves."""

    def test_both_census_rows_left_not_declared_and_are_bound(self) -> None:
        """The refusal is gone from every reachable face and the descriptor binds five models."""
        assert "Sofr" not in NOT_DECLARED and "FederalFundsRate" not in NOT_DECLARED
        assert {"FederalFundsRate", "SOFR"} <= set(DECLARED_GAPS)
        assert FederalReserveFederalFundsRateFetcher.canonical_model == "FederalFundsRate"
        assert FederalReserveSOFRFetcher.canonical_model == "SOFR"

    def test_the_facet_counts_are_the_upstream_validator_lists(self) -> None:
        """9 scale stages and 1 sentinel column for FFR; 5 and 5 for SOFR.

        The counts are the pinned validator populations, not this file's taste: nine divided
        fields (federal_funds_rate.py:63-75), the ('', "''") revision rewrite (:57-61), and
        five divided published percent fields with the ('', "''", NA) nulling (sofr.py:34-50).
        """
        assert len(FEDERAL_FUNDS_RATE.column_derives) == 9
        assert len(SOFR.column_derives) == 5
        for plan in FEDERAL_FUNDS_RATE.column_derives + SOFR.column_derives:
            assert [stage.kind for stage in plan.stages] == ["scale"]
        ffr_sentinel = [c.name for c in FEDERAL_FUNDS_RATE.columns if c.null_sentinel]
        sofr_sentinel = [c.name for c in SOFR.columns if c.null_sentinel]
        assert ffr_sentinel == ["revision_indicator"]
        assert sofr_sentinel == [
            "rate",
            "percentile_1",
            "percentile_25",
            "percentile_75",
            "percentile_99",
        ]

    def test_the_date_window_is_a_query_pair_not_a_path_composition(self) -> None:
        """sofr.py:83-86 and federal_funds_rate.py:116-119 interpolate startDate/endDate."""
        for spec in (FEDERAL_FUNDS_RATE, SOFR):
            assert spec.path_placeholders == ()
            assert {param.encoded_key for param in spec.params} == {"startDate", "endDate"}
            assert spec.rows_pointer == "refRates"

    def test_end_date_is_the_relative_bound_and_start_date_the_literal(self) -> None:
        """The C96 carrier fills the omission upstream fills from ``datetime.now()``."""
        by_name = {p.name: p for p in FEDERAL_FUNDS_RATE.params}
        assert by_name["end_date"].relative_default == RelativeDefault()
        assert by_name["start_date"].default == date(2016, 3, 1)
        by_name = {p.name: p for p in SOFR.params}
        assert by_name["end_date"].relative_default == RelativeDefault()
        assert by_name["start_date"].default == date(2018, 4, 2)


class TestPercentRescale:
    """The column_derives facet: every listed percent cell comes back divided by 100."""

    @every_case
    def test_published_percents_come_back_as_fractions(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """Each triple is named by value; no scale stage would leave the percent undivided."""
        row = fetched_rows(spec, document_of(rows))[0]
        for key, _published, expected in scaled:
            column = next(c for c in spec.columns if c.source_key == key)
            assert getattr(row, column.name) == pytest.approx(expected), key

    @every_case
    def test_a_declaration_without_the_scale_plans_leaves_the_percent_undivided(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """Counterfact: the tampered spec (no column_derives) publishes the percent itself."""
        tampered = replace(spec, column_derives=())
        row = fetched_rows(tampered, document_of(rows))[0]
        key, published, expected = scaled[0]
        column = next(c for c in spec.columns if c.source_key == key)
        assert getattr(row, column.name) == pytest.approx(float(published))
        assert getattr(row, column.name) != pytest.approx(expected)

    @every_case
    def test_a_published_zero_stays_zero_and_undeclared_keys_never_reach_the_row(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """``v != 0 else 0`` upstream (federal_funds_rate.py:80) and 0/100 here agree.

        The same arm proves the pops are no-ops: the documents publish ``type`` and
        ``footnoteId`` (SOFR also a ``revisionIndicator``) that no column declares, and the
        row carries exactly the declared columns, so no drop facet is needed.
        """
        emitted = fetched_rows(spec, document_of(rows))
        zero_row = next(row for row in emitted if row.date == ZERO_DATE[spec.model])
        assert zero_row.rate == 0.0
        assert zero_row.rate is not None
        assert set(zero_row.model_dump()) == {column.name for column in spec.columns}

    def test_volume_and_standard_deviation_are_not_rescaled(self) -> None:
        """Upstream's validator lists do not reach them, so no stage may divide them."""
        row = fetched_rows(FEDERAL_FUNDS_RATE, FFR_DOCUMENT)[0]
        assert (row.volume, row.standard_deviation) == (185.0, 0.05)
        scaled_names = {plan.column for plan in FEDERAL_FUNDS_RATE.column_derives}
        assert {"volume", "standard_deviation"}.isdisjoint(scaled_names)
        sofr_row = fetched_rows(SOFR, SOFR_DOCUMENT)[0]
        assert sofr_row.volume == 2350.0


class TestNullSentinels:
    """The sentinel facet: the spelled no-value texts read as None before any stage runs."""

    def test_ffr_revisions_spellings_come_back_as_none(self) -> None:
        """('', "''") are exactly federal_funds_rate.py:61; a real label survives verbatim."""
        rows = fetched_rows(FEDERAL_FUNDS_RATE, FFR_DOCUMENT)
        assert rows[0].revision_indicator == "R"
        assert rows[1].revision_indicator is None
        assert rows[2].revision_indicator is None

    def test_ffr_na_is_not_a_spelled_sentinel_and_survives_as_text(self) -> None:
        """The declaration may not null what the upstream validator does not spell."""
        rows = fetched_rows(FEDERAL_FUNDS_RATE, FFR_DOCUMENT)
        assert rows[3].revision_indicator == "NA"

    def test_removing_the_revision_sentinel_returns_the_empty_string(self) -> None:
        """Counterfact: without the facet the '' cell is copied through, not nulled."""
        columns = tuple(
            replace(column, null_sentinel=()) if column.name == "revision_indicator" else column
            for column in FEDERAL_FUNDS_RATE.columns
        )
        tampered = replace(FEDERAL_FUNDS_RATE, columns=columns)
        rows = fetched_rows(tampered, FFR_DOCUMENT)
        assert rows[1].revision_indicator == ""

    @pytest.mark.parametrize("spelling", ["", "''", "NA"])
    def test_sofr_percent_cells_come_back_as_none_for_every_spelling(self, spelling: str) -> None:
        """sofr.py:48 nulls the spellings before the division at :49; the engine matches the
        record text before the scale stage, so sentinel-plus-scale is one column, one order."""
        document = {"refRates": [{**SOFR_ROW_A, "percentRate": spelling}]}
        row = fetched_rows(SOFR, document)[0]
        assert row.rate is None
        # The other percent cells of the same row still divide: the nulling is per cell.
        assert row.percentile_1 == pytest.approx(0.051)

    def test_removing_a_sofr_sentinel_refuses_where_upstream_nulls(self) -> None:
        """Counterfact: without the facet the '' text reaches the scale stage and raises --
        a refusal upstream never produces, so the reading differs in kind, not just value."""
        columns = tuple(
            replace(column, null_sentinel=()) if column.name == "rate" else column
            for column in SOFR.columns
        )
        tampered = replace(SOFR, columns=columns)
        with pytest.raises(ValueTransformError):
            fetched_rows(tampered, {"refRates": [dict(SOFR_EMPTY)]})


class TestRelativeWindow:
    """The request-side facet: omitted bounds are filled the way transform_query fills them."""

    @pytest.mark.parametrize(
        ("spec", "start_literal"),
        [(FEDERAL_FUNDS_RATE, "2016-03-01"), (SOFR, "2018-04-02")],
        ids=case_ids,
    )
    def test_an_omitted_end_date_is_filled_with_the_request_date(
        self, spec: ModelSpec, start_literal: str
    ) -> None:
        """Counterfact below: without relative_default the key would not be sent at all."""
        document = FFR_DOCUMENT if spec is FEDERAL_FUNDS_RATE else SOFR_DOCUMENT
        fetcher, transport = served(spec, document)
        rows_of(fetcher, spec)
        sent = transport.calls[0]["params"]
        assert sent["endDate"] == date.today().isoformat()
        assert sent["startDate"] == start_literal

    @every_case
    def test_a_named_bound_beats_the_default(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """The bound is filled only when the caller names none (federal_funds_rate.py:101-102)."""
        fetcher, transport = served(spec, document_of(rows))
        rows_of(fetcher, spec, start_date=date(2020, 1, 1), end_date=date(2020, 12, 31))
        sent = transport.calls[0]["params"]
        assert sent["startDate"] == "2020-01-01"
        assert sent["endDate"] == "2020-12-31"

    @every_case
    def test_a_declaration_without_the_relative_bound_sends_no_end_date(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """Counterfact: strip the RelativeDefault and the omission reaches the wire unfilled."""
        params = tuple(
            ParamSpec("end_date", "date", query_key="endDate", note="stripped for the counterfact")
            if parameter.name == "end_date"
            else parameter
            for parameter in spec.params
        )
        tampered = replace(spec, params=params)
        fetcher, transport = served(tampered, document_of(rows))
        rows_of(fetcher, tampered)
        assert "endDate" not in transport.calls[0]["params"]


class TestRowOrder:
    """The sort facet: rows come back ascending by date whatever order the body published."""

    @every_case
    def test_a_newest_first_body_comes_back_ascending(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """Counterfact below: without order_by the published (reversed) order survives."""
        emitted = fetched_rows(spec, document_of(rows, reverse=True))
        assert [row.date for row in emitted] == sorted(row.date for row in emitted)
        assert emitted[0].date == date(2024, 6, 25)

    @every_case
    def test_dropping_the_order_declaration_keeps_the_published_sequence(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """The stage is load-bearing: a tampered spec returns the newest row first."""
        tampered = replace(spec, order_by=None)
        emitted = fetched_rows(tampered, document_of(rows, reverse=True))
        assert emitted[0].date == max(row.date for row in emitted)


class TestQueryBoundary:
    """A caller may not invent parameters the two upstream query models do not carry."""

    @every_case
    def test_only_the_two_window_keys_are_accepted(
        self, spec: ModelSpec, rows: tuple, scaled: tuple
    ) -> None:
        """The records' other keys are response facts, not inputs; a typo'd name is a refusal."""
        query_model = build_query_model(spec)
        assert set(query_model.model_fields) == {"start_date", "end_date"}
        with pytest.raises(ValidationError):
            query_model.model_validate({"percentRate": "1.0"})
        sent = encode_query(query_model(), spec, now=date(2026, 1, 15))
        assert sent == {
            "startDate": spec.params[0].default.isoformat(),
            "endDate": "2026-01-15",
        }
