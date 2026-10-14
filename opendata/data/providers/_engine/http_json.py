"""Turn a :class:`~opendata.data.providers._engine.spec.ModelSpec` into a fetcher.

One factory covers every JSON REST provider model in the fixed 350-task ledger:
it builds the typed query, rejects bad requests before any byte is sent, drives
and bounds pagination, classifies upstream failures into stable codes, and
normalizes records into contract rows whose columns are exactly the declared
ones. Per-model code therefore stays a declaration, and the semantics that
acceptance judges (parameter rejection, empty-versus-missing, page
completeness, unit fidelity) are written once and tested uniformly.

The read face has three declarable facets, in the order they run: ``rows_pointer`` says where the
records are, :class:`~opendata.data.providers._engine.spec.RowFilterSpec` says which of the
published records the model is about, and :class:`~opendata.data.providers._engine.spec.ColumnSpec`
says what to take out of each selected record. The middle facet is a client-side *selection*, and
:func:`apply_row_filters` runs it between the declared-total agreement check and normalization for
that reason. A fourth facet, :class:`~opendata.data.providers._engine.spec.RowOrderSpec`, says which
of the selected records come first and how many of them the model keeps; :func:`apply_row_order`
runs it immediately after the selection, on the same side of normalization, so it orders what
survived filtering and nothing about the completeness judgement changes.

The module performs I/O only through :func:`_http_get_json`, which the contract
suite replaces with a synthetic recorder, so no test can reach a live endpoint.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, cast

from pydantic import BeforeValidator, ConfigDict, ValidationError, create_model

from opendata.data.capability import Capability
from opendata.data.domains import contract_model, load_domains
from opendata.data.models.base import ContractModel
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers._engine.key_composition import (
    KeyCompositionError,
    render_composite_key,
)
from opendata.data.providers._engine.row_expansion import (
    NestedArrayExpansion,
    RecordRepointPlan,
    SeriesPivotPlan,
    WideTablePivotPlan,
    expand_nested_array,
    pivot_series,
    repoint_records,
    transpose_wide_table,
)
from opendata.data.providers._engine.spec import ROW_FILTER_VALUE_SEPARATOR
from opendata.data.providers._engine.value_transform import DerivePlan, apply_derive

if TYPE_CHECKING:
    from opendata.data.providers._engine.key_composition import CompositeKeySpec
    from opendata.data.providers._engine.spec import (
        ColumnSpec,
        ModelSpec,
        ParamSpec,
        RowFilterOp,
        RowFilterSpec,
        RowOrderSpec,
    )

DEFAULT_TIMEOUT = 30.0

#: Only these characters may appear in an unencoded query value we pass through.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~:/?=\-+%,\s]*$")

#: A value that is provably one URL path segment, so it can be substituted into a path template
#: without percent-encoding and cannot open a second segment or reach the query string.
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~\-]+$")


class ProviderEngineError(RuntimeError):
    """Stable, attributable failure of an engine-driven provider model."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        url: str | None = None,
        rejected: Sequence[str] = (),
        missing: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        """Store the code and the request that produced it."""
        self.code = code
        self.status = status
        self.url = url
        self.rejected = tuple(rejected)
        self.missing = tuple(missing)
        self.invalid = tuple(invalid)
        detail = "" if status is None else f" status={status}"
        detail += "" if url is None else f" url={url}"
        detail += "" if not self.rejected else f" rejected={list(self.rejected)}"
        detail += "" if not self.missing else f" missing={list(self.missing)}"
        detail += "" if not self.invalid else f" invalid={list(self.invalid)}"
        super().__init__(code + detail)


class ProviderEngineQueryError(ProviderEngineError):
    """The caller's query fields failed the generated model's strict validation."""


@dataclass(frozen=True)
class HttpResponse:
    """A status code, the document a JSON body parsed into, and the raw body a decoder reads.

    ``payload`` is the additive seam for :mod:`.decoders`: a JSON declaration never looks at it,
    and a delimited declaration never looks at ``document``. A test transport supplies whichever of
    the two the declaration asks for, so no case has to fake an HTTP client to hand the engine a
    CSV or a zip body.
    """

    status: int
    document: Any
    payload: bytes | str | None = None


#: ``(url, params, *, timeout, source, headers) -> HttpResponse``; the test seam for I/O.
HttpGet = Callable[..., HttpResponse]


def _refuse_bool(value: object) -> object:
    """Keep a published boolean from silently satisfying a numeric column."""
    if isinstance(value, bool):
        raise ValueError("a published boolean is not a numeric value")
    return value


def _column_type(column: ColumnSpec) -> Any:  # noqa: ANN401 - dynamic pydantic field type
    """Map a declared column kind onto a pydantic-validated annotation."""
    if column.kind == "date":
        return date
    if column.kind == "str_list":
        return list[str]
    if column.kind == "enum":
        return str
    if column.kind in ("int", "float"):
        numeric = int if column.kind == "int" else float
        return Annotated[numeric, BeforeValidator(_refuse_bool)]
    return {"str": str, "bool": bool}[column.kind]


def _param_type(parameter: ParamSpec) -> Any:  # noqa: ANN401 - dynamic pydantic field type
    """Map a declared parameter kind onto a pydantic-validated annotation."""
    if parameter.kind == "date":
        return date
    if parameter.kind == "str_list":
        return list[str]
    if parameter.kind == "enum":
        return Literal[parameter.enum]
    return {"str": str, "int": int, "float": float, "bool": bool}[parameter.kind]


def build_query_model(
    spec: ModelSpec,
    *,
    source: str | None = None,
    market: str | None = None,
) -> type[Any]:
    """Build a strict query class for wire params and optional governed identity fields.

    ``source`` and ``market`` are accepted only when the fetcher factory supplies their registered
    values. They validate the service's governance identity on the generated query; they are not
    ``ModelSpec.params`` and therefore never enter :func:`encode_query`. The factory values are
    defaults so direct fetcher callers can continue to provide only declared query parameters;
    explicitly supplied alternate identities are still rejected. Omitting these keyword arguments
    keeps the original one-argument builder contract unchanged.
    """
    identity_values = tuple(
        (name, value)
        for name, value in (("source", source), ("market", market))
        if value is not None
    )
    declared_names = {parameter.name for parameter in spec.params}
    collisions = sorted(name for name, _ in identity_values if name in declared_names)
    if collisions:
        raise ValueError(
            f"{spec.model}: governance identity collides with declared wire params {collisions}"
        )

    fields: dict[str, tuple[Any, Any]] = {}
    for parameter in spec.params:
        annotation = _param_type(parameter)
        if parameter.serialise == "json":
            # A json-serialised parameter carries a structure, not a scalar of the declared kind:
            # the strict per-kind annotation would refuse the very blob the declaration says to
            # send. Validation moves to the wire, where anything ``json.dumps`` cannot render is
            # refused as ``_PARAM_UNENCODABLE`` rather than quietly stringified.
            annotation = Any
        if parameter.required:
            fields[parameter.name] = (annotation, ...)
        elif parameter.default is not None:
            fields[parameter.name] = (annotation, parameter.default)
        else:
            fields[parameter.name] = (annotation | None, None)
    for name, value in identity_values:
        fields[name] = (Literal[value], value)
    name = f"{spec.model}Query"
    generated = create_model(  # type: ignore[call-overload]
        name,
        __config__=ConfigDict(extra="forbid", populate_by_name=False),
        **fields,
    )
    return cast("type[Any]", generated)


