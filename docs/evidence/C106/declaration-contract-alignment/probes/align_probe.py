#!/usr/bin/env python3
"""C106: measure whether each declared column states its own reviewed row field.

Every reading here is computed from the live declarations against the row classes the reviewed
domains publish -- nothing is typed in from a summary. Three faces:

``census``
    For every engine-declared model, every declared column is compared with its reviewed row
    field on two axes: the value type the field accepts, and requiredness (``required`` against
    ``is_required()``, ``nullable`` against whether the annotation admits ``None``). The one
    documented exception is the temporal reading the engine already takes: a ``date``-kind column
    whose row field is a ``datetime`` is published by the row class as the midnight instant, so the
    kind and the field are allowed to differ exactly there (see
    ``opendata.data.providers._engine.testing._row_temporal_type``). A remainder outside that
    exception is a declaration disagreeing with its own contract, which is what this round found
    108 times at commit 4fda2d8.

``arms``
    Each repair this round made is shown on both sides: the cell the contract accepts comes back as
    a row value, and the cell it must refuse is refused with that model's own
    ``*_SHAPE_INVALID``. A declaration edit that only moved the fixture would leave the refusal side
    unreachable, and a refusal that fires on every body would leave the accept side unreachable.

``causality``
    The Literal-domain branch of ``sampled_cell`` is switched off in memory -- the pre-repair
    behaviour, without touching a file -- and the same record must then be refused. This is the arm
    that distinguishes "the fixture reads the field" from "the fixture happens to publish a legal
    cell".

Exit is 0 only when the census shows no real disagreement, every arm read as expected, the three
census controls each read non-zero, and the causality arm differs from the live arm.
``--census-only`` prints just the census, so one judge can be run against two checkouts.
"""

from __future__ import annotations

import datetime
import os
import sys
import typing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_provider_model_contracts as contracts  # noqa: E402
from opendata.data.providers._engine import testing as fixture  # noqa: E402
from opendata.data.providers._engine.http_json import (  # noqa: E402
    HttpResponse,
    ProviderEngineError,
    build_row_model,
)
from opendata.data.providers._engine.row_expansion import RowExpansionError  # noqa: E402
from opendata.data.providers.catalog import engine_declared_models  # noqa: E402

if TYPE_CHECKING:
    from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec

#: The Python types each declared kind is written to produce. ``float`` takes ``int`` because the
#: reviewed field coerces it; the temporal kinds are the pair the row-class reading reconciles.
KIND_TYPES: dict[str, set[type]] = {
    "str": {str},
    "int": {int},
    "float": {int, float},
    "bool": {bool},
    "date": {datetime.date, datetime.datetime},
    "time": {datetime.time},
    "enum": {str},
    "str_list": {list},
}

#: The models the arms drive, named once so the precondition guard and the arms cannot disagree.
ARM_MODELS: frozenset[str] = frozenset({"EconomicCalendar", "SOFR", "SvenssonYieldCurve"})

#: 4 TradingEconomics + 4 SOFR + 5 Svensson; the judge refuses a shrunken population.
ARM_FLOOR = 13


def specs_by_identity() -> dict[str, tuple[str, ModelSpec]]:
    """Every engine-declared model, keyed by ``source::Model``."""
    return {f"{source}::{spec.model}": (source, spec) for source, spec in engine_declared_models()}


def column_of(spec: ModelSpec, name: str) -> ColumnSpec:
    """The declared column an arm damages, refusing loudly when the name is not declared."""
    return next(column for column in spec.columns if column.name == name)


def row_field_types(
    annotation: object,
) -> tuple[set[type], bool, tuple[object, ...] | None]:
    """Flatten a row field annotation to (accepted types, admits None, Literal members)."""
    if typing.get_origin(annotation) is typing.Literal:
        members = typing.get_args(annotation)
        return {type(member) for member in members}, False, members
    args = typing.get_args(annotation)
    if not args:
        return ({annotation} if isinstance(annotation, type) else set()), False, None
    admits_none = type(None) in args
    bases: set[type] = set()
    literals: tuple[object, ...] | None = None
    for arg in args:
        if arg is type(None):
            continue
        if typing.get_origin(arg) is typing.Literal:
            members = typing.get_args(arg)
            literals = (literals or ()) + members
            bases |= {type(member) for member in members}
        else:
            bases.add(arg if isinstance(arg, type) else type(arg))
    return bases, admits_none, literals


