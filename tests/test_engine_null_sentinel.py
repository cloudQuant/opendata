"""Declared null sentinels: the engine facet ``columns.null_sentinel`` asked for.

Upstream publishes "no value" as text. The five C97 census rows name four different shapes of it:
a whole-record empty-string rewrite held in a before-validator (benzinga
``price_target.py:259-261``, intrinio ``insider_trading.py:110-118``), a named-field-set map whose
null returns *before* the value is divided (``sofr.py:46-50``, ``federal_funds_rate.py:57-61``), a
frame-wide ``ND`` map (``money_measures.py:85``, ``svensson_yield_curve.py:1062-1082``) and a
frame-wide ``N/A`` round trip
before the sort (econdb ``helpers.py:481-482``). The engine had ``nullable`` -- a column *may* be
null -- and no field spelling which published text becomes that null, so each act above was a
fetcher behaviour with no declaration to hold it.

The faces, in the order they are judged:

* a declared sentinel becomes the same ``None`` an absent key becomes, for both literal families the
  rows cite (``""`` and ``"ND"``/``"NA"``/``"N/A"``/``"''"``);
* the match is on text, exactly: a published ``0`` or ``False`` is never caught by a spelling that
  meant ``"0"``, and a cell that merely resembles a spelling is copied verbatim;
* the guard runs before the column's cast stage, because the upstream rule that nulls the spelling
  returns before it coerces -- and stripping the declaration makes that same cell reach the cast and
  raise, which is the counterfact that proves the runtime consults the field at all;
* a sentinel on a column whose value comes from the response envelope is honoured there too, because
  that is the second place the engine reads a published value;
* every declaration the facet cannot mean is refused when the record is built, naming the offender:
  a non-``str`` spelling, a list instead of a tuple, one spelling twice, a non-nullable column, and
  the silent no-op this round hunts -- a sentinel on a column whose value is *stamped* from a
  parameter or a constant, which no record read can ever reach.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import HttpResponse, make_http_json_fetcher
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers._engine.value_transform import (
    CastSpec,
    DerivePlan,
    DeriveStage,
    ValueTransformError,
)

PROBE_SOURCE = "probe"

#: Published cells in the shapes the five rows cite: a blank, three Fed spellings, and one real
#: value per column that must survive untouched.
TICKER_ROWS: list[dict[str, Any]] = [
    {"ticker": "MSFT", "note": "", "flag": "ND"},
    {"ticker": "AAPL", "note": "raised", "flag": "NA"},
    {"ticker": "GE", "note": "N/A", "flag": "''"},
]

EMPTY_SENTINELS = ("",)
FED_SENTINELS = ("NA", "ND", "N/A", "''")


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove offline-ness structurally: the real HTTP read path raises the moment it is reached."""

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a null-sentinel case reached the network")

    monkeypatch.setattr(http_json, "_http_get_json", forbidden)


def _columns(
    empty: tuple[str, ...] = (),
    flag: tuple[str, ...] = (),
    extra: ColumnSpec | None = None,
) -> tuple[ColumnSpec, ...]:
    """Three copied columns plus, for one case, a column of its own declaration."""
    columns = (
        ColumnSpec("symbol", "str", source_key="ticker"),
        ColumnSpec("note", "str", null_sentinel=empty),
        ColumnSpec("flag", "str", null_sentinel=flag),
    )
    return columns + ((extra,) if extra is not None else ())


def _probe_spec(
    model: str,
    columns: tuple[ColumnSpec, ...],
    **overrides: Any,
) -> ModelSpec:
    """A throwaway declaration whose only interesting part is its column sentinel sets."""
    declaration: dict[str, Any] = {
        "model": model,
        "domain": f"probe_{model.casefold()}",
        "asset_class": "equity",
        "period": "snapshot",
        "market": "all",
        "base_url": "https://probe.test",
        "path": "/ratings",
        "columns": columns,
        "params": (ParamSpec("symbol", "str", required=True),),
        "rows_pointer": "records",
        "scenario": "离线判定面：列哨兵置空的引擎能力",
        "error_prefix": f"PROBE_{model}".upper(),
    }
    declaration.update(overrides)
    return ModelSpec(**declaration)


def _fetch(
    spec: ModelSpec,
    records: list[Any],
    *,
    envelope: dict[str, Any] | None = None,
    source: str = PROBE_SOURCE,
    **query: object,
) -> list:
    """Serve one recorded document to ``spec``'s fetcher offline and return its rows."""
    document: dict[str, Any] = {"records": records}
    if envelope is not None:
        document = {**envelope, **document}
    transport = FixedResponseTransport(HttpResponse(200, document))
    generated = make_http_json_fetcher(source, spec)
    fetcher = type(generated.__name__, (generated,), {"http_transport": transport})()
    return list(fetcher.fetch(ctx=fixture_context(source, spec, sends=2), **query))


