"""Offline witnesses for the two engine seams C86 wired: value mapping and path composition.

Both capabilities are measured from source text by ``scripts/quality/model_capability_census.py``,
so the roadmap flips present when the tokens exist. Presence is not correctness: these tests run
the seams and pin the request each declaration produces, including the refusal arms that prove
every guard on the new lines is live rather than unreachable.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from opendata.data.providers._engine import key_composition
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    encode_query,
    render_path,
)
from opendata.data.providers._engine.key_composition import CompositeKeySpec, KeyComponent
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec

PREFIX = "TEST"


def _spec(**overrides: object) -> ModelSpec:
    """A minimal valid declaration with one parameter and one column, overridable per test."""
    base: dict[str, object] = {
        "model": "Widget",
        "domain": "equity",
        "asset_class": "share",
        "period": "tick",
        "market": "all",
        "base_url": "https://example.test",
        "path": "/widget",
        "columns": (ColumnSpec(name="value", kind="str"),),
        "params": (ParamSpec(name="symbol", kind="str", required=True),),
        "scenario": "wiring witness",
        "error_prefix": PREFIX,
    }
    base.update(overrides)
    return ModelSpec(**base)  # type: ignore[arg-type]


def _query(**values: object) -> SimpleNamespace:
    """A validated query stand-in: the seams read attributes, nothing else."""
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------- value mapping


def _mapped_spec() -> ModelSpec:
    """A model whose ``coverage`` parameter says annual and sends ``A``."""
    return _spec(
        params=(
            ParamSpec(
                name="coverage",
                kind="enum",
                enum=("annual", "quarterly"),
                value_map=(("annual", "A"), ("quarterly", "Q")),
            ),
        )
    )


def test_value_map_sends_the_upstream_spelling() -> None:
    """The positive control: the outgoing query carries the mapped token, not the caller's."""
    assert encode_query(_query(coverage="quarterly"), _mapped_spec()) == {"coverage": "Q"}


def test_value_map_is_absent_by_default_and_passes_text_through() -> None:
    """Without a map the same parameter is encoded verbatim, so the arm above can differ."""
    plain = _spec(params=(ParamSpec(name="coverage", kind="enum", enum=("annual", "quarterly")),))
    assert encode_query(_query(coverage="quarterly"), plain) == {"coverage": "quarterly"}


def test_unmapped_value_refuses_instead_of_passing_through() -> None:
    """A value the map does not spell never reaches the wire under a reviewed spelling."""
    spec = _spec(params=(ParamSpec(name="tickers", kind="str", value_map=(("AAPL", "x"),)),))
    with pytest.raises(ProviderEngineError) as caught:
        encode_query(_query(tickers="MSFT"), spec)
    assert caught.value.code == f"{PREFIX}_PARAM_VALUE_UNMAPPED"
    assert caught.value.rejected == ("tickers",)