def build_row_model(spec: ModelSpec) -> type[ContractModel]:
    """Return the row class this declaration publishes.

    A domain with declared semantics owns a reviewed contract model, and the services that serve a
    provider model accept a row only when ``type(row)`` is exactly that class
    (``provider_model_query._serialize_contract_row``, ``provider_model_ingest`` contract check).
    Publishing the contract itself is therefore what makes a declaration readable end to end; a
    synthesized look-alike class produces rows those services refuse, and the refusal surfaces at
    query time -- far away from the declaration that caused it.

    Raises:
        ValueError: If the domain's contract fields are not exactly the declared column names -- a
            declaration may neither add a column the reviewed contract lacks nor drop one it has.
    """
    domain_spec = load_domains().get(spec.domain)
    if domain_spec is not None and domain_spec.semantics_declared:
        contract_type = contract_model(spec.domain)
        declared = {column.name for column in spec.columns}
        contract_fields = set(contract_type.model_fields)
        if declared != contract_fields:
            raise ValueError(
                f"{spec.model}: declared columns {sorted(declared ^ contract_fields)} "
                f"do not match contract {contract_type.__name__}"
            )
        return contract_type
    fields: dict[str, tuple[Any, Any]] = {}
    for column in spec.columns:
        annotation = _column_type(column)
        if column.required and not column.nullable:
            fields[column.name] = (annotation, ...)
        elif column.required:
            fields[column.name] = (annotation | None, ...)
        else:
            fields[column.name] = (annotation | None, None)
    row_type = create_model(  # type: ignore[call-overload]
        f"{spec.model}Row",
        __base__=ContractModel,
        __config__=ConfigDict(extra="forbid", frozen=True),
        **fields,
    )
    return cast("type[ContractModel]", row_type)


def resolve_bound(query: object, parameter: ParamSpec, *, now: date | None = None) -> object:
    """Return the value this parameter contributes to the request.

    The caller's own value always wins: a relative bound fills an *omission*, which is the exact
    place upstream fills it (``historical_dividends.py:75-78`` assigns ``start_date`` and
    ``end_date`` only ``if params.get(...) is None``). With nothing on the query and a
    :class:`~opendata.data.providers._engine.spec.RelativeDefault` on the declaration, the bound is
    resolved against ``now``; with neither, the parameter is simply not sent, as before.

    ``now`` is the request date, read once per fetch by :func:`fetch_pages` and threaded down, so
    the two bounds of one window cannot straddle midnight and disagree with each other. A caller
    who passes no clock gets ``date.today()`` at the moment of the read -- the same clock the
    reviewed upstream reads -- and never a literal substituted for a bound it cannot resolve.

    Raises:
        ValueError: Propagated from :meth:`RelativeDefault.resolve` for a clock that is not a bare
            calendar date. A declaration that resolved to nothing would send a narrower request
            than it claims to make.
    """
    value = getattr(query, parameter.name, None)
    if value is not None or parameter.relative_default is None:
        return value
    return parameter.relative_default.resolve(date.today() if now is None else now)


def bound_request(query: object, spec: ModelSpec, *, now: date | None = None) -> object:
    """Return the request-side view of a validated query, with every relative bound filled in.

    The view carries one attribute per declared parameter: the caller's own value where there is
    one, the bound resolved from the declaration where there is not, and ``None`` where the
    declaration says nothing either. Fetching reads both the query string and the path template out
    of this one object, so a window whose two bounds are both relative is measured against one
    clock rather than two, and the two halves of one request cannot disagree about what day it was.

    The view is the request's, not the row's: the row-side stages run on the caller's own object,
    and :class:`~opendata.data.providers._engine.spec.ModelSpec` refuses a filter, a cap or a stamp
    aimed at a relatively bound parameter rather than letting it read the omission the request no
    longer carries.
    """
    clock = date.today() if now is None else now
    return SimpleNamespace(
        **{parameter.name: resolve_bound(query, parameter, now=clock) for parameter in spec.params}
    )


def encode_query(query: object, spec: ModelSpec, *, now: date | None = None) -> dict[str, str]:
    """Project a validated query onto the endpoint's query-string parameters.

    A parameter the path template consumes is deliberately left out of the query string: an
    upstream that encodes the resource in the URL (``/quotes/{symbol}.json``) does not accept it
    twice, and sending it twice would make the logged request differ from the declared one. A
    composite placeholder consumes every parameter its positions read, so an SDMX model's ``freq``
    lives only inside ``{dimension_key}``.

    The declaration's :attr:`~ModelSpec.static_query` constants go in first and are not read from
    the query: they are the upstream's own URL literals (which release, which series, which payload
    format), and ``ModelSpec`` refuses one whose key a parameter, a credential or a paging key also
    claims, so no caller-set value can overwrite the addressing the model was reviewed against.

    An omitted parameter is read through :func:`resolve_bound`, so a declaration carrying a
    :class:`~opendata.data.providers._engine.spec.RelativeDefault` sends the resolved date where an
    older declaration sent nothing.
    """
    encoded: dict[str, str] = dict(spec.static_query)
    consumed_by_path = spec.path_consumed_params
    for parameter in spec.params:
        if parameter.name in consumed_by_path:
            continue
        value = resolve_bound(query, parameter, now=now)
        if value is None:
            continue
        encoded[parameter.encoded_key] = _apply_value_map(
            parameter, _encode_value(parameter, value, spec), spec
        )
    return encoded


