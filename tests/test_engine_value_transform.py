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
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from opendata.data.providers._engine.value_transform import (
    CastSpec,
    ScaleSpec,
    ValueTransformError,
    apply_cast,
    apply_scale,
    project_column,
    resolve_output_units,
    transform_value,
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
