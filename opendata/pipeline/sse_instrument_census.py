"""Parse bounded Shanghai Stock Exchange instrument response captures offline.

The adapter accepts only caller-supplied bytes and the four selector contexts
used by the vendored SSE stock-list request: current Main Board A, current STAR
Market, and the corresponding ``COMPANY_STATUS=3`` candidate lists. Selector
metadata is caller-declared context. It does not authenticate the source,
establish that the request reached SSE, validate a security identity, or prove
that rows cover any exchange or historical period. Pagination, source
authenticity, and market completeness therefore remain UNKNOWN.

Rows, ``pageHelp``, request summaries, response bytes, and locally computed
hashes are retained separately. Decoded JSON numbers use Python's finite
integer/float types; exact numeric tokens remain available only in the retained
response bytes. No field is promoted into an instrument identity or lifecycle
event. In particular, ``COMPANY_CODE`` is kept as an uninterpreted source field
and ``DELIST_DATE`` is not interpreted as termination or last-trading date.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import TypeAlias

__all__ = [
    "MAX_SSE_CENSUS_BODY_BYTES",
    "SSEInstrumentCensusCapture",
    "SSEInstrumentCensusError",
    "SSEInstrumentPageEvidence",
    "SSEInstrumentPageInput",
    "SSEInstrumentSelector",
    "SSEInstrumentSourceObservation",
    "build_sse_instrument_census",
]

MAX_SSE_CENSUS_BODY_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_CAPTURE_BYTES = 64 * 1024 * 1024
_MAX_REQUEST_SUMMARY_BYTES = 64 * 1024
_MAX_PAGES = 128
_MAX_ROWS = 100_000
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 2_200_000
_MAX_JSON_INTEGER_BITS = 4_096
_MAX_JSON_TEXT_CHARS = 16_384
_MAX_JSON_FIELDS = 256
_MAX_JSON_CONTAINER_ITEMS = 100_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = "sse.instrument"
_CURRENT_STATUSES = "2,4,5,7,8"
_MAPPING_PROXY_TYPE: type[object] = type(MappingProxyType({}))

FrozenJson: TypeAlias = (
    bool | int | float | str | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"] | None
)


class SSEInstrumentCensusError(ValueError):
    """A stable content-free refusal code for malformed SSE captures."""

    def __init__(self, code: str) -> None:
        """Create an error without embedding source values or response text.

        Args:
            code: Stable machine-readable refusal code.
        """
        self.code = code
        super().__init__(code)


class SSEInstrumentSelector(str, Enum):
    """One fixed caller-declared query context from the vendored SSE adapter."""

    CURRENT_MAIN_A = "current_main_a"
    CURRENT_STAR = "current_star"
    STATUS3_MAIN_A_CANDIDATE = "status3_main_a_candidate"
    STATUS3_STAR_CANDIDATE = "status3_star_candidate"


def _selector_parameters(selector: SSEInstrumentSelector) -> tuple[str, str]:
    """Return the fixed stock-type and status query values for a selector."""
    stock_type = (
        "8"
        if selector
        in (SSEInstrumentSelector.CURRENT_STAR, SSEInstrumentSelector.STATUS3_STAR_CANDIDATE)
        else "1"
    )
    status = (
        "3"
        if selector
        in (
            SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
            SSEInstrumentSelector.STATUS3_STAR_CANDIDATE,
        )
        else _CURRENT_STATUSES
    )
    return stock_type, status


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class SSEInstrumentPageInput:
    """One bounded response plus caller-declared selector and request summary.

    The summary describes the caller's request. It is not an exact-wire proof;
    the parser only checks that its declared stock type and status agree with
    the fixed selector family. Response hashes are recomputed from exact bytes.
    """

    selector: SSEInstrumentSelector | str
    sequence_number: int
    raw_response_body: bytes = field(repr=False, compare=False)
    declared_response_sha256: str
    request_summary: Mapping[str, object] | None = field(default=None, repr=False, compare=False)
    http_status_code: int = 200
    _summary_json: bytes | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate fixed query context and request/response resource bounds."""
        try:
            selector = SSEInstrumentSelector(self.selector)
        except (TypeError, ValueError):
            raise SSEInstrumentCensusError("selector_invalid") from None
        object.__setattr__(self, "selector", selector)
        if type(self.sequence_number) is not int or not 1 <= self.sequence_number <= _MAX_PAGES:
            raise SSEInstrumentCensusError("page_sequence_invalid")
        if type(self.raw_response_body) is not bytes:
            raise SSEInstrumentCensusError("response_body_type_invalid")
        if not self.raw_response_body:
            raise SSEInstrumentCensusError("response_body_empty")
        if len(self.raw_response_body) > MAX_SSE_CENSUS_BODY_BYTES:
            raise SSEInstrumentCensusError("response_body_too_large")
        if (
            type(self.declared_response_sha256) is not str
            or _SHA256_RE.fullmatch(self.declared_response_sha256) is None
        ):
            raise SSEInstrumentCensusError("response_digest_invalid")
        if type(self.http_status_code) is not int or not 200 <= self.http_status_code < 300:
            raise SSEInstrumentCensusError("successful_http_status_invalid")
        if self.request_summary is not None:
            if not isinstance(self.request_summary, Mapping):
                raise SSEInstrumentCensusError("request_summary_invalid")
            try:
                frozen = _freeze_json(self.request_summary, byte_limit=_MAX_REQUEST_SUMMARY_BYTES)
            except SSEInstrumentCensusError:
                raise
            except Exception:
                raise SSEInstrumentCensusError("request_summary_invalid") from None
            if not isinstance(frozen, Mapping):
                raise SSEInstrumentCensusError("request_summary_invalid")
            _validate_summary_selector(frozen, selector)
            encoded = _encode_canonical_summary(frozen)
            if len(encoded) > _MAX_REQUEST_SUMMARY_BYTES:
                raise SSEInstrumentCensusError("request_summary_too_large")
            object.__setattr__(self, "request_summary", frozen)
            object.__setattr__(self, "_summary_json", encoded)
        else:
            object.__setattr__(self, "_summary_json", None)

    def __repr__(self) -> str:
        """Return a bounded summary that does not expose response contents."""
        return (
            "SSEInstrumentPageInput("
            f"selector={SSEInstrumentSelector(self.selector).value!r}, "
            f"sequence_number={self.sequence_number}, "
            f"response_bytes={len(self.raw_response_body)}, response_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class SSEInstrumentSourceObservation:
    """One frozen raw SSE row with no canonical identity or lifecycle mapping."""

    raw_row: Mapping[str, FrozenJson] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Copy a bounded, nonempty JSON object without interpreting fields."""
        if not isinstance(self.raw_row, Mapping):
            raise SSEInstrumentCensusError("response_row_invalid")
        try:
            frozen = _freeze_json(self.raw_row)
        except SSEInstrumentCensusError:
            raise SSEInstrumentCensusError("response_row_invalid") from None
        if not isinstance(frozen, Mapping) or not frozen:
            raise SSEInstrumentCensusError("response_row_invalid")
        object.__setattr__(self, "raw_row", frozen)

    def __repr__(self) -> str:
        """Redact source values from ordinary object representations."""
        return (
            f"SSEInstrumentSourceObservation(field_count={len(self.raw_row)}, values_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class SSEInstrumentPageEvidence:
    """Parsed page fields and original request/response evidence for one selector."""

    selector: SSEInstrumentSelector
    sequence_number: int
    source_rows: tuple[SSEInstrumentSourceObservation, ...] = field(repr=False, compare=False)
    page_help: Mapping[str, FrozenJson] | None = field(repr=False, compare=False)
    request_summary: Mapping[str, FrozenJson] | None = field(repr=False, compare=False)
    request_summary_sha256: str | None
    response_sha256: str
    http_status_code: int
    raw_response_body: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate retained evidence and keep page representations redacted."""
        if type(self.selector) is not SSEInstrumentSelector:
            raise SSEInstrumentCensusError("page_selector_invalid")
        if type(self.sequence_number) is not int or not 1 <= self.sequence_number <= _MAX_PAGES:
            raise SSEInstrumentCensusError("page_sequence_invalid")
        if type(self.source_rows) is not tuple or len(self.source_rows) > _MAX_ROWS:
            raise SSEInstrumentCensusError("page_rows_invalid")
        if any(type(row) is not SSEInstrumentSourceObservation for row in self.source_rows):
            raise SSEInstrumentCensusError("page_rows_invalid")
        if self.page_help is not None and not isinstance(self.page_help, Mapping):
            raise SSEInstrumentCensusError("page_help_invalid")
        if self.page_help is not None:
            frozen_page_help = _freeze_json(self.page_help)
            if not isinstance(frozen_page_help, Mapping):
                raise SSEInstrumentCensusError("page_help_invalid")
            object.__setattr__(self, "page_help", frozen_page_help)
        if self.request_summary is not None and not isinstance(self.request_summary, Mapping):
            raise SSEInstrumentCensusError("request_summary_invalid")
        if self.request_summary_sha256 is not None and (
            type(self.request_summary_sha256) is not str
            or _SHA256_RE.fullmatch(self.request_summary_sha256) is None
        ):
            raise SSEInstrumentCensusError("request_summary_digest_invalid")
        if self.request_summary is not None:
            frozen_summary = _freeze_json(
                self.request_summary, byte_limit=_MAX_REQUEST_SUMMARY_BYTES
            )
            if not isinstance(frozen_summary, Mapping):
                raise SSEInstrumentCensusError("request_summary_invalid")
            _validate_summary_selector(frozen_summary, self.selector)
            summary_bytes = _encode_canonical_summary(frozen_summary)
            if len(summary_bytes) > _MAX_REQUEST_SUMMARY_BYTES:
                raise SSEInstrumentCensusError("request_summary_too_large")
            object.__setattr__(self, "request_summary", frozen_summary)
            expected_summary_hash = hashlib.sha256(summary_bytes).hexdigest()
            if self.request_summary_sha256 != expected_summary_hash:
                raise SSEInstrumentCensusError("request_summary_digest_mismatch")
        elif self.request_summary_sha256 is not None:
            raise SSEInstrumentCensusError("request_summary_digest_mismatch")
        if (
            type(self.response_sha256) is not str
            or _SHA256_RE.fullmatch(self.response_sha256) is None
        ):
            raise SSEInstrumentCensusError("response_digest_invalid")
        if type(self.http_status_code) is not int or not 200 <= self.http_status_code < 300:
            raise SSEInstrumentCensusError("successful_http_status_invalid")
        if type(self.raw_response_body) is not bytes or not self.raw_response_body:
            raise SSEInstrumentCensusError("response_body_invalid")
        if len(self.raw_response_body) > MAX_SSE_CENSUS_BODY_BYTES:
            raise SSEInstrumentCensusError("response_body_too_large")
        if not hmac.compare_digest(
            hashlib.sha256(self.raw_response_body).hexdigest(), self.response_sha256
        ):
            raise SSEInstrumentCensusError("response_digest_mismatch")

    def __repr__(self) -> str:
        """Render counts and hashes while withholding all captured contents."""
        return (
            "SSEInstrumentPageEvidence("
            f"selector={self.selector.value!r}, sequence_number={self.sequence_number}, "
            f"row_count={len(self.source_rows)}, page_help_present={self.page_help is not None}, "
            "response_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class SSEInstrumentCensusCapture:
    """Immutable raw census capture whose metadata keeps all scope claims modest."""

    observed_at: datetime
    pages: tuple[SSEInstrumentPageEvidence, ...] = field(repr=False, compare=False)
    source_id: str = _SOURCE_ID
    declared_exchange_scope: tuple[str, ...] = ("SSE",)
    scope_basis: str = "caller_declared_fixed_sse_source_context"
    pagination_status: str = "UNKNOWN"
    source_authenticity: str = "UNKNOWN"
    market_completeness: str = "UNKNOWN"

    def __post_init__(self) -> None:
        """Freeze the page sequence and validate capture-level bounds."""
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise SSEInstrumentCensusError("observed_at_invalid")
        try:
            if self.observed_at.utcoffset() is None:
                raise SSEInstrumentCensusError("observed_at_invalid")
        except (OverflowError, ValueError):
            raise SSEInstrumentCensusError("observed_at_invalid") from None
        if isinstance(self.pages, (str, bytes, bytearray, Mapping)) or not isinstance(
            self.pages, Iterable
        ):
            raise SSEInstrumentCensusError("pages_invalid")
        try:
            pages = tuple(islice(iter(self.pages), _MAX_PAGES + 1))
        except Exception:
            raise SSEInstrumentCensusError("pages_invalid") from None
        if len(pages) > _MAX_PAGES:
            raise SSEInstrumentCensusError("page_count_exceeded")
        if any(type(page) is not SSEInstrumentPageEvidence for page in pages):
            raise SSEInstrumentCensusError("page_evidence_invalid")
        if self.source_id != _SOURCE_ID:
            raise SSEInstrumentCensusError("source_id_invalid")
        if self.declared_exchange_scope != ("SSE",):
            raise SSEInstrumentCensusError("declared_scope_invalid")
        if self.scope_basis != "caller_declared_fixed_sse_source_context":
            raise SSEInstrumentCensusError("scope_basis_invalid")
        if self.pagination_status != "UNKNOWN":
            raise SSEInstrumentCensusError("pagination_status_invalid")
        if self.source_authenticity != "UNKNOWN":
            raise SSEInstrumentCensusError("source_authenticity_invalid")
        if self.market_completeness != "UNKNOWN":
            raise SSEInstrumentCensusError("market_completeness_invalid")
        if tuple(page.sequence_number for page in pages) != tuple(range(1, len(pages) + 1)):
            raise SSEInstrumentCensusError("page_sequence_not_contiguous")
        total_bytes = sum(len(page.raw_response_body) for page in pages)
        if total_bytes > _MAX_TOTAL_CAPTURE_BYTES:
            raise SSEInstrumentCensusError("capture_body_total_too_large")
        row_count = sum(len(page.source_rows) for page in pages)
        if row_count > _MAX_ROWS:
            raise SSEInstrumentCensusError("capture_row_count_exceeded")
        object.__setattr__(self, "pages", pages)

    def __repr__(self) -> str:
        """Expose summary counts only; nested source records remain redacted."""
        return (
            "SSEInstrumentCensusCapture("
            f"page_count={len(self.pages)}, "
            f"row_count={sum(len(p.source_rows) for p in self.pages)}, "
            f"response_bytes={sum(len(p.raw_response_body) for p in self.pages)}, "
            "pagination='UNKNOWN', source_authenticity='UNKNOWN', "
            "market_completeness='UNKNOWN', response_redacted=True)"
        )

    @property
    def request_summary_sha256s(self) -> tuple[str | None, ...]:
        """Return the local hashes of canonical caller request summaries."""
        return tuple(page.request_summary_sha256 for page in self.pages)

    @property
    def response_body_sha256s(self) -> tuple[str, ...]:
        """Return local SHA-256 digests verified against exact response bytes."""
        return tuple(page.response_sha256 for page in self.pages)

    @property
    def raw_response_bodies(self) -> tuple[bytes, ...]:
        """Return exact response entity bytes in local capture order."""
        return tuple(page.raw_response_body for page in self.pages)


def build_sse_instrument_census(
    pages: Iterable[SSEInstrumentPageInput], *, observed_at: datetime
) -> SSEInstrumentCensusCapture:
    """Parse caller-captured SSE JSON pages into raw, immutable evidence.

    The adapter accepts only the four fixed selector values and does not make
    HTTP requests, read configuration, or write to storage. Each request
    summary is retained as caller-supplied context and hashed canonically; each
    response digest is recomputed over exact body bytes. The source's
    ``pageHelp`` object and ``result`` rows are retained without mapping any
    business field to an identity, exchange-qualified symbol, or lifecycle
    event. Selector and SSE scope labels remain caller-declared context.

    Args:
        pages: Bounded ordered response captures from the fixed selector family.
        observed_at: Caller-supplied timezone-aware time of local observation.

    Returns:
        Frozen rows, page metadata, request summaries, exact bodies and hashes;
        pagination, source authenticity, and market completeness remain UNKNOWN.

    Raises:
        SSEInstrumentCensusError: If bytes, JSON, selector context, or resource
            bounds are invalid.
    """
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise SSEInstrumentCensusError("observed_at_invalid")
    try:
        if observed_at.utcoffset() is None:
            raise SSEInstrumentCensusError("observed_at_invalid")
    except (OverflowError, ValueError):
        raise SSEInstrumentCensusError("observed_at_invalid") from None
    if isinstance(pages, (str, bytes, bytearray, Mapping)) or not isinstance(pages, Iterable):
        raise SSEInstrumentCensusError("pages_invalid")
    try:
        page_inputs = tuple(islice(iter(pages), _MAX_PAGES + 1))
    except Exception:
        raise SSEInstrumentCensusError("pages_invalid") from None
    if len(page_inputs) > _MAX_PAGES:
        raise SSEInstrumentCensusError("page_count_exceeded")
    if any(type(page) is not SSEInstrumentPageInput for page in page_inputs):
        raise SSEInstrumentCensusError("page_input_invalid")

    total_bytes = 0
    total_rows = 0
    page_evidence: list[SSEInstrumentPageEvidence] = []
    for page_input in page_inputs:
        body = page_input.raw_response_body
        total_bytes += len(body)
        if total_bytes > _MAX_TOTAL_CAPTURE_BYTES:
            raise SSEInstrumentCensusError("capture_body_total_too_large")
        response_hash = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(response_hash, page_input.declared_response_sha256):
            raise SSEInstrumentCensusError("response_digest_mismatch")
        rows, page_help = _parse_sse_response(body, max_rows=_MAX_ROWS - total_rows)
        total_rows += len(rows)
        summary_json = page_input._summary_json
        summary_hash = (
            hashlib.sha256(summary_json).hexdigest() if summary_json is not None else None
        )
        request_summary: Mapping[str, FrozenJson] | None = None
        if page_input.request_summary is not None:
            frozen_summary = _freeze_json(
                page_input.request_summary, byte_limit=_MAX_REQUEST_SUMMARY_BYTES
            )
            if not isinstance(frozen_summary, Mapping):
                raise SSEInstrumentCensusError("request_summary_invalid")
            request_summary = frozen_summary
        page_evidence.append(
            SSEInstrumentPageEvidence(
                selector=SSEInstrumentSelector(page_input.selector),
                sequence_number=page_input.sequence_number,
                source_rows=rows,
                page_help=page_help,
                request_summary=request_summary,
                request_summary_sha256=summary_hash,
                response_sha256=response_hash,
                http_status_code=page_input.http_status_code,
                raw_response_body=body,
            )
        )
    try:
        return SSEInstrumentCensusCapture(observed_at=observed_at, pages=tuple(page_evidence))
    except SSEInstrumentCensusError:
        raise
    except Exception:
        raise SSEInstrumentCensusError("capture_invalid") from None


def _parse_sse_response(
    body: bytes, *, max_rows: int
) -> tuple[tuple[SSEInstrumentSourceObservation, ...], Mapping[str, FrozenJson] | None]:
    """Decode one bounded SSE JSON envelope and preserve rows plus pageHelp."""
    if type(body) is not bytes:
        raise SSEInstrumentCensusError("response_body_type_invalid")
    if not body:
        raise SSEInstrumentCensusError("response_body_empty")
    if len(body) > MAX_SSE_CENSUS_BODY_BYTES:
        raise SSEInstrumentCensusError("response_body_too_large")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SSEInstrumentCensusError("response_utf8_invalid") from None
    _preflight_json(text)
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            parse_int=_parse_integer,
        )
    except SSEInstrumentCensusError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, OverflowError):
        raise SSEInstrumentCensusError("response_json_invalid") from None
    if type(payload) is not dict:
        raise SSEInstrumentCensusError("response_root_invalid")
    raw_rows = payload.get("result")
    if type(raw_rows) is not list:
        raise SSEInstrumentCensusError("response_result_invalid")
    if len(raw_rows) > max_rows:
        raise SSEInstrumentCensusError("response_row_count_exceeded")
    raw_page_help = payload.get("pageHelp")
    if "pageHelp" in payload and type(raw_page_help) is not dict:
        raise SSEInstrumentCensusError("response_page_help_invalid")
    if "pageHelp" not in payload:
        page_help = None
    else:
        frozen_page_help = _freeze_json(raw_page_help)
        if not isinstance(frozen_page_help, Mapping):
            raise SSEInstrumentCensusError("response_page_help_invalid")
        page_help = frozen_page_help
    observations: list[SSEInstrumentSourceObservation] = []
    for raw_row in raw_rows:
        if type(raw_row) is not dict:
            raise SSEInstrumentCensusError("response_row_invalid")
        try:
            observations.append(SSEInstrumentSourceObservation(raw_row=raw_row))
        except SSEInstrumentCensusError:
            raise
        except Exception:
            raise SSEInstrumentCensusError("response_row_invalid") from None
    return tuple(observations), page_help


