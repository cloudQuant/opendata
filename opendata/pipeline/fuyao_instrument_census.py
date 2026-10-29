"""Parse bounded Fuyao instrument response captures into raw census evidence.

This adapter only parses bytes supplied by its caller. It does not call Fuyao,
read credentials, touch storage, or infer an exchange, asset class, lifecycle
state, market scope, or complete instrument universe. The caller's request
summary is kept separate from the raw response digest: a summary hash does not
prove the exact wire request or what Fuyao received. A locally verified body
hash proves only that the declared digest matches the supplied bytes; it does
not authenticate the source. Pagination remains UNKNOWN because this adapter
does not establish a source page contract or a trustworthy total. ``raw_row``
preserves decoded JSON object structure and values, but JSON decimal tokens are
decoded to finite Python floats and can lose precision. The exact response bytes
retained in the capture are the source-fidelity record.
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
from itertools import islice
from typing import Any

from opendata.data.instrument_source_census import (
    InstrumentSourceCensus,
    InstrumentSourceCensusError,
    InstrumentSourceObservation,
    InstrumentSourcePage,
    PageAttemptStatus,
    PageReceipt,
    PaginationAssessment,
    ReportedTotalBasis,
    assess_source_pagination,
)

__all__ = [
    "MAX_FUYAO_CENSUS_BODY_BYTES",
    "FuyaoInstrumentCensusCapture",
    "FuyaoInstrumentCensusError",
    "FuyaoInstrumentPageInput",
    "build_fuyao_instrument_census",
]

MAX_FUYAO_CENSUS_BODY_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_CAPTURE_BYTES = 64 * 1024 * 1024
_MAX_REQUEST_SUMMARY_BYTES = 64 * 1024
_MAX_CAPTURE_PAGES = 4_096
_MAX_CENSUS_ROWS = 100_000
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 2_200_000
_MAX_JSON_INTEGER_BITS = 4_096
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = "fuyao.instrument"


class FuyaoInstrumentCensusError(ValueError):
    """A stable, content-free refusal code for malformed census captures."""

    def __init__(self, code: str) -> None:
        """Create an error without embedding source values or response text.

        Args:
            code: Stable machine-readable refusal code.
        """
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FuyaoInstrumentPageInput:
    """Caller-supplied page capture and its explicit, unverified request facts.

    ``request_summary`` describes the caller's intended request and is
    canonically encoded and hashed locally. It is not the exact wire request.
    ``declared_response_sha256`` must match the exact supplied entity bytes,
    including for a captured failed response. Neither digest authenticates
    Fuyao or proves what an upstream server received.

    ``http_status_code`` and paging fields are caller declarations. A page is
    accepted as successful only when its declared HTTP status is 2xx and its
    Fuyao envelope has ``code=0``. This parser cannot verify that the status
    came from the source. A supplied total is retained with UNKNOWN meaning;
    the adapter never derives one from rows.
    """

    sequence_number: int
    raw_response_body: bytes | None = field(repr=False, compare=False)
    declared_response_sha256: str | None
    request_summary: Mapping[str, object] | None = field(default=None, repr=False, compare=False)
    source_page_number: int | str | None = None
    http_status_code: int | None = None
    request_cursor: str | None = field(default=None, repr=False, compare=False)
    next_cursor: str | None = field(default=None, repr=False, compare=False)
    terminal: bool | None = None
    reported_total: int | None = None
    attempt_status: PageAttemptStatus | str = PageAttemptStatus.SUCCEEDED
    failure_code: str | None = field(default=None, repr=False, compare=False)
    _request_summary_canonical_json: bytes | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate capture bounds and keep unsuccessful pages row-free."""
        if (
            type(self.sequence_number) is not int
            or not 1 <= self.sequence_number <= _MAX_CAPTURE_PAGES
        ):
            raise FuyaoInstrumentCensusError("page_sequence_invalid")
        if self.source_page_number is not None and type(self.source_page_number) not in (int, str):
            raise FuyaoInstrumentCensusError("source_page_number_invalid")
        if type(self.source_page_number) is int and self.source_page_number.bit_length() > 63:
            raise FuyaoInstrumentCensusError("source_page_number_invalid")
        if type(self.source_page_number) is str and len(self.source_page_number) > 128:
            raise FuyaoInstrumentCensusError("source_page_number_invalid")
        if self.http_status_code is not None and (
            type(self.http_status_code) is not int or not 100 <= self.http_status_code <= 599
        ):
            raise FuyaoInstrumentCensusError("http_status_invalid")
        for name in ("request_cursor", "next_cursor"):
            cursor = getattr(self, name)
            if cursor is not None and (type(cursor) is not str or len(cursor) > 512):
                raise FuyaoInstrumentCensusError("page_cursor_invalid")
        if self.terminal is not None and type(self.terminal) is not bool:
            raise FuyaoInstrumentCensusError("page_terminal_invalid")
        if self.reported_total is not None and (
            type(self.reported_total) is not int or self.reported_total < 0
        ):
            raise FuyaoInstrumentCensusError("reported_total_invalid")
        try:
            status = PageAttemptStatus(self.attempt_status)
        except (TypeError, ValueError):
            raise FuyaoInstrumentCensusError("page_attempt_status_invalid") from None
        object.__setattr__(self, "attempt_status", status)
        if self.failure_code is not None and (
            type(self.failure_code) is not str
            or not self.failure_code
            or len(self.failure_code) > 128
            or any(not char.isprintable() for char in self.failure_code)
        ):
            raise FuyaoInstrumentCensusError("page_failure_code_invalid")
        if status is PageAttemptStatus.FAILED:
            if self.failure_code is None:
                raise FuyaoInstrumentCensusError("failed_page_code_required")
            if (
                self.next_cursor is not None
                or self.terminal is not None
                or self.reported_total is not None
            ):
                raise FuyaoInstrumentCensusError("failed_page_has_paging_fields")
        elif self.failure_code is not None:
            raise FuyaoInstrumentCensusError("nonfailed_page_has_failure_code")
        if status is PageAttemptStatus.UNKNOWN:
            if self.raw_response_body is not None:
                raise FuyaoInstrumentCensusError("unknown_page_has_response_body")
            if (
                self.next_cursor is not None
                or self.terminal is not None
                or self.reported_total is not None
            ):
                raise FuyaoInstrumentCensusError("unknown_page_has_paging_fields")
        if self.raw_response_body is not None and type(self.raw_response_body) is not bytes:
            raise FuyaoInstrumentCensusError("response_body_type_invalid")
        if (self.raw_response_body is None) != (self.declared_response_sha256 is None):
            raise FuyaoInstrumentCensusError("response_body_digest_pair_invalid")
        if self.declared_response_sha256 is not None and (
            type(self.declared_response_sha256) is not str
            or _SHA256_RE.fullmatch(self.declared_response_sha256) is None
        ):
            raise FuyaoInstrumentCensusError("response_digest_invalid")
        if status is PageAttemptStatus.SUCCEEDED and self.raw_response_body is None:
            raise FuyaoInstrumentCensusError("successful_page_body_required")
        if status is PageAttemptStatus.SUCCEEDED and (
            self.http_status_code is None or not 200 <= self.http_status_code < 300
        ):
            raise FuyaoInstrumentCensusError("successful_http_status_invalid")
        if self.request_summary is not None and not isinstance(self.request_summary, Mapping):
            raise FuyaoInstrumentCensusError("request_summary_invalid")
        object.__setattr__(
            self,
            "_request_summary_canonical_json",
            _canonical_request_summary(self.request_summary),
        )