def render_path(spec: ModelSpec, query: object, *, now: date | None = None) -> str:
    """Substitute the declared path template with single-segment values from the validated query.

    A placeholder that names a :attr:`~ModelSpec.composite_keys` entry is composed from the
    parameters its positions name; every other placeholder is one attribute of the query, read
    through :func:`resolve_bound` so a relatively defaulted bound addresses the resource it is
    reviewed against instead of refusing as missing.

    Raises:
        ProviderEngineError: A placeholder has no value, a composition refused one of its
            positions, a declared code table has no spelling for the value, or a value that is not
            provably one URL path segment. All are refused before any socket opens: a value carrying
            ``/`` or ``?`` would silently address a different resource than the declaration
            describes.
    """
    path = spec.path
    composite = {key.name: key for key in spec.composite_keys}
    by_name = {parameter.name: parameter for parameter in spec.params}
    for name in spec.path_placeholders:
        if name in composite:
            path = path.replace(
                f"{{{name}}}", _render_composite(composite[name], query, spec, now=now)
            )
            continue
        parameter = by_name.get(name)
        value = (
            getattr(query, name, None)
            if parameter is None
            else resolve_bound(query, parameter, now=now)
        )
        if value is None:
            raise ProviderEngineError(f"{spec.error_prefix}_QUERY_INVALID", url=path)
        text = value.isoformat() if isinstance(value, date) else str(value)
        # A declared code table belongs to the parameter, not to the place its value is used, so a
        # mapped placeholder segment carries the upstream spelling too -- and an unmapped one is
        # refused here rather than sent as the caller's own text, which would address a resource no
        # review covered.
        if parameter is not None and parameter.value_map:
            text = _apply_value_map(parameter, text, spec)
        if not _PATH_SEGMENT.match(text):
            raise ProviderEngineError(f"{spec.error_prefix}_PARAM_UNENCODABLE", url=path)
        path = path.replace(f"{{{name}}}", text)
    return path


#: What joined text may not contain to still be one path segment. The comma is allowed because a
#: composite separator is a legal segment character (an SDMX resource key is ``agency,agency``, and
#: the composition kernel already refused any separator that is URL structure).
_SEGMENT_BREAKING = "/?#\\&"


def _render_composite(
    key: CompositeKeySpec, query: object, spec: ModelSpec, *, now: date | None = None
) -> str:
    """Compose one path segment from the query values its declaration names.

    The kernel owns the per-position rules -- charset, missing policy, separator safety -- so this
    seam only resolves the attributes, translates the kernel's attributable refusal into the
    model's own error prefix, and re-checks that the *joined* result cannot break out of one
    segment or start a query string.

    A position that reads a parameter declaring a :attr:`~ParamSpec.value_map` is mapped before the
    kernel sees it: the map belongs to the parameter, and a model whose callers say ``monthly`` and
    whose key carries ``M`` would otherwise compose the caller's own text into the resource address.
    An absent value is left for the kernel's own missing policy, so a wildcard position still
    defaults rather than being reported as unmapped.
    """
    by_name = {parameter.name: parameter for parameter in spec.params}
    values: dict[str, object] = {}
    for component in key.components:
        if component.kind != "param" or not component.source:
            continue
        parameter = by_name.get(component.source)
        value = (
            getattr(query, component.source, None)
            if parameter is None
            else resolve_bound(query, parameter, now=now)
        )
        if parameter is not None and parameter.value_map and value is not None:
            value = _apply_value_map(parameter, str(value), spec)
        values[component.source] = value
    try:
        text = render_composite_key(key, values)
    except KeyCompositionError as error:
        raise ProviderEngineError(
            f"{spec.error_prefix}_{error.code}",
            rejected=(error.reason, error.component),
            url=spec.path,
        ) from None
    if any(character in text for character in _SEGMENT_BREAKING):
        raise ProviderEngineError(
            f"{spec.error_prefix}_KEY_COMPOSITION_INVALID",
            rejected=("path_structure", key.name),
            url=spec.path,
        )
    return text


def _encode_value(parameter: ParamSpec, value: object, spec: ModelSpec) -> str:
    """Render one parameter as an upstream query-string value.

    A parameter that declares ``serialise`` is rendered by :func:`_serialise_value` instead: the
    declaration names the wire form, so the kind's own default rendering never sees the value.
    Every other parameter -- the whole shipped population before this field existed -- is
    rendered exactly as before, byte for byte.
    """
    if parameter.serialise:
        return _serialise_value(parameter, value, spec)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if not all(_SAFE_VALUE.match(str(item)) for item in value):
            raise ProviderEngineError(f"{spec.error_prefix}_PARAM_UNENCODABLE")
        return ",".join(str(item) for item in value)
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not _SAFE_VALUE.match(text):
        raise ProviderEngineError(f"{spec.error_prefix}_PARAM_UNENCODABLE")
    return text


def _serialise_value(parameter: ParamSpec, value: object, spec: ModelSpec) -> str:
    """Render one parameter in exactly the wire form its declaration's ``serialise`` names.

    This is the request-side half of ``request.serialised_param``: a query parameter whose value
    must reach the endpoint in a spelling the parameter's own kind does not give. ``json``
    renders the caller's structure as one compact JSON blob -- wsj's ``id=`` parameter is a
    URL-encoded JSON document (gainers.py:88 builds it before the get), and the transport's own
    URL encoding is what turns the blob into the ``%7B...%7D`` the endpoint reads. The
    ``strftime:<format>`` spelling renders a date through that literal format --
    TreasuryAuctions assembles ``%m/%d/%Y`` strings into ``startDate``/``endDate`` on the
    request side (treasury_auctions.py:209 and :212), not ISO. A value the named form cannot
    render is attributed as ``_PARAM_UNENCODABLE``, the same refusal the unserialised path
    raises for an unsafe list; it is never answered with the kind's default text, because a
    request sent in a form the record does not describe is the silent face the declaration
    exists to prevent.
    """
    if parameter.serialise == "json":
        try:
            return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise ProviderEngineError(
                f"{spec.error_prefix}_PARAM_UNENCODABLE",
                rejected=(parameter.name,),
                url=spec.path,
            ) from exc
    fmt = parameter.serialise.removeprefix("strftime:")
    render = getattr(value, "strftime", None)
    if not callable(render):
        raise ProviderEngineError(
            f"{spec.error_prefix}_PARAM_UNENCODABLE",
            rejected=(parameter.name,),
            url=spec.path,
        )
    return str(render(fmt))


def _apply_value_map(parameter: ParamSpec, text: str, spec: ModelSpec) -> str:
    """Translate one caller-facing parameter value into the spelling the endpoint expects.

    A model whose callers say ``annual`` and whose upstream says ``A`` declares the pair once here
    instead of letting every provider package translate it. An unmapped value is a refusal, not a
    passthrough: sending the caller's own text under a declaration that reviewed a different
    spelling would ask the upstream a question the record does not describe answering.
    """
    if not parameter.value_map:
        return text
    mapped = parameter.mapped_values.get(text)
    if mapped is None:
        raise ProviderEngineError(
            f"{spec.error_prefix}_PARAM_VALUE_UNMAPPED",
            rejected=(parameter.name,),
            url=spec.path,
        )
    return mapped