def _validate_summary_selector(
    summary: Mapping[str, FrozenJson], selector: SSEInstrumentSelector
) -> None:
    """Require a supplied summary's query selector to match fixed context."""
    raw_params = summary.get("params", summary)
    if not isinstance(raw_params, Mapping):
        raise SSEInstrumentCensusError("request_summary_params_invalid")
    expected_stock_type, expected_status = _selector_parameters(selector)
    if raw_params.get("STOCK_TYPE") != expected_stock_type:
        raise SSEInstrumentCensusError("request_summary_stock_type_mismatch")
    if raw_params.get("COMPANY_STATUS") != expected_status:
        raise SSEInstrumentCensusError("request_summary_status_mismatch")


def _freeze_json(
    value: object,
    *,
    depth: int = 0,
    node_budget: list[int] | None = None,
    byte_budget: list[int] | None = None,
    byte_limit: int = MAX_SSE_CENSUS_BODY_BYTES,
) -> FrozenJson:
    """Copy finite JSON-compatible values into bounded immutable containers."""
    if node_budget is None:
        node_budget = [0]
    if byte_budget is None:
        byte_budget = [0]
    node_budget[0] += 1
    if node_budget[0] > _MAX_JSON_NODES:
        raise SSEInstrumentCensusError("json_nodes_exceeded")
    if depth > _MAX_JSON_DEPTH:
        raise SSEInstrumentCensusError("json_depth_exceeded")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > _MAX_JSON_INTEGER_BITS:
            raise SSEInstrumentCensusError("json_integer_too_large")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise SSEInstrumentCensusError("json_nonfinite_number")
        return value
    if type(value) is str:
        if len(value) > _MAX_JSON_TEXT_CHARS:
            raise SSEInstrumentCensusError("json_text_too_large")
        byte_budget[0] += _utf8_byte_length(value)
        if byte_budget[0] > byte_limit:
            raise SSEInstrumentCensusError("json_value_too_large")
        return value
    if type(value) in (dict, _MAPPING_PROXY_TYPE):
        if not isinstance(value, Mapping):
            raise SSEInstrumentCensusError("json_value_type_invalid")
        if len(value) > _MAX_JSON_FIELDS:
            raise SSEInstrumentCensusError("json_object_too_large")
        copied: dict[str, FrozenJson] = {}
        for key, item in value.items():
            if (
                type(key) is not str
                or not key
                or len(key) > 128
                or any(not char.isprintable() for char in key)
            ):
                raise SSEInstrumentCensusError("json_field_name_invalid")
            byte_budget[0] += _utf8_byte_length(key)
            if byte_budget[0] > byte_limit:
                raise SSEInstrumentCensusError("json_value_too_large")
            copied[key] = _freeze_json(
                item,
                depth=depth + 1,
                node_budget=node_budget,
                byte_budget=byte_budget,
                byte_limit=byte_limit,
            )
        return MappingProxyType(copied)
    if type(value) in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise SSEInstrumentCensusError("json_value_type_invalid")
        if len(value) > _MAX_JSON_CONTAINER_ITEMS:
            raise SSEInstrumentCensusError("json_array_too_large")
        return tuple(
            _freeze_json(
                item,
                depth=depth + 1,
                node_budget=node_budget,
                byte_budget=byte_budget,
                byte_limit=byte_limit,
            )
            for item in value
        )
    raise SSEInstrumentCensusError("json_value_type_invalid")


