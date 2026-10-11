"""Declared value casts and rescales for engine output columns.

The engine today enforces a column's *type* but converts nothing:
``_column_type`` in :mod:`~opendata.data.providers._engine.http_json` maps a
declared kind onto one pydantic domain, so a published epoch-milliseconds
integer never becomes a date and a published ``"0.29"`` percent string never
becomes a fraction of ``Decimal("0.29")``. This module is the pure
transformation kernel for the two column-seam capabilities the census asked
for (``columns.cast`` and ``columns.rescale``): a frozen declaration in, a
transformed value out, no I/O and no engine mutation. The integration into
:class:`~opendata.data.providers._engine.spec.ColumnSpec` is owned elsewhere;
every rule here stands alone so the seam can be wired once the declaration
fields land.

Fail-closed is the whole contract. A value that does not match the declared
input shape raises :class:`ValueTransformError` instead of yielding ``None``
or ``NaN`` -- the silent-null bug class this repo tracks -- and a rescale
that is not exactly representable raises unless the declaration carries its
own rounding rule and scale. Scale arithmetic happens in the quantity's own
domain (``Decimal`` or integer): floats are refused as inputs, factors are
published as decimal *strings*, and nothing round-trips through ``float()``.

Timing note for epoch casts: a published ``date`` means the calendar day in a
*declared* zone, so every epoch rule requires an explicit IANA timezone and
machine-local conversion is unreachable by construction.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from typing import Literal
from zoneinfo import ZoneInfo

#: The cast rules the census rows actually need. Anything outside this set is
#: a malformed declaration, not a rule to attempt at runtime.
CastKind = Literal[
    "epoch_ms_to_date",
    "epoch_ms_to_datetime",
    "epoch_s_to_date",
    "numeric_to_decimal",
    "numeric_to_float",
    "date_string_to_iso",
    "bool_from_spellings",
]

#: Cast kinds whose input is an epoch integer and whose declaration must carry a timezone.
_EPOCH_KINDS = frozenset({"epoch_ms_to_date", "epoch_ms_to_datetime", "epoch_s_to_date"})

#: How a scale applies to the published value: the record states factor *and* direction,
#: so the same declaration reads as "published percents divided by 100" or its inverse.
ScaleDirection = Literal["divide", "multiply"]

#: Rounding modes a rescale declaration may carry. Only these strings are accepted, so a
#: typo fails when the record is written rather than at the first inexact division.
_ROUNDING_MODES = frozenset(
    {
        "ROUND_UP",
        "ROUND_DOWN",
        "ROUND_CEILING",
        "ROUND_FLOOR",
        "ROUND_HALF_UP",
        "ROUND_HALF_DOWN",
        "ROUND_HALF_EVEN",
        "ROUND_05UP",
    }
)

#: Working precision for a scale division that the declaration says is rounded anyway.
#: Far above any published rate's significant digits, so the quantize step sees the true
#: expansion of the quotient and the boundary is decided by the declared mode alone.
_SCALE_PRECISION = 60

#: The shared epoch instant; conversion is integer-millisecond arithmetic, never a float
#: ``fromtimestamp``, and the result is only ever rendered in a *declared* zone.
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ValueTransformError(ValueError):
    """A value did not match its declared cast or scale, or the declaration itself is void.

    Every refusal in this kernel surfaces as this one code-stable error class so the engine
    seam (and the census-row tests built on it) can tell "the source published a shape the
    declaration does not describe" from an ordinary bug, exactly as ``ProviderEngineError``
    separates shape failures upstream.
    """


def _parse_exact_decimal(text: str, label: str) -> Decimal:
    """Return the ``Decimal`` spelled by ``text``, refusing anything not finitely written."""
    try:
        value = Decimal(text.strip())
    except decimal.InvalidOperation as error:
        raise ValueError(f"{label} {text!r} is not a decimal number") from error
    if not value.is_finite():
        raise ValueError(f"{label} {text!r} is not a finite decimal number")
    return value


@dataclass(frozen=True)
class CastSpec:
    """One declared conversion the engine applies to a published value.

    Attributes:
        kind: Which declared conversion to attempt; unknown kinds fail at construction.
        timezone: IANA zone name, required by every epoch kind and rejected elsewhere. A
            published date is the calendar day in *this* zone, never the machine's local one.
        input_format: ``strptime`` format the source publishes dates in, for
            ``date_string_to_iso`` only.
        true_spellings: Case-folded spellings that mean ``True``, for bool casts only.
        false_spellings: Case-folded spellings that mean ``False``, for bool casts only.
        note: Why the cast exists, for the review record.
    """

    kind: CastKind
    timezone: str = ""
    input_format: str = ""
    true_spellings: tuple[str, ...] = ()
    false_spellings: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        """Reject a cast declaration the kernel cannot carry out unambiguously."""
        if self.kind not in {
            "epoch_ms_to_date",
            "epoch_ms_to_datetime",
            "epoch_s_to_date",
            "numeric_to_decimal",
            "numeric_to_float",
            "date_string_to_iso",
            "bool_from_spellings",
        }:
            raise ValueError(f"unknown cast kind {self.kind!r}")
        if self.kind in _EPOCH_KINDS:
            _resolve_timezone(self.timezone)
            if self.input_format or self.true_spellings or self.false_spellings:
                raise ValueError(f"an epoch cast declares only a timezone, got {self!r}")
            return
        if self.kind == "date_string_to_iso":
            if not self.input_format.strip():
                raise ValueError("a date_string_to_iso cast must declare its input format")
            if self.timezone:
                raise ValueError("a published date string carries no zone to declare")
            return
        if self.kind == "bool_from_spellings":
            if not self.true_spellings or not self.false_spellings:
                raise ValueError(
                    "a bool cast must declare both its true and its false spelling set"
                )
            folded_true = {item.casefold() for item in self.true_spellings}
            folded_false = {item.casefold() for item in self.false_spellings}
            if "" in folded_true | folded_false:
                raise ValueError("a bool spelling cannot be blank")
            if folded_true & folded_false:
                overlap = sorted(folded_true & folded_false)
                raise ValueError(f"spellings {overlap} are declared both true and false")
            if self.timezone or self.input_format:
                raise ValueError("a bool cast declares no timezone and no date format")

    @classmethod
    def epoch_ms_to_date(cls, timezone: str, *, note: str = "") -> CastSpec:
        """Declare an epoch-milliseconds column as a calendar date in ``timezone``."""
        return cls(kind="epoch_ms_to_date", timezone=timezone, note=note)

    @classmethod
    def epoch_ms_to_datetime(cls, timezone: str, *, note: str = "") -> CastSpec:
        """Declare an epoch-milliseconds column as a zoned datetime in ``timezone``."""
        return cls(kind="epoch_ms_to_datetime", timezone=timezone, note=note)

    @classmethod
    def date_string(cls, input_format: str, *, note: str = "") -> CastSpec:
        """Declare a published date string that must be re-read under ``input_format``."""
        return cls(kind="date_string_to_iso", input_format=input_format, note=note)

    @classmethod
    def bool_spellings(
        cls, true_spellings: tuple[str, ...], false_spellings: tuple[str, ...], *, note: str = ""
    ) -> CastSpec:
        """Declare a bool column by the spellings the source actually publishes."""
        return cls(
            kind="bool_from_spellings",
            true_spellings=true_spellings,
            false_spellings=false_spellings,
            note=note,
        )


def _resolve_timezone(name: str) -> ZoneInfo:
    """Return the declared IANA zone, refusing blank, ``local`` and unresolvable names."""
    if not name or not name.strip() or name.strip().casefold() in {"local", "machine"}:
        raise ValueTransformError(f"an epoch cast needs an explicit IANA timezone, got {name!r}")
    try:
        return ZoneInfo(name.strip())
    except (KeyError, ValueError, OSError) as error:
        raise ValueTransformError(f"{name!r} is not a resolvable IANA timezone") from error


def _as_quantity(value: object, label: str) -> Decimal:
    """Return ``value`` as an exact ``Decimal``, refusing bools and floats outright."""
    if isinstance(value, bool):
        raise ValueTransformError(f"{label}: a published boolean is not a number")
    if isinstance(value, float):
        raise ValueTransformError(
            f"{label}: a float would drift; publish it as a string or declare a cast first"
        )
    if isinstance(value, Decimal):
        quantity = value
    elif isinstance(value, int):
        quantity = Decimal(value)
    elif isinstance(value, str):
        try:
            quantity = Decimal(value.strip())
        except decimal.InvalidOperation as error:
            raise ValueTransformError(f"{label}: {value!r} is not a decimal number") from error
    else:
        raise ValueTransformError(f"{label}: cannot read {value!r} as a number")
    if not quantity.is_finite():
        raise ValueTransformError(f"{label}: {value!r} is not a finite number")
    return quantity


def _as_epoch_integer(value: object, label: str) -> int:
    """Return a published epoch instant as an exact integer count, never a float."""
    if isinstance(value, (bool, float)):
        raise ValueTransformError(f"{label}: an epoch must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError as error:
            raise ValueTransformError(f"{label}: {value!r} is not an epoch integer") from error
    raise ValueTransformError(f"{label}: cannot read {value!r} as an epoch integer")


def _epoch_to_zoned(count: int, unit: str, zone: ZoneInfo) -> datetime:
    """Shift the UTC epoch by an integer count of ``unit`` and render it in ``zone``."""
    delta = timedelta(milliseconds=count) if unit == "ms" else timedelta(seconds=count)
    try:
        return (_EPOCH_UTC + delta).astimezone(zone)
    except OverflowError as error:
        raise ValueTransformError(
            f"epoch {count} ({unit}) is outside the representable range"
        ) from error


def apply_cast(cast: CastSpec, value: object) -> object:
    """Return ``value`` converted by the declared rule, or raise; never a silent ``None``.

    Args:
        cast: The declared conversion.
        value: The raw published value.

    Returns:
        A ``date``/``datetime``/``Decimal``/``float``/``bool`` per the declared kind.

    Raises:
        ValueTransformError: The value does not match the declared input shape.
    """
    label = f"cast {cast.kind}"
    if cast.kind in _EPOCH_KINDS:
        count = _as_epoch_integer(value, label)
        zone = _resolve_timezone(cast.timezone)
        unit = "s" if cast.kind == "epoch_s_to_date" else "ms"
        moment = _epoch_to_zoned(count, unit, zone)
        return moment.date() if cast.kind.endswith("to_date") else moment
    if cast.kind == "numeric_to_decimal":
        return _as_quantity(value, label)
    if cast.kind == "numeric_to_float":
        return float(_as_quantity(value, label))
    if cast.kind == "date_string_to_iso":
        if not isinstance(value, str):
            raise ValueTransformError(f"{label}: a published date must be a string, got {value!r}")
        try:
            return datetime.strptime(value.strip(), cast.input_format).date()
        except ValueError as error:
            raise ValueTransformError(
                f"{label}: {value!r} does not match declared format {cast.input_format!r}"
            ) from error
    # bool_from_spellings: the only rule left, judged on the declared spelling sets.
    if not isinstance(value, str):
        raise ValueTransformError(
            f"{label}: {value!r} is not spelled in the declared sets; a bool cast reads "
            "published strings only"
        )
    folded = value.strip().casefold()
    if folded in {item.casefold() for item in cast.true_spellings}:
        return True
    if folded in {item.casefold() for item in cast.false_spellings}:
        return False
    raise ValueTransformError(
        f"{label}: {value!r} is in neither the true nor the false spelling set"
    )


@dataclass(frozen=True)
class ScaleSpec:
    """One declared multiplicative rescale, invertible by its own record.

    The factor is published as a decimal *string* so it enters the arithmetic exactly;
    ``float`` factors are refused at construction. The record states factor and direction
    (``divide`` reads as "the source publishes the value times this factor"), so its
    :meth:`inverse` is declarable from the record alone. A division that does not terminate
    raises unless the declaration carries both a ``rounding`` mode and the ``places`` it
    quantizes to -- an undeclared or ambiguous scale is refused, not guessed.

    Attributes:
        factor: Positive decimal string, not ``1`` (a no-op scale is ambiguous).
        direction: How the factor applies to the published value.
        output_units: The unit written onto the output column after scaling; never blank,
            because a rescaled value without a declared unit is exactly the ambiguity this
            repo records as ``columns.rescale`` absent.
        rounding: One of the ``ROUND_*`` modes, or ``None`` to demand exactness.
        places: Decimal places the inexact result is quantized to; only meaningful with
            ``rounding``, and the two must be declared together.
        source_rule: The census vocabulary for this scale ("percent_to_fraction", ...),
            kept verbatim for the review record; it carries no arithmetic.
    """

    factor: str
    direction: ScaleDirection = "divide"
    output_units: str = ""
    rounding: str | None = None
    places: int | None = None
    source_rule: str = ""

    def __post_init__(self) -> None:
        """Reject a scale that cannot be applied exactly or inverted unambiguously."""
        if isinstance(self.factor, float):
            raise ValueError(f"a scale factor cannot be a float, got {self.factor!r}")
        if not isinstance(self.factor, str):
            raise ValueError(f"a scale factor must be a decimal string, got {self.factor!r}")
        try:
            parsed = _parse_exact_decimal(self.factor, "scale factor")
        except ValueError as error:
            raise ValueError(str(error)) from error
        if parsed <= 0:
            raise ValueError(f"a scale factor must be positive, got {self.factor!r}")
        if parsed == 1:
            raise ValueError("a scale of exactly 1 is a no-op and an ambiguity, not a rescale")
        if self.direction not in {"divide", "multiply"}:
            raise ValueError(f"unknown scale direction {self.direction!r}")
        if not self.output_units.strip():
            raise ValueError("a rescale must declare the unit it writes onto the column")
        if (self.rounding is None) != (self.places is None):
            raise ValueError("rounding and places are declared together or not at all")
        if self.rounding is not None and self.rounding not in _ROUNDING_MODES:
            raise ValueError(f"rounding {self.rounding!r} is not one of {sorted(_ROUNDING_MODES)}")
        if self.places is not None and (
            isinstance(self.places, bool) or not isinstance(self.places, int) or self.places < 0
        ):
            raise ValueError(f"places must be a non-negative int, got {self.places!r}")

    @classmethod
    def percent_to_fraction(cls, *, output_units: str = "fraction") -> ScaleSpec:
        """Declare a published percent (x100) rescaled to a fraction by dividing by 100."""
        return cls(
            factor="100",
            direction="divide",
            output_units=output_units,
            source_rule="percent_to_fraction",
        )

    @classmethod
    def basis_points_to_fraction(cls, *, output_units: str = "fraction") -> ScaleSpec:
        """Declare a published basis-point value (x10000 of the fraction) divided by 10000."""
        return cls(
            factor="10000",
            direction="divide",
            output_units=output_units,
            source_rule="basis_points_to_fraction",
        )

    @classmethod
    def strike_per_mille(cls, *, output_units: str) -> ScaleSpec:
        """Declare a 1/1000-scale strike divided by 1000 into its traded unit."""
        return cls(
            factor="1000", direction="divide", output_units=output_units, source_rule="per_mille"
        )

    def inverse(self) -> ScaleSpec:
        """Return the declaration that reverses this scale's factor and direction."""
        flipped: Literal["divide", "multiply"] = (
            "multiply" if self.direction == "divide" else "divide"
        )
        return ScaleSpec(
            factor=self.factor,
            direction=flipped,
            output_units=self.output_units,
            rounding=self.rounding,
            places=self.places,
            source_rule=f"inverse_of({self.source_rule or self.factor})",
        )


