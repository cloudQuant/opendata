"""Alpha Vantage HTTP and response-contract handling."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, ClassVar, Protocol

from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpFetchError,
    get_shared_http_client,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

ALPHA_VANTAGE_ENDPOINT = "https://www.alphavantage.co/query"


class AlphaVantageError(RuntimeError):
    """Base error for rejected Alpha Vantage requests or responses.

    The ``code``/``status`` faces make each refusal machine-readable for the
    offline witness counterfact phase; ``status`` stays ``None`` unless the
    raise site was driven by an explicit HTTP status.
    """

    code: ClassVar[str] = "ALPHA_VANTAGE_ERROR"
    status: ClassVar[int | None] = None


class AlphaVantageMissingKeyError(AlphaVantageError):
    """Raised before transport when no API key is configured."""

    code: ClassVar[str] = "ALPHA_VANTAGE_MISSING_KEY"


class AlphaVantageRateLimitError(AlphaVantageError):
    """Raised when the provider reports a rate or entitlement limit."""

    code: ClassVar[str] = "ALPHA_VANTAGE_RATE_LIMITED"


class AlphaVantageResponseError(AlphaVantageError):
    """Raised when the response does not match the documented contract."""

    code: ClassVar[str] = "ALPHA_VANTAGE_BAD_RESPONSE"


class AlphaVantageHttpResponse(Protocol):
    """Minimum HTTP response surface consumed by this client."""

    status_code: int
    text: str


class AlphaVantageTransport(Protocol):
    """Transport seam that keeps fixtures independent of live HTTP."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout: float | tuple[float, float] | None,
        source: str,
        cache_context: Mapping[str, str],
    ) -> AlphaVantageHttpResponse:
        """Issue a governed GET using provider parameters and cache identity."""
        ...


class AlphaVantageClient:
    """Call the public query endpoint through the shared governed client."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: AlphaVantageTransport | GovernedHttpClient | None = None,
        endpoint: str = ALPHA_VANTAGE_ENDPOINT,
    ) -> None:
        """Configure the API key, transport, and HTTPS endpoint."""
        key = api_key if api_key is not None else os.environ.get("ALPHA_VANTAGE_API_KEY")
        self._api_key = key.strip() if isinstance(key, str) else ""
        # Resolution is deferred to the request so the module-level
        # ``get_shared_http_client`` name is read per call (offline witnesses
        # patch that attribute after this constructor runs).
        self._http_client = http_client
        self._endpoint = endpoint

    def get_json(
        self,
        params: Mapping[str, str],
        *,
        timeout: float | None,
    ) -> dict[str, object]:
        """Fetch and validate one JSON response without exposing credentials."""
        body = self._get_text(params, timeout=timeout)
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AlphaVantageResponseError("Alpha Vantage returned invalid JSON") from exc
        if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
            raise AlphaVantageResponseError("Alpha Vantage returned a non-object JSON response")
        payload = parsed
        self._raise_provider_message(payload)
        return payload

    def _get_text(self, params: Mapping[str, str], *, timeout: float | None) -> str:
        if not self._api_key:
            raise AlphaVantageMissingKeyError(
                "ALPHA_VANTAGE_API_KEY is required before querying Alpha Vantage"
            )
        if not self._endpoint.startswith("https://www.alphavantage.co/"):
            raise ValueError("Alpha Vantage endpoint must use the official HTTPS host")
        query: dict[str, str] = {str(key): str(value) for key, value in params.items()}
        if "apikey" in query or "api_key" in query:
            raise ValueError("API keys are supplied by the client, not request parameters")
        query["apikey"] = self._api_key
        cache_context = {
            key: value
            for key, value in query.items()
            if key != "apikey" and key in {"function", "symbol", "interval", "month", "outputsize"}
        }
        transport = self._http_client if self._http_client is not None else get_shared_http_client()
        try:
            response = transport.get(
                self._endpoint,
                params=query,
                timeout=timeout,
                source="alpha_vantage",
                cache_context=cache_context,
            )
        except HttpFetchError as exc:
            if exc.category is FailureCategory.RATE_LIMITED:
                raise AlphaVantageRateLimitError(
                    "Alpha Vantage HTTP rate limit was reached"
                ) from exc
            raise
        if not 200 <= response.status_code < 300:
            if response.status_code == 429:
                raise AlphaVantageRateLimitError("Alpha Vantage HTTP rate limit was reached")
            raise AlphaVantageResponseError(
                f"Alpha Vantage returned HTTP status {response.status_code}"
            )
        return response.text

    @staticmethod
    def _raise_provider_message(payload: Mapping[str, object]) -> None:
        """Keep API errors distinct from a valid empty time series."""
        if "Information" in payload or "Note" in payload:
            raise AlphaVantageRateLimitError(
                "Alpha Vantage returned an Information/Note limit or entitlement message"
            )
        if "Error Message" in payload:
            raise AlphaVantageResponseError("Alpha Vantage rejected the request")
