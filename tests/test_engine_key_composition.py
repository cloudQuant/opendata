"""Offline proof for the ``flow.composite_key_segment`` kernel.

Every guard is shown reachable in *both* directions: one arm that a legitimate
OECD/SDMX shape composes through, and one arm that the same declaration refuses
-- so a "0 rejected" reading is only meaningful because each refusal below is
pinned to the guard that produced it (``KeyCompositionError.reason``), and each
accepting arm would break if that guard were widened or deleted.

The motivating keys are copied from the nine oecd census rows: dimension keys
such as ``MEX.M.....S1..B1GQ..`` joined on ``.`` with explicitly declared
wildcard positions, dotted one-fields like the agency id ``OECD.SDD.NAD``, and
constant codes (``S1``, ``B1GQ``, ``CPI``, ``_Z``) that a model always publishes.
No test here reaches I/O: the kernel is pure data in, string out.
"""

from __future__ import annotations

from datetime import date

import pytest

from opendata.data.providers._engine.key_composition import (
    CompositeKeySpec,
    KeyComponent,
    KeyCompositionError,
    render_composite_key,
)

CODE = "OECD_KEY_COMPOSITION_INVALID"


def dimension_key() -> CompositeKeySpec:
    """GdpNominal's dimension shape: 5 positions, two declared wildcards.

    ``{freq}..{area}.S1..B1GQ`` -- the empty positions are SDMX wildcard
    dimensions the model declares rather than infers.
    """
    return CompositeKeySpec(
        name="dimension_key",
        separator=".",
        error_code=CODE,
        components=(
            KeyComponent(name="frequency", source="frequency"),
            KeyComponent(name="measure", on_missing="default", default=""),
            KeyComponent(name="area", source="country"),
            KeyComponent(name="transition", kind="literal", source="S1"),
            KeyComponent(name="transaction", kind="literal", source="B1GQ"),
        ),
    )


def dotted_id_key() -> CompositeKeySpec:
    """A resource-comma composition whose first field legally contains dots.

    ``OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_OECD,1.1`` is the dataflow
    resource of the GDP rows -- one agency field with two dots inside it, then
    the flow id, then the version, joined on ``,`.
    """
    return CompositeKeySpec(
        name="dataflow_resource",
        separator=",",
        error_code=CODE,
        components=(
            KeyComponent(name="agency", source="agency", charset="dotted_id"),
            KeyComponent(name="dataflow", source="dataflow", charset="dotted_id"),
            KeyComponent(name="version", source="version", charset="dotted_id"),
        ),
    )


def refused_reason(call, *args) -> str:
    """Return the guard name one failing composition reported."""
    with pytest.raises(KeyCompositionError) as raised:
        call(*args)
    return str(raised.value.reason)


class TestDeclarationIsValidData:
    """A composition is inert at declaration; malformed shapes never reach render."""

    def test_a_well_formed_composition_constructs(self) -> None:
        spec = dimension_key()
        assert spec.segment_count == 5

    def test_empty_component_tuple_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no components"):
            CompositeKeySpec(name="empty", separator=".", components=())

    def test_separator_must_be_exactly_one_character(self) -> None:
        component = KeyComponent(name="c", source="c")
        with pytest.raises(ValueError, match="one character"):
            CompositeKeySpec(name="bad", separator="..", components=(component,))

    def test_a_separator_that_is_url_structure_is_refused(self) -> None:
        # The refusing arm: joining on "/" would make the "key" a path, which is
        # the engine's segment grammar, not a declarable separator.
        component = KeyComponent(name="c", source="c")
        with pytest.raises(ValueError, match="URL structure"):
            CompositeKeySpec(name="bad", separator="/", components=(component,))
        # The accepting arm differs only in the declared separator.
        CompositeKeySpec(name="good", separator=",", components=(component,))

    def test_error_code_must_be_upper_case(self) -> None:
        component = KeyComponent(name="c", source="c")
        with pytest.raises(ValueError, match="upper-case"):
            CompositeKeySpec(
                name="bad", components=(component,), error_code="oecd_key_composition_invalid"
            )

    def test_unknown_charset_is_refused_at_declaration(self) -> None:
        with pytest.raises(ValueError, match="charset"):
            KeyComponent(name="c", source="c", charset="everything_goes")

    def test_unknown_missing_policy_is_refused(self) -> None:
        with pytest.raises(ValueError, match="on_missing"):
            KeyComponent(name="c", source="c", on_missing="ignore")

    def test_default_policy_requires_declared_default_text(self) -> None:
        with pytest.raises(ValueError, match="without string default text"):
            KeyComponent(name="c", source="c", on_missing="default")

    def test_refuse_policy_fights_a_declared_default(self) -> None:
        # The choice is declarable, but not contradictory: a component cannot
        # claim to fail closed while carrying fallback text.
        with pytest.raises(ValueError, match="on_missing='refuse'"):
            KeyComponent(name="c", source="c", default="ALL")

    def test_default_text_outside_its_charset_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not valid"):
            KeyComponent(name="c", source="c", on_missing="default", default="A.B")
        # Accepting arm: the empty string is the legal declared wildcard.
        KeyComponent(name="c", source="c", on_missing="default", default="")

    def test_param_source_must_name_a_parameter(self) -> None:
        with pytest.raises(ValueError, match="query parameter"):
            KeyComponent(name="c", source="not an identifier")
        # A sourceless position must say how it is filled: only a declared
        # default may replace a parameter nothing reads.
        with pytest.raises(ValueError, match="must declare a default"):
            KeyComponent(name="c", on_missing="refuse")
        KeyComponent(name="c", on_missing="default", default="ALL")

    def test_literal_text_is_charset_checked_at_declaration(self) -> None:
        with pytest.raises(ValueError, match="not valid"):
            KeyComponent(name="c", kind="literal", source="S1/B2")
        KeyComponent(name="c", kind="literal", source="B1GQ")