def apply_scale(scale: ScaleSpec, value: object) -> Decimal:
    """Return ``value`` rescaled in its own ``Decimal`` domain, exact or explicitly rounded.

    Args:
        scale: The declared factor, direction and -- when the quotient may not terminate --
            the rounding rule.
        value: The published numeric quantity (``int``, exact ``Decimal`` or decimal string;
            floats are refused so no arithmetic drifts through binary ``float``).

    Returns:
        The rescaled ``Decimal``.

    Raises:
        ValueTransformError: The input is not a declared-shaped number, or the quotient is
            inexact and the declaration carries no rounding rule.
    """
    quantity = _as_quantity(value, f"scale by {scale.factor}")
    factor = _parse_exact_decimal(scale.factor, "scale factor")
    if scale.direction == "divide":
        numerator, denominator = quantity, factor
    else:
        numerator, denominator = quantity * factor, Decimal(1)
    # The quotient is expanded far beyond any published rate so an inexact division trips
    # the flag rather than the precision ceiling; the *active* context (localcontext copies
    # the given one) is where the flag lands.
    context = Context(prec=_SCALE_PRECISION, rounding=scale.rounding or "ROUND_HALF_EVEN")
    with localcontext(context) as active:
        result = numerator / denominator
        inexact = active.flags[decimal.Inexact]
    if inexact and (scale.rounding is None or scale.places is None):
        raise ValueTransformError(
            f"{quantity} {scale.direction}d by {scale.factor} is not exact and the "
            "declaration carries no rounding rule; the scale is ambiguous"
        )
    if scale.places is None:
        return result
    quantum = Decimal(1).scaleb(-scale.places)
    try:
        return result.quantize(quantum, rounding=scale.rounding)
    except decimal.InvalidOperation as error:
        raise ValueTransformError(
            f"rescale of {quantity} does not fit {scale.places} declared places"
        ) from error


