"""Turn a :class:`~opendata.data.providers._engine.spec.ModelSpec` into a fetcher.

One factory covers every JSON REST provider model in the fixed 350-task ledger:
it builds the typed query, rejects bad requests before any byte is sent, drives
and bounds pagination, classifies upstream failures into stable codes, and
normalizes records into contract rows whose columns are exactly the declared
ones. Per-model code therefore stays a declaration, and the semantics that
acceptance judges (parameter rejection, empty-versus-missing, page
completeness, unit fidelity) are written once and tested uniformly.

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
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, cast

from pydantic import BeforeValidator, ConfigDict, ValidationError, create_model

from opendata.data.capability import Capability
from opendata.data.domains import contract_model, load_domains
from opendata.data.models.base import ContractModel
from opendata.data.protocol import FetchContext, Fetcher

if TYPE_CHECKING:
    from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec

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
    ) -> None:
        """Store the code and the request that produced it."""
        self.code = code
        self.status = status
        self.url = url
        self.rejected = tuple(rejected)
        self.missing = tuple(missing)
        detail = "" if status is None else f" status={status}"
        detail += "" if url is None else f" url={url}"
        detail += "" if not self.rejected else f" rejected={list(self.rejected)}"
        detail += "" if not self.missing else f" missing={list(self.missing)}"
        super().__init__(code + detail)


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


def build_query_model(spec: ModelSpec) -> type[Any]:
    """Build the strict typed query class accepting exactly the declared params."""
    fields: dict[str, tuple[Any, Any]] = {}
    for parameter in spec.params:
        annotation = _param_type(parameter)
        if parameter.required:
            fields[parameter.name] = (annotation, ...)
        elif parameter.default is not None:
            fields[parameter.name] = (annotation, parameter.default)
        else:
            fields[parameter.name] = (annotation | None, None)
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


def encode_query(query: object, spec: ModelSpec) -> dict[str, str]:
    """Project a validated query onto the endpoint's query-string parameters.

    A parameter the path template consumes is deliberately left out of the query string: an
    upstream that encodes the resource in the URL (``/quotes/{symbol}.json``) does not accept it
    twice, and sending it twice would make the logged request differ from the declared one.
    """
    encoded: dict[str, str] = {}
    consumed_by_path = set(spec.path_placeholders)
    for parameter in spec.params:
        if parameter.name in consumed_by_path:
            continue
        value = getattr(query, parameter.name, None)
        if value is None:
            continue
        encoded[parameter.encoded_key] = _encode_value(parameter, value, spec)
    return encoded


def render_path(spec: ModelSpec, query: object) -> str:
    """Substitute the declared path template with single-segment values from the validated query.

    Raises:
        ProviderEngineError: A placeholder has no value, or a value that is not provably one URL
            path segment. Both are refused before any socket opens: a value carrying ``/`` or
            ``?`` would silently address a different resource than the declaration describes.
    """
    path = spec.path
    for name in spec.path_placeholders:
        value = getattr(query, name, None)
        if value is None:
            raise ProviderEngineError(f"{spec.error_prefix}_QUERY_INVALID", url=path)
        text = value.isoformat() if isinstance(value, date) else str(value)
        if not _PATH_SEGMENT.match(text):
            raise ProviderEngineError(f"{spec.error_prefix}_PARAM_UNENCODABLE", url=path)
        path = path.replace(f"{{{name}}}", text)
    return path


def _encode_value(parameter: ParamSpec, value: object, spec: ModelSpec) -> str:
    """Render one parameter as an upstream query-string value."""
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


def resolve_rows(document: object, pointer: str, spec: ModelSpec) -> list[Any]:
    """Follow ``pointer`` into ``document`` and return the record list.

    An absent path is a shape failure; an empty list is a legitimate empty
    result. The two are never conflated, which is the distinction AC2-10 asks
    each model to make.
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
    return current


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


def normalize_record(
    record: object,
    document: object,
    spec: ModelSpec,
    row_model: type[ContractModel],
) -> ContractModel:
    """Build one contract row from a raw record, keeping nulls as nulls."""
    if not isinstance(record, dict):
        raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
    values: dict[str, Any] = {}
    absent: list[ColumnSpec] = []
    for column in spec.columns:
        key = column.source_key or column.name
        if key not in record:
            absent.append(column)
            continue
        value = record[key]
        _reject_non_finite(value, spec)
        _reject_bool_for_numeric(column, value, spec)
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
            _reject_non_finite(document[envelope_key], spec)
            _reject_bool_for_numeric(column, document[envelope_key], spec)
            values[column.name] = document[envelope_key]
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
) -> tuple[Any, ...]:
    """Fetch every page of a request, refusing to report a partial result as whole.

    Raises:
        ProviderEngineError: Missing credential, rejected parameters, an
            upstream status, a body that is not the declared shape, or a page
            bound reached before the result was complete.
    """
    timeout = ctx.timeout if ctx is not None and ctx.timeout is not None else DEFAULT_TIMEOUT
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


def make_http_json_fetcher(source: str, spec: ModelSpec) -> type[Fetcher[Any, Any]]:
    """Build one provider model's fetcher class from its declaration."""
    declared_query_model = build_query_model(spec)
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
                # cannot tell a typo in a parameter name from a missing one -- so the refusal says
                # which keys upstream does not accept. Names only; values are never echoed.
                raise ProviderEngineError(
                    f"{spec.error_prefix}_QUERY_INVALID",
                    rejected=sorted(
                        str(error["loc"][0])
                        for error in exc.errors()
                        if error["loc"] and error["type"] == "extra_forbidden"
                    ),
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
            failure, and ``params`` is not consulted here because the query stage already ran.
            """
            if type(raw) is not tuple or any(not isinstance(page, (dict, list)) for page in raw):
                raise ProviderEngineError(f"{spec.error_prefix}_SHAPE_INVALID")
            rows: list[ContractModel] = []
            for page in raw:
                rows.extend(
                    normalize_record(record, page, spec, declared_row_model)
                    for record in resolve_rows(page, spec.rows_pointer, spec)
                )
            published = _declared_totals(raw, spec)
            distinct = set(published)
            if len(distinct) > 1:
                raise ProviderEngineError(f"{spec.error_prefix}_TOTAL_CONFLICT")
            if distinct:
                expected = published[0]
                if len(rows) < expected:
                    raise ProviderEngineError(f"{spec.error_prefix}_INCOMPLETE")
                if len(rows) > expected:
                    raise ProviderEngineError(f"{spec.error_prefix}_TOTAL_CONFLICT")
            return tuple(rows)

    published_name = f"{source.replace('_', ' ').title().replace(' ', '')}{spec.model}Fetcher"
    EngineFetcher.__name__ = published_name
    EngineFetcher.__qualname__ = published_name
    # The class reports the module that declares it, not the factory that built it. Design §7.1
    # derives a provider's routing label from its package directory, and the OpenBB map plane
    # admits a registry model only when it resolves to its exact local binding ``(module, name)``.
    EngineFetcher.__module__ = cast("str", sys._getframe(1).f_globals["__name__"])
    return EngineFetcher
