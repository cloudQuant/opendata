"""Declarative request BODY and body-driven paging for provider model engines.

The engine's only send seam today is ``_http_get_json``
(:mod:`~opendata.data.providers._engine.http_json`), which calls the governed
client's ``get``: an offset is written into the query keys
(``params[paging.offset_key] = str(collected)``) and never into a body, so a
model whose upstream request *is* a JSON document -- TreasuryPrices' POST
payload, TMX's GraphQL envelope, yfinance's screener criteria -- has no
declaration at all. This module is the missing data half: it builds the request
body from validated query fields and expresses body-driven paging as
"mutate this declared JSON pointer between page 1 and page N", so the transport
caller (integrated into ``ModelSpec`` serially by the engine owner) can execute
the plan without this kernel ever performing I/O.

Security is part of the contract, not a layer on top of it. The body is built
from user-supplied parameters, so every field is validated against its declared
type, a parameter no field declares is refused rather than passed through, and
a string is placed as a literal: nothing here templates, evaluates, attribute-
resolves or parses a dotted key. A body location is a ``tuple[str, ...]`` of
object keys, never a string that gets split, so a segment containing ``.`` or
``[`` addresses exactly one literal key and cannot reach into a structure the
declaration did not build.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import math
from dataclasses import dataclass
from datetime import date, time
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlencode

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opendata.data.providers._engine.spec import ValueKind

#: The value domains this kernel validates against -- the same ``ValueKind``
#: universe :class:`~opendata.data.providers._engine.spec.ParamSpec` declares.
_VALUE_KINDS = frozenset({"str", "int", "float", "bool", "date", "time", "enum", "str_list"})

#: The content types a body declaration can carry. ``form`` is only legal for a
#: flat body of scalars, because "a=b&c=d" has no nesting and no list grammar.
_CONTENT_TYPES = frozenset({"json", "form"})

#: The three paging faces this kernel drives: how the request document advances.
_PAGING_KINDS = frozenset({"offset", "page", "cursor"})

#: How deep a declared pointer may nest. Real bodies (GraphQL variables,
#: screener criteria) nest two or three levels; a bound turns a stray deep
#: path into a declaration error instead of an unbounded structure.
_MAX_PATH_DEPTH = 8

#: How many fields one body may declare, so an accidental cross-join of every
#: parameter into the payload is a refusal rather than a plausibly shaped body.
_MAX_BODY_FIELDS = 64

#: A response-echoed cursor is copied into the next body verbatim, so it is
#: bounded like any other user-reachable string: long enough for an opaque
#: server token, short enough that a malformed echo cannot grow the request.
_MAX_CURSOR_LENGTH = 1024

#: Body paging and serialization failure codes, stable and attributable the
#: way ``ProviderEngineError`` codes are. Values are never echoed into a
#: refusal; only the declared names the refusal concerns.
CODE_FIELD_UNDECLARED = "BODY_FIELD_UNDECLARED"
CODE_FIELD_MISSING = "BODY_FIELD_MISSING"
CODE_TYPE_INVALID = "BODY_TYPE_INVALID"
CODE_PATH_CONFLICT = "BODY_PATH_CONFLICT"
CODE_FORM_SHAPE_INVALID = "BODY_FORM_SHAPE_INVALID"
CODE_CURSOR_INVALID = "BODY_CURSOR_INVALID"
CODE_POINTER_INVALID = "BODY_POINTER_INVALID"
CODE_PAGING_INVALID = "BODY_PAGING_INVALID"


class RequestBodyError(ValueError):
    """Stable refusal of a body declaration, a body build or a paging plan.

    Attributes:
        code: One of the module's ``CODE_*`` constants.
        rejected: Names the refusal concerns (parameters, keys); never values.
    """

    def __init__(self, code: str, *, rejected: Sequence[str] = ()) -> None:
        """Store the code and the names it refuses."""
        self.code = code
        self.rejected = tuple(rejected)
        detail = "" if not self.rejected else f" rejected={list(self.rejected)}"
        super().__init__(code + detail)


def _check_identifier(value: object, label: str) -> str:
    """Return ``value`` when it is a usable python identifier."""
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
        raise ValueError(f"{label} must be an identifier, got {value!r}")
    return value


def _is_prefix(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    """Whether ``short`` nests strictly inside ``long`` as a declared path."""
    return len(short) < len(long) and long[: len(short)] == short


def validate_path_segments(segments: object, label: str) -> tuple[str, ...]:
    """Return ``segments`` as a declared pointer, or refuse.

    A pointer is a tuple of literal JSON object keys. A dotted string is
    rejected outright -- there is no "split on dots" anywhere in this kernel,
    so ``("query.filter",)`` names the single key ``"query.filter"`` and can
    never address into a nested structure the declaration did not build.
    """
    if not isinstance(segments, tuple):
        raise ValueError(f"{label} must be a tuple of literal key segments")
    if not 1 <= len(segments) <= _MAX_PATH_DEPTH:
        raise ValueError(f"{label} must hold between 1 and {_MAX_PATH_DEPTH} segments")
    for segment in segments:
        if not isinstance(segment, str) or not segment:
            raise ValueError(f"{label} segment must be a non-empty string, got {segment!r}")
    return segments


def _normalized_value(kind: str, value: object, enum: tuple[str, ...], label: str) -> object:
    """Coerce one validated parameter onto the JSON data model, refusing drift.

    A declared string is returned as exactly the string it is -- no format
    call, no template substitution, no attribute lookup -- and anything that
    is not already the declared domain (a ``"3"`` for an ``int``, a ``True``
    for an ``int``, a non-finite float) is refused by name.
    """
    if kind == "str":
        if not isinstance(value, str):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        number = float(value)
        if not math.isfinite(number):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return number
    if kind == "bool":
        if not isinstance(value, bool):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return value
    if kind == "date":
        if not isinstance(value, date):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return value.isoformat()
    if kind == "time":
        if not isinstance(value, time):
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return value.isoformat()
    if kind == "enum":
        if not isinstance(value, str) or value not in enum:
            raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
        return value
    # kind == "str_list": every element must already be exactly a str; a list
    # of mixed objects is not a declared string list.
    if not isinstance(value, (list, tuple)) or not all(type(item) is str for item in value):
        raise RequestBodyError(CODE_TYPE_INVALID, rejected=(label,))
    return [str(item) for item in value]


@dataclass(frozen=True)
class BodyFieldSpec:
    """One body slot: the parameter that fills it and where the value lands.

    Attributes:
        parameter: Name of the validated query parameter supplying the value;
            the name a refusal echoes.
        kind: Value domain enforced before the value is placed.
        path: Tuple of literal JSON object keys; the last is the leaf key and
            the preceding ones name the nesting the declaration builds.
        required: Whether omitting the parameter is a refusal.
        default: Value applied when an optional parameter is omitted; the
            field is then present with this value.
        enum: Allowed values when ``kind`` is ``"enum"``.
    """

    parameter: str
    kind: ValueKind
    path: tuple[str, ...]
    required: bool = False
    default: object = None
    enum: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a malformed field declaration at construction time."""
        _check_identifier(self.parameter, "body field parameter")
        if self.kind not in _VALUE_KINDS:
            raise ValueError(f"body field {self.parameter!r} has unsupported kind {self.kind!r}")
        if self.kind == "enum" and not self.enum:
            raise ValueError(f"enum body field {self.parameter!r} declares no values")
        if self.kind != "enum" and self.enum:
            raise ValueError("only enum body fields may declare enum values")
        if self.required and self.default is not None:
            raise ValueError(f"body field {self.parameter!r} cannot be required and defaulted")
        checked = validate_path_segments(self.path, f"field {self.parameter!r} path")
        object.__setattr__(self, "path", checked)
        if self.default is not None:
            # A default ships in every serialized body, so it must satisfy the
            # same domain the runtime values do -- checked when the record is
            # written, not when the first caller happens to omit the parameter.
            _normalized_value(self.kind, self.default, self.enum, self.parameter)

    @property
    def leaf_key(self) -> str:
        """The JSON object key this field's value is stored under."""
        return self.path[-1]