def census(
    *,
    models: list[tuple[str, ModelSpec]] | None = None,
    date_accepts_datetime: bool = True,
) -> tuple[int, int, int, list[str], int]:
    """Compare every declared column with its reviewed row field.

    Returns ``(models, columns_checked, mismatched, remainder_rows, temporal_covered)``.
    ``temporal_covered`` counts the columns the documented date/datetime exception actually
    absorbed, so a zero on ``mismatched`` cannot come from an exception that never fired.
    """
    pairs = models if models is not None else list(engine_declared_models())
    columns_checked = 0
    kind_rows: list[str] = []
    req_rows: list[str] = []
    temporal_covered = 0
    for source, spec in pairs:
        row_model = build_row_model(spec)
        hints = typing.get_type_hints(row_model)
        for column in spec.columns:
            columns_checked += 1
            field = row_model.model_fields[column.name]
            bases, admits_none, literals = row_field_types(hints[column.name])
            wanted = KIND_TYPES.get(column.kind, set())
            if column.kind == "date" and datetime.datetime in bases:
                temporal_covered += 1
                if not date_accepts_datetime:
                    wanted = {datetime.date}
            if bases and not bases & wanted:
                kind_rows.append(
                    f"{source}::{spec.model}.{column.name} kind={column.kind} "
                    f"field={literals or bases}"
                )
            if literals is not None and not all(type(m) in wanted for m in literals):
                kind_rows.append(
                    f"{source}::{spec.model}.{column.name} kind={column.kind} "
                    f"domain={literals} outside the kind's types"
                )
            if column.required != field.is_required() or column.nullable != admits_none:
                req_rows.append(
                    f"{source}::{spec.model}.{column.name} "
                    f"declared req={column.required} nul={column.nullable} / "
                    f"field req={field.is_required()} none={admits_none}"
                )
    return (
        len(pairs),
        columns_checked,
        len(kind_rows) + len(req_rows),
        [*kind_rows, *req_rows],
        temporal_covered,
    )


def tampered_models(
    model: str, column_name: str, **column_updates: object
) -> list[tuple[str, ModelSpec]]:
    """One declaration edited in memory only, leaving every file on disk untouched."""
    rebuilt: list[tuple[str, ModelSpec]] = []
    hits = 0
    for source, spec in engine_declared_models():
        if spec.model != model:
            rebuilt.append((source, spec))
            continue
        columns: list[ColumnSpec] = []
        for column in spec.columns:
            if column.name == column_name:
                columns.append(replace(column, **column_updates))  # type: ignore[arg-type]
                hits += 1
            else:
                columns.append(column)
        rebuilt.append((source, replace(spec, columns=tuple(columns))))
    if hits != 1:
        raise AssertionError(f"tamper target {model}.{column_name} matched {hits} columns")
    return rebuilt


def refusal_or_row(
    source: str, spec: ModelSpec, pages: list[HttpResponse], kwargs: dict[str, Any]
) -> tuple[str, Any]:
    """``("refused:<class>", None)``, or ``("ok", the first row the engine built)``."""
    try:
        _, rows = contracts.fetch(source, spec, *pages, **kwargs)
    except (ProviderEngineError, RowExpansionError, ValueError) as error:
        return f"refused:{type(error).__name__}", None
    return "ok", cast("Any", rows)[0]


def arm(
    label: str,
    source: str,
    spec: ModelSpec,
    pages: list[HttpResponse],
    kwargs: dict[str, Any],
    field: str,
    want_refusal: bool,
    want: str,
) -> bool:
    """Measure one arm on both sides, print its reading, return whether it read as expected."""
    reading, row = refusal_or_row(source, spec, pages, kwargs)
    if row is not None:
        reading = f"ok:{field}={getattr(row, field)!r}"
    expected = reading.startswith("refused:") == want_refusal and (not want or reading == want)
    print(f"  arm: {label} -> {reading} expected_refusal_read={'yes' if expected else 'no'}")
    return expected


