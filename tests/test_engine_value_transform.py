"""Offline acceptance suite for the declared column casts and rescales.

Every guard in :mod:`opendata.data.providers._engine.value_transform` is shown on both
sides here: a positive arm proving the declared conversion or scale works, and a refusal
arm proving a wrongly-shaped input raises instead of yielding ``None``/``NaN``. The
float-drift arms exist so the suite fails if someone swaps the ``Decimal`` path for
binary floats, which is the exactness the 1/1000-strike and published-percent rows
were recorded as needing.

No case touches I/O: values are the shapes the census rows record (Tiingo/Tradier epoch
milliseconds, the Fed's published percents, TreasuryAuctions' ``%m/%d/%Y`` dates,
Deribit/Tradier strike scaling) handed to the kernel directly.

The last three classes are the wiring, not the kernel: the same casts and scales declared as
``ModelSpec.column_derives`` plans and run by ``normalize_record`` where records become rows.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from opendata.data.providers._engine.http_json import (
    build_query_model,
    build_row_model,
    normalize_record,
)
from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.value_transform import (
    CastSpec,
    DerivePlan,
    DeriveStage,
    ScaleSpec,
    ValueTransformError,
    apply_cast,
    apply_scale,
    project_column,
    resolve_output_units,
    transform_value,
)
from opendata.data.providers.federal_reserve.specs import (
    MONEY_MEASURES,
    SVENSSON_YIELD_CURVE,
    TREASURY_RATES,
)

#: 2024-07-04T00:00:00Z -- 2024-07-03 20:00 America/New_York: the day itself moves by zone.
EPOCH_MS_MIDNIGHT_UTC = 1720051200000
#: 2024-07-04T12:00:00Z, 08:00 in New York: same calendar day under both zones.
EPOCH_MS_NOON_UTC = 1720080000000

EPOCH_S_NOON_UTC = EPOCH_MS_NOON_UTC // 1000


# --------------------------------------------------------------------------- epoch casts


def test_epoch_ms_to_date_uses_the_declared_timezone() -> None:
    """The published instant is projected onto the declared zone's calendar day."""
    utc_cast = CastSpec.epoch_ms_to_date("UTC")
    ny_cast = CastSpec.epoch_ms_to_date("America/New_York")
    assert apply_cast(utc_cast, EPOCH_MS_MIDNIGHT_UTC) == date(2024, 7, 4)
    assert apply_cast(ny_cast, EPOCH_MS_MIDNIGHT_UTC) == date(2024, 7, 3)


def test_epoch_ms_to_datetime_returns_the_declared_zone_aware_moment() -> None:
    """Tradier/Tiingo intraday arms get a zoned datetime, never a naive local one."""
    cast = CastSpec.epoch_ms_to_datetime("America/New_York")
    moment = apply_cast(cast, EPOCH_MS_MIDNIGHT_UTC)
    assert isinstance(moment, datetime)
    assert moment.utcoffset() is not None
    assert moment.tzinfo is not None and str(moment.tzinfo) == "America/New_York"
    assert moment.hour == 20 and moment.minute == 0


def test_epoch_s_to_date_positive_arm() -> None:
    """A seconds-unit epoch (Benzinga ``last_updated``) casts like the ms arms."""
    assert apply_cast(CastSpec(kind="epoch_s_to_date", timezone="UTC"), EPOCH_S_NOON_UTC) == date(
        2024, 7, 4
    )


def test_epoch_cast_without_timezone_is_refused_at_declaration() -> None:
    """Machine-local conversion is unreachable: no tz (or ``local``) never constructs."""
    with pytest.raises(ValueError, match="timezone"):
        CastSpec(kind="epoch_ms_to_date")
    with pytest.raises(ValueError, match="timezone"):
        CastSpec.epoch_ms_to_date("local")
    with pytest.raises(ValueError, match="IANA"):
        CastSpec.epoch_ms_to_date("Mars/Phobos")


def test_epoch_cast_refuses_wrongly_shaped_inputs() -> None:
    """A float ms, a string ms, a bool and junk all raise -- none becomes a silent None."""
    cast = CastSpec.epoch_ms_to_datetime("UTC")
    for bad in (1720051200000.5, "not-an-epoch", True, None, [1, 2]):
        with pytest.raises(ValueError, match="epoch"):
            apply_cast(cast, bad)
    # A clean numeric string is the one string the census shapes legitimately publish.
    assert apply_cast(cast, str(EPOCH_MS_MIDNIGHT_UTC)) == apply_cast(cast, EPOCH_MS_MIDNIGHT_UTC)


