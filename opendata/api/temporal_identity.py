"""Read-only, pinned REST access to reviewed temporal identity snapshots.

This API deliberately has no production configuration fallback. An embedding
application must place an operator-reviewed pin registry and an already
migrated snapshot store on ``app.state``. Missing state fails closed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, Protocol, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from starlette.responses import JSONResponse, Response

from opendata.api.dependencies import (
    CurrentPrincipal,  # noqa: TC001 - FastAPI needs this runtime dependency alias.
)
from opendata.api.published_access import (
    DatasetGrantRegistry,
    PublishedAccessError,
    require_published_access,
)
from opendata.api.published_data import get_dataset_grants
from opendata.data.temporal_identity_pins import (
    MAX_TEMPORAL_IDENTITY_PINS as _MAX_TEMPORAL_IDENTITY_PINS,
)
from opendata.data.temporal_identity_pins import (
    TemporalIdentityConfigurationError,
    TemporalIdentityPin,
    TemporalIdentityPinRegistry,
)
from opendata.data.temporal_instrument_identity import (
    Exchange,
    IdentityResolution,
    IdentityResolutionSegment,
    IdentityResolutionStatus,
    MarketSymbolPair,
    TemporalIdentityRegistry,
    TemporalInstrumentIdentityError,
)

MAX_TEMPORAL_IDENTITY_PINS = _MAX_TEMPORAL_IDENTITY_PINS

MAX_REQUEST_BYTES = 8 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_RESOLUTION_SEGMENTS = 256
MAX_REQUEST_STREAM_CHUNKS = 4_096
MAX_REQUEST_JSON_FIELDS = 10
MAX_WINDOW_DAYS = 3_660
IDENTITY_RESOLVE_OPERATION = "identity.resolve"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOL_PATTERN = r"^[0-9]{6}$"
_Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
_Sha256 = Annotated[str, StringConstraints(strict=True, pattern=_SHA256_PATTERN)]
_Symbol = Annotated[str, StringConstraints(strict=True, pattern=_SYMBOL_PATTERN)]


class _ResolveRequest(BaseModel):
    """Strict, bounded selectors for one temporal identity lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_id: _Identifier
    authority_id: _Identifier
    registry_id: _Identifier
    reference_version: _Identifier
    snapshot_sha256: _Sha256
    window_start: date
    window_end_exclusive: date
    exchange: Exchange | None = None
    symbol: _Symbol | None = None
    instrument_id: _Identifier | None = None

    @model_validator(mode="after")
    def _validate_query(self) -> _ResolveRequest:
        """Require exactly one selector family and a bounded half-open date window."""
        pair_selected = self.exchange is not None and self.symbol is not None
        instrument_selected = self.instrument_id is not None
        if pair_selected == instrument_selected or (self.exchange is None) != (self.symbol is None):
            raise ValueError("supply exactly one pair or instrument_id selector")
        if self.window_start >= self.window_end_exclusive:
            raise ValueError("window must be a nonempty half-open interval")
        if (self.window_end_exclusive - self.window_start).days > MAX_WINDOW_DAYS:
            raise ValueError("window exceeds the fixed calendar-day limit")
        return self