def _expand_rows(records: list[Any], spec: ModelSpec) -> list[Any]:
    """Route the resolved record list through the stages the declaration named.

    ``ModelSpec.row_expansion`` is the spec-plane half of ``path.explode``: one pointer
    reaches one record list, and each stage here turns that list into the rows the model
    publishes -- a nested array exploded per parent, a wide table read per series and
    pivoted into one row per timestamp, a record list projected through per-record
    sub-pointers. The plans are the frozen ones from :mod:`.row_expansion` and they are
    applied to the list this function already resolved: the kernel re-reads pointers
    *inside* the document it is given and never fetches a second one.

    Raises:
        RowExpansionError: Propagated from the kernel untouched, with the pointer and the
            record position that made it a shape failure. A row set the stages could not produce
            is attributed, never answered with the empty frame that would pass as a published
            zero.
    """
    if not spec.row_expansion:
        return records
    expanded: list[Any] = records
    for index, stage in enumerate(spec.row_expansion):
        if isinstance(stage, NestedArrayExpansion):
            expanded = expand_nested_array(expanded, stage)
        elif isinstance(stage, WideTablePivotPlan):
            expanded = transpose_wide_table(expanded, stage)
        elif isinstance(stage, SeriesPivotPlan):
            expanded = pivot_series(expanded, stage)
        elif isinstance(stage, RecordRepointPlan):
            expanded = repoint_records(expanded, stage)
        else:
            raise ProviderEngineError(
                f"{spec.error_prefix}_SHAPE_INVALID",
                rejected=(f"row expansion stage {index} is {type(stage).__name__}",),
            )
    return expanded


def resolve_rows(document: object, pointer: str, spec: ModelSpec) -> list[Any]:
    """Follow ``pointer`` into ``document`` and return the record list.

    An absent path is a shape failure; an empty list is a legitimate empty
    result. The two are never conflated, which is the distinction AC2-10 asks
    each model to make.

    A declaration that names ``row_expansion`` does not stop at the record list: what
    comes back is the list the declared stages produce from it, so the rows normalized
    downstream are the exploded/pivoted ones. A declaration that names none -- every
    declaration written before the field existed -- returns the resolved list itself.
    """
    current = document
    if pointer:
        for part in pointer.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
                continue
            if isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
                continue
            raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
    if current is None:
        raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
    if not isinstance(current, list):
        raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
    return _expand_rows(current, spec)