def test_epoch_float_input_never_round_trips_through_float() -> None:
    """Even a float that is exactly an integer is refused; ms must arrive as an integer."""
    with pytest.raises(ValueError, match="integer"):
        apply_cast(CastSpec.epoch_ms_to_date("UTC"), float(EPOCH_MS_MIDNIGHT_UTC))


# ---------------------------------------------------------------------- numeric and date


def test_numeric_string_casts_to_decimal_exactly() -> None:
    """The Fed's published ``"4.33"`` arrives as Decimal(\"4.33\"), not a widened float."""
    assert apply_cast(CastSpec(kind="numeric_to_decimal"), "4.33") == Decimal("4.33")
    assert apply_cast(CastSpec(kind="numeric_to_decimal"), 433) == Decimal("433")
    assert apply_cast(CastSpec(kind="numeric_to_decimal"), Decimal("0.05")) == Decimal("0.05")


@pytest.mark.parametrize("bad", ["", "   ", "1,000", "abc", "NaN", "Infinity", True, 3.5, None])
def test_numeric_cast_refuses_values_outside_the_declared_shape(bad: object) -> None:
    """A wrongly-shaped numeric string raises; it is never nulled or NaN'd through."""
    with pytest.raises(ValueTransformError):
        apply_cast(CastSpec(kind="numeric_to_decimal"), bad)


def test_numeric_to_float_is_a_declared_domain_not_a_drift_habit() -> None:
    """The float cast exists for float-armed columns and still refuses shape failures."""
    assert apply_cast(CastSpec(kind="numeric_to_float"), "4.33") == pytest.approx(4.33)
    with pytest.raises(ValueError):
        apply_cast(CastSpec(kind="numeric_to_float"), "4.33%")


def test_published_date_string_recast_under_declared_format() -> None:
    """TreasuryAuctions publishes ``%m/%d/%Y``; the ISO date is the projection target."""
    cast = CastSpec.date_string("%m/%d/%Y")
    assert apply_cast(cast, "07/04/2024") == date(2024, 7, 4)


def test_date_string_refuses_the_undecorated_iso_shape() -> None:
    """The mismatched format raises rather than being accepted by a lenient parser."""
    with pytest.raises(ValueError, match="does not match declared format"):
        apply_cast(CastSpec.date_string("%m/%d/%Y"), "2024-07-04")
    with pytest.raises(ValueError, match="must be a string"):
        apply_cast(CastSpec.date_string("%m/%d/%Y"), 20240704)
    with pytest.raises(ValueError, match="input format"):
        CastSpec.date_string("")


# ---------------------------------------------------------------------------- bool casts


def test_bool_cast_reads_only_the_declared_spellings() -> None:
    """Case-folded spelling sets decide, so ``Yes``/``no`` are declared and honoured."""
    cast = CastSpec.bool_spellings(("yes", "true"), ("no", "false"))
    assert apply_cast(cast, "Yes") is True
    assert apply_cast(cast, " false ") is False


def test_bool_cast_refuses_undeclared_spellings_and_raw_bools() -> None:
    """A published ``maybe`` or a raw JSON bool raises; the spelling set is exhaustive."""
    cast = CastSpec.bool_spellings(("yes",), ("no",))
    for bad in ("maybe", "1", True, 1):
        with pytest.raises(ValueError):
            apply_cast(cast, bad)


def test_bool_declaration_rejects_ambiguous_spelling_sets() -> None:
    """One spelling both true and false (or an empty set) is refused at construction."""
    with pytest.raises(ValueError, match="true and false"):
        CastSpec.bool_spellings(("Y", "yes"), ("yES", "n"))
    with pytest.raises(ValueError, match="true and its false"):
        CastSpec.bool_spellings(("yes",), ())


# ------------------------------------------------------------------------------ rescale


