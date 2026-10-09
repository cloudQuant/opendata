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

#: Body shapes the engine decodes into record rows. ``json`` reads the parsed document; the
#: delimited kinds read the response bytes themselves, never a file name or a URL suffix.
BodyDecoderKind = Literal["json", "csv", "tsv", "zip_csv"]

#: Pagination strategies the engine can drive and bound.
PaginationKind = Literal["none", "offset", "page", "cursor"]

#: Row predicates the engine evaluates client-side over the records a source published.
RowFilterOp = Literal["contains", "equals", "not_equals", "in", "prefix"]

#: Async applicability, mirroring design §6.16 and AC2-16.
AsyncMode = Literal["bounded_thread", "unsupported"]

_IDENTIFIER_TYPES = frozenset({"str", "int", "float", "bool", "date", "enum", "str_list"})

#: The ops :class:`RowFilterSpec` can evaluate, checked against the literal because a declaration
#: that reached the fetch stage with an unknown op would select nothing and come back as a
#: legitimate empty result rather than as the typo it is.
_ROW_FILTER_OPS = frozenset({"contains", "equals", "not_equals", "in", "prefix"})

#: How a filter combines its columns: one hit selects the row (``any``) or every column must hit.
_ROW_FILTER_MATCHES = frozenset({"any", "all"})

#: The separator ``op="in"`` publishes its alternatives with. ``|`` is already this format's list
#: separator -- the character the FINRA ``zip_csv`` bodies delimit their fields with -- so a needle
#: list needs no new syntax and cannot be mistaken for a comma inside one value.
ROW_FILTER_VALUE_SEPARATOR = "|"

#: The decoder kinds whose body is delimited text rather than a JSON document.
DELIMITED_DECODERS = frozenset({"csv", "tsv", "zip_csv"})

#: The delimiter each decoder kind means when the declaration does not override it. ``|`` is what
#: FINRA's download endpoints publish, which is why the delimiter is declarable at all.
_DEFAULT_DELIMITER = {"json": "", "csv": ",", "tsv": "\t", "zip_csv": ","}

#: The text encodings a delimited body is tried in, in order, when the declaration names none.
#: ``utf-8-sig`` first because OECD writes SDMX-CSV with a byte-order mark.
_DEFAULT_ENCODINGS = ("utf-8-sig", "cp1252")

#: How many preamble rows a decoder may be told to skip. A real published preamble is a handful of
#: metadata lines (the Fed's is five), and a count large enough to swallow a whole table would turn
#: every short body into an empty-table refusal -- so the bound is declared, not inferred.
_MAX_PREAMBLE_ROWS = 100

#: A path template placeholder: ``{symbol}``. Anything else between braces is a malformed
#: declaration rather than a segment to encode.
_PATH_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: An HTTP field name as RFC 7230 defines the token grammar. A declared header name that is not one
#: of these cannot be sent, and a name built from arbitrary text is a header-injection vector.
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")

#: A query-string key as these endpoints spell one. ``&`` and ``=`` are the query's own structure,
#: so a declared key containing either would address a second parameter from inside a constant.
_QUERY_KEY = re.compile(r"[A-Za-z0-9_.~-]+\Z")


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
class DecoderSpec:
    """How one response body becomes the record rows the engine normalizes.

    The default is exactly what the engine did before decoders existed: read the parsed JSON
    document. A delimited declaration says something the transport cannot infer -- OECD publishes
    SDMX-CSV, FINRA publishes ``|``-separated CSV, Fama-French and the SEC ``EquityFTD`` feed ship
    the CSV inside a zip -- and the engine then reads the response *bytes* it was given, so no
    code ever guesses a shape from a file extension or a URL suffix.

    Attributes:
        kind: Body shape the engine is allowed to decode.
        delimiter: Single character separating fields; defaults per ``kind``.
        member: The archive member a ``zip_csv`` body must be read from.
        encoding: Text encoding tried first; defaults to UTF-8 (BOM-tolerant) then CP1252.
        preamble_rows: Non-blank rows the body puts before its header, counted the way the source
            publishes them rather than guessed. The Fed's ``Output.aspx`` downloads lead with five
            ``Series Description``/``Unit:``/``Multiplier:``/``Currency:``/``Unique Identifier:``
            metadata rows and upstream discards them by handing pandas ``header=5``, which counts
            non-blank rows because blank-line skipping happens first -- so this field skips leading
            rows from the same blank-filtered row list the header is read out of.
    """

    kind: BodyDecoderKind = "json"
    delimiter: str | None = None
    member: str | None = None
    encoding: str | None = None
    preamble_rows: int = 0

    def __post_init__(self) -> None:
        """Reject a decoder declaration the engine cannot carry out."""
        if self.kind not in {"json", "csv", "tsv", "zip_csv"}:
            raise ValueError(f"unknown decoder kind {self.kind!r}")
        if isinstance(self.preamble_rows, bool) or not isinstance(self.preamble_rows, int):
            raise ValueError(f"preamble_rows must be an int, got {self.preamble_rows!r}")
        if not 0 <= self.preamble_rows <= _MAX_PREAMBLE_ROWS:
            raise ValueError(
                f"preamble_rows must be between 0 and {_MAX_PREAMBLE_ROWS}, "
                f"got {self.preamble_rows}"
            )
        if self.kind == "json":
            if self.delimiter is not None or self.member is not None:
                raise ValueError("a json decoder declares no delimiter and no zip member")
            if self.preamble_rows:
                raise ValueError("a json document has no header row, so it has no preamble to skip")
            return
        if self.delimiter is not None and (len(self.delimiter) != 1 or self.delimiter in '\r\n"'):
            raise ValueError(f"delimiter {self.delimiter!r} must be one non-line character")
        if self.encoding is not None and not self.encoding.strip():
            raise ValueError("an encoding declaration cannot be blank")
        if self.kind == "zip_csv" and not (self.member or "").strip():
            raise ValueError("a zip_csv decoder must name the member it reads")
        if self.kind in {"csv", "tsv"} and self.member is not None:
            raise ValueError(f"a {self.kind} body is not an archive, so it has no member to select")

    @property
    def delimited(self) -> bool:
        """Whether the body is read as delimited text instead of as a JSON document."""
        return self.kind in DELIMITED_DECODERS

    @property
    def effective_delimiter(self) -> str:
        """Return the delimiter this decoder reads, declared or kind-default."""
        return self.delimiter if self.delimiter is not None else _DEFAULT_DELIMITER[self.kind]

    @property
    def encodings(self) -> tuple[str, ...]:
        """Return the text encodings tried for this body, declared first when given."""
        if self.kind == "json":
            return _DEFAULT_ENCODINGS
        declared = (self.encoding or "").strip()
        if not declared:
            return _DEFAULT_ENCODINGS
        return (declared, *(name for name in _DEFAULT_ENCODINGS if name != declared))