def _reject_non_finite(value: object, spec: ModelSpec) -> None:
    """Refuse NaN/Infinity published as JSON floats rather than rounding them."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")


def _reject_bool_for_numeric(column: ColumnSpec, value: object, spec: ModelSpec) -> None:
    """Refuse a published boolean where the declaration says a number.

    A row built through a domain's contract class carries only that class's annotation, and pydantic
    widens ``True`` to ``1`` for an ``int`` field. The declared column kind is what records that
    upstream published a flag, so the refusal is judged on the declaration, not on the row class.
    """
    if column.kind in ("int", "float") and isinstance(value, bool):
        raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID", rejected=(column.name,))


def _stamped_value(plan: DerivePlan, query: object, spec: ModelSpec) -> object:
    """Produce one column's value from something that is not a field of the record.

    ``source_kind="parameter"`` stamps a validated query parameter onto every row, which is the
    shape of the hand-written ``{"symbol": query.symbol, **item}`` an endpoint that answers with
    bare observations needs in order to record what it was asked for; ``"constant"`` publishes the
    text the declaration itself carries. Either way the plan's stages run on the stamped value
    exactly as they run on a published one, so a stamped ``"2024-03"`` is the same declared date
    cast as a published ``2024-03`` would be. The parameter is read as the validated attribute, not
    as its encoded upstream spelling: the column documents what the caller asked for.

    Raises:
        ValueTransformError: Propagated untouched from a stage the kernel refuses.
        ProviderEngineError: ``*_SHAPE_INVALID`` for a source kind this seam does not route, the
            same guard :func:`_expand_rows` keeps for a stage it cannot apply.
    """
    if plan.source_kind == "parameter":
        raw: object = getattr(query, plan.source, None)
    elif plan.source_kind == "constant":
        raw = plan.source
    else:
        raise ProviderEngineError(
            f"{spec.error_prefix}_SHAPE_INVALID",
            rejected=(f"derive source kind {plan.source_kind} is not routed",),
        )
    if raw is None:
        # An omitted parameter stamps nothing. ``None`` keeps the engine's absent-versus-empty
        # discipline: the row says the caller did not say, and a non-nullable required column
        # refuses at the row model rather than inventing a value.
        return None
    return apply_derive(plan, raw)


def _is_null_sentinel(column: ColumnSpec, value: object) -> bool:
    """Whether one raw published value is a spelling this column declares as no value.

    The comparison is exact, case-sensitive and only ever made against text: ``null_sentinel`` is
    admitted as a ``tuple[str, ...]`` by :class:`ColumnSpec`, so a published ``0`` or ``False`` is
    never caught by a spelling that meant the string ``"0"``. The position is the engine's copy of
    the upstream ``mode="before"`` validators that own these rules -- benzinga's empty-string
    rewrite (``price_target.py:259-261``), intrinio's (``insider_trading.py:110-118``), the Fed
    releases' ``ND``/``NA``/``N/A`` maps (``money_measures.py:85``, ``sofr.py:46-50``,
    ``svensson_yield_curve.py:1062-1082``) -- each of which returns the null *before* coercing,
    which is why :func:`normalize_record` skips the column's declared stages when it fires.

    Args:
        column: The declared column, whose ``null_sentinel`` may be empty.
        value: The value the record (or the response envelope) published under the key the column
            reads.

    Returns:
        ``True`` only when the column declares sentinels and this value is one of them.
    """
    if not column.null_sentinel:
        return False
    return isinstance(value, str) and value in column.null_sentinel


def normalize_record(
    record: object,
    document: object,
    spec: ModelSpec,
    row_model: type[ContractModel],
    query: object = None,
) -> ContractModel:
    """Build one contract row from a raw record, keeping nulls as nulls.

    A column the declaration derives is the only column whose value is not a copy: its raw value
    comes from the key its plan names (which may be a key the model publishes no column for), from
    a query parameter, or from the declaration itself, and it is then run through the plan's stages
    by :mod:`.value_transform`. Every other column -- and every declaration written before
    ``column_derives`` existed, whose default is empty -- takes ``record[source_key or name]``
    verbatim through the same two shape guards as before.

    A column that declares ``null_sentinel`` is the one read value that is not copied: text the
    declaration spells as "no value" becomes the same ``None`` an absent key becomes, and the
    column's own cast and rescale stages are skipped, because the upstream rule that nulls the
    spelling returns before it coerces. A declaration written before the field exists declares no
    sentinel and is therefore unchanged, cell for cell.

    Raises:
        ProviderEngineError: ``*_SHAPE_INVALID`` for a non-mapping record, a non-finite published
            float, a boolean in a numeric column, a missing required key or a row the declared
            types refuse.
        ValueTransformError: Propagated untouched from a declared derive stage whose input is not
            the shape the cast or rescale declares -- as :func:`_expand_rows` propagates
            :class:`RowExpansionError`, so a value the source did not publish stays attributed to
            the rule that refused it instead of becoming a silent null.
    """
    if not isinstance(record, dict):
        raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
    derives = {plan.column: plan for plan in spec.column_derives}
    values: dict[str, Any] = {}
    absent: list[ColumnSpec] = []
    for column in spec.columns:
        plan = derives.get(column.name)
        if plan is not None and plan.source_kind != "record":
            values[column.name] = _stamped_value(plan, query, spec)
            continue
        key = column.source_key or column.name
        if plan is not None and plan.source.strip():
            # A record-source plan may read a key the model publishes no column for; the column it
            # writes is still the declared one, so nothing undeclared reaches the row model.
            key = plan.source
        if key not in record:
            absent.append(column)
            continue
        value = record[key]
        if _is_null_sentinel(column, value):
            values[column.name] = None
            continue
        _reject_non_finite(value, spec)
        _reject_bool_for_numeric(column, value, spec)
        if plan is not None:
            value = apply_derive(plan, value)
        values[column.name] = value
    # A record value always wins: an envelope fact is a fallback for what the record does not
    # publish, not a replacement for what it does. ``row_envelope`` keys are declared columns, so
    # no response-level field can enter the row model as an undeclared extra.
    for column in absent:
        envelope_key = column.source_key or column.name
        if (
            column.name in spec.row_envelope
            and isinstance(document, dict)
            and envelope_key in document
        ):
            envelope_value = document[envelope_key]
            _reject_non_finite(envelope_value, spec)
            _reject_bool_for_numeric(column, envelope_value, spec)
            values[column.name] = (
                None if _is_null_sentinel(column, envelope_value) else envelope_value
            )
            continue
        if column.required:
            raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID", missing=(column.name,))
        values[column.name] = None
    try:
        return row_model(**values)
    except ValidationError as error:
        raise ProviderEngineError(
            f"{spec.error_prefix}_SHAPE_INVALID",
            rejected=sorted({str(item["loc"][0]) for item in error.errors() if item.get("loc")}),
        ) from None


def _credential_value(spec: ModelSpec) -> str:
    """Read the declared credential from the environment, failing before I/O."""
    if spec.credential is None:
        return ""
    value = os.environ.get(spec.credential)
    if value and value.strip():
        return value.strip()
    raise ProviderEngineError(f"{spec.error_prefix}_CREDENTIAL_MISSING")


def _http_get_json(
    url: str,
    params: dict[str, str],
    *,
    timeout: float,
    source: str,
    headers: Mapping[str, str] | None = None,
    decoder: str = "json",
) -> HttpResponse:
    """GET one response body through the governed client; patched in tests.

    ``decoder`` is ``"json"`` for every declaration that predates the body decoders, and that path
    is unchanged: the text is parsed, and a body that is not JSON is ``..._BAD_RESPONSE``. A
    delimited decoder keeps the bytes whole on :attr:`HttpResponse.payload` for
    :mod:`.decoders` to read, because its body was never going to parse as JSON.
    """
    from opendata.data.http_client import HttpFetchError, get_shared_http_client

    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            headers=dict(headers or {}),
            timeout=timeout,
            source=source,
        )
    except HttpFetchError as exc:
        raise ProviderEngineError(
            f"{source.upper()}_HTTP_ERROR", status=exc.status, url=exc.url
        ) from exc
    if decoder != "json":
        return HttpResponse(response.status_code, None, payload=response.content)
    try:
        return HttpResponse(response.status_code, json.loads(response.text))
    except json.JSONDecodeError:
        raise ProviderEngineError(
            f"{source.upper()}_BAD_RESPONSE", status=response.status_code, url=url
        ) from None


def _decode_document(spec: ModelSpec, response: HttpResponse, url: str) -> Any:  # noqa: ANN401
    """Return the record document the declared decoder reads out of one response.

    A JSON declaration gets ``response.document`` back -- the same object, from the same field, as
    before decoders existed. Anything else is decoded from the bytes the body actually was.
    """
    if not spec.decoder.delimited:
        return response.document
    from opendata.data.providers._engine.decoders import decode_response

    return decode_response(spec, response, url=url)


def _production_transport(spec: ModelSpec) -> HttpGet:
    """Return the send seam a declaration's body shape needs.

    The wrapper resolves ``_http_get_json`` at call time, so a suite that replaces the module
    attribute -- the engine's only I/O seam -- keeps working for a delimited declaration: the fake
    is called with the plain ``(url, params, *, timeout, source, headers)`` signature it already
    implements and supplies the raw body itself. Only the governed client is told which shape to
    read, and no declaration can route a test onto a socket.
    """
    if not spec.decoder.delimited:
        return _http_get_json

    def _get(
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return _http_get_json(
            url, params, timeout=timeout, source=source, headers=headers, decoder=spec.decoder.kind
        )

    return _get


def _classify_status(status: int, spec: ModelSpec, url: str) -> None:
    """Map a non-success status onto the model's stable failure code."""
    if 200 <= status < 300:
        return
    codes = {
        400: "QUERY_REJECTED",
        401: "CREDENTIAL_REJECTED",
        403: "CREDENTIAL_REJECTED",
        404: "RESOURCE_MISSING",
        422: "QUERY_REJECTED",
        429: "RATE_LIMITED",
    }
    if status in codes:
        raise ProviderEngineError(f"{spec.error_prefix}_{codes[status]}", status=status, url=url)
    if status >= 500:
        raise ProviderEngineError(
            f"{spec.error_prefix}_UPSTREAM_UNAVAILABLE", status=status, url=url
        )
    raise ProviderEngineError(f"{spec.error_prefix}_HTTP_ERROR", status=status, url=url)


def _page_request(
    spec: ModelSpec,
    base_params: dict[str, str],
    page_number: int,
    collected: int,
) -> dict[str, str]:
    """Add the paging keys for the requested page to the encoded query."""
    paging = spec.pagination
    params = dict(base_params)
    if paging.kind == "none":
        return params
    if paging.kind == "cursor":
        return params
    if paging.limit_key is not None and paging.limit_key not in params:
        params[paging.limit_key] = str(paging_page_size(spec))
    if paging.kind == "offset" and paging.offset_key is not None:
        params[paging.offset_key] = str(collected)
    if paging.kind == "page" and paging.offset_key is not None:
        params[paging.offset_key] = str(page_number)
    return params


def paging_page_size(spec: ModelSpec) -> int:
    """Return the page size the engine requests when the caller set none."""
    limit_key = spec.pagination.limit_key
    if limit_key:
        for parameter in spec.params:
            if parameter.encoded_key == limit_key and isinstance(parameter.default, int):
                return parameter.default
    return 100