@dataclass(frozen=True, slots=True, repr=False)
class FuyaoInstrumentCensusCapture:
    """A source census plus aligned request summaries and exact response bytes.

    ``request_summary_sha256s``, ``response_body_sha256s``,
    ``http_status_codes``, and ``raw_response_bodies`` follow the same order as
    ``source_census.pages``. Status codes are caller declarations, not source-
    authenticity evidence.
    Request summary hashes are locally calculated over canonical summary JSON;
    response hashes are locally calculated over exact entity bytes. The latter
    are also copied to each successful page's ``PageReceipt``. Their presence
    does not make any paging or source-authenticity claim.
    """

    source_census: InstrumentSourceCensus = field(repr=False, compare=False)
    request_summary_sha256s: tuple[str | None, ...]
    response_body_sha256s: tuple[str | None, ...]
    http_status_codes: tuple[int | None, ...]
    raw_response_bodies: tuple[bytes | None, ...] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Freeze aligned evidence and reject mismatched capture records."""
        if type(self.source_census) is not InstrumentSourceCensus:
            raise FuyaoInstrumentCensusError("capture_census_invalid")
        count = len(self.source_census.pages)
        if count > _MAX_CAPTURE_PAGES:
            raise FuyaoInstrumentCensusError("capture_page_count_exceeded")
        for name in (
            "request_summary_sha256s",
            "response_body_sha256s",
            "http_status_codes",
            "raw_response_bodies",
        ):
            values = getattr(self, name)
            if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Iterable):
                raise FuyaoInstrumentCensusError("capture_alignment_invalid")
            try:
                copied = tuple(islice(iter(values), count + 1))
            except Exception:
                raise FuyaoInstrumentCensusError("capture_alignment_invalid") from None
            if len(copied) != count:
                raise FuyaoInstrumentCensusError("capture_alignment_invalid")
            object.__setattr__(self, name, copied)
        for value in self.request_summary_sha256s + self.response_body_sha256s:
            if value is not None and (
                type(value) is not str or _SHA256_RE.fullmatch(value) is None
            ):
                raise FuyaoInstrumentCensusError("capture_digest_invalid")
        for page, status_code in zip(self.source_census.pages, self.http_status_codes, strict=True):
            if status_code is not None and (
                type(status_code) is not int or not 100 <= status_code <= 599
            ):
                raise FuyaoInstrumentCensusError("capture_http_status_invalid")
            if page.attempt_status is PageAttemptStatus.SUCCEEDED and (
                status_code is None or not 200 <= status_code < 300
            ):
                raise FuyaoInstrumentCensusError("capture_success_http_status_invalid")
        total_body_bytes = 0
        for page, body, digest in zip(
            self.source_census.pages,
            self.raw_response_bodies,
            self.response_body_sha256s,
            strict=True,
        ):
            if body is not None and type(body) is not bytes:
                raise FuyaoInstrumentCensusError("capture_body_invalid")
            if body is not None:
                if len(body) > MAX_FUYAO_CENSUS_BODY_BYTES:
                    raise FuyaoInstrumentCensusError("response_body_too_large")
                total_body_bytes += len(body)
                if total_body_bytes > _MAX_TOTAL_CAPTURE_BYTES:
                    raise FuyaoInstrumentCensusError("capture_body_total_too_large")
            if (body is None) != (digest is None):
                raise FuyaoInstrumentCensusError("capture_body_digest_pair_invalid")
            if body is not None and not hmac.compare_digest(
                hashlib.sha256(body).hexdigest(), digest or ""
            ):
                raise FuyaoInstrumentCensusError("capture_body_digest_mismatch")
            if page.attempt_status is PageAttemptStatus.SUCCEEDED:
                if (
                    body is None
                    or page.receipt is None
                    or page.receipt.request_sha256 is not None
                    or page.receipt.response_sha256 != digest
                ):
                    raise FuyaoInstrumentCensusError("capture_success_receipt_invalid")
            elif page.rows or page.receipt is not None:
                raise FuyaoInstrumentCensusError("capture_failed_page_has_evidence")

    def __repr__(self) -> str:
        """Return a compact summary without rendering raw census observations."""
        return (
            "FuyaoInstrumentCensusCapture("
            f"page_count={len(self.source_census.pages)}, "
            f"row_count={sum(len(page.rows) for page in self.source_census.pages)})"
        )

    @property
    def pagination(self) -> PaginationAssessment:
        """Assess the census using the shared conservative pagination DTO."""
        return assess_source_pagination(self.source_census)


def build_fuyao_instrument_census(
    pages: Iterable[FuyaoInstrumentPageInput], *, observed_at: datetime
) -> FuyaoInstrumentCensusCapture:
    """Build immutable raw source-census evidence from captured Fuyao pages.

    Successful response bytes are size-limited before parsing, strict UTF-8
    JSON with unique keys and finite numbers, and checked against the caller's
    declared SHA-256. Each ``data.item`` object is preserved with decoded JSON
    structure and values; decimal number tokens are decoded to Python floats
    and can lose precision. Exact response bytes remain available as the
    source-fidelity record. No business field is normalized or interpreted.
    Failed pages become explicit failed DTO pages with no valid rows. The
    caller's request summary and locally verified response digest remain
    separate records.

    The total's meaning remains UNKNOWN even when the caller supplies a
    reported total. Pagination assessment therefore remains UNKNOWN; neither a
    short page nor a row count is promoted to a source completeness claim.

    Args:
        pages: Bounded, ordered page attempts captured by the caller.
        observed_at: Caller-supplied time for this observation, not a source
            timestamp inferred from the response.

    Returns:
        A frozen raw census with aligned request-summary and response evidence.

    Raises:
        FuyaoInstrumentCensusError: If bytes, hashes, JSON, envelope, or page
            declarations are invalid or exceed their bounds.
    """
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise FuyaoInstrumentCensusError("observed_at_invalid")
    try:
        if observed_at.utcoffset() is None:
            raise FuyaoInstrumentCensusError("observed_at_invalid")
        page_inputs: tuple[FuyaoInstrumentPageInput, ...] = tuple(
            islice(iter(_bounded_page_iterable(pages)), _MAX_CAPTURE_PAGES + 1)
        )
    except FuyaoInstrumentCensusError:
        raise
    except Exception:
        raise FuyaoInstrumentCensusError("pages_invalid") from None
    if len(page_inputs) > _MAX_CAPTURE_PAGES:
        raise FuyaoInstrumentCensusError("page_count_exceeded")
    if any(type(page) is not FuyaoInstrumentPageInput for page in page_inputs):
        raise FuyaoInstrumentCensusError("page_input_invalid")

    dto_pages: list[InstrumentSourcePage] = []
    summaries: list[str | None] = []
    response_hashes: list[str | None] = []
    http_status_codes: list[int | None] = []
    response_bodies: list[bytes | None] = []
    total_body_bytes = 0
    total_rows = 0
    for page_input in page_inputs:
        body = page_input.raw_response_body
        body_hash: str | None = None
        if body is not None:
            if len(body) > MAX_FUYAO_CENSUS_BODY_BYTES:
                raise FuyaoInstrumentCensusError("response_body_too_large")
            total_body_bytes += len(body)
            if total_body_bytes > _MAX_TOTAL_CAPTURE_BYTES:
                raise FuyaoInstrumentCensusError("capture_body_total_too_large")
            body_hash = hashlib.sha256(body).hexdigest()
            if not hmac.compare_digest(body_hash, page_input.declared_response_sha256 or ""):
                raise FuyaoInstrumentCensusError("response_digest_mismatch")

        summary_bytes = page_input._request_summary_canonical_json
        summary_hash = (
            hashlib.sha256(summary_bytes).hexdigest() if summary_bytes is not None else None
        )
        status = PageAttemptStatus(page_input.attempt_status)
        rows: tuple[InstrumentSourceObservation, ...] = ()
        if status is PageAttemptStatus.SUCCEEDED:
            if body is None:
                raise FuyaoInstrumentCensusError("successful_page_body_required")
            rows = _parse_rows(body, max_rows=_MAX_CENSUS_ROWS - total_rows)
            total_rows += len(rows)
            page_receipt = PageReceipt(request_sha256=None, response_sha256=body_hash)
        else:
            page_receipt = None

        try:
            dto_pages.append(
                InstrumentSourcePage(
                    sequence_number=page_input.sequence_number,
                    source_page_number=page_input.source_page_number,
                    request_cursor=page_input.request_cursor,
                    next_cursor=page_input.next_cursor,
                    terminal=page_input.terminal,
                    reported_total=page_input.reported_total,
                    rows=rows,
                    reported_total_basis=(
                        ReportedTotalBasis.UNKNOWN
                        if page_input.reported_total is not None
                        else None
                    ),
                    attempt_status=status,
                    failure_code=page_input.failure_code,
                    receipt=page_receipt,
                )
            )
        except InstrumentSourceCensusError:
            raise FuyaoInstrumentCensusError("source_page_invalid") from None
        summaries.append(summary_hash)
        response_hashes.append(body_hash)
        http_status_codes.append(page_input.http_status_code)
        response_bodies.append(body)

    try:
        census = InstrumentSourceCensus(
            source_id=_SOURCE_ID,
            observed_at=observed_at,
            pages=tuple(dto_pages),
        )
    except InstrumentSourceCensusError:
        raise FuyaoInstrumentCensusError("source_census_invalid") from None
    return FuyaoInstrumentCensusCapture(
        source_census=census,
        request_summary_sha256s=tuple(summaries),
        response_body_sha256s=tuple(response_hashes),
        http_status_codes=tuple(http_status_codes),
        raw_response_bodies=tuple(response_bodies),
    )


def _bounded_page_iterable(
    pages: Iterable[FuyaoInstrumentPageInput],
) -> Iterable[FuyaoInstrumentPageInput]:
    """Reject scalar page sources while allowing one bounded materialization."""
    if isinstance(pages, (str, bytes, bytearray, Mapping)) or not isinstance(pages, Iterable):
        raise FuyaoInstrumentCensusError("pages_invalid")
    return pages


def _parse_rows(body: bytes, *, max_rows: int) -> tuple[InstrumentSourceObservation, ...]:
    """Parse an envelope into decoded JSON rows, preserving object structure.

    JSON decimal number tokens are decoded by ``json.loads`` as Python floats;
    callers needing their exact token representation must use the captured body.
    """
    if len(body) > MAX_FUYAO_CENSUS_BODY_BYTES:
        raise FuyaoInstrumentCensusError("response_body_too_large")
    if not body:
        raise FuyaoInstrumentCensusError("response_body_empty")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FuyaoInstrumentCensusError("response_utf8_invalid") from None
    _preflight_json(text)
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            parse_int=_parse_integer,
        )
    except FuyaoInstrumentCensusError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, OverflowError):
        raise FuyaoInstrumentCensusError("response_json_invalid") from None
    if not isinstance(payload, dict):
        raise FuyaoInstrumentCensusError("response_root_invalid")
    code = payload.get("code")
    if type(code) is not int:
        raise FuyaoInstrumentCensusError("response_code_invalid")
    if code != 0:
        raise FuyaoInstrumentCensusError("response_not_successful")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FuyaoInstrumentCensusError("response_data_invalid")
    items = data.get("item")
    if type(items) is not list:
        raise FuyaoInstrumentCensusError("response_items_invalid")
    if len(items) > max_rows:
        raise FuyaoInstrumentCensusError("response_row_count_exceeded")
    observations: list[InstrumentSourceObservation] = []
    for item in items:
        if not isinstance(item, dict):
            raise FuyaoInstrumentCensusError("response_row_invalid")
        try:
            observations.append(InstrumentSourceObservation(raw_row=item))
        except (InstrumentSourceCensusError, UnicodeError, TypeError, ValueError):
            raise FuyaoInstrumentCensusError("response_row_invalid") from None
    return tuple(observations)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys at every nesting level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FuyaoInstrumentCensusError("response_json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-standard NaN and Infinity tokens."""
    del value
    raise FuyaoInstrumentCensusError("response_json_nonfinite_number")