def resolve_output_units(column_units: str, scale: ScaleSpec | None) -> str:
    """Return the unit string the engine writes onto a projected column.

    Without a scale the column keeps its published units verbatim (today's behaviour); with
    one, only the scale's declared ``output_units`` is acceptable -- a rescaled value that
    still says "percent" would misrepresent the row.
    """
    if scale is None:
        return column_units
    if not scale.output_units.strip():
        raise ValueTransformError("a rescaled column cannot keep its published units undeclared")
    return scale.output_units


def transform_value(
    value: object,
    *,
    cast: CastSpec | None = None,
    scale: ScaleSpec | None = None,
) -> object:
    """Compose one column's declared reading: cast first, then rescale.

    The order is the only one that makes sense at the column seam: a cast turns the
    published shape into a quantity (``"4.33"`` -> ``Decimal("4.33")``), and a rescale acts
    on that quantity. A scale on a value the declaration did not cast is attempted only if
    it is already a number in the declared sense; floats raise either way.
    """
    current: object = value
    if cast is not None:
        current = apply_cast(cast, current)
    if scale is not None:
        current = apply_scale(scale, current)
    return current


def project_column(
    value: object,
    *,
    cast: CastSpec | None = None,
    scale: ScaleSpec | None = None,
    column_units: str = "",
) -> tuple[object, str]:
    """Return the ``(value, units)`` pair one fully declared column projects to."""
    return transform_value(value, cast=cast, scale=scale), resolve_output_units(column_units, scale)


