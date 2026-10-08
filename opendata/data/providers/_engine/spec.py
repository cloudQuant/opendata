"""Shared declaration primitives for provider model engines.

Provider packages declare their models as data (:class:`ModelSpec`) and the
engine turns each declaration into a concrete ``Fetcher``. Declaring the shape
here rather than hand-writing one class per upstream model is what keeps the
350 fixed provider×model tasks reviewable: every interface fact a task needs is
visible in one record, and the same validation and rejection semantics apply to
all of them.

Nothing in this package performs I/O or imports an upstream SDK.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Literal

#: Value domains the engine can encode, validate and normalize.
ValueKind = Literal["str", "int", "float", "bool", "date", "enum", "str_list"]

#: Pagination strategies the engine can drive and bound.
PaginationKind = Literal["none", "offset", "page", "cursor"]

#: Async applicability, mirroring design §6.16 and AC2-16.
AsyncMode = Literal["bounded_thread", "unsupported"]

_IDENTIFIER_TYPES = frozenset({"str", "int", "float", "bool", "date", "enum", "str_list"})

#: A path template placeholder: ``{symbol}``. Anything else between braces is a malformed
#: declaration rather than a segment to encode.
_PATH_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _check_identifier(value: str, label: str) -> str:
    """Return ``value`` when it is a usable, non-``auto`` python identifier."""
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
        raise ValueError(f"{label} must be an identifier, got {value!r}")
    if value.casefold() == "auto":
        raise ValueError(f"{label} must not be the fallback source id")
    return value


@dataclass(frozen=True)
class ParamSpec:
    """One accepted request parameter: its type, upstream key and bound.

    Attributes:
        name: Parameter name callers use, and the name rejected requests echo.
        kind: Value domain enforced before any I/O.
        query_key: Name the upstream endpoint expects; defaults to ``name``.
        required: Whether omitting the parameter fails before any I/O.
        default: Value applied when the caller omits an optional parameter.
        enum: Allowed values when ``kind`` is ``"enum"``.
        note: Why the bound exists, for the review record.
    """

    name: str
    kind: ValueKind
    query_key: str | None = None
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        """Reject malformed parameter declarations at construction time."""
        _check_identifier(self.name, "parameter name")
        if self.kind not in _IDENTIFIER_TYPES:
            raise ValueError(f"parameter {self.name!r} has unsupported kind {self.kind!r}")
        if self.kind == "enum" and not self.enum:
            raise ValueError(f"enum parameter {self.name!r} declares no values")
        if self.kind != "enum" and self.enum:
            raise ValueError("only enum parameters may declare enum values")
        if self.query_key is not None and not self.query_key.strip():
            raise ValueError(f"parameter {self.name!r} has a blank query key")
        if self.default is not None and self.required:
            raise ValueError(f"parameter {self.name!r} cannot be required and defaulted")

    @property
    def encoded_key(self) -> str:
        """Return the upstream query-string key for this parameter."""
        return self.query_key or self.name


@dataclass(frozen=True)
class ColumnSpec:
    """One declared output column and how it is read out of the raw record.

    Attributes:
        name: Column name in the normalized row.
        source_key: Raw record key carrying the value; defaults to ``name``.
        kind: Declared value domain used for type enforcement.
        required: A raw record missing this key is a shape failure, not a null.
        nullable: Whether the upstream publishes an explicit null for it.
        units: Source unit of measure kept verbatim in the review record.
    """

    name: str
    kind: ValueKind = "str"
    source_key: str | None = None
    required: bool = False
    nullable: bool = True
    units: str = ""

    def __post_init__(self) -> None:
        """Reject malformed column declarations at construction time."""
        _check_identifier(self.name, "column name")
        if self.kind not in _IDENTIFIER_TYPES:
            raise ValueError(f"column {self.name!r} has unsupported kind {self.kind!r}")
        if self.source_key is not None and not self.source_key.strip():
            raise ValueError(f"column {self.name!r} has a blank source key")
        if not self.required and not self.nullable:
            raise ValueError(f"column {self.name!r} is optional but declared non-nullable")


@dataclass(frozen=True)
class PaginationSpec:
    """How pages are requested, bounded and proven complete.

    Attributes:
        kind: Strategy the endpoint implements.
        limit_key: Query key for the page size, when the endpoint takes one.
        offset_key: Query key for the row/page offset, for ``offset``/``page``.
        cursor_key: Query key carrying the returned cursor, for ``cursor``.
        cursor_field: Response field holding the next cursor.
        total_key: Response field holding the declared total, when published.
        max_pages: Hard bound; exceeding it is reported as incomplete.
        max_rows: Hard bound; exceeding it is reported as incomplete.
    """

    kind: PaginationKind = "none"
    limit_key: str | None = None
    offset_key: str | None = None
    cursor_key: str | None = None
    cursor_field: str | None = None
    total_key: str | None = None
    max_pages: int = 50
    max_rows: int = 100_000

    def __post_init__(self) -> None:
        """Reject a page bound that cannot be enforced or is unbounded."""
        if self.kind not in {"none", "offset", "page", "cursor"}:
            raise ValueError(f"unknown pagination kind {self.kind!r}")
        if not 0 < self.max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        if not 0 < self.max_rows <= 1_000_000:
            raise ValueError("max_rows must be between 1 and 1000000")
        if self.kind == "cursor" and not (self.cursor_key and self.cursor_field):
            raise ValueError("cursor pagination requires cursor_key and cursor_field")
        if self.kind in {"offset", "page"} and not self.limit_key:
            raise ValueError(f"{self.kind} pagination requires limit_key")
        # Without an offset key nothing in the request changes between pages, so the same page would
        # be re-requested until max_pages fired. A bound is not a paging strategy.
        if self.kind in {"offset", "page"} and not self.offset_key:
            raise ValueError(f"{self.kind} pagination requires offset_key")


@dataclass(frozen=True)
class ModelSpec:
    """One fixed provider×model task expressed as an executable declaration.

    Attributes:
        model: Canonical upstream model name; the task identity's last part.
        domain: Routing domain this model answers for.
        asset_class: Capability asset class.
        period: Capability period granularity.
        market: Capability market, or ``"all"`` for market-agnostic catalogs.
        base_url: Default endpoint root, overridable by settings.
        path: Request path template, without scheme and query string.
        params: Accepted request parameters.
        columns: Declared normalized output columns, in published order.
        rows_pointer: Dotted path to the record list inside the response.
        pagination: Page driving and completeness bounds.
        credential: Environment variable holding the access token, if any.
        credential_query_key: Query key the endpoint expects for that token.
        async_mode: AC2-16 applicability declaration for this model.
        scenario: Consumer scenario the model serves, for the review record.
        error_prefix: Stable failure code prefix, usually the source in caps.
        row_envelope: Response keys copied onto every row as source facts.
        notes: Anything a reviewer must not infer from the code alone.
    """

    model: str
    domain: str
    asset_class: str
    period: str
    market: str
    base_url: str
    path: str
    columns: tuple[ColumnSpec, ...]
    params: tuple[ParamSpec, ...] = ()
    rows_pointer: str = ""
    pagination: PaginationSpec = field(default_factory=PaginationSpec)
    credential: str | None = None
    credential_query_key: str | None = None
    async_mode: AsyncMode = "bounded_thread"
    scenario: str = ""
    error_prefix: str = "PROVIDER"
    row_envelope: tuple[str, ...] = ()
    notes: str = ""

    @property
    def path_placeholders(self) -> tuple[str, ...]:
        """Names this model's path interpolates out of the validated query.

        A path template is how an upstream encodes a resource in the URL rather than the query
        string (``/quotes/{symbol}.json``). Declaring it here, rather than letting a fetcher build
        a URL by string concatenation, keeps the request shape reviewable from the record alone:
        the placeholder names are checked against the declared parameters at construction time, and
        the engine percent-encodes each substituted value, so no caller-supplied text can open a
        second path segment or reach the query string.
        """
        return tuple(_PATH_PLACEHOLDER.findall(self.path))

    def __post_init__(self) -> None:
        """Reject declarations that cannot produce a reviewable fetcher."""
        _check_identifier(self.model, "model name")
        _check_identifier(self.domain, "domain name")
        for label in ("asset_class", "period", "market"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-blank string")
        if not self.base_url.startswith("https://"):
            raise ValueError(f"{self.model}: base_url must be an https origin")
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ValueError(f"{self.model}: path must be a bare absolute path")
        if not self.columns:
            raise ValueError(f"{self.model}: a model without declared columns is metadata only")
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.model}: duplicate column names")
        # A response-level fact is published as a row column, so an envelope key that is not a
        # declared column would make the engine hand the row model an undeclared field.
        for envelope_key in self.row_envelope:
            if envelope_key not in names:
                raise ValueError(
                    f"{self.model}: row envelope key {envelope_key!r} is not a declared column"
                )
        param_names = [parameter.name for parameter in self.params]
        if len(param_names) != len(set(param_names)):
            raise ValueError(f"{self.model}: duplicate parameter names")
        if self.credential is not None and not self.credential.isidentifier():
            raise ValueError(f"{self.model}: credential must name an environment variable")
        if self.credential and not self.credential_query_key:
            raise ValueError(f"{self.model}: a credential must declare its query key")
        if self.async_mode not in {"bounded_thread", "unsupported"}:
            raise ValueError(f"{self.model}: unsupported async_mode {self.async_mode!r}")
        if not self.scenario.strip():
            raise ValueError(f"{self.model}: a task without a scenario is not reviewable")
        if not self.error_prefix.isupper():
            raise ValueError(f"{self.model}: error_prefix must be upper-case")
        # Brace hygiene is checked before name resolution: ``"/a/{b"`` yields no match for the
        # placeholder pattern, so a template that cannot be substituted would otherwise be
        # accepted and then sent upstream with its braces intact.
        if self.path.count("{") != len(self.path_placeholders) or self.path.count("}") != len(
            self.path_placeholders
        ):
            raise ValueError(f"{self.model}: path has a malformed {{placeholder}}")
        declared = {parameter.name for parameter in self.params}
        for placeholder in self.path_placeholders:
            if placeholder not in declared:
                raise ValueError(
                    f"{self.model}: path placeholder {placeholder!r} is not a declared parameter"
                )
        # ``render_path`` refuses a placeholder with no value, but only at fetch time; a placeholder
        # that is optional and has no default is a declaration that cannot address a resource on its
        # own, so the task author is told when the record is written rather than on first query.
        by_name = {parameter.name: parameter for parameter in self.params}
        for placeholder in self.path_placeholders:
            parameter = by_name[placeholder]
            if not parameter.required and parameter.default is None:
                raise ValueError(
                    f"{self.model}: path placeholder {placeholder!r} must be required or defaulted"
                )