class _SnapshotStore(Protocol):
    """Accept only the read interface used by the temporal identity route."""

    def get(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> TemporalIdentityRegistry:
        """Read one server-pinned immutable registry."""


class _TemporalIdentityRoute(APIRoute):
    """Translate route and dependency failures to a bounded safe error envelope."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap FastAPI validation, authentication, and store failures safely."""
        route_handler = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except PublishedAccessError:
                return _error_response(403, "dataset_unavailable", "Dataset is unavailable")
            except RequestValidationError:
                return _error_response(422, "invalid_request", "Request validation failed")
            except HTTPException as exc:
                code, message = _http_error_contract(exc.status_code, exc.detail)
                return _error_response(exc.status_code, code, message, exc.headers)
            except Exception:
                return _error_response(
                    500,
                    "temporal_identity_failed",
                    "Temporal identity request failed",
                )

        return handler


router = APIRouter(prefix="/api/v2", route_class=_TemporalIdentityRoute)


@router.post(
    "/identity/resolve",
    tags=["Temporal Identity"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "A bounded JSON request. Supply either exchange and symbol together, "
                "or instrument_id, with exact operator-pinned snapshot fields."
            ),
            "content": {"application/json": {"schema": _ResolveRequest.model_json_schema()}},
        }
    },
)
async def resolve_temporal_identity(
    request: Request,
    principal: CurrentPrincipal,
    grants: DatasetGrantRegistry = Depends(get_dataset_grants),
) -> Response:
    """Resolve one explicitly pinned identity selector across a finite date window.

    Authorization is evaluated before operator pin or snapshot-store lookup.
    The route performs no upload, write, latest-version lookup, or universe
    completeness claim. Unknown intervals remain explicit in the response.

    Args:
        request: Bounded JSON request and trusted application state.
        principal: Existing JWT or API-key identity.
        grants: Operator-reviewed dataset/source grants.

    Returns:
        A bounded JSON partition of the requested half-open window.
    """
    payload = await _read_request(request)
    try:
        body = _ResolveRequest.model_validate_json(payload)
    except Exception:
        raise _http_error(422, "invalid_request", "Request validation failed") from None

    access = require_published_access(
        principal,
        body.dataset_id,
        IDENTITY_RESOLVE_OPERATION,
        grants,
        purpose="research",
    )
    pins = getattr(request.app.state, "temporal_identity_pin_registry", None)
    if type(pins) is not TemporalIdentityPinRegistry:
        raise _http_error(
            503,
            "temporal_identity_unavailable",
            "Temporal identity service is unavailable",
        )
    pin = pins.get(access.dataset_id)
    if pin is None or not _request_matches_pin(body, pin) or access.source_id != pin.source_id:
        raise PublishedAccessError

    store = getattr(request.app.state, "temporal_identity_snapshot_store", None)
    if not _is_snapshot_reader(store):
        raise _http_error(
            503,
            "temporal_identity_unavailable",
            "Temporal identity service is unavailable",
        )
    try:
        registry = await asyncio.to_thread(
            store.get,
            authority_id=pin.authority_id,
            registry_id=pin.registry_id,
            reference_version=pin.reference_version,
            expected_sha256=pin.snapshot_sha256,
        )
    except Exception:
        raise _http_error(
            503,
            "temporal_identity_unavailable",
            "Temporal identity service is unavailable",
        ) from None
    if type(registry) is not TemporalIdentityRegistry or (
        registry.reference_version != pin.reference_version
        or any(assignment.source_id != pin.source_id for assignment in registry.assignments)
    ):
        raise _http_error(
            503,
            "temporal_identity_unavailable",
            "Temporal identity service is unavailable",
        )

    try:
        if _exceeds_segment_limit(registry, body):
            raise _http_error(
                413,
                "identity_resolution_too_large",
                "Identity resolution exceeds the segment limit",
            )
        resolution = _resolve(registry, body)
    except TemporalInstrumentIdentityError:
        raise _http_error(
            422,
            "invalid_identity_resolution",
            "Identity resolution is invalid or ambiguous",
        ) from None
    if len(resolution.segments) > MAX_RESOLUTION_SEGMENTS:
        raise _http_error(
            413,
            "identity_resolution_too_large",
            "Identity resolution exceeds the segment limit",
        )
    response = JSONResponse(
        content=_serialize_resolution(resolution, pin),
        headers={"Cache-Control": "private, no-store", "Vary": "Authorization, X-API-Key"},
    )
    if len(response.body) > MAX_RESPONSE_BYTES:
        return _error_response(
            413,
            "identity_response_too_large",
            "Identity response exceeds the size limit",
        )
    return response