def test_mapped_value_keeps_the_declared_query_key() -> None:
    """Only the value is translated; an aliased query key still names the parameter upstream."""
    spec = _spec(
        params=(
            ParamSpec(
                name="coverage",
                kind="enum",
                enum=("annual",),
                query_key="cf",
                value_map=(("annual", "A"),),
            ),
        )
    )
    assert encode_query(_query(coverage="annual"), spec) == {"cf": "A"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "int", "value_map": (("1", "01"),)}, "may map values only"),
        (
            {"kind": "enum", "enum": ("a", "b"), "value_map": (("a", "A"),)},
            "declare no upstream spelling",
        ),
        (
            {"kind": "str", "value_map": (("a", "A"), ("a", "B"))},
            "maps the same caller value twice",
        ),
        ({"kind": "str", "value_map": (("a",),)}, "(caller, upstream)"),
        ({"kind": "str", "value_map": (("a", ""),)}, "(caller, upstream)"),
    ],
    ids=["wrong-kind", "partial-enum", "duplicate-origin", "short-pair", "blank-target"],
)
def test_malformed_value_map_refuses_at_construction(kwargs: dict, message: str) -> None:
    """A map that could not be honoured is a record defect, caught when the record is written."""
    with pytest.raises(ValueError, match=message):
        ParamSpec(name="coverage", **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- composition


def _component(source: str, *, label: str = "", **overrides: object) -> KeyComponent:
    """One composition position with the SDMX defaults a task author would write."""
    return KeyComponent(name=label or source or "wildcard", source=source, **overrides)  # type: ignore[arg-type]


def _dimension(components: tuple[KeyComponent, ...], separator: str = ".") -> CompositeKeySpec:
    """A key over the positions ``components`` names, joined by ``separator``."""
    return CompositeKeySpec(name="dimension_key", separator=separator, components=components)


def _composite_spec(separator: str = ".") -> ModelSpec:
    """A model with one composed placeholder, one scalar one, and one query-only parameter."""
    components = (
        _component("freq"),
        _component("", label="wildcard", on_missing="default", default=""),
        _component("area"),
        _component("measure"),
    )
    return _spec(
        path="/data/{dimension_key}/{symbol}",
        params=(
            ParamSpec(name="freq", kind="str", required=True),
            ParamSpec(name="area", kind="str", required=True),
            ParamSpec(name="measure", kind="str", required=True),
            ParamSpec(name="symbol", kind="str", required=True),
            ParamSpec(name="format", kind="str", default="csv"),
        ),
        composite_keys=(_dimension(components, separator),),
    )


COMPOSED_QUERY = {
    "freq": "A",
    "area": "USA",
    "measure": "CPI",
    "symbol": "SP500",
    "format": "csv",
}


def test_composite_placeholder_joins_its_positions_in_order() -> None:
    """One query renders the composed segment and the scalar placeholder beside it."""
    assert render_path(_composite_spec(), _query(**COMPOSED_QUERY)) == "/data/A..USA.CPI/SP500"


def test_composite_components_are_not_also_sent_in_the_query_string() -> None:
    """The path consumed them; repeating them would make the request differ from the record.

    ``format`` is the control: it is a declared parameter no placeholder reads, so it must still be
    sent. An assertion that the whole query were empty would also pass if the engine dropped every
    parameter, and that is not the fact this test pins.
    """
    assert encode_query(_query(**COMPOSED_QUERY), _composite_spec()) == {"format": "csv"}


def test_comma_joined_resource_key_is_allowed_inside_one_segment() -> None:
    """The scalar regex omits the comma, so only a composition may join on it."""
    rendered = render_path(_composite_spec(separator=","), _query(**COMPOSED_QUERY))
    assert rendered == "/data/A,,USA,CPI/SP500"


def test_scalar_placeholder_still_refuses_a_value_that_opens_a_segment() -> None:
    """The pre-existing guard is unchanged by the composition branch."""
    with pytest.raises(ProviderEngineError) as caught:
        render_path(_composite_spec(), _query(**{**COMPOSED_QUERY, "symbol": "a/b"}))
    assert caught.value.code == f"{PREFIX}_PARAM_UNENCODABLE"


def test_refusing_position_surfaces_the_model_error_prefix() -> None:
    """A missing required position is the model's own code, attributable to the guard."""
    refusing = _component("freq", on_missing="refuse")
    spec = _spec(
        path="/data/{dimension_key}",
        params=(ParamSpec(name="freq", kind="str"),),
        composite_keys=(_dimension((refusing,)),),
    )
    with pytest.raises(ProviderEngineError) as caught:
        render_path(spec, _query(freq=None))
    assert caught.value.code == f"{PREFIX}_KEY_COMPOSITION_INVALID"
    assert "missing_component" in caught.value.rejected


def test_outer_guard_refuses_a_joined_segment_that_could_break_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel's charset is not the last word: the seam re-checks the joined text.

    An "anything" charset is forged here so a position may publish ``?``. Without the seam's own
    ``_SEGMENT_BREAKING`` check the composed URL would carry a query string the declaration never
    described; this arm is what shows that guard is live and not unreachable code.
    """
    monkeypatch.setitem(key_composition.SEGMENT_CHARSETS, "anything", re.compile(r".*\Z"))
    components = (
        KeyComponent(name="probe", source="probe", charset="anything"),
        KeyComponent(name="tail", source="tail", charset="anything"),
    )
    spec = _spec(
        path="/data/{dimension_key}",
        params=(ParamSpec(name="probe", kind="str"), ParamSpec(name="tail", kind="str")),
        composite_keys=(_dimension(components, separator=";"),),
    )
    with pytest.raises(ProviderEngineError) as caught:
        render_path(spec, _query(probe="A", tail="?format=csv"))
    assert caught.value.code == f"{PREFIX}_KEY_COMPOSITION_INVALID"
    assert "path_structure" in caught.value.rejected


@pytest.mark.parametrize(
    ("path", "key_name", "components", "params", "message"),
    [
        (
            "/data/ok",
            "dimension_key",
            (_component("freq"),),
            (ParamSpec(name="freq", kind="str", required=True),),
            "spells no path placeholder",
        ),
        (
            "/data/{symbol}",
            "symbol",
            (_component("freq"),),
            (
                ParamSpec(name="freq", kind="str", required=True),
                ParamSpec(name="symbol", kind="str", required=True),
            ),
            "collides with a parameter name",
        ),
        (
            "/data/{dimension_key}",
            "dimension_key",
            (_component("ghost"),),
            (ParamSpec(name="freq", kind="str", required=True),),
            "does not declare as a parameter",
        ),
    ],
    ids=["unreferenced", "name-collision", "unbound-source"],
)
def test_composition_declaration_refuses_at_construction(
    path: str,
    key_name: str,
    components: tuple[KeyComponent, ...],
    params: tuple[ParamSpec, ...],
    message: str,
) -> None:
    """A composition that cannot run is caught while the record is written, not on first query."""
    with pytest.raises(ValueError, match=message):
        _spec(
            path=path,
            params=params,
            composite_keys=(CompositeKeySpec(name=key_name, components=components),),
        )


def test_composite_placeholder_exempts_the_scalar_required_rule() -> None:
    """The composed name is not a parameter, so it cannot be "required or defaulted" as one."""
    wildcard = KeyComponent(name="wildcard", source="", on_missing="default", default="0")
    spec = _spec(
        path="/data/{dimension_key}",
        params=(ParamSpec(name="unused", kind="str"),),
        composite_keys=(_dimension((wildcard,)),),
    )
    assert render_path(spec, _query()) == "/data/0"