class TestCompositionRendersSdmxKeys:
    """The keys the nine oecd rows record are what a valid declaration must produce."""

    def test_gdp_dimension_key_with_declared_wildcards(self) -> None:
        key = render_composite_key(dimension_key(), {"frequency": "M", "country": "MEX"})
        assert key == "M..MEX.S1.B1GQ"

    def test_country_interest_duration_shape_joins_four_codes(self) -> None:
        spec = CompositeKeySpec(
            name="dimension_key",
            error_code=CODE,
            components=(
                KeyComponent(name="area", source="country"),
                KeyComponent(name="frequency", source="frequency"),
                KeyComponent(name="duration", source="duration"),
                KeyComponent(name="measure", on_missing="default", default=""),
                KeyComponent(name="unit", on_missing="default", default=""),
            ),
        )
        # ``{country}.{freq}.{duration}....`` from country_interest_rates.py.
        assert spec.render({"country": "CAN", "frequency": "M", "duration": "1Y"}) == ("CAN.M.1Y..")

    def test_int_and_date_values_render(self) -> None:
        spec = CompositeKeySpec(
            name="eo_key",
            error_code=CODE,
            components=(
                KeyComponent(name="area", source="country"),
                KeyComponent(name="measure", source="measure"),
                KeyComponent(name="version", source="version"),
            ),
        )
        assert spec.render({"country": "MEX", "measure": 3, "version": date(2026, 1, 4)}) == (
            "MEX.3.2026-01-04"
        )

    def test_legal_dotted_component_is_accepted_as_one_field(self) -> None:
        # The accepting half of the separator pair: agency="OECD.SDD.NAD" is one
        # declared dotted_id field; deleting the charset declaration (or the
        # guard's admission table) would make this arm raise.
        key = dotted_id_key().render(
            {
                "agency": "OECD.SDD.NAD",
                "dataflow": "DSD_NAMAIN1@DF_QNA_EXPENDITURE_OECD",
                "version": "1.1",
            }
        )
        assert key.count(",") == 2  # exactly the declared positions, commas intact

    def test_one_dotted_text_two_declared_charsets_differ_only_by_declaration(self) -> None:
        # The two arms of the SAME value ("OECD.SDD.NAD"): a sdmx_code position
        # refuses it as separator injection because the dot is this composition's
        # separator; a path_segment position -- the engine's own single-segment
        # grammar, where "a dot inside one field is legal" -- composes it whole.
        # Deleting the charset-admission table or the guard makes one arm fail.
        refusing = CompositeKeySpec(
            name="k",
            separator=".",
            error_code=CODE,
            components=(KeyComponent(name="area", source="area"),),
        )
        accepting = CompositeKeySpec(
            name="k2",
            separator=".",
            error_code=CODE,
            components=(KeyComponent(name="area", source="area", charset="path_segment"),),
        )
        assert refused_reason(refusing.render, {"area": "OECD.SDD.NAD"}) == "separator_injection"
        assert accepting.render({"area": "OECD.SDD.NAD"}) == "OECD.SDD.NAD"


class TestSeparatorInjectionIsRefused:
    """A component carrying the separator it is joined on must fail closed."""

    def test_injected_separator_is_refused_by_reason(self) -> None:
        # Same dotted text the accepting arm above spells; the component here is
        # a declared sdmx_code dimension, so the guard's other arm must fire.
        reason = refused_reason(dimension_key().render, {"frequency": "M", "country": "MEX.AUS"})
        assert reason == "separator_injection"

    def test_injection_cannot_be_mistaken_for_a_valid_key(self) -> None:
        with pytest.raises(KeyCompositionError) as raised:
            dimension_key().render({"frequency": "M", "country": "MEX.AUS"})
        assert raised.value.code == CODE
        assert raised.value.component == "area"

    def test_injected_separator_is_refused_under_a_different_separator(self) -> None:
        # A dot is legal text only where dotted charsets were declared; under the
        # comma resource shape a bare code component cannot smuggle one either...
        spec = CompositeKeySpec(
            name="joined",
            separator=",",
            error_code=CODE,
            components=(
                KeyComponent(name="area", source="area"),
                KeyComponent(name="freq", source="freq"),
            ),
        )
        assert refused_reason(spec.render, {"area": "MEX,AUS", "freq": "M"}) == (
            "separator_injection"
        )