class TestSentinelBecomesNull:
    """The facet itself: published text the declaration spells as no value comes out ``None``."""

    def test_empty_string_is_a_sentinel(self) -> None:
        """The benziga/intrinio shape: a blank cell a before-validator already rewrote to ``None``.

        The ``N/A`` cell survives: this column declared only the empty spelling, and a set is the
        column's own -- which is the face the next case judges from the other side.
        """
        spec = _probe_spec("Blank", _columns(empty=EMPTY_SENTINELS))
        rows = _fetch(spec, TICKER_ROWS, symbol="MSFT")
        assert [row.note for row in rows] == [None, "raised", "N/A"]

    def test_letter_spelled_sentinels_are_a_sentinel_set(self) -> None:
        """The Fed shape: ``ND``/``NA``/``''`` nulled by the same one field."""
        spec = _probe_spec("Letters", _columns(flag=FED_SENTINELS))
        rows = _fetch(spec, TICKER_ROWS, symbol="MSFT")
        assert [row.flag for row in rows] == [None, None, None]

    def test_two_sets_are_column_scoped(self) -> None:
        """Each column answers for its own spellings: a sentinel set is no model-wide switch."""
        spec = _probe_spec("Scoped", _columns(empty=EMPTY_SENTINELS, flag=FED_SENTINELS))
        rows = _fetch(spec, TICKER_ROWS, symbol="MSFT")
        assert [(row.note, row.flag) for row in rows] == [
            (None, None),
            ("raised", None),
            ("N/A", None),
        ]

    def test_a_record_that_omits_the_key_is_unchanged(self) -> None:
        """An absent key is already the null the sentinel rewrites toward, so nothing is refused."""
        spec = _probe_spec("Omitted", _columns(flag=FED_SENTINELS))
        rows = _fetch(spec, [{"ticker": "MSFT", "note": "x"}], symbol="MSFT")
        assert rows[0].flag is None


class TestMatchIsOnText:
    """The guard compares published text and nothing else, so no coercion happens at the seam."""

    def test_a_published_number_or_boolean_is_never_a_sentinel(self) -> None:
        """``0``, ``0.0`` and ``False`` are never caught by a spelling meant as the text ``0``."""
        column = ColumnSpec("rate", "float", null_sentinel=("0", "false", "ND"))
        assert [http_json._is_null_sentinel(column, value) for value in (0, 0.0, False, "ND")] == [
            False,
            False,
            False,
            True,
        ]

    def test_a_published_zero_survives_a_declared_zero_spelling(self) -> None:
        """The row keeps the count the source published while the same text in a cell is a null."""
        spec = _probe_spec(
            "Zero",
            _columns(extra=ColumnSpec("score", "float", null_sentinel=("0",))),
        )
        rows = _fetch(
            spec,
            [{"ticker": "A", "score": 0.0}, {"ticker": "B", "score": "0"}],
            symbol="A",
        )
        assert [row.score for row in rows] == [0.0, None]

    def test_the_match_is_case_sensitive_and_exact(self) -> None:
        """A cell that merely resembles a spelling is a published value like any other."""
        spec = _probe_spec("Exact", _columns(flag=("ND",)))
        rows = _fetch(
            spec, [{"ticker": "A", "flag": "nd"}, {"ticker": "B", "flag": " ND"}], symbol="A"
        )
        assert [row.flag for row in rows] == ["nd", " ND"]


class TestSentinelRunsBeforeCoercion:
    """Position is the claim: the null returns before the cast, exactly as upstream's does."""

    @staticmethod
    def _casted(sentinel: tuple[str, ...]) -> ModelSpec:
        """A rate column carrying the Fed's ``ND`` sentinel and a declared float cast."""
        return _probe_spec(
            "Casted",
            (
                ColumnSpec("symbol", "str", source_key="ticker"),
                ColumnSpec("rate", "float", null_sentinel=sentinel),
            ),
            column_derives=(
                DerivePlan(
                    column="rate",
                    source="",
                    stages=(DeriveStage("cast", cast=CastSpec("numeric_to_float")),),
                ),
            ),
        )

    def test_a_sentinel_cell_never_reaches_the_cast(self) -> None:
        """With the declaration ``ND`` is the null, while a published quantity is still cast."""
        rows = _fetch(
            self._casted(("ND",)),
            [{"ticker": "A", "rate": "ND"}, {"ticker": "B", "rate": "4.5"}],
            symbol="A",
        )
        assert [row.rate for row in rows] == [None, 4.5]

    def test_counterfact_stripping_the_declaration_moves_the_row(self) -> None:
        """The runtime really consults the field: stripped, the same cell reaches the cast."""
        declared = self._casted(("ND",))
        stripped = replace(
            declared,
            columns=tuple(replace(column, null_sentinel=()) for column in declared.columns),
        )
        with pytest.raises(ValueTransformError):
            _fetch(stripped, [{"ticker": "A", "rate": "ND"}], symbol="A")
        kept = _fetch(declared, [{"ticker": "A", "rate": "ND"}], symbol="A")
        assert [row.rate for row in kept] == [None]