def test_percent_rescale_is_exact_where_float_drifts() -> None:
    """0.29 published as a percent scales to Decimal 0.0029; the float path cannot say that.

    If someone replaces the Decimal arithmetic with ``float(value) / 100``, the first
    assertion fails on the type or the exactness and the drift witness below shows why:
    ``0.29/100`` as a float is not 0.0029, and Decimal(float) exposes every stray bit.
    """
    scaled = apply_scale(ScaleSpec.percent_to_fraction(), "0.29")
    assert scaled == Decimal("0.0029")
    assert isinstance(scaled, Decimal)
    # Drift witness: the exact binary value of the float path is not 0.0029, and a float
    # result would fail the cross-type comparison above as well as the isinstance arm.
    assert Decimal(float("0.29") / 100) != Decimal("0.0029")


def test_per_mille_strike_rescale_keeps_exactness() -> None:
    """A 1/1000-scale strike of 100005 milli-units is exactly 100.005, not a hair off."""
    strike = ScaleSpec.strike_per_mille(output_units="USD")
    scaled = apply_scale(strike, 100005)
    assert scaled == Decimal("100.005")
    assert isinstance(scaled, Decimal)
    # Drift witness: the float path's exact binary value is not 100.005.
    assert Decimal(100005 / 1000.0) != Decimal("100.005")
    # And multiplying the scaled strike back (the declaration's inverse) restores it.
    assert apply_scale(strike.inverse(), scaled) == Decimal("100005")


def test_basis_points_rescale_to_fraction_and_to_percent() -> None:
    """433 bps declares as a fraction 4.33/100; both preset and custom spellings work."""
    assert apply_scale(ScaleSpec.basis_points_to_fraction(), "433") == Decimal("0.0433")
    bps_to_percent = ScaleSpec(factor="100", output_units="percent", source_rule="bps_to_percent")
    assert apply_scale(bps_to_percent, 433) == Decimal("4.33")


def test_scale_refuses_float_and_bool_inputs() -> None:
    """A raw float never enters the scale arithmetic; declare a numeric cast first."""
    percent = ScaleSpec.percent_to_fraction()
    with pytest.raises(ValueError, match="float"):
        apply_scale(percent, 0.29)
    with pytest.raises(ValueError, match="boolean"):
        apply_scale(percent, True)


def test_inexact_scale_without_a_declared_rounding_raises() -> None:
    """Dividing by 3 does not terminate; an undeclared rounding is the ambiguity refused."""
    thirds = ScaleSpec(factor="3", output_units="x")
    with pytest.raises(ValueError, match="rounding"):
        apply_scale(thirds, 1)
    # multiply-by-0.5 stays exact, so no rounding is needed and none is asked for.
    halve = ScaleSpec(factor="0.5", direction="multiply", output_units="x")
    assert apply_scale(halve, 7) == Decimal("3.5")


def test_declared_rounding_decides_the_half_boundary() -> None:
    """1/8 = 0.125 at two places: HALF_UP crosses to 0.13, HALF_EVEN banks to 0.12."""
    half_up = ScaleSpec(
        factor="8", output_units="x", rounding="ROUND_HALF_UP", places=2, source_rule="eighths"
    )
    half_even = ScaleSpec(
        factor="8", output_units="x", rounding="ROUND_HALF_EVEN", places=2, source_rule="eighths"
    )
    assert apply_scale(half_up, 1) == Decimal("0.13")
    assert apply_scale(half_even, 1) == Decimal("0.12")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"factor": "100", "output_units": ""},  # a rescaled value with no declared unit
        {"factor": "1", "output_units": "x"},  # a no-op scale is an ambiguity
        {"factor": "0", "output_units": "x"},  # a scale that erases the value
        {"factor": "-100", "output_units": "x"},
        {"factor": "NaN", "output_units": "x"},
        {"factor": "100", "direction": "sideways", "output_units": "x"},
        {"factor": "3", "output_units": "x", "rounding": "ROUND_HALF_UP"},  # places missing
        {"factor": "3", "output_units": "x", "places": 2},  # rounding missing
        {"factor": "3", "output_units": "x", "rounding": "ROUND_MADNESS", "places": 2},
    ],
)
def test_void_scale_declarations_are_refused_at_construction(kwargs: dict[str, object]) -> None:
    """Every ambiguous or unrunnable scale fails when the record is written."""
    with pytest.raises(ValueError):
        ScaleSpec(**kwargs)  # type: ignore[arg-type]


def test_float_scale_factor_cannot_be_declared() -> None:
    """The factor is a decimal string; a float factor is refused, drift and all."""
    with pytest.raises(ValueError, match="float"):
        ScaleSpec(factor=0.1, output_units="x")  # type: ignore[arg-type]