@dataclass(frozen=True)
class RequestBodySpec:
    """The whole send document as data: which fields, where, and in what form.

    Attributes:
        fields: The only slots a caller may fill; anything else is refused.
        content_type: ``"json"`` for a JSON object, ``"form"`` for a flat
            ``application/x-www-form-urlencoded`` body of scalar fields.
        notes: Why the body looks like this, for the review record.
    """

    fields: tuple[BodyFieldSpec, ...]
    content_type: Literal["json", "form"] = "json"
    notes: str = ""

    def __post_init__(self) -> None:
        """Reject a declaration this kernel could not serialize safely."""
        if self.content_type not in _CONTENT_TYPES:
            raise ValueError(f"unknown body content type {self.content_type!r}")
        if not self.fields:
            raise ValueError("a body declaration with no fields sends nothing")
        if len(self.fields) > _MAX_BODY_FIELDS:
            raise ValueError(f"a body may declare at most {_MAX_BODY_FIELDS} fields")
        seen_parameters: list[str] = []
        seen_paths: list[tuple[str, ...]] = []
        for field in self.fields:
            if not isinstance(field, BodyFieldSpec):
                raise ValueError("every body field must be a BodyFieldSpec")
            if field.parameter in seen_parameters:
                raise ValueError(f"body field {field.parameter!r} is declared twice")
            seen_parameters.append(field.parameter)
            for taken in seen_paths:
                # Equal paths overwrite each other; a path that nests inside
                # another would open a dict where a scalar already sits.
                if (
                    taken == field.path
                    or _is_prefix(taken, field.path)
                    or _is_prefix(field.path, taken)
                ):
                    raise ValueError(
                        f"body paths {taken!r} and {field.path!r} collide in one structure"
                    )
            seen_paths.append(field.path)
            if self.content_type == "form":
                if len(field.path) != 1:
                    raise ValueError(
                        f"a form body has no nesting, so field {field.parameter!r} "
                        f"cannot declare path {field.path!r}"
                    )
                if field.kind == "str_list":
                    raise ValueError(
                        f"a form body has no list grammar, so field {field.parameter!r} "
                        "cannot be a str_list"
                    )

    @property
    def content_type_header(self) -> str:
        """The Content-Type value the transport caller must send with this body."""
        if self.content_type == "form":
            return "application/x-www-form-urlencoded"
        return "application/json"

    @property
    def declared_parameters(self) -> frozenset[str]:
        """Every parameter name this body accepts -- and the only ones it does."""
        return frozenset(field.parameter for field in self.fields)