def effective_page_size(spec: ModelSpec, params: Mapping[str, str]) -> int:
    """Return the page size the request actually carries, which is what a short page means.

    Two separate mistakes live here if the declared default is used blindly. The request would
    have to be rewritten to the default to match it, and that silently discards a caller who asked
    for a smaller page (the query stage's parameters are supposed to survive unchanged). And the
    comparison ``len(rows) < page_size`` is the engine's only evidence that the endpoint has no
    more pages: measured against the default rather than the size actually sent, a caller-set page
    size makes the first page look short, so paging stops after one page and a partial result is
    reported as whole.
    """
    limit_key = spec.pagination.limit_key
    if limit_key is None:
        return 0
    sent = params.get(limit_key)
    if sent is not None:
        try:
            parsed = int(sent)
        except ValueError:
            return paging_page_size(spec)
        if parsed > 0:
            return parsed
    return paging_page_size(spec)


def _next_cursor(document: object, spec: ModelSpec) -> str | None:
    """Read the continuation cursor from a response document."""
    field_name = spec.pagination.cursor_field
    if spec.pagination.strict_nullable_string:
        if not isinstance(document, dict) or field_name is None or field_name not in document:
            raise ProviderEngineError(
                f"{spec.error_prefix}_SHAPE_INVALID",
                missing=() if field_name is None else (field_name,),
            )
        value = document[field_name]
        if value is None:
            return None
        if isinstance(value, str) and value:
            return value
        raise ProviderEngineError(
            f"{spec.error_prefix}_SHAPE_INVALID",
            invalid=(field_name,),
        )
    if field_name is None or not isinstance(document, dict) or field_name not in document:
        return None
    value = document[field_name]
    if value in (None, "", 0, False):
        return None
    if isinstance(value, (str, int)):
        return str(value)
    raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")


def fetch_pages(
    spec: ModelSpec,
    query: object,
    ctx: FetchContext | None,
    *,
    transport: HttpGet | None = None,
    source: str = "",
    now: date | None = None,
) -> tuple[Any, ...]:
    """Fetch every page of a request, refusing to report a partial result as whole.

    ``now`` is the date a relative bound is measured against, read once for the whole fetch; it
    defaults to today, the way every reviewed upstream reads it. Both the query string and the path
    template are rendered from that one reading.

    Raises:
        ProviderEngineError: Missing credential, rejected parameters, an
            upstream status, a body that is not the declared shape, or a page
            bound reached before the result was complete.
        ValueError: A relative bound reached :func:`bound_request` with a clock that is not a bare
            calendar date.
    """
    timeout = ctx.timeout if ctx is not None and ctx.timeout is not None else DEFAULT_TIMEOUT
    # ``query`` is re-bound to its request-side view here, before either read: one clock fills every
    # relative bound the declaration carries, so a window cannot be addressed on one day and asked
    # for on the next. The row-side stages run outside this function on the caller's own object.
    query = bound_request(query, spec, now=now)
    base_params = encode_query(query, spec)
    if spec.credential:
        base_params[spec.credential_query_key or "api_key"] = _credential_value(spec)
    url = f"{spec.base_url.rstrip('/')}{render_path(spec, query)}"
    headers = dict(spec.static_headers)
    get: HttpGet = transport or _production_transport(spec)
    pages: list[object] = []
    collected = 0
    page_number = 1
    cursor: str | None = None
    seen_cursors: set[str] = set()
    paging = spec.pagination
    while True:
        params = _page_request(spec, base_params, page_number, collected)
        if paging.kind == "cursor" and cursor is not None and paging.cursor_key:
            params[paging.cursor_key] = cursor
        page_size = effective_page_size(spec, params)
        response = get(
            url,
            params,
            timeout=timeout,
            source=source or spec.error_prefix.lower(),
            headers=headers,
        )
        _classify_status(response.status, spec, url)
        document = _decode_document(spec, response, url)
        rows = resolve_rows(document, spec.rows_pointer, spec)
        pages.append(document)
        collected += len(rows)
        if paging.kind == "none":
            break
        if collected > paging.max_rows:
            raise ProviderEngineError(f"{spec.error_prefix}_INCOMPLETE", url=url)
        if paging.kind == "cursor":
            cursor = _next_cursor(document, spec)
            if cursor is None:
                break
            if paging.strict_nullable_string:
                if cursor in seen_cursors:
                    raise ProviderEngineError(f"{spec.error_prefix}_INCOMPLETE", url=url)
                seen_cursors.add(cursor)
            page_number += 1
            if page_number > paging.max_pages:
                raise ProviderEngineError(f"{spec.error_prefix}_INCOMPLETE", url=url)
            continue
        if len(rows) < page_size:
            break
        page_number += 1
        if page_number > paging.max_pages:
            raise ProviderEngineError(f"{spec.error_prefix}_INCOMPLETE", url=url)
    return tuple(pages)


def _declared_total(document: object, spec: ModelSpec) -> int | None:
    """Read the endpoint's declared total row count, when it publishes one."""
    key = spec.pagination.total_key
    if key is None or not isinstance(document, dict) or key not in document:
        return None
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _declared_totals(pages: Sequence[object], spec: ModelSpec) -> list[int]:
    """Collect the totals every page that publishes one declares.

    Summing them, which is what this face used to do, is wrong for the common shape: an endpoint
    that reports the size of the whole result repeats that number on every page, so three pages
    of a 40-row answer add up to 120 and the engine calls a complete answer incomplete. The
    reading the declaration can actually support is that the pages agree on one total and the
    rows collected equal it. A ``0`` is a published total (an empty result), not an absent one.
    """
    return [total for total in (_declared_total(page, spec) for page in pages) if total is not None]


def _rendered(value: object) -> str:
    """Render one validated parameter value the way a row filter compares it.

    ``str(True)`` is ``"True"``, so a gate declared as ``when_value="True"`` fires on a boolean
    parameter the way the upstream branch it mirrors does; a date renders ISO because that is the
    string a needle is written against.
    """
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _gate_applies(row_filter: RowFilterSpec, query: object) -> bool:
    """Whether one filter's gate is open for this query.

    A filter that declares no ``when_param`` always applies. Otherwise the parameter has to carry
    exactly ``when_value``: an omitted parameter (``None``) gates nothing, which is how a model
    declares "branch on what the caller asked for" without the engine owning the branch.
    """
    if not row_filter.when_param:
        return True
    value = getattr(query, row_filter.when_param, None)
    if value is None:
        return False
    return _rendered(value) == row_filter.when_value


def _needle(row_filter: RowFilterSpec, query: object) -> str:
    """The text this filter compares against, taken from the query when it declares a source."""
    if row_filter.value_param:
        return _rendered(getattr(query, row_filter.value_param, None))
    return row_filter.value


