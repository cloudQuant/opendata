"""Governed, key-safe transport for the offline FMP model prototype."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    ProviderEngineQueryError,
)
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from requests import Response

FMP_DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"
FMP_HISTORICAL_RESPONSE_LIMIT = 5_000
_DEFAULT_TIMEOUT_SECONDS = 30.0

#: ``(url, params, *, timeout, source, headers) -> HttpResponse``; the offline witness seam.
FMPTransport = Callable[..., HttpResponse]


class FMPProviderError(ProviderEngineError):
    """Stable FMP errors that never include request parameters or response bodies.

    The engine's typed refusal is the base so an FMP failure carries the same machine-readable
    face (``code``/``status``/``url``/``rejected``/``missing``/``invalid``) as a declared-model
    refusal, and a witness can judge the refusal the provider actually produced. ``partial`` and
    ``requests_made`` stay on the face because FMP's bounded calendars promise to say whether the
    answer is a prefix of the window.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        partial: bool = False,
        requests_made: int = 0,
        url: str | None = None,
        rejected: Sequence[str] = (),
        missing: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        """Create a sanitized error with machine-readable status and attribution fields."""
        super().__init__(
            code,
            status=status,
            url=url,
            rejected=rejected,
            missing=missing,
            invalid=invalid,
        )
        self.partial = partial
        self.requests_made = requests_made