def build_request_body(spec: RequestBodySpec, values: Mapping[str, object]) -> dict[str, object]:
    """Project the validated query onto the declared body document.

    A parameter no field declares is refused (never passed through), a required
    field the caller omitted is refused, and an optional field with neither a
    value nor a default simply does not appear -- the required-vs-omitted
    distinction acceptance judges.

    Raises:
        RequestBodyError: ``BODY_FIELD_UNDECLARED`` for a value no field
            claims, ``BODY_FIELD_MISSING`` for an absent required field,
            ``BODY_TYPE_INVALID`` for a value outside its declared domain,
            ``BODY_PATH_CONFLICT`` for a build collision.
    """
    claimed = spec.declared_parameters
    undeclared = sorted(str(key) for key in values if key not in claimed)
    if undeclared:
        raise RequestBodyError(CODE_FIELD_UNDECLARED, rejected=undeclared)
    body: dict[str, object] = {}
    missing: list[str] = []
    for field in spec.fields:
        supplied = values.get(field.parameter)
        if supplied is None:
            if field.required:
                missing.append(field.parameter)
                continue
            if field.default is None:
                # Omitted and un-defaulted: the key is absent, not null. A
                # null would be a different request than the one declared.
                continue
            supplied = field.default
        placed = _normalized_value(field.kind, supplied, field.enum, field.parameter)
        _place(body, field.path, placed, field.parameter)
    if missing:
        raise RequestBodyError(CODE_FIELD_MISSING, rejected=sorted(missing))
    return body


