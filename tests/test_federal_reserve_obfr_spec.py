"""C99 offline judgement: the OBFR declaration, facet by facet, with two-sided counterfacts.

The census row ``OBB2-federal_reserve-OvernightBankFundingRate`` left the roadmap's
``shipped_cover_rows`` pool by being declared and bound in this package. Its need set —
``published_value_rescale``, ``client_side_sort``, ``relative_date_default`` — was re-audited
against the pinned upstream oracle before the declaration landed, and the re-read CONFIRMED
the census's own refutation of a sentinel need: ``normalize_percent`` guards only ``None``
(overnight_bank_funding_rate.py:57-61), unlike SOFR's validator at sofr.py:46-50, and the
``revision_indicator`` nuller at :41-45 is dead for the published row because transform_data
pops the key at :118. So the row carries NO ``columns.null_sentinel`` need — and one of the
arms below measures exactly that absence.

Every case drives the generated fetcher against an inline document through the same
``FixedResponseTransport`` stub the sibling rate tests use — no socket, no live grant, no
recorded capture (none is pinned; the body is synthesized and the spec's notes say so).

Four facets, each with a pre-named reading:
* the divide-by-100 of the five published percent columns — clean reads ``percentRate``
  ``"2.34"`` as ``0.0234``; with the scale stages stripped the cell comes back UNDIVIDED at
  ``2.34`` (the reading the rescale counterfact names);
* the ascending date sort — clean re-orders a newest-first body; with ``order_by`` removed the
  published order survives and ``rows[0].date`` stays ``2024-06-25`` instead of ``2024-06-24``;
* the query-pair window — an empty query sends ``startDate=2016-03-01`` (the absolute literal
  at :79-80) and ``endDate`` as the request date (the ``datetime.now()`` fill at :78, 81-82);
* the sentinel ABSENCE — a published ``""`` percent cell is REFUSED (the kernel reads staged
  columns as exact decimal text and upstream's ``v / 100`` at :60 would raise too), and the
  counterfact shows a wrongly declared SOFR-style sentinel nulls the cell before the stage and
  the pinned contract's required ``rate: float`` then refuses that null (C100 digest 452b4367).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    build_query_model,
    encode_query,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import ModelSpec, RelativeDefault
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers._engine.value_transform import ValueTransformError
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.models.overnight_bank_funding_rate import (
    FederalReserveOvernightBankFundingRateFetcher,
)
from opendata.data.providers.federal_reserve.specs import (
    DECLARED_GAPS,
    NOT_DECLARED,
    OVERNIGHT_BANK_FUNDING_RATE,
    SOFR_NULL_SENTINEL,
)

#: One OBFR record, cells spelled as decimal text the way the kernel reads them; ``type``,
#: ``revisionIndicator`` and ``footnoteId`` are published but undeclared — the pops at
#: overnight_bank_funding_rate.py:117-119 are no-ops here (normalize_record reads declared
#: columns only), so the row model must not see them.
OBFR_ROW_NEW = {
    "effectiveDate": "2024-06-25",
    "percentRate": "2.34",
    "percentPercentile1": "2.08",
    "percentPercentile25": "2.25",
    "percentPercentile75": "2.40",
    "percentPercentile99": "2.55",
    "volumeInBillions": "98.0",
    "type": "OBFR",
    "footnoteId": "F",
    "revisionIndicator": "R",
}
#: The earlier day, published AFTER the newer one so the sort facet has work to do, plus the
#: published integer zero the division keeps at zero (:60 ``v != 0 else 0``).
OBFR_ROW_OLD = {
    **OBFR_ROW_NEW,
    "effectiveDate": "2024-06-24",
    "percentRate": 0,
}
#: Newest-first on purpose: upstream sorts (:122), the document does not arrive sorted.
OBFR_ROWS = (OBFR_ROW_NEW, OBFR_ROW_OLD)

#: The five percent columns the upstream validator's list reaches (:47-55, division :60) as
#: ``(record key, published cell, expected fraction)`` triples.
OBFR_SCALED = (
    ("percentRate", "2.34", 0.0234),
    ("percentPercentile1", "2.08", 0.0208),
    ("percentPercentile25", "2.25", 0.0225),
    ("percentPercentile75", "2.40", 0.024),
    ("percentPercentile99", "2.55", 0.0255),
)


def served(spec: ModelSpec, rows: tuple[dict, ...]) -> tuple:
    """Wire one generated fetcher to a canned JSON document and return it with its recorder."""
    document = {"refRates": [dict(row) for row in rows]}
    transport = FixedResponseTransport(HttpResponse(200, document))
    fetcher_type = make_http_json_fetcher(SOURCE, spec)
    bound = type(fetcher_type.__name__, (fetcher_type,), {"http_transport": transport})
    return bound(), transport


def rows_of(fetcher, spec: ModelSpec, **kwargs: object) -> list:
    """Fetch through an offline grant and return the normalized rows."""
    return list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1), **kwargs))


class TestDeclarationFace:
    """What the declaration says, read off the spec itself."""

    def test_the_census_row_left_not_declared_and_is_bound(self) -> None:
        """The refusal text is gone from every reachable face and the descriptor binds six."""
        assert "OvernightBankFundingRate" not in NOT_DECLARED
        assert "OvernightBankFundingRate" in DECLARED_GAPS
        assert (
            FederalReserveOvernightBankFundingRateFetcher.canonical_model
            == "OvernightBankFundingRate"
        )

    def test_the_facet_counts_are_the_upstream_validator_lists(self) -> None:
        """Five scale stages (the validator list :47-55) and ZERO sentinel columns."""
        assert len(OVERNIGHT_BANK_FUNDING_RATE.column_derives) == 5
        for plan in OVERNIGHT_BANK_FUNDING_RATE.column_derives:
            assert [stage.kind for stage in plan.stages] == ["scale"]
        assert [c.name for c in OVERNIGHT_BANK_FUNDING_RATE.columns if c.null_sentinel] == []
        assert [p.name for p in OVERNIGHT_BANK_FUNDING_RATE.params] == ["start_date", "end_date"]

    def test_the_window_is_a_query_pair_with_an_absolute_start_and_relative_end(self) -> None:
        """overnight_bank_funding_rate.py:78-82 fills start ABSOLUTE, end RELATIVE (:96-98)."""
        assert OVERNIGHT_BANK_FUNDING_RATE.path_placeholders == ()
        assert OVERNIGHT_BANK_FUNDING_RATE.rows_pointer == "refRates"
        assert {p.encoded_key for p in OVERNIGHT_BANK_FUNDING_RATE.params} == {
            "startDate",
            "endDate",
        }
        by_name = {p.name: p for p in OVERNIGHT_BANK_FUNDING_RATE.params}
        assert by_name["start_date"].default == date(2016, 3, 1)
        assert by_name["end_date"].relative_default == RelativeDefault()


class TestRescaleFacet:
    """columns.rescale: five published percents divide by 100; volume does not."""

    def test_clean_arm_publishes_fractions(self) -> None:
        fetcher, _ = served(OVERNIGHT_BANK_FUNDING_RATE, OBFR_ROWS)
        rows = rows_of(fetcher, OVERNIGHT_BANK_FUNDING_RATE)
        by_date = {row.date: row for row in rows}
        latest = by_date[date(2024, 6, 25)]
        for field, _cell, expected in (
            ("rate", None, 0.0234),
            ("percentile_1", None, 0.0208),
            ("percentile_25", None, 0.0225),
            ("percentile_75", None, 0.024),
            ("percentile_99", None, 0.0255),
        ):
            assert getattr(latest, field) == expected, field
        # the published integer zero stays zero (:60), and volume is copied verbatim.
        assert by_date[date(2024, 6, 24)].rate == 0.0
        assert latest.volume == 98.0

    def test_break_the_scale_stages_and_the_reading_moves(self) -> None:
        """Counterfact: with column_derives stripped the cell survives UNDIVIDED at 2.34."""
        maimed = replace(OVERNIGHT_BANK_FUNDING_RATE, column_derives=())
        fetcher, _ = served(maimed, OBFR_ROWS)
        rows = rows_of(fetcher, maimed)
        by_date = {row.date: row for row in rows}
        assert by_date[date(2024, 6, 25)].rate == 2.34
        assert by_date[date(2024, 6, 24)].volume == 98.0


class TestSortFacet:
    """rows.order_limit: the stage sorts a newest-first body ascending, with no cap."""

    def test_clean_arm_sorts_ascending(self) -> None:
        fetcher, _ = served(OVERNIGHT_BANK_FUNDING_RATE, OBFR_ROWS)
        rows = rows_of(fetcher, OVERNIGHT_BANK_FUNDING_RATE)
        assert [row.date for row in rows] == [date(2024, 6, 24), date(2024, 6, 25)]

    def test_break_the_order_and_the_published_order_survives(self) -> None:
        """Counterfact: without order_by rows[0].date stays the newest 2024-06-25."""
        maimed = replace(OVERNIGHT_BANK_FUNDING_RATE, order_by=None)
        fetcher, _ = served(maimed, OBFR_ROWS)
        rows = rows_of(fetcher, maimed)
        assert rows[0].date == date(2024, 6, 25)


class TestWindowFacet:
    """param.relative_default: an unbounded query still carries the upstream window."""

    def test_clean_arm_fills_the_bounds_the_oracle_fills(self) -> None:
        query = build_query_model(OVERNIGHT_BANK_FUNDING_RATE)()
        sent = encode_query(query, OVERNIGHT_BANK_FUNDING_RATE)
        assert sent["startDate"] == "2016-03-01"
        assert sent["endDate"] == date.today().isoformat()

    def test_break_the_relative_bound_and_the_end_vanishes(self) -> None:
        """Counterfact: drop the RelativeDefault and an unbounded query sends no endDate."""
        params = tuple(
            replace(p, relative_default=None) if p.name == "end_date" else p
            for p in OVERNIGHT_BANK_FUNDING_RATE.params
        )
        maimed = replace(OVERNIGHT_BANK_FUNDING_RATE, params=params)
        sent = encode_query(build_query_model(maimed)(), maimed)
        assert "endDate" not in sent
        assert sent["startDate"] == "2016-03-01"


class TestSentinelAbsenceFacet:
    """The census refutation, measured: this row declares NO sentinel and must refuse ``""``.

    Upstream's normalize_percent guards only None (:57-61) and then divides (:60), so a
    published ``""`` cell is a crash upstream, not a null -- declaring the SOFR spelling
    family here would null a cell the oracle never nulls.
    """

    def test_clean_arm_refuses_a_text_percent_cell(self) -> None:
        rows = ({**OBFR_ROW_NEW, "effectiveDate": "2024-06-26", "percentRate": ""},)
        fetcher, _ = served(OVERNIGHT_BANK_FUNDING_RATE, rows)
        with pytest.raises((ValueTransformError, ValueError)):
            rows_of(fetcher, OVERNIGHT_BANK_FUNDING_RATE)

    def test_break_by_declaring_the_sofr_sentinel_and_the_pinned_contract_refuses_the_null(
        self,
    ) -> None:
        """Counterfact: the wrongly copied sentinel rewrites "" to None before the stage, and
        the pinned required rate: float field (models/federal_reserve_clean_rates.py, C100
        digest 452b43673cf4) refuses exactly that null -- a refusal of a different kind than
        the clean arm's stage error, which is what this arm measures."""
        columns = tuple(
            replace(c, null_sentinel=SOFR_NULL_SENTINEL) if c.name == "rate" else c
            for c in OVERNIGHT_BANK_FUNDING_RATE.columns
        )
        maimed = replace(OVERNIGHT_BANK_FUNDING_RATE, columns=columns)
        rows = ({**OBFR_ROW_NEW, "effectiveDate": "2024-06-26", "percentRate": ""},)
        fetcher, _ = served(maimed, rows)
        with pytest.raises(ProviderEngineError) as raised:
            rows_of(fetcher, maimed)
        assert list(raised.value.rejected) == ["rate"]