def _op_matches(
    op: RowFilterOp,
    cell: str,
    needle: str,
    alternatives: tuple[str, ...],
    ignore_case: bool,
) -> bool:
    """Evaluate one predicate against one published cell."""
    left, right = (cell.casefold(), needle.casefold()) if ignore_case else (cell, needle)
    if op == "contains":
        return right in left
    if op == "equals":
        return left == right
    if op == "not_equals":
        return left != right
    if op == "prefix":
        return left.startswith(right)
    options = (option.casefold() for option in alternatives) if ignore_case else alternatives
    return left in options


def _row_matches(row_filter: RowFilterSpec, record: Mapping[str, Any], needle: str) -> bool:
    """Whether one raw record satisfies one filter whose gate is already known to be open."""
    if not row_filter.columns:
        # Nothing to test, and a filter that tests nothing must not drop a row: an empty selection
        # and a legitimate no-hit answer would become indistinguishable.
        return True
    alternatives = (
        tuple(row_filter.value.split(ROW_FILTER_VALUE_SEPARATOR)) if row_filter.op == "in" else ()
    )
    hits: list[bool] = []
    for key in row_filter.columns:
        cell = record.get(key)
        if cell is None:
            # The record does not publish this key. Selecting is not validating, so the row simply
            # does not match -- it is not the source's shape that is wrong, the row is not it.
            hits.append(False)
            continue
        text = cell if isinstance(cell, str) else str(cell)
        hits.append(_op_matches(row_filter.op, text, needle, alternatives, row_filter.ignore_case))
    return all(hits) if row_filter.match == "all" else any(hits)


def apply_row_filters(records: list[Any], query: object, spec: ModelSpec) -> list[Any]:
    """Return the records the declaration's ``row_filters`` select, in published order.

    A client-side filter is a *selection* over what the source published, not a second opinion on
    whether the source was complete. That is why :meth:`EngineFetcher.transform_data` runs this
    after the declared-total agreement check: completeness is judged on the unfiltered record list,
    and ``row_filters`` can therefore never weaken that judgement -- a filter that shrank the
    answer first would turn an ``*_INCOMPLETE`` failure into a quiet short success.

    Rows are judged on the decoded record and before per-row normalization, so a filter can test a
    key the model does not publish (cboe searches ``description`` and ``index_symbol`` while its
    rows call them ``symbol``). Multiple filters are ANDed; within one filter ``match`` decides
    whether any or every column has to hit.

    Two declarations make a filter unable to judge anything, and both are read as "select nothing
    away": a filter that names no ``columns``, and one whose needle resolves to the empty string
    because the caller left the needle parameter out. Upstream's own substring search keeps every
    row for an empty needle, and a selection that silently discarded the whole published answer
    would be indistinguishable from a genuine no-hit result.

    Raises:
        ProviderEngineError: ``*_SHAPE_INVALID`` for a record that is not a mapping at all. A
            missing *key* is a non-match, but a record that is not a row would be refused by
            normalization anyway, and letting a filter discard it would present a malformed
            document as a legitimate empty result.
    """
    active: list[tuple[RowFilterSpec, str]] = []
    for row_filter in spec.row_filters:
        if not _gate_applies(row_filter, query):
            continue
        needle = _needle(row_filter, query)
        if not needle:
            continue
        active.append((row_filter, needle))
    if not active:
        return list(records)
    kept: list[Any] = []
    for record in records:
        if not isinstance(record, dict):
            raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
        if all(_row_matches(row_filter, record, needle) for row_filter, needle in active):
            kept.append(record)
    return kept


def _order_cell_class(value: object) -> str:
    """Which comparison class one published sort cell belongs to.

    ``bool`` is not a number here even though python makes it an ``int`` subclass: the engine
    already refuses a published boolean in a numeric column (:func:`_reject_bool_for_numeric`), so
    letting ``True`` order a weight column would rank a flag against quantities. A list or dict --
    what a nested value decodes to -- carries no order the engine could defend either.
    """
    if isinstance(value, bool):
        return "unrankable"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    return "unrankable"