def _place(body: dict[str, object], path: tuple[str, ...], value: object, label: str) -> None:
    """Write ``value`` at the declared pointer, building only declared levels."""
    current: dict[str, object] = body
    for segment in path[:-1]:
        level = current.get(segment)
        if level is None:
            created: dict[str, object] = {}
            current[segment] = created
            current = created
            continue
        if not isinstance(level, dict):
            raise RequestBodyError(CODE_PATH_CONFLICT, rejected=(label,))
        current = level
    if path[-1] in current:
        raise RequestBodyError(CODE_PATH_CONFLICT, rejected=(label,))
    current[path[-1]] = value


def serialize_body(spec: RequestBodySpec, body: Mapping[str, object]) -> bytes:
    """Return the canonical bytes for one built body.

    JSON is deterministic by construction: keys sorted at every level, no
    incidental whitespace, ASCII-escaped, non-finite numbers refused (a body
    built through :func:`build_request_body` never carries one) -- so two
    builds of the same declaration hash to the same bytes. A form body is the
    sorted ``key=value`` pairs of a flat scalar document, so ordering cannot
    drift between runs either.

    Raises:
        RequestBodyError: ``BODY_FORM_SHAPE_INVALID`` when a form body carries
            a nested value, a list or a non-scalar the grammar cannot spell.
    """
    if spec.content_type == "form":
        pairs: list[tuple[str, str]] = []
        for key, value in body.items():
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, (int, float)):
                text = str(value)
            elif isinstance(value, str):
                text = value
            else:
                raise RequestBodyError(CODE_FORM_SHAPE_INVALID, rejected=(key,))
            pairs.append((key, text))
        pairs.sort()
        return urlencode(pairs).encode("ascii")
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return canonical.encode("ascii")


def body_digest(serialized: bytes) -> str:
    """Return the reproducible sha256 hex digest of one serialized body."""
    return hashlib.sha256(serialized).hexdigest()


@dataclass(frozen=True)
class BodyPagingSpec:
    """Body-driven paging: where inside the body the page marker lives.

    This is the facet ``PaginationSpec`` cannot express: the engine writes
    ``params[paging.offset_key] = str(collected)`` into the query keys, so a
    model whose offset lives inside the JSON document has no paging at all.
    Here the location is a declared pointer, the plan says "this pointer holds
    this value on page N", and the cursor kind writes a token echoed from the
    response into the next body verbatim.

    Attributes:
        kind: ``offset`` advances by ``step`` rows, ``page`` writes the page
            number, ``cursor`` carries the response-echoed token.
        offset_path: Pointer holding the offset/page value (offset, page).
        limit_path: Pointer holding the page size, written on every page.
        cursor_path: Pointer the echoed cursor is written into (cursor).
        cursor_response_path: Pointer read out of the response document for
            the next cursor (cursor).
        total_response_path: Optional pointer to the declared total rows in
            the response (yfinance's ``total_results``).
        page_size: Rows requested per page; also the default offset step and
            the short-page threshold.
        start: Offset value on page 1 (offset kind only).
        step: Offset increment; defaults to ``page_size``.
        max_pages: Hard page bound; reaching it early is an incomplete stop.
        max_rows: Hard row bound; exceeding it is an incomplete stop.
        stop_on_short_page: Whether fewer rows than ``page_size`` ends paging.
    """

    kind: Literal["offset", "page", "cursor"]
    offset_path: tuple[str, ...] = ()
    limit_path: tuple[str, ...] = ()
    cursor_path: tuple[str, ...] = ()
    cursor_response_path: tuple[str, ...] = ()
    total_response_path: tuple[str, ...] = ()
    page_size: int = 100
    start: int = 0
    step: int | None = None
    max_pages: int = 50
    max_rows: int = 100_000
    stop_on_short_page: bool = True

    def __post_init__(self) -> None:
        """Reject a paging declaration this kernel could not drive."""
        if self.kind not in _PAGING_KINDS:
            raise ValueError(f"unknown body paging kind {self.kind!r}")
        if not 0 < self.page_size <= 100_000:
            raise ValueError("page_size must be between 1 and 100000")
        if not 0 < self.max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        if not 0 < self.max_rows <= 1_000_000:
            raise ValueError("max_rows must be between 1 and 1000000")
        if self.step is not None and (isinstance(self.step, bool) or self.step <= 0):
            raise ValueError("a declared step must be a positive int")
        if isinstance(self.start, bool) or self.start < 0:
            raise ValueError("start must be a non-negative int")
        if self.kind in ("offset", "page"):
            checked = validate_path_segments(self.offset_path, "offset_path")
            object.__setattr__(self, "offset_path", checked)
        if self.kind == "cursor":
            checked_cursor = validate_path_segments(self.cursor_path, "cursor_path")
            object.__setattr__(self, "cursor_path", checked_cursor)
            echo = validate_path_segments(self.cursor_response_path, "cursor_response_path")
            object.__setattr__(self, "cursor_response_path", echo)
        if self.limit_path:
            checked_limit = validate_path_segments(self.limit_path, "limit_path")
            object.__setattr__(self, "limit_path", checked_limit)
        if self.total_response_path:
            total = validate_path_segments(self.total_response_path, "total_response_path")
            object.__setattr__(self, "total_response_path", total)

    @property
    def effective_step(self) -> int:
        """Rows the offset advances per page; the page size unless declared."""
        return self.step if self.step is not None else self.page_size