# --------------------------------------------------------------------------- derive plans
#
# Everything above is a rule for one value; this section is the declaration the engine binds those
# rules to. ``columns.derive`` was recorded absent because a declared column could only *name a key
# *: ``normalize_record`` copies one source key per column and enforces its declared type, so a
# column whose value is restated (a published ``2024-03`` read as a date), rescaled (a published
# percent turned into a fraction), or taken from something that is not a record field at all (a
# query parameter stamped onto every row) had no way to say so. A plan below says exactly that, and
# it runs on nothing but the two entry points this module already publishes -- :func:`apply_cast`
# and :func:`apply_scale` -- in the order :func:`transform_value` documents. Anything the kernel
# cannot execute is not a stage kind here, which is what keeps the declaration honest rather than
# aspirational.

#: The stage kinds a derive plan may declare: the kernel's column-seam operations, no others.
DeriveStageKind = Literal["cast", "scale"]

#: Where a plan reads the value it computes from: a record key, one validated query parameter, or
#: text the declaration itself publishes for every row.
DeriveSourceKind = Literal["record", "parameter", "constant"]

#: The order the kernel composes stages in, taken verbatim from :func:`transform_value`'s rule: a
#: cast turns the published shape into a quantity and the rescale acts on that quantity. A plan
#: written the other way round would describe arithmetic this module does not perform.
STAGE_SEQUENCE: tuple[DeriveStageKind, ...] = ("cast", "scale")