@dataclass(frozen=True)
class RowFilterSpec:
    """One client-side selection over the records a source published.

    The engine's other two read-facets -- ``rows_pointer`` and :class:`ColumnSpec` -- say where the
    records are and which keys to take out of them. Neither can say "drop the ones that do not
    match", so a model whose upstream answer is a *selection* over a published document (cboe's
    index search reads the whole directory and keeps the rows whose symbol or name contains the
    needle) had no declaration at all and was recorded as not declarable.

    The predicate is judged on the decoded record, so ``columns`` names raw record keys -- not
    declared output columns, and not necessarily the same names: the directory publishes
    ``index_symbol`` where the declaration publishes ``symbol``. A record that does not publish a
    tested key is a non-match rather than a shape failure, because selecting is not validating.

    Attributes:
        op: Predicate evaluated against each tested cell.
        columns: Record keys the predicate is evaluated against; empty means the filter tests
            nothing, and a filter that tests nothing cannot drop a row.
        value: Literal needle, or -- for ``op="in"`` -- the alternatives joined by ``|``.
        value_param: Name of a declared parameter whose validated value is the needle instead.
        when_param: Name of a declared parameter this filter is gated on.
        when_value: Rendered value of that parameter the gate fires on.
        ignore_case: Whether needle and cell are compared case-folded.
        match: ``any`` selects the row on one column hit, ``all`` requires every tested column.
    """

    op: RowFilterOp = "contains"
    columns: tuple[str, ...] = ()
    value: str = ""
    value_param: str = ""
    when_param: str = ""
    when_value: str = ""
    ignore_case: bool = False
    match: Literal["any", "all"] = "any"

    def __post_init__(self) -> None:
        """Reject a predicate the engine cannot evaluate, or cannot evaluate unambiguously."""
        if self.op not in _ROW_FILTER_OPS:
            raise ValueError(f"row filter op {self.op!r} is not one of {sorted(_ROW_FILTER_OPS)}")
        if self.match not in _ROW_FILTER_MATCHES:
            raise ValueError(f"row filter match {self.match!r} is not 'any' or 'all'")
        for column in self.columns:
            if not isinstance(column, str) or not column.strip():
                raise ValueError(
                    f"a row filter column must be a non-blank record key, got {column!r}"
                )
        if self.value_param and self.value:
            raise ValueError(
                f"a {self.op} row filter takes its needle from value or value_param, not both"
            )
        if not self.value and not self.value_param:
            raise ValueError(
                f"a {self.op} row filter declares no needle: give it value or value_param"
            )
        if self.op == "in" and not self.value.strip():
            raise ValueError(
                "an 'in' row filter must publish its alternatives in value, separated by "
                f"{ROW_FILTER_VALUE_SEPARATOR!r}"
            )


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
        static_headers: Request headers the endpoint requires on every page.
        static_query: Query-string constants the endpoint requires on every request and a caller
            may not set -- the release id, the series hash, the payload format. These are the
            upstream code's own literals, not part of its accepted query model, so declaring them
            here instead of as defaulted parameters keeps ``params`` naming exactly what a caller
            may pass and stops a caller from re-addressing a reviewed model at a different series.
        notes: Anything a reviewer must not infer from the code alone.
        decoder: Body shape read into rows; the default is the JSON document path.
        row_filters: Client-side selections applied after the source's own completeness was judged;
            the default applies none, so every declaration written before row filters is unchanged.
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
    static_headers: tuple[tuple[str, str], ...] = ()
    static_query: tuple[tuple[str, str], ...] = ()
    notes: str = ""
    decoder: DecoderSpec = field(default_factory=DecoderSpec)
    # Declared last so a positional ``ModelSpec(...)`` built before row filters existed keeps
    # binding to the same fields.
    row_filters: tuple[RowFilterSpec, ...] = ()

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
        # A declared header is sent verbatim on every page, so it is checked as an input that
        # reaches the wire: an RFC 7230 token for the field name, no CR or LF in the value -- which
        # would let one declaration inject a second header -- and no name declared in two casings,
        # because HTTP field names are case-insensitive.
        seen_headers: list[str] = []
        for header in self.static_headers:
            if not isinstance(header, tuple) or len(header) != 2:
                raise ValueError(f"{self.model}: a static header is a (name, value) pair")
            name, value = header
            if not isinstance(name, str) or not _HEADER_TOKEN.match(name):
                raise ValueError(f"{self.model}: {name!r} is not an HTTP field name")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{self.model}: header {name!r} declares no value")
            if any(character in value for character in "\r\n"):
                raise ValueError(f"{self.model}: header {name!r} may not contain CR or LF")
            if name.casefold() in seen_headers:
                raise ValueError(f"{self.model}: header {name!r} is declared twice")
            seen_headers.append(name.casefold())
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
        # A static query constant is sent on every request and a caller may not set it, so it is
        # checked as an input that reaches the wire: a key from the query grammar, nothing in the
        # value that could break out of one line, and a key nothing else in this record already
        # claims. A parameter, the credential or a paging key written under the same name would land
        # in the same dict, and two spellings of one key make the request the transport sends differ
        # from the request the declaration describes.
        claimed_keys = {
            parameter.encoded_key
            for parameter in self.params
            if parameter.name not in self.path_placeholders
        }
        for key in (
            self.credential_query_key,
            self.pagination.limit_key,
            self.pagination.offset_key,
            self.pagination.cursor_key,
        ):
            if key:
                claimed_keys.add(key)
        seen_query_keys: list[str] = []
        for entry in self.static_query:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError(f"{self.model}: a static query entry is a (key, value) pair")
            key, value = entry
            if not isinstance(key, str) or not _QUERY_KEY.match(key):
                raise ValueError(f"{self.model}: {key!r} is not a query-string key")
            if not isinstance(value, str) or any(character in value for character in "\r\n"):
                raise ValueError(f"{self.model}: static query {key!r} has no sendable value")
            if key in claimed_keys:
                raise ValueError(
                    f"{self.model}: static query {key!r} is already claimed by a declared "
                    "parameter, a credential key or a paging key"
                )
            if key in seen_query_keys:
                raise ValueError(f"{self.model}: static query {key!r} is declared twice")
            seen_query_keys.append(key)
        # A delimited body *is* the record list, one dict per line keyed by the header, so a pointer
        # into a nested document is a declaration no decoder can honour. Refusing it here is what
        # keeps a wrong-member or wrong-delimiter failure from being mistaken for a shape failure.
        if self.decoder.delimited and self.rows_pointer:
            raise ValueError(
                f"{self.model}: a {self.decoder.kind} body has no nested record list, so "
                f"rows_pointer {self.rows_pointer!r} cannot be followed"
            )
        # A row filter reaches the fetch stage as data, so every name it spells is resolved against
        # this record's own parameters here rather than as an attribute lookup that silently finds
        # nothing. A filter whose needle parameter does not exist would search for an empty string
        # and report the whole published answer as a legitimate no-hit result -- the same reason
        # ``path_placeholders`` must name a declared parameter.
        for index, row_filter in enumerate(self.row_filters):
            label = f"{self.model}: row filter {index} ({row_filter.op})"
            if row_filter.value_param and row_filter.value_param not in declared:
                raise ValueError(
                    f"{label} takes its needle from value_param {row_filter.value_param!r}, "
                    f"which {self.model} does not declare as a parameter"
                )
            if row_filter.when_param and row_filter.when_param not in declared:
                raise ValueError(
                    f"{label} is gated on when_param {row_filter.when_param!r}, "
                    f"which {self.model} does not declare as a parameter"
                )
            if not row_filter.columns and not row_filter.when_param:
                raise ValueError(
                    f"{label} tests no record keys and gates on no parameter, so it is not a "
                    f"selection {self.model} can carry out"
                )