@dataclass(frozen=True)
class BodyMutation:
    """One declared pointer moved between the page-1 body and this page.

    Attributes:
        path: The declared pointer the transport caller must mutate.
        previous: Value the page-1 body held there (``None`` when unset).
        value: Value this page's body must hold there.
    """

    path: tuple[str, ...]
    previous: object
    value: object


@dataclass(frozen=True)
class BodyPagePlan:
    """The executable page request as pure data.

    Attributes:
        page_number: 1-based page this plan describes.
        body: The complete page body, page-1 baseline plus paging writes.
        serialized: Canonical bytes of ``body``.
        digest: sha256 hex of ``serialized``.
        mutations: The pointer moves from page 1 to this page -- the form a
            transport caller applies to an already-built baseline.
        offset_value: The offset/page number written, when this kind writes one.
        cursor: The echoed cursor written, when this kind carries one.
    """

    page_number: int
    body: dict[str, object]
    serialized: bytes
    digest: str
    mutations: tuple[BodyMutation, ...]
    offset_value: int | None = None
    cursor: str | None = None


def _checked_cursor(value: object) -> str:
    """Validate a response-echoed cursor before it is copied into a body."""
    if not isinstance(value, str) or not value or len(value) > _MAX_CURSOR_LENGTH:
        raise RequestBodyError(CODE_CURSOR_INVALID)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise RequestBodyError(CODE_CURSOR_INVALID)
    return value


def plan_body_page(
    body_spec: RequestBodySpec,
    paging: BodyPagingSpec,
    values: Mapping[str, object],
    page_number: int,
    *,
    cursor: object = None,
) -> BodyPagePlan:
    """Build the page-N body and say which declared pointers moved.

    The page-1 baseline is :func:`build_request_body` plus the page-size and
    offset writes; page N mutates the declared offset pointer (or writes the
    echoed cursor). No I/O happens here: the returned plan is data the
    transport caller executes.

    Raises:
        RequestBodyError: Anything :func:`build_request_body` refuses, plus
            ``BODY_PAGING_INVALID`` for a bad page number or a cursor page past
            the first with no echoed cursor, ``BODY_CURSOR_INVALID`` for a
            cursor that is not a copyable token, and ``BODY_PATH_CONFLICT``
            when a paging pointer collides with a declared field value.
    """
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise RequestBodyError(CODE_PAGING_INVALID)
    baseline = build_request_body(body_spec, values)
    body = _clone(baseline)
    mutations: list[BodyMutation] = []

    if paging.limit_path:
        previous = _peek(body, paging.limit_path)
        _force(body, paging.limit_path, paging.page_size)
        mutations.append(BodyMutation(paging.limit_path, previous, paging.page_size))

    offset_value: int | None = None
    checked_cursor: str | None = None
    if paging.kind in ("offset", "page"):
        if paging.kind == "page":
            offset_value = page_number
        else:
            offset_value = paging.start + (page_number - 1) * paging.effective_step
        previous = _peek(body, paging.offset_path)
        _force(body, paging.offset_path, offset_value)
        mutations.append(BodyMutation(paging.offset_path, previous, offset_value))
    elif page_number == 1 and cursor is None:
        checked_cursor = None
    elif page_number > 1 and cursor is None:
        # Without the echoed token every later page would re-request page 1;
        # that is a broken drive, not a first page.
        raise RequestBodyError(CODE_PAGING_INVALID)
    else:
        checked_cursor = _checked_cursor(cursor)
        previous = _peek(body, paging.cursor_path)
        _force(body, paging.cursor_path, checked_cursor)
        mutations.append(BodyMutation(paging.cursor_path, previous, checked_cursor))

    serialized = serialize_body(body_spec, body)
    return BodyPagePlan(
        page_number=page_number,
        body=body,
        serialized=serialized,
        digest=body_digest(serialized),
        mutations=tuple(mutations),
        offset_value=offset_value,
        cursor=checked_cursor,
    )