async def _read_request(request: Request) -> bytes:
    """Read a strict JSON object while enforcing byte and chunk bounds."""
    if request.query_params:
        raise _http_error(422, "invalid_request", "Query parameters are not supported")
    media_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
    if media_type != "application/json":
        raise _http_error(415, "unsupported_media_type", "Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise _http_error(400, "invalid_request", "Request headers are invalid")
        normalized_length = content_length.lstrip("0") or "0"
        maximum_length = str(MAX_REQUEST_BYTES)
        if len(normalized_length) > len(maximum_length) or (
            len(normalized_length) == len(maximum_length) and normalized_length > maximum_length
        ):
            raise _http_error(413, "request_too_large", "Request body exceeds the size limit")
    chunks: list[bytes] = []
    total = 0
    chunk_count = 0
    async for chunk in request.stream():
        chunk_count += 1
        if chunk_count > MAX_REQUEST_STREAM_CHUNKS:
            raise _http_error(413, "request_too_large", "Request body exceeds the stream limit")
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            raise _http_error(413, "request_too_large", "Request body exceeds the size limit")
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise _http_error(422, "invalid_request", "Request body is required")
    if not _json_structure_within_limits(payload):
        raise _http_error(422, "invalid_request", "Request validation failed")
    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _http_error(422, "invalid_request", "Request validation failed") from None
    if not isinstance(parsed, dict):
        raise _http_error(422, "invalid_request", "Request body must be a JSON object")
    return payload


def _json_structure_within_limits(payload: bytes) -> bool:
    """Allow only a shallow JSON value with a bounded number of top-level fields.

    The request model contains scalar selectors only. This byte-wise scan avoids
    recursively walking attacker-controlled JSON before the parser sees it.
    """
    depth = 0
    field_separators = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { or [
            depth += 1
            if depth > 1:
                return False
        elif byte in (0x7D, 0x5D):  # } or ]
            depth -= 1
        elif byte == 0x2C and depth == 1:  # ,
            field_separators += 1
            if field_separators >= MAX_REQUEST_JSON_FIELDS:
                return False
    return True


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject repeated JSON keys at every nesting level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject non-standard JSON NaN and infinity constants."""
    raise ValueError("invalid JSON constant")


def _request_matches_pin(body: _ResolveRequest, pin: TemporalIdentityPin) -> bool:
    """Compare every client-provided version field with the trusted server pin."""
    return (
        body.dataset_id == pin.dataset_id
        and body.authority_id == pin.authority_id
        and body.registry_id == pin.registry_id
        and body.reference_version == pin.reference_version
        and body.snapshot_sha256 == pin.snapshot_sha256
    )


def _is_snapshot_reader(value: object) -> TypeGuard[_SnapshotStore]:
    """Check the minimal immutable snapshot read interface without invoking it."""
    return callable(getattr(value, "get", None))


def _exceeds_segment_limit(registry: TemporalIdentityRegistry, body: _ResolveRequest) -> bool:
    """Count clipped known and unknown intervals without allocating result rows."""
    pair = None
    if body.instrument_id is None:
        if body.exchange is None or body.symbol is None:
            raise TemporalInstrumentIdentityError("an explicit pair is required")
        pair = MarketSymbolPair(body.exchange, body.symbol)
    cursor = body.window_start
    segment_count = 0
    for assignment in registry.assignments:
        if pair is not None:
            if assignment.pair != pair:
                continue
        elif assignment.instrument_id != body.instrument_id:
            continue
        segment_start = max(body.window_start, assignment.valid_from)
        segment_end = min(
            body.window_end_exclusive, assignment.valid_to or body.window_end_exclusive
        )
        if segment_start >= segment_end:
            continue
        if body.instrument_id is not None and segment_start < cursor:
            raise TemporalInstrumentIdentityError(
                "instrument_id maps to multiple pairs during the requested window"
            )
        if cursor < segment_start:
            segment_count += 1
        segment_count += 1
        if segment_count > MAX_RESOLUTION_SEGMENTS:
            return True
        cursor = segment_end
    if cursor < body.window_end_exclusive:
        segment_count += 1
    return segment_count > MAX_RESOLUTION_SEGMENTS


def _resolve(registry: TemporalIdentityRegistry, body: _ResolveRequest) -> IdentityResolution:
    """Run exactly one validated pair or opaque-instrument resolution."""
    if body.instrument_id is not None:
        return registry.resolve_instrument(
            body.instrument_id,
            body.window_start,
            body.window_end_exclusive,
        )
    if body.exchange is None or body.symbol is None:
        raise TemporalInstrumentIdentityError("an explicit pair is required")
    pair = MarketSymbolPair(body.exchange, body.symbol)
    return registry.resolve_pair(pair, body.window_start, body.window_end_exclusive)


def _serialize_resolution(
    resolution: IdentityResolution,
    pin: TemporalIdentityPin,
) -> dict[str, object]:
    """Project a resolver result without implying complete market coverage."""
    return {
        "dataset_id": pin.dataset_id,
        "authority_id": pin.authority_id,
        "registry_id": pin.registry_id,
        "reference_version": pin.reference_version,
        "snapshot_sha256": pin.snapshot_sha256,
        "subject": resolution.subject.value,
        "pair": _serialize_pair(resolution.pair),
        "instrument_id": resolution.instrument_id,
        "window": {
            "start": resolution.window_start.isoformat(),
            "end_exclusive": resolution.window_end.isoformat(),
            "semantics": "half_open_calendar_dates",
        },
        "segments": [_serialize_segment(segment) for segment in resolution.segments],
        "completeness": "not_established_by_identity_resolution",
        "source_integrity": "snapshot_bytes_pinned_source_evidence_not_authenticated",
    }


def _serialize_pair(pair: MarketSymbolPair | None) -> dict[str, str] | None:
    """Serialize an explicit exchange-qualified pair, if one was requested."""
    if pair is None:
        return None
    return {"exchange": Exchange(pair.exchange).value, "symbol": pair.symbol}


def _serialize_segment(segment: IdentityResolutionSegment) -> dict[str, object]:
    """Serialize one known assignment or evidence-unknown interval."""
    return {
        "start": segment.valid_from.isoformat(),
        "end_exclusive": segment.valid_to.isoformat(),
        "status": IdentityResolutionStatus(segment.status).value,
        "assignment_id": segment.assignment_id,
        "instrument_id": segment.instrument_id,
        "pair": _serialize_pair(segment.pair),
        "source_id": segment.source_id,
        "source_version": segment.source_version,
    }


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Create a fixed public error contract without propagating sensitive details."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _http_error_contract(status_code: int, detail: object = None) -> tuple[str, str]:
    """Preserve only known domain errors and map all other details safely."""
    fallback = {
        400: ("invalid_request", "Request is invalid"),
        401: ("not_authenticated", "Authentication is required"),
        403: ("forbidden", "Request is not permitted"),
        413: ("request_too_large", "Request exceeds a supported size limit"),
        415: ("unsupported_media_type", "Content-Type must be application/json"),
        422: ("invalid_request", "Request validation failed"),
        429: ("rate_limited", "Request rate limit exceeded"),
        503: ("temporal_identity_unavailable", "Temporal identity service is unavailable"),
    }
    allowed = {
        (400, "invalid_request"): fallback[400],
        (413, "request_too_large"): (
            "request_too_large",
            "Request body exceeds the size limit",
        ),
        (413, "identity_resolution_too_large"): (
            "identity_resolution_too_large",
            "Identity resolution exceeds the segment limit",
        ),
        (413, "identity_response_too_large"): (
            "identity_response_too_large",
            "Identity response exceeds the size limit",
        ),
        (415, "unsupported_media_type"): fallback[415],
        (422, "invalid_request"): fallback[422],
        (422, "invalid_identity_resolution"): (
            "invalid_identity_resolution",
            "Identity resolution is invalid or ambiguous",
        ),
        (503, "temporal_identity_unavailable"): fallback[503],
    }
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str) and (status_code, code) in allowed:
            return allowed[(status_code, code)]
    return fallback.get(status_code, ("request_failed", "Temporal identity request failed"))


def _error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a compact non-cacheable error without exception or database data."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization, X-API-Key",
            **(dict(headers) if headers is not None else {}),
        },
    )


__all__ = [
    "IDENTITY_RESOLVE_OPERATION",
    "MAX_REQUEST_BYTES",
    "MAX_RESOLUTION_SEGMENTS",
    "MAX_RESPONSE_BYTES",
    "TemporalIdentityConfigurationError",
    "TemporalIdentityPin",
    "TemporalIdentityPinRegistry",
    "router",
]