def _parse_float(token: str) -> float:
    """Decode a finite JSON decimal into the source DTO's supported float type."""
    value = float(token)
    if not math.isfinite(value):
        raise FuyaoInstrumentCensusError("response_json_nonfinite_number")
    return value


def _parse_integer(token: str) -> int:
    """Decode a JSON integer with a fixed bit-width bound."""
    value = int(token)
    if value.bit_length() > _MAX_JSON_INTEGER_BITS:
        raise FuyaoInstrumentCensusError("response_json_integer_too_large")
    return value


def _preflight_json(text: str) -> None:
    """Bound JSON nesting and approximate node count before the decoder runs."""
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
                raise FuyaoInstrumentCensusError("response_json_depth_exceeded")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise FuyaoInstrumentCensusError("response_json_invalid")
        elif character in "-0123456789tfn" and (
            not previous_significant or previous_significant in "[:,"
        ):
            nodes += 1
        if not character.isspace():
            previous_significant = character
        if nodes > _MAX_JSON_NODES:
            raise FuyaoInstrumentCensusError("response_json_nodes_exceeded")
    if in_string or depth != 0:
        raise FuyaoInstrumentCensusError("response_json_invalid")


def _canonical_request_summary(summary: Mapping[str, object] | None) -> bytes | None:
    """Encode a bounded JSON summary without implying it is wire-level proof."""
    if summary is None:
        return None
    try:
        copied = _copy_summary_value(summary, depth=0, budget=[0], byte_budget=[0])
        encoded = json.dumps(
            copied,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except FuyaoInstrumentCensusError:
        raise
    except Exception:
        raise FuyaoInstrumentCensusError("request_summary_invalid") from None
    if len(encoded) > _MAX_REQUEST_SUMMARY_BYTES:
        raise FuyaoInstrumentCensusError("request_summary_too_large")
    return encoded


def _copy_summary_value(
    value: object, *, depth: int, budget: list[int], byte_budget: list[int]
) -> object:
    """Copy request summaries into finite, bounded JSON-compatible values."""
    budget[0] += 1
    if budget[0] > 20_000:
        raise FuyaoInstrumentCensusError("request_summary_nodes_exceeded")
    if depth > 12:
        raise FuyaoInstrumentCensusError("request_summary_depth_exceeded")
    if value is None or type(value) in (bool, int, str):
        if type(value) is int and value.bit_length() > _MAX_JSON_INTEGER_BITS:
            raise FuyaoInstrumentCensusError("request_summary_integer_too_large")
        if type(value) is str:
            if len(value) > 16_384:
                raise FuyaoInstrumentCensusError("request_summary_text_too_large")
            _count_summary_text(value, byte_budget)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise FuyaoInstrumentCensusError("request_summary_nonfinite_number")
        return value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise FuyaoInstrumentCensusError("request_summary_fields_exceeded")
        copied: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 128:
                raise FuyaoInstrumentCensusError("request_summary_key_invalid")
            _count_summary_text(key, byte_budget)
            copied[key] = _copy_summary_value(
                item, depth=depth + 1, budget=budget, byte_budget=byte_budget
            )
        return copied
    if isinstance(value, (list, tuple)):
        if len(value) > 4_096:
            raise FuyaoInstrumentCensusError("request_summary_array_exceeded")
        return [
            _copy_summary_value(item, depth=depth + 1, budget=budget, byte_budget=byte_budget)
            for item in value
        ]
    raise FuyaoInstrumentCensusError("request_summary_value_invalid")


def _count_summary_text(value: str, byte_budget: list[int]) -> None:
    """Check UTF-8 and bound source text before copying it into a summary."""
    try:
        byte_budget[0] += len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise FuyaoInstrumentCensusError("request_summary_unicode_invalid") from None
    if byte_budget[0] > _MAX_REQUEST_SUMMARY_BYTES:
        raise FuyaoInstrumentCensusError("request_summary_too_large")