def _clone(body: Mapping[str, object]) -> dict[str, object]:
    """Shallow-copy nested levels so a plan never mutates the baseline."""
    clone: dict[str, object] = {}
    for key, value in body.items():
        clone[key] = _clone(value) if isinstance(value, dict) else value
    return clone


def _peek(body: Mapping[str, object], path: tuple[str, ...]) -> object:
    """Return the value at a declared pointer, or ``None`` when unset."""
    current: object = body
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _force(body: dict[str, object], path: tuple[str, ...], value: object) -> None:
    """Write a paging value at its declared pointer, refusing a collision."""
    current: dict[str, object] = body
    for segment in path[:-1]:
        level = current.get(segment)
        if level is None:
            created: dict[str, object] = {}
            current[segment] = created
            current = created
            continue
        if not isinstance(level, dict):
            raise RequestBodyError(CODE_PATH_CONFLICT, rejected=(path[-1],))
        current = level
    current[path[-1]] = value


def read_declared_total(document: object, paging: BodyPagingSpec) -> int | None:
    """Follow the declared total pointer into a response document.

    A missing path means the page publishes no total and returns ``None``.

    Raises:
        RequestBodyError: ``BODY_POINTER_INVALID`` when the pointer lands on
            something that is not an int-shaped total.
    """
    if not paging.total_response_path:
        return None
    found = _read_pointer(document, paging.total_response_path)
    if found is None:
        return None
    if isinstance(found, bool) or not isinstance(found, (int, str)):
        raise RequestBodyError(CODE_POINTER_INVALID)
    try:
        return int(found)
    except ValueError:
        raise RequestBodyError(CODE_POINTER_INVALID) from None


def read_response_cursor(document: object, paging: BodyPagingSpec) -> str | None:
    """Follow the declared cursor pointer into a response document.

    A missing or empty token means the source has no next page. Anything the
    :func:`plan_body_page` copy rules would refuse is refused here, so a
    malformed echo never reaches the next body.
    """
    if not paging.cursor_response_path:
        return None
    found = _read_pointer(document, paging.cursor_response_path)
    if found is None or found == "":
        return None
    if not isinstance(found, str):
        raise RequestBodyError(CODE_CURSOR_INVALID)
    return _checked_cursor(found)


def _read_pointer(document: object, path: tuple[str, ...]) -> object:
    """Walk declared object keys only; a list is not addressable here."""
    current: object = document
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def paging_decision(
    paging: BodyPagingSpec,
    *,
    page_number: int,
    rows_returned: int,
    collected: int,
    total: int | None = None,
    next_cursor: str | None = None,
) -> Literal["complete", "continue", "bound_reached"]:
    """Judge one fetched page against the declared stop condition.

    Returns ``"complete"`` when the source is exhausted, ``"bound_reached"``
    when a declared bound stopped paging before completion -- the transport
    caller must report that as incomplete, never as a whole result -- and
    ``"continue"`` when the caller should plan page ``page_number + 1``. A
    source that ends on the bound page is complete: the bound only refuses to
    keep driving a source that showed no sign of stopping.
    """
    source_exhausted = rows_returned == 0 or (
        paging.stop_on_short_page and rows_returned < paging.page_size
    )
    if total is not None and collected >= total:
        return "complete"
    if source_exhausted:
        return "complete"
    if paging.kind == "cursor" and next_cursor is None:
        return "complete"
    if page_number >= paging.max_pages or collected > paging.max_rows:
        return "bound_reached"
    return "continue"