@dataclass(frozen=True)
class DeriveStage:
    """One executed step of a derive plan: a kind the kernel runs plus the rule it runs.

    The kind is spelled out even though the carried declaration already implies it, because the
    census reads presence off declared *stages* and because a plan whose payload does not match its
    kind (a ``"cast"`` stage handing over a :class:`ScaleSpec`) is the same typo class every other
    declaration in this package refuses when the record is written.

    Attributes:
        kind: Which kernel entry point this stage is: ``"cast"`` or ``"scale"``.
        cast: The declared conversion; required by ``kind="cast"`` and refused by any other kind.
        scale: The declared rescale; required by ``kind="scale"`` and refused by any other kind.
    """

    kind: DeriveStageKind
    cast: CastSpec | None = None
    scale: ScaleSpec | None = None

    def __post_init__(self) -> None:
        """Reject a stage the kernel has no entry point for, or one carrying the wrong rule."""
        if self.kind not in STAGE_SEQUENCE:
            raise ValueError(
                f"derive stage kind {self.kind!r} is not one of {list(STAGE_SEQUENCE)}; "
                "the kernel runs a cast and a rescale and nothing else"
            )
        if self.kind == "cast":
            if not isinstance(self.cast, CastSpec):
                raise ValueError(f'a "cast" derive stage must carry a CastSpec, got {self.cast!r}')
            if self.scale is not None:
                raise ValueError('a "cast" derive stage carries no ScaleSpec')
            return
        if not isinstance(self.scale, ScaleSpec):
            raise ValueError(f'a "scale" derive stage must carry a ScaleSpec, got {self.scale!r}')
        if self.cast is not None:
            raise ValueError('a "scale" derive stage carries no CastSpec')

    def apply(self, value: object) -> object:
        """Run this stage's kernel entry point on one value.

        Raises:
            ValueTransformError: The value does not match the stage's declaration.
        """
        if self.kind == "cast" and self.cast is not None:
            return apply_cast(self.cast, value)
        if self.kind == "scale" and self.scale is not None:
            return apply_scale(self.scale, value)
        # Unreachable through __post_init__, and kept as a refusal rather than a passthrough: a
        # stage that arrives here would silently hand back the input and read as a derivation.
        raise ValueTransformError(f"derive stage {self.kind!r} carries no runnable declaration")