def arms() -> tuple[int, int]:
    """Run every claim both ways; return (arms measured, arms that read as expected)."""
    specs = specs_by_identity()
    results: list[bool] = []

    te_source, te = specs["tradingeconomics::EconomicCalendar"]
    te_kwargs = contracts.valid_query_kwargs(te)
    for cell, want_refusal in ((1, False), (2, True), (3, True), ("x", True)):
        pages = contracts.amend_first_record(te, contracts.pages_for(te, 1), "DateSpan", value=cell)
        results.append(
            arm(
                f"date_span={cell!r}",
                te_source,
                te,
                pages,
                te_kwargs,
                "date_span",
                want_refusal,
                "",
            )
        )

    sofr_source, sofr = specs["federal_reserve::SOFR"]
    sofr_kwargs = contracts.valid_query_kwargs(sofr)
    # ``rate`` is required and non-nullable on both sides, and it carries the divide-by-100 scale
    # stage. That stage refuses a JSON number by design ("a float would drift"), so the accept arm
    # has to publish text -- and the number arm is the counterfact proving the exactness guard is
    # live rather than the stage merely never running.
    sofr_plan: tuple[tuple[str, object, bool, bool, str], ...] = (
        ("SOFR percentRate dropped", None, True, True, ""),
        ("SOFR percentRate null", None, False, True, ""),
        ("SOFR percentRate text '4.33'", "4.33", False, False, "ok:rate=0.0433"),
        ("SOFR percentRate number 4.33", 4.33, False, True, ""),
    )
    for label, value, drop, want_refusal, want in sofr_plan:
        pages = contracts.amend_first_record(
            sofr, contracts.pages_for(sofr, 1), "percentRate", value=value, drop=drop
        )
        results.append(
            arm(
                label,
                sofr_source,
                sofr,
                pages,
                sofr_kwargs,
                "rate",
                want_refusal,
                want,
            )
        )

    sv_source, sv = specs["federal_reserve::SvenssonYieldCurve"]
    sv_kwargs = contracts.valid_query_kwargs(sv)
    # beta0 is ``float | None`` on the reviewed contract, so a null cell and a sentinel cell both
    # belong in the accept column; the refusal arms that matter are the two ways a text cell can
    # fail -- non-numeric text, and text that is NOT one of the four declared null tokens. Without
    # the second the sentinel would be unfalsifiable: any junk string would look equally null.
    sv_plan: tuple[tuple[object, bool, str], ...] = (
        ("alpha", True, ""),
        ("ND", False, "ok:beta0=None"),
        ("1.5", False, "ok:beta0=1.5"),
        (None, False, "ok:beta0=None"),
        ("n.a.", True, ""),
    )
    for sv_cell, want_refusal, want in sv_plan:
        pages = contracts.amend_first_record(sv, contracts.pages_for(sv, 1), "BETA0", value=sv_cell)
        results.append(
            arm(
                f"Svensson BETA0={sv_cell!r}",
                sv_source,
                sv,
                pages,
                sv_kwargs,
                "beta0",
                want_refusal,
                want,
            )
        )

    return len(results), sum(results)


def _row_domain_disabled(spec: ModelSpec, column: ColumnSpec) -> tuple[object, ...] | None:
    """The pre-repair reading of the fixture's row-class hook: it saw no domain.

    Returning None is exactly what ``sampled_cell`` did before this round taught it to read the
    reviewed row class, so it is the stand-in the causality arm switches in. The parameter names
    mirror ``_row_literal_members`` because the patch has to be a signature-for-signature swap.
    """
    return None


def causality() -> tuple[str, str]:
    """Switch the Literal branch off in memory and show the same body is now refused."""
    te_source, spec = specs_by_identity()["tradingeconomics::EconomicCalendar"]
    kwargs = contracts.valid_query_kwargs(spec)
    column = column_of(spec, "date_span")

    live = fixture.sampled_cell(spec, column, 1)
    original = fixture._row_literal_members
    pre: object = None
    reading = "not_run"
    try:
        fixture._row_literal_members = _row_domain_disabled
        pre = fixture.sampled_cell(spec, column, 1)
        pages = contracts.amend_first_record(
            spec, contracts.pages_for(spec, 1), "DateSpan", value=pre
        )
        reading, _ = refusal_or_row(te_source, spec, pages, kwargs)
    finally:
        fixture._row_literal_members = original
    return f"live_cell={live!r} disabled_branch_cell={pre!r}", reading