def _order_limit(order: RowOrderSpec, query: object) -> int | None:
    """The row count this query caps the ordered records at, or ``None`` for no cap.

    A literal ``limit`` is the declaration's own number. ``limit_param`` mirrors how
    :class:`~opendata.data.providers._engine.spec.RowFilterSpec` takes its needle: the value is read
    off the *validated* query, so the parameter's declared type was already enforced and nothing is
    re-validated here.

    A cap that cannot be read caps nothing. An omitted parameter is "the caller did not say", the
    same reading an empty needle gets in :func:`apply_row_filters`; a non-positive or unparseable
    value is the caller's input rather than a bound the declaration was written against, and turning
    a published answer into an empty frame on the strength of it is the silent-empty failure class
    this engine refuses elsewhere.
    """
    if not order.limit_param:
        return order.limit
    raw: object = getattr(query, order.limit_param, None)
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    try:
        parsed = int(_rendered(raw).strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def apply_row_order(records: list[Any], query: object, spec: ModelSpec) -> list[Any]:
    """Return the records the declaration's ``order_by`` wants, in the declared order.

    The stage runs on what :func:`apply_row_filters` kept and before :func:`normalize_record` builds
    a row from it. Both ends of that placement are load-bearing. Ordering the *published* list would
    let a top-n decide how many rows the source "published", and completeness is judged on the
    published records -- so, exactly as with filtering, a short answer stays short no matter how few
    rows the cap keeps. Ordering after normalization would mean sorting rows by the names the
    declaration gave them, which is a different vocabulary: ``column`` names the raw record key.

    Earlier stages that already produced a sequence are reordered by nothing but this declaration:
    paging ran in :func:`fetch_pages`, and ``path.explode`` ran in :func:`_expand_rows` inside
    :func:`resolve_rows`, so an ordering sees the exploded child rows and overrides their published
    sequence on purpose. A model whose rows must stay in the source's own order -- a pivot's
    ascending timestamps -- declares no ``order_by``, and a declaration that both pages and caps is
    refused by :class:`ModelSpec` because the cap would only ever see one page.

    Ties keep published order. :func:`sorted` is stable and ``reverse=True`` preserves that
    stability, so a descending sort does not mirror the tied rows the way reversing the result list
    would: two records with the same cell come out in the order the source put them, whichever way
    the column is read.

    A record whose sort cell cannot be compared is placed **last**, never dropped and never a shape
    failure. "Refuse the record" would make an ordering facet a second selector -- the one thing a
    facet whose whole job is sequence must not become, since a row discarded over the shape of a key
    it did not publish is indistinguishable from a row a filter discarded -- while a record that
    publishes no such key, or one this column cannot order, says something about the source rather
    than about this query. The comparison class is the first rankable cell in published order: a
    column that starts with numbers and then yields text is not comparable across those cells, so
    the text ones join the trailing unrankable run. That run keeps published order, and the cap may
    cut it off -- an unrankable row is last, so a top-n of rankable rows never has one smuggled in
    ahead of a rankable row.

    A cap larger than the record count keeps every record: the count is applied by slicing, never by
    demanding that the source have published that many rows.

    Raises:
        ProviderEngineError: ``*_SHAPE_INVALID`` for a record that is not a mapping at all, the same
            guard :func:`apply_row_filters` keeps -- a record that is not a row is refused by
            normalization anyway, and discarding or repositioning it here would present a malformed
            document as an ordering outcome.
    """
    order = spec.order_by
    if order is None:
        return list(records)
    cap = _order_limit(order, query)
    rankable_class: str | None = None
    ranked: list[dict[str, Any]] = []
    unranked: list[Any] = []
    for record in records:
        if not isinstance(record, dict):
            raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
        cell = record.get(order.column)
        kind = "unrankable" if cell is None else _order_cell_class(cell)
        if kind == "unrankable":
            unranked.append(record)
            continue
        if rankable_class is None:
            rankable_class = kind
        if kind != rankable_class:
            unranked.append(record)
            continue
        ranked.append(record)
    # Every cell in ``ranked`` shares one comparison class, so the comparison below cannot raise:
    # the mixed-type case was routed to the unrankable run instead of coerced or guessed at.
    ordered = sorted(
        ranked,
        key=lambda record: record[order.column],
        reverse=order.direction == "desc",
    )
    kept = ordered + unranked
    return kept[:cap] if cap is not None else kept


def make_http_json_fetcher(source: str, spec: ModelSpec) -> type[Fetcher[Any, Any]]:
    """Build one provider model's fetcher class from its declaration."""
    declared_query_model = build_query_model(spec, source=source, market=spec.market)
    declared_row_model = build_row_model(spec)
    declared_capability = Capability(
        asset_class=spec.asset_class,
        domain=spec.domain,
        period=spec.period,
        market=spec.market,
        source=source,
        verified=False,
        notes=spec.notes,
    )

    class EngineFetcher(Fetcher[Any, Any]):
        """Fetcher generated from a fixed provider×model declaration."""

        async_mode = spec.async_mode
        canonical_model = spec.model
        capability = declared_capability
        model_spec = spec
        query_model = declared_query_model
        row_model = declared_row_model
        #: A transport seam for offline verification. ``None`` runs the governed HTTP client, so
        #: every production call still passes through admission, deadlines and attempt accounting;
        #: a test constructs the fetcher with a fake and never reaches a socket.
        http_transport: ClassVar[HttpGet | None] = None

        def transform_query(self, **kwargs: object) -> Any:  # noqa: ANN401 - generated query
            """Reject anything the declaration does not accept, before any I/O."""
            try:
                return declared_query_model.model_validate(kwargs)
            except ValidationError as exc:
                # The declaration and the caller are written by different people, and a code alone
                # cannot tell a typo in a parameter name from a missing one -- so the refusal splits
                # the validation errors into the three faces they came from: keys upstream does not
                # accept, required keys the caller left out, and keys whose value is the wrong
                # shape. Names only; values are never echoed.
                faces: dict[str, set[str]] = {"rejected": set(), "missing": set(), "invalid": set()}
                for error in exc.errors():
                    if not error["loc"]:
                        continue
                    kind = error["type"]
                    if kind == "missing":
                        face = "missing"
                    elif kind == "extra_forbidden":
                        face = "rejected"
                    else:
                        face = "invalid"
                    faces[face].add(str(error["loc"][0]))
                raise ProviderEngineQueryError(
                    f"{spec.error_prefix}_QUERY_INVALID",
                    rejected=sorted(faces["rejected"]),
                    missing=sorted(faces["missing"]),
                    invalid=sorted(faces["invalid"]),
                ) from None

        def extract_data(self, params: object, ctx: FetchContext) -> tuple[object, ...]:
            """Fetch every declared page and keep each raw response document."""
            if type(params) is not declared_query_model:
                raise ProviderEngineError(f"{spec.error_prefix}_QUERY_INVALID")
            return fetch_pages(
                spec,
                params,
                ctx,
                transport=self.http_transport,
                source=source,
            )

        def transform_data(self, raw: object, params: object) -> tuple[ContractModel, ...]:
            """Normalize the accumulated pages into declared contract rows.

            A page is either a response object or, for an endpoint whose whole document is the
            record list, the list itself. Both are declared shapes; anything else is a shape
            failure. ``params`` is consulted only to select rows -- the query stage already ran, so
            no value is re-validated here.
            """
            if type(raw) is not tuple or any(not isinstance(page, (dict, list)) for page in raw):
                raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
            pages = [(page, resolve_rows(page, spec.rows_pointer, spec)) for page in raw]
            # Completeness is judged on what the source *published*, before anything is selected: a
            # client-side filter is a selection over those records, so it runs after this check and
            # never inside it. Filtering first would let ``row_filters`` make a short answer look
            # agreeing -- 4 published, 1 kept, 1 expected -- which is precisely the weakening a
            # declaration must not be able to perform.
            published = sum(len(records) for _, records in pages)
            totals = _declared_totals(raw, spec)
            distinct = set(totals)
            if len(distinct) > 1:
                raise ProviderEngineError(f"{spec.error_prefix}_TOTAL_CONFLICT")
            if distinct:
                expected = totals[0]
                if published < expected:
                    raise ProviderEngineError(f"{spec.error_prefix}_INCOMPLETE")
                if published > expected:
                    raise ProviderEngineError(f"{spec.error_prefix}_TOTAL_CONFLICT")
            return tuple(
                normalize_record(record, page, spec, declared_row_model, params)
                for page, records in pages
                # The ordering stage reads what the selection kept, never the published list, so a
                # top-n cannot decide how many rows the source is credited with publishing.
                for record in apply_row_order(
                    apply_row_filters(records, params, spec),
                    params,
                    spec,
                )
            )

    # ``Any`` is the most that static typing can say about a model built at runtime, but the
    # catalog's parameter face is read at runtime with ``typing.get_type_hints`` on this method's
    # return annotation. Without the concrete class on the function object every generated model
    # advertises an empty parameter list, so a caller sees a callable that accepts nothing -- the
    # annotation is therefore carried per generated function, which is a fresh object each call.
    EngineFetcher.transform_query.__annotations__["return"] = declared_query_model

    published_name = f"{source.replace('_', ' ').title().replace(' ', '')}{spec.model}Fetcher"
    EngineFetcher.__name__ = published_name
    EngineFetcher.__qualname__ = published_name
    # The class reports the module that declares it, not the factory that built it. Design §7.1
    # derives a provider's routing label from its package directory, and the OpenBB map plane
    # admits a registry model only when it resolves to its exact local binding ``(module, name)``.
    EngineFetcher.__module__ = cast("str", sys._getframe(1).f_globals["__name__"])
    return EngineFetcher