def test_scale_record_states_factor_and_direction_and_inverts() -> None:
    """Invertibility-by-declaration: the record itself carries the reversing scale."""
    percent = ScaleSpec.percent_to_fraction()
    inverse = percent.inverse()
    assert (inverse.factor, inverse.direction) == ("100", "multiply")
    assert (percent.factor, percent.direction) == ("100", "divide")
    original = Decimal("4.33")
    assert apply_scale(inverse, apply_scale(percent, original)) == original


# ------------------------------------------------------------------- units and composition


def test_units_are_written_only_from_the_scale_declaration() -> None:
    """Unscaled columns keep published units verbatim; scaled ones carry the declared unit."""
    percent = ScaleSpec.percent_to_fraction()
    assert resolve_output_units("percent", None) == "percent"
    assert resolve_output_units("percent", percent) == "fraction"


def test_project_column_composes_cast_then_scale() -> None:
    """The Fed SOFR row: published string percent -> Decimal fraction + declared units."""
    cast = CastSpec(kind="numeric_to_decimal")
    value, units = project_column(
        "4.33", cast=cast, scale=ScaleSpec.percent_to_fraction(), column_units="percent"
    )
    assert value == Decimal("0.0433")
    assert units == "fraction"


def test_transform_value_cast_failure_raises_before_the_scale_runs() -> None:
    """A shape failure in the cast is not papered over by the rescale arm."""
    with pytest.raises(ValueError, match="decimal"):
        transform_value("4.33%", cast=CastSpec(kind="numeric_to_decimal"), scale=None)
    percent = ScaleSpec.percent_to_fraction()
    with pytest.raises(ValueError, match="float"):
        transform_value(4.33, scale=percent)


# ---------------------------------------------------------------- the plan at the spec plane
#
# Everything above judges the kernel alone. These cases are the wiring: a declared
# ``ModelSpec.column_derives`` plan names the column it produces, the source its raw value comes
# from and the stages run on it, and ``normalize_record`` is the seam where records become rows.
# Both reachable refusals are shown -- the declaration-time one (a plan spelling a name this model
# does not declare) and the fetch-time one (a published cell the declared cast refuses).


def _spec(
    columns: tuple[ColumnSpec, ...],
    *,
    derives: tuple[DerivePlan, ...] = (),
    params: tuple[ParamSpec, ...] = (),
) -> ModelSpec:
    """Build a minimal declaration whose domain is unregistered, so rows are declared columns."""
    return ModelSpec(
        model="derive_probe",
        domain="engine_value_transform_probe",
        asset_class="equity",
        period="d",
        market="us",
        base_url="https://example.invalid",
        path="/probe",
        columns=columns,
        params=params,
        scenario="离线探针：声明面派生列的接线与拒绝",
        error_prefix="PROBE",
        column_derives=derives,
    )


def _row(spec: ModelSpec, record: dict[str, object], query: object = None) -> Any:
    """Normalize one record through the seam the declaration generates."""
    return normalize_record(record, None, spec, build_row_model(spec), query)