@dataclass(frozen=True)
class DerivePlan:
    """One declared column whose value is produced rather than copied out of the record.

    The plan is the whole declaration of the computation: a source, and the kernel stages run on
    whatever that source supplies. ``source_kind="record"`` reads a key of the record the row is
    built from (its own ``source_key`` when ``source`` is blank, or another published key, which is
    how one column can be computed from a second key without that key becoming a column of its
    own); ``"parameter"`` stamps one validated query parameter onto every row -- the shape an
    endpoint that answers with bare observations leaves the caller's own symbol unrecorded -- and
    ``"constant"`` publishes text the declaration itself carries.

    A plan that stages nothing and reads the column's own record key computes nothing, so it is
    refused here rather than accepted as a no-op: ``columns.rename`` is a different capability and
    an empty plan would let a declaration look like a derivation while changing no value.

    Attributes:
        column: The declared output column this plan produces.
        source_kind: Which face of the request or response supplies the raw value.
        source: The record key, the parameter name, or the constant text; blank means the column's
            own source key and is only allowed for ``"record"``.
        stages: The kernel stages, at most one per kind and in :data:`STAGE_SEQUENCE` order.
        note: Why the column is computed, for the review record.
    """

    column: str
    source_kind: DeriveSourceKind = "record"
    source: str = ""
    stages: tuple[DeriveStage, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        """Reject a plan the engine could not carry out, or could not attribute to a column."""
        if not isinstance(self.column, str) or not self.column.strip():
            raise ValueError(f"a derive plan must name the column it produces, got {self.column!r}")
        if self.source_kind not in {"record", "parameter", "constant"}:
            raise ValueError(
                f"derive source kind {self.source_kind!r} is not one of "
                "'record', 'parameter', 'constant'"
            )
        if self.source_kind != "record" and not self.source.strip():
            raise ValueError(
                f"a {self.source_kind} derive plan for {self.column!r} must name the "
                f"{self.source_kind} it reads"
            )
        if self.source_kind == "record" and not self.stages:
            raise ValueError(
                f"the derive plan for {self.column!r} stages nothing and overrides no source, "
                "so it computes nothing"
            )
        for stage in self.stages:
            if not isinstance(stage, DeriveStage):
                raise ValueError(
                    f"the derive plan for {self.column!r} stages must be DeriveStage, "
                    f"got {type(stage).__name__}"
                )
        kinds = [stage.kind for stage in self.stages]
        if len(kinds) != len(set(kinds)):
            raise ValueError(
                f"the derive plan for {self.column!r} runs one stage kind twice: {kinds}"
            )
        expected = [kind for kind in STAGE_SEQUENCE if kind in set(kinds)]
        if kinds != expected:
            raise ValueError(
                f"the derive plan for {self.column!r} declares stages {kinds}, which are not the "
                f"kernel's composition order {expected}"
            )


def apply_derive(plan: DerivePlan, value: object) -> object:
    """Run one declared plan over the value its source supplies.

    Args:
        plan: The column's declared stages.
        value: The raw value the plan's source resolved to.

    Returns:
        The value the plan produces -- a ``date``, ``Decimal``, ``float``, ``bool`` or the source
        itself when the plan declares no stage and only restates where the value comes from.

    Raises:
        ValueTransformError: A stage's input does not match its declaration. This is the kernel's
            own code-stable refusal and the engine seam propagates it untouched, so a row the
            published shape cannot produce is attributed to the cast that refused it rather than
            answered with the silent null this repo tracks.
    """
    current: object = value
    for stage in plan.stages:
        current = stage.apply(current)
    return current