class FMPQueryValidationError(ProviderEngineQueryError, FMPProviderError):
    """An invalid FMP query recognized by both provider and query services."""

    def __init__(
        self,
        *,
        missing: Sequence[str] = (),
        rejected: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        """Preserve the sanitized FMP code and initialize both exception contracts."""
        FMPProviderError.__init__(
            self,
            "FMP_BAD_QUERY",
            url=None,
            rejected=rejected,
            missing=missing,
            invalid=invalid,
        )

    @classmethod
    def from_validation_error(cls, exc: ValidationError) -> FMPQueryValidationError:
        """Blame each offending parameter key: missing, extra-forbidden, or invalid."""
        errors = exc.errors()
        missing = sorted({str(e["loc"][0]) for e in errors if e["type"] == "missing"})
        rejected = sorted({str(e["loc"][0]) for e in errors if e["type"] == "extra_forbidden"})
        invalid = sorted(
            {
                str(e["loc"][0])
                for e in errors
                if e["type"] not in {"missing", "extra_forbidden"} and e["loc"]
            }
        )
        return cls(missing=missing, rejected=rejected, invalid=invalid)


class FMPQueryLimitError(FMPProviderError):
    """A response cannot be proven complete within the caller's declared budget."""

    def __init__(
        self,
        code: str,
        *,
        limit_kind: str,
        maximum: int,
        requests_made: int,
    ) -> None:
        """Create an explicit incomplete-result error."""
        self.limit_kind = limit_kind
        self.maximum = maximum
        super().__init__(
            code,
            partial=requests_made > 0,
            requests_made=requests_made,
        )


def require_api_key() -> str:
    """Read only the explicit FMP_API_KEY environment variable."""
    key = os.environ.get("FMP_API_KEY", "")
    if not key.strip():
        raise FMPProviderError("FMP_API_KEY_MISSING")
    return key.strip()


def contract_shape_error(
    model: Any,  # noqa: ANN401  # the row contract class being validated
    exc: Exception,
    code: str = "FMP_BAD_SHAPE",
) -> FMPProviderError:
    """Attribute a refused row contract to the contract columns that rejected the cell.

    FMP's hand-written adapters used to answer every shape failure with a bare code, which left a
    caller unable to tell which column the source broke. Each pydantic error's ``loc`` head is
    translated through the model's own alias map, so the refusal names the published column
    (``period_ending``) rather than the wire key (``date``), and no source value is ever echoed.
    A failure that is not a validation error keeps the bare code, which is the honest answer.
    """
    if not isinstance(exc, ValidationError):
        return FMPProviderError(code)
    names: dict[str, str] = {}
    for name, field in getattr(model, "model_fields", {}).items():
        names[name] = name
        alias = getattr(field, "alias", None)
        if isinstance(alias, str):
            names[alias] = name
    missing: set[str] = set()
    invalid: set[str] = set()
    for error in exc.errors():
        location = error.get("loc") or ()
        head = str(location[0]) if location else ""
        column = names.get(head, head)
        if not column:
            continue
        (missing if error.get("type") == "missing" else invalid).add(column)
    return FMPProviderError(code, missing=sorted(missing), invalid=sorted(invalid))


def fetch_array(
    endpoint: str,
    params: Mapping[str, str],
    timeout: float | None,
    transport: FMPTransport | None = None,
) -> list[dict[str, Any]]:
    """Fetch one JSON object array through the governed client or the offline witness seam.

    ``transport`` is the seam an offline witness injects on the fetcher class: the same status
    classification, error-envelope rejection and array-of-objects shape check run on either leg,
    so a witness judges the provider's own refusal rather than a fixture's convenience. ``None``
    keeps the governed shared HTTP client, which is what every production call uses.
    """
    api_key = require_api_key()
    if transport is None:
        response = _get_response(endpoint, params, api_key, timeout)
        status = response.status_code
        document = _parse_document(response.text, response.headers.get("Content-Type", ""), status)
    else:
        status, document = _transport_document(endpoint, params, api_key, timeout, transport)

    if isinstance(document, dict) and _is_api_error_document(document):
        raise FMPProviderError("FMP_API_ERROR", status=status)
    if not isinstance(document, list) or any(not isinstance(row, dict) for row in document):
        raise FMPProviderError("FMP_BAD_SHAPE", status=status)
    return cast("list[dict[str, Any]]", document)


def _transport_document(
    endpoint: str,
    params: Mapping[str, str],
    api_key: str,
    timeout: float | None,
    transport: FMPTransport,
) -> tuple[int, Any]:  # the seam hands back whatever the body parsed to
    """Send one witness-served GET and classify its status exactly like the governed leg."""
    url = _endpoint_url(endpoint)
    seconds = _DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        response = transport(url, {**params, "apikey": api_key}, timeout=seconds, source="fmp")
    except RequestBudgetError:
        raise
    except ProviderEngineError as exc:
        # The engine transport can carry a prepared URL with the key; keep the public face stable.
        raise FMPProviderError(_http_error_code(exc.status), status=exc.status) from None
    except Exception:
        raise FMPProviderError("FMP_TRANSPORT_ERROR") from None
    if not isinstance(response, HttpResponse):
        raise FMPProviderError("FMP_TRANSPORT_ERROR")
    if not 200 <= response.status < 300:
        raise FMPProviderError(_http_error_code(response.status), status=response.status)
    if response.payload is not None:
        body = (
            response.payload.decode("utf-8")
            if isinstance(response.payload, bytes)
            else str(response.payload)
        )
        return response.status, _parse_document(body, "application/json", response.status)
    return response.status, response.document


def _parse_document(body: str, content_type: str, status: int) -> Any:  # noqa: ANN401  # JSON document
    """Reject an HTML gateway, then parse the body into the document the routes publish."""
    if _looks_like_html(body, content_type):
        raise FMPProviderError("FMP_HTML_RESPONSE", status=status)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        code = "FMP_TRUNCATED_RESPONSE" if _looks_truncated(body, exc) else "FMP_BAD_RESPONSE"
        raise FMPProviderError(code, status=status) from None


def _endpoint_url(endpoint: str) -> str:
    """Build the stable stable-API URL for one documented endpoint."""
    return f"{FMP_DEFAULT_BASE_URL}/{endpoint.lstrip('/')}"


def _get_response(
    endpoint: str,
    params: Mapping[str, str],
    api_key: str,
    timeout: float | None,
) -> Response:
    """Issue a key-bearing request and replace all transport errors safely."""
    url = _endpoint_url(endpoint)
    response: Response | None = None
    failure_code: str | None = None
    failure_status: int | None = None
    request_params = {**params, "apikey": api_key}
    try:
        response = get_shared_http_client().get(
            url,
            params=request_params,
            timeout=_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            source="fmp",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        failure_status = exc.status
        failure_code = _http_error_code(exc.status)
    except Exception:
        # The transport exception can contain the prepared URL and API key.
        # Do not chain or retain it in the public provider error.
        failure_code = "FMP_TRANSPORT_ERROR"

    if failure_code is not None:
        raise FMPProviderError(failure_code, status=failure_status)
    if response is None:  # pragma: no cover - invariant guard
        raise FMPProviderError("FMP_TRANSPORT_ERROR")
    if not 200 <= response.status_code < 300:
        raise FMPProviderError(
            _http_error_code(response.status_code),
            status=response.status_code,
        )
    return response


def _http_error_code(status: int | None) -> str:
    """Map transport status codes into stable FMP error categories."""
    if status == 401:
        return "FMP_AUTH_ERROR"
    if status == 403:
        return "FMP_FORBIDDEN"
    if status == 429:
        return "FMP_RATE_LIMITED"
    if status is not None and status >= 500:
        return "FMP_UPSTREAM_ERROR"
    return "FMP_HTTP_ERROR"


def _looks_like_html(body: str, content_type: str) -> bool:
    """Identify an HTML gateway page before reporting a JSON parse error."""
    stripped = body.lstrip().lower()
    return "text/html" in content_type.lower() or stripped.startswith(("<!doctype html", "<html"))


def _looks_truncated(body: str, error: json.JSONDecodeError) -> bool:
    """Use only parser position to distinguish an incomplete tail from bad JSON."""
    stripped = body.rstrip()
    return (
        not stripped
        or error.msg.startswith("Unterminated")
        or error.pos >= max(0, len(stripped) - 1)
    )


def _is_api_error_document(document: dict[str, Any]) -> bool:
    """Recognize common JSON error envelopes without exposing their text."""
    return any(key in document for key in ("Error Message", "error", "Error", "message"))