class TestDeclaredDeriveStages:
    """POSITIVE: each stage kind and each source kind the plan binds."""

    def test_a_cast_stage_computes_a_date_from_a_published_period_cell(self) -> None:
        """The ``2024-03`` blocker: the column is a date no declared kind could read verbatim."""
        spec = _spec(
            (ColumnSpec("month", "date", source_key="Period", required=True, nullable=False),),
            derives=(
                DerivePlan(
                    column="month",
                    stages=(DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m")),),
                ),
            ),
        )
        assert _row(spec, {"Period": "2024-03"}).month == date(2024, 3, 1)

    def test_a_scale_stage_restates_a_published_percent_as_a_fraction(self) -> None:
        """The published figure is rescaled in the Decimal domain before the row is built."""
        spec = _spec(
            (ColumnSpec("yield_rate", "float", source_key="Yield", units="percent"),),
            derives=(
                DerivePlan(
                    column="yield_rate",
                    stages=(DeriveStage(kind="scale", scale=ScaleSpec.percent_to_fraction()),),
                ),
            ),
        )
        assert _row(spec, {"Yield": "4.33"}).yield_rate == pytest.approx(0.0433)

    def test_a_plan_runs_cast_then_scale_in_the_kernels_own_order(self) -> None:
        """Both stages on one column: "0.29" -> Decimal -> fraction, the SOFR-shaped row."""
        spec = _spec(
            (ColumnSpec("rate", "float", source_key="RAW"),),
            derives=(
                DerivePlan(
                    column="rate",
                    stages=(
                        DeriveStage(kind="cast", cast=CastSpec(kind="numeric_to_decimal")),
                        DeriveStage(kind="scale", scale=ScaleSpec.percent_to_fraction()),
                    ),
                ),
            ),
        )
        assert _row(spec, {"RAW": "0.29"}).rate == pytest.approx(0.0029)

    def test_a_plan_can_read_a_key_the_model_publishes_no_column_for(self) -> None:
        """The source override is a record key, not a second column."""
        spec = _spec(
            (ColumnSpec("settled_on", "date"),),
            derives=(
                DerivePlan(
                    column="settled_on",
                    source="settle_date",
                    stages=(DeriveStage(kind="cast", cast=CastSpec.date_string("%m/%d/%Y")),),
                ),
            ),
        )
        row = _row(spec, {"settle_date": "07/04/2024", "issue_date": "07/02/2024"})
        assert row.settled_on == date(2024, 7, 4)
        assert set(row.model_fields) == {"settled_on"}

    def test_a_parameter_source_stamps_the_query_onto_every_row(self) -> None:
        """The intrinio shape: ``symbol`` is not in the record, so the declaration supplies it."""
        spec = _spec(
            (
                ColumnSpec("symbol", "str", required=True, nullable=False),
                ColumnSpec("amount", "float"),
            ),
            derives=(DerivePlan(column="symbol", source_kind="parameter", source="symbol"),),
            params=(ParamSpec("symbol", "str", required=True),),
        )
        query = build_query_model(spec).model_validate({"symbol": "AAPL"})
        rows = [
            _row(spec, {"amount": 1.5}, query),
            _row(spec, {"amount": 2.0}, query),
        ]
        assert [row.symbol for row in rows] == ["AAPL", "AAPL"]

    def test_a_constant_source_publishes_what_no_record_carries(self) -> None:
        """A fact the endpoint never states becomes a declared column."""
        spec = _spec(
            (ColumnSpec("country", "str", required=True, nullable=False),),
            derives=(DerivePlan(column="country", source_kind="constant", source="US"),),
        )
        assert _row(spec, {}).country == "US"

    def test_a_constant_can_be_a_declared_date(self) -> None:
        """Stamped text still runs the declared stages."""
        spec = _spec(
            (ColumnSpec("vintage", "date"),),
            derives=(
                DerivePlan(
                    column="vintage",
                    source_kind="constant",
                    source="2024-03",
                    stages=(DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m")),),
                ),
            ),
        )
        assert _row(spec, {}).vintage == date(2024, 3, 1)


class TestDeriveDeclarationRefusals:
    """REFUSAL: a plan the record cannot carry out is refused when it is written."""

    def test_a_plan_naming_an_undeclared_column_refuses_at_declaration(self) -> None:
        """The reachable spec-plane arm: the row model has no field to write."""
        stage = DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m"))
        with pytest.raises(ValueError, match="does not declare"):
            _spec(
                (ColumnSpec("month", "date"),),
                derives=(DerivePlan(column="mnth", stages=(stage,)),),
            )

    def test_a_plan_reading_an_undeclared_parameter_refuses_at_declaration(self) -> None:
        """A stamp whose parameter does not exist would write None onto every row forever."""
        with pytest.raises(ValueError, match="does not"):
            _spec(
                (ColumnSpec("symbol", "str"),),
                derives=(DerivePlan(column="symbol", source_kind="parameter", source="ticker"),),
            )

    def test_an_unknown_stage_kind_refuses_at_declaration(self) -> None:
        """The kernel runs a cast and a rescale; a plan claiming more never reaches a fetch."""
        with pytest.raises(ValueError, match="stage kind"):
            DeriveStage(kind="pivot")

    def test_an_unknown_source_kind_refuses_at_declaration(self) -> None:
        """Only record, parameter and constant sources are routed."""
        with pytest.raises(ValueError, match="source kind"):
            DerivePlan(column="month", source_kind="envelope", source="Time Period")

    def test_a_stage_payload_that_is_not_its_kind_refuses_at_declaration(self) -> None:
        """A "cast" stage handing over a ScaleSpec is the typo the kind spelling exists to catch."""
        with pytest.raises(ValueError, match="CastSpec"):
            DeriveStage(kind="cast", scale=ScaleSpec.percent_to_fraction())

    def test_stages_outside_the_kernels_composition_order_refuse(self) -> None:
        """cast-then-scale is the only order transform_value documents, and it is enforced."""
        cast = DeriveStage(kind="cast", cast=CastSpec(kind="numeric_to_decimal"))
        scale = DeriveStage(kind="scale", scale=ScaleSpec.percent_to_fraction())
        with pytest.raises(ValueError, match="composition order"):
            DerivePlan(column="rate", stages=(scale, cast))
        with pytest.raises(ValueError, match="twice"):
            DerivePlan(column="rate", stages=(scale, scale))

    def test_a_plan_that_computes_nothing_refuses_at_declaration(self) -> None:
        """No stages and no source override is a rename wearing a derive plan's clothes."""
        with pytest.raises(ValueError, match="computes nothing"):
            DerivePlan(column="month")

    def test_two_plans_for_one_column_refuse_at_declaration(self) -> None:
        """One column has one producing plan, or the row depends on iteration order."""
        stage = DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m"))
        with pytest.raises(ValueError, match="two plans"):
            _spec(
                (ColumnSpec("month", "date"),),
                derives=(
                    DerivePlan(column="month", stages=(stage,)),
                    DerivePlan(column="month", source="other", stages=(stage,)),
                ),
            )

    def test_a_derived_column_cannot_also_be_an_envelope_copy(self) -> None:
        """The value would be written by the plan and then by the response, and only one is true."""
        stage = DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m"))
        with pytest.raises(ValueError, match="envelope"):
            ModelSpec(
                model="derive_probe",
                domain="engine_value_transform_probe",
                asset_class="equity",
                period="d",
                market="us",
                base_url="https://example.invalid",
                path="/probe",
                columns=(ColumnSpec("month", "date", source_key="Time Period"),),
                row_envelope=("month",),
                scenario="离线探针：派生列与信封复制互斥",
                error_prefix="PROBE",
                column_derives=(DerivePlan(column="month", stages=(stage,)),),
            )

    def test_a_published_cell_the_cast_does_not_describe_raises_the_kernels_error(
        self,
    ) -> None:
        """Fetch-time refusal keeps ValueTransformError, as _expand_rows keeps RowExpansionError."""
        spec = _spec(
            (ColumnSpec("month", "date", source_key="Period"),),
            derives=(
                DerivePlan(
                    column="month",
                    stages=(DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m")),),
                ),
            ),
        )
        with pytest.raises(ValueTransformError, match="does not match declared format"):
            _row(spec, {"Period": "ND"})


class TestEmptyDeriveLeavesRowsByteIdentical:
    """The default arm: a declaration naming no derive behaves exactly as the engine did."""

    def test_the_new_field_defaults_empty_for_every_declaration_on_disk(self) -> None:
        """No production declaration is reinterpreted by the field landing."""
        assert MONEY_MEASURES.column_derives == ()
        assert TREASURY_RATES.column_derives == ()
        assert SVENSSON_YIELD_CURVE.column_derives == ()

    def test_an_undated_column_is_copied_verbatim_including_the_missing_token(self) -> None:
        """The Fed's "ND" still reaches the row untouched -- no cast, no null, no refusal."""
        row = _row(
            _spec((ColumnSpec("month", "str", source_key="Time Period"),)),
            {"Time Period": "ND"},
        )
        assert row.month == "ND"

    def test_a_record_source_plan_with_the_columns_own_key_is_the_only_change(self) -> None:
        """One staged column differs; every other column of the same row is copied as before."""
        staged = _spec(
            (
                ColumnSpec("period", "date", source_key="Time Period"),
                ColumnSpec("m1", "str", source_key="M1_N.M"),
            ),
            derives=(
                DerivePlan(
                    column="period",
                    stages=(DeriveStage(kind="cast", cast=CastSpec.date_string("%Y-%m")),),
                ),
            ),
        )
        record = {"Time Period": "1959-01", "M1_N.M": "ND"}
        assert _row(staged, record).m1 == "ND"
        assert _row(staged, record).period == date(1959, 1, 1)