def literal_domain_under_test() -> tuple[object, ...] | None:
    """The domain the fixture reads off the reviewed row class for the arm model."""
    spec = next(s for _source, s in engine_declared_models() if s.model == "EconomicCalendar")
    return fixture._row_literal_members(spec, column_of(spec, "date_span"))


def credential_supplied() -> bool:
    """True when the models under arm can get past their own credential check."""
    return all(
        os.environ.get(spec.credential)
        for _source, spec in engine_declared_models()
        if spec.model in ARM_MODELS and spec.credential
    )


def main() -> int:
    """Print the three faces, judge them, and exit non-zero on any unmet claim."""
    print(f"face: python={sys.version.split()[0]} cwd={os.getcwd()}")
    models, columns_checked, mismatched, remainders, temporal_covered = census()
    print(
        f"identity: models={models} columns_checked={columns_checked} "
        f"mismatched_columns={mismatched} temporal_exception_covered={temporal_covered}"
    )
    for row in remainders:
        print(f"  remainder: {row}")
    if "--census-only" in sys.argv:
        # Same judge, run against a second checkout, so the before/after delta is one instrument.
        print(f"census_only_exit={'1' if mismatched else '0'}")
        return 1 if mismatched else 0

    # A zero on ``mismatched`` is only a measurement if the same judge reads non-zero when the
    # disagreement is put back: two in-memory tampers, one per axis, plus the exception
    # switched off.
    _, _, no_exception, rows_no_exception, _ = census(date_accepts_datetime=False)
    print(
        f"control-exception-off: mismatched_columns={no_exception} "
        f"(live {mismatched} + covered {temporal_covered}) rows={len(rows_no_exception)}"
    )
    _, _, tamper_kind, rows_kind, _ = census(models=tampered_models("SOFR", "date", kind="int"))
    print(f"control-kind-tamper: mismatched_columns={tamper_kind} row={rows_kind[:1]}")
    _, _, tamper_req, rows_req, _ = census(models=tampered_models("SOFR", "rate", nullable=True))
    print(f"control-nullability-tamper: mismatched_columns={tamper_req} row={rows_req[:1]}")

    arms_total, arms_expected = arms()
    print(f"arms: total={arms_total} read_as_expected={arms_expected}")
    print(f"identity: arms_population={arms_total}")
    cells, reading = causality()
    print(f"causality: {cells} body_under_disabled_branch -> {reading}")
    literal_members = literal_domain_under_test()
    print(f"identity: literal_members_under_test={literal_members!r}")

    failures: list[str] = []
    if mismatched:
        failures.append(f"census still disagrees on {mismatched} column(s)")
    if no_exception != mismatched + temporal_covered or not temporal_covered:
        failures.append(
            f"exception control does not account: off={no_exception} live={mismatched} "
            f"covered={temporal_covered} (want off == live + covered, covered > 0)"
        )
    if tamper_kind != mismatched + 1:
        failures.append(
            f"kind tamper read {tamper_kind}, want {mismatched + 1} -- the kind axis is not live"
        )
    if tamper_req != mismatched + 1:
        failures.append(
            f"requiredness tamper read {tamper_req}, want {mismatched + 1} -- that axis is not live"
        )
    if arms_expected != arms_total:
        failures.append(f"arms read_as_expected={arms_expected} of total={arms_total} (want all)")
    if arms_total < ARM_FLOOR:
        failures.append(
            f"arm population shrank to {arms_total}, want {ARM_FLOOR} (4 TE + 4 SOFR + 5 Svensson)"
        )
    if not reading.startswith("refused:"):
        failures.append(
            "causality arm did NOT differ: the Literal branch is not what admits the cell"
        )
    if not literal_members:
        failures.append("the Literal branch reads no domain for date_span, so census is vacuous")
    if not credential_supplied():
        failures.append(
            "no fixture credential in the environment, so every arm refuses for the wrong reason"
        )
    for line in failures:
        print(f"  finding: {line}")
    print(f"verdict: {'PASS' if not failures else 'FAIL'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