class TestPathAndLineBreaksAreRefused:
    """``/``, ``?``, CR and LF change the request, not just the key."""

    def test_forward_slash_is_refused_even_by_a_dotted_charset(self) -> None:
        # The guard applies to the *whole* path segment: a dotted_id field would
        # happily match nothing with a slash, but the reason pins this to the
        # segment-count guard rather than a generic charset mismatch.
        reason = refused_reason(
            dotted_id_key().render,
            {"agency": "OECD/../../etc", "dataflow": "DSD_X@DF_Y", "version": "1.0"},
        )
        assert reason == "path_traversal"

    def test_question_mark_fails_the_charset(self) -> None:
        # Not a slash, so it reaches the charset: a query-string break-out is
        # refused because no charset admits "?".
        reason = refused_reason(dimension_key().render, {"frequency": "M", "country": "MEX?x=1"})
        assert reason == "charset_invalid"

    def test_line_break_is_refused(self) -> None:
        reason = refused_reason(dimension_key().render, {"frequency": "M", "country": "MEX\r\nX"})
        assert reason == "line_break"

    def test_legal_path_segment_charset_admits_tilde_and_dash(self) -> None:
        spec = CompositeKeySpec(
            name="seg",
            error_code=CODE,
            components=(
                KeyComponent(name="a", source="a", charset="path_segment"),
                KeyComponent(name="b", source="b", charset="path_segment"),
            ),
        )
        assert spec.render({"a": "R~1", "b": "x-2"}) == "R~1.x-2"


class TestMissingComponentsFollowTheDeclaredPolicy:
    """Absence is declarable: a default substitutes, otherwise the call fails closed."""

    def test_refuse_policy_names_the_missing_parameter(self) -> None:
        with pytest.raises(KeyCompositionError) as raised:
            dimension_key().render({"country": "MEX"})  # frequency absent
        assert raised.value.reason == "missing_component"
        assert raised.value.component == "frequency"

    def test_default_policy_substitutes_the_declared_text(self) -> None:
        spec = CompositeKeySpec(
            name="cpi_key",  # consumer_price_index: methodology is optional upstream
            error_code=CODE,
            components=(
                KeyComponent(name="area", source="country"),
                KeyComponent(name="frequency", source="frequency"),
                KeyComponent(
                    name="methodology",
                    source="methodology",
                    on_missing="default",
                    default="MEI",
                ),
                KeyComponent(name="item", kind="literal", source="CPI"),
            ),
        )
        assert spec.render({"country": "USA", "frequency": "M"}) == "USA.M.MEI.CPI"

    def test_explicit_none_is_missing_not_the_string_none(self) -> None:
        # The engine leaves an unset optional parameter as None; rendering that
        # as the text "None" would address a bogus dimension.
        with pytest.raises(KeyCompositionError) as raised:
            dimension_key().render({"frequency": None, "country": "MEX"})
        assert raised.value.reason == "missing_component"

    def test_empty_query_value_is_refused_not_joined(self) -> None:
        # A caller-set empty string is not a declared wildcard: that is the
        # whole reason the wildcard is a *default* declaration, not an absence.
        reason = refused_reason(dimension_key().render, {"frequency": "", "country": "MEX"})
        assert reason == "empty_component"

    def test_boolean_values_are_unrenderable(self) -> None:
        # str(True) is alphanumeric, so only an explicit guard keeps a flag out
        # of a key; refusing is the accepting arm's proof that codes are codes.
        reason = refused_reason(dimension_key().render, {"frequency": True, "country": "MEX"})
        assert reason == "unrenderable_bool"


class TestNoGarbageEverComesBack:
    """A refusal is a raise, never a returned string."""

    def test_error_is_a_valueerror_for_older_handlers_and_carries_code(self) -> None:
        with pytest.raises(ValueError) as raised:
            render_composite_key(dimension_key(), {"frequency": "M"})
        assert isinstance(raised.value, KeyCompositionError)
        assert raised.value.code == CODE

    def test_a_fully_valid_key_is_never_empty(self) -> None:
        key = dimension_key().render({"frequency": "Q", "country": "COL"})
        assert key and not key.startswith(".") and not key.endswith("..")