def _utf8_byte_length(value: str) -> int:
    """Measure strict UTF-8 text without allowing encoder details to escape."""
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise SSEInstrumentCensusError("json_text_invalid_unicode") from None


def _encode_canonical_summary(summary: Mapping[str, FrozenJson]) -> bytes:
    """Serialize a bounded frozen summary in deterministic key order."""
    try:
        return json.dumps(
            _thaw_json(summary),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except SSEInstrumentCensusError:
        raise
    except Exception:
        raise SSEInstrumentCensusError("request_summary_invalid") from None


def _thaw_json(value: FrozenJson) -> object:
    """Create ordinary JSON containers for canonical summary serialization."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate response JSON keys at every nesting level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SSEInstrumentCensusError("response_json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON NaN and infinity tokens."""
    del value
    raise SSEInstrumentCensusError("response_json_nonfinite_number")


def _parse_float(token: str) -> float:
    """Decode a finite decimal token into the generic raw-row number type."""
    value = float(token)
    if not math.isfinite(value):
        raise SSEInstrumentCensusError("response_json_nonfinite_number")
    return value


def _parse_integer(token: str) -> int:
    """Decode an integer token within the fixed 4096-bit bound."""
    value = int(token)
    if value.bit_length() > _MAX_JSON_INTEGER_BITS:
        raise SSEInstrumentCensusError("response_json_integer_too_large")
    return value


def _preflight_json(text: str) -> None:
    """Bound nesting and approximate JSON node count before decoding."""
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    previous_significant = ""
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            nodes += 1
        elif character in "[{":
            depth += 1
            nodes += 1
            if depth > _MAX_JSON_DEPTH:
                raise SSEInstrumentCensusError("response_json_depth_exceeded")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise SSEInstrumentCensusError("response_json_invalid")
        elif character in "-0123456789tfn" and (
            not previous_significant or previous_significant in ":[,"
        ):
            nodes += 1
        if not character.isspace():
            previous_significant = character
        if nodes > _MAX_JSON_NODES:
            raise SSEInstrumentCensusError("response_json_nodes_exceeded")
    if in_string or depth != 0:
        raise SSEInstrumentCensusError("response_json_invalid")