class TestEnvelopeSentinel:
    """The second read of a published value is the response envelope, and it is guarded too."""

    @staticmethod
    def _spec(model: str, flag: tuple[str, ...]) -> ModelSpec:
        """A declaration whose ``flag`` column is a response-level fact the record omits."""
        return _probe_spec(model, _columns(flag=flag), row_envelope=("flag",))

    def test_an_envelope_cell_is_nulled_by_the_same_declaration(self) -> None:
        """A record that omits the key falls back to the document, which may publish ``ND``."""
        rows = _fetch(
            self._spec("Envelope", FED_SENTINELS),
            [{"ticker": "A", "note": "x"}],
            envelope={"flag": "ND"},
            symbol="A",
        )
        assert rows[0].flag is None

    def test_an_envelope_cell_that_is_not_a_sentinel_is_copied(self) -> None:
        """The guard nulls the spellings it was given and nothing else."""
        rows = _fetch(
            self._spec("EnvelopeCopy", FED_SENTINELS),
            [{"ticker": "A", "note": "x"}],
            envelope={"flag": "R"},
            symbol="A",
        )
        assert rows[0].flag == "R"


class TestNoSentinelIsNoChange:
    """Backward face: a declaration that names none reads every cell exactly as before."""

    def test_an_empty_sentinel_set_copies_the_published_text(self) -> None:
        """``""`` and ``"ND"`` stay values while no column declares them as spellings."""
        rows = _fetch(_probe_spec("Untouched", _columns()), TICKER_ROWS, symbol="MSFT")
        assert [(row.note, row.flag) for row in rows] == [
            ("", "ND"),
            ("raised", "NA"),
            ("N/A", "''"),
        ]


class TestConstructionRefusals:
    """A sentinel declaration the runtime could not carry out is refused when it is written."""

    def test_a_non_text_spelling_is_refused(self) -> None:
        """A numeric spelling could null a published ``0``, so it is not a sentinel at all."""
        with pytest.raises(ValueError, match="tuple of the published text"):
            ColumnSpec("rate", "float", null_sentinel=(0,))

    def test_a_list_is_refused(self) -> None:
        """The declaration is frozen; a mutable sentinel set would hash its way out of review."""
        with pytest.raises(ValueError, match="tuple of the published text"):
            ColumnSpec("rate", "float", null_sentinel=["ND"])

    def test_one_spelling_twice_is_refused(self) -> None:
        """A repeated spelling says one rule twice and hides which entry the reviewer meant."""
        with pytest.raises(ValueError, match="same null sentinel twice"):
            ColumnSpec("rate", "float", null_sentinel=("ND", "ND"))

    def test_a_non_nullable_column_is_refused(self) -> None:
        """Nulling a published value is exactly the null the row model declares it never accepts."""
        with pytest.raises(ValueError, match="non-nullable"):
            ColumnSpec("rate", "float", required=True, nullable=False, null_sentinel=("ND",))

    def test_a_stamped_column_is_refused(self) -> None:
        """The silent no-op this round hunts: a stamped value is never read out of a record."""
        plan = DerivePlan(column="symbol", source="symbol", source_kind="parameter")
        with pytest.raises(ValueError, match="stamped by a derive plan"):
            _probe_spec(
                "Stamped",
                (
                    ColumnSpec("symbol", "str", source_key="ticker", null_sentinel=("",)),
                    ColumnSpec("note", "str"),
                    ColumnSpec("flag", "str"),
                ),
                column_derives=(plan,),
            )

    def test_a_record_sourced_plan_is_not_refused(self) -> None:
        """A plan that reads a record key does read published text, so its sentinel can fire."""
        plan = DerivePlan(
            column="rate",
            source="rate",
            stages=(DeriveStage("cast", cast=CastSpec("numeric_to_float")),),
        )
        spec = _probe_spec(
            "RecordPlan",
            (
                ColumnSpec("symbol", "str", source_key="ticker"),
                ColumnSpec("rate", "float", null_sentinel=("ND",)),
            ),
            column_derives=(plan,),
        )
        rows = _fetch(spec, [{"ticker": "A", "rate": "ND"}], symbol="A")
        assert rows[0].rate is None
