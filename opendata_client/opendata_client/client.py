"""Minimal Python client for the opendata REST API (design §10.4).

The consumer (``backtrader_web``) installs this directory as a git
dependency, hands over an API key and asks for bars:

    client = OpendataClient("http://127.0.0.1:8000", api_key=key)
    page = client.stock_daily(["600519"], start="2024-01-01", adjust="qfq")

Synchronous on purpose: the consumer prepares backtest data in the same
process that runs the backtest. WebSocket subscription (the "notify then
pull" helper of design §10.4) is not part of this minimal client yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT = 30.0


class OpendataClientError(RuntimeError):
    """Base error: the API answered with a failure or was unreachable."""


class AuthenticationError(OpendataClientError):
    """The API key or token was rejected (HTTP 401)."""


class PermissionDeniedError(OpendataClientError):
    """The credential is valid but out of scope (HTTP 403)."""


class NotFoundError(OpendataClientError):
    """The domain, asset class or table does not exist (HTTP 404)."""


class InvalidQueryError(OpendataClientError):
    """The API rejected the request (HTTP 400/422)."""


class UnsupportedQueryError(OpendataClientError):
    """The API cannot serve the request yet (HTTP 501)."""


@dataclass(frozen=True)
class Page:
    """One page of rows plus the metadata the API reports.

    Attributes:
        rows: Row dicts keyed by the API's column names.
        columns: Column order as returned.
        page: One-based page number.
        page_size: Requested page size.
        count: Rows in this page.
    """

    rows: list[dict[str, Any]]
    columns: list[str] = field(default_factory=list)
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    count: int = 0

    def symbols(self) -> set[str]:
        """Return the distinct symbols present in this page.

        Returns:
            The set of symbol values found in ``rows``.
        """
        return {str(row["symbol"]) for row in self.rows if "symbol" in row}


def _csv(value: str | Sequence[str] | None) -> str | None:
    """Render a list of identifiers as the API's comma separated form.

    Args:
        value: A single value or a sequence.

    Returns:
        The comma separated string, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return ",".join(str(item) for item in value)


class OpendataClient:
    """Synchronous REST client for the opendata API.

    Attributes:
        base_url: API root, e.g. ``http://127.0.0.1:8000``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Create a client.

        Args:
            base_url: API root; ``/api/v1`` is appended per request.
            api_key: Consumer API key (``od-...``); sent as ``X-API-Key``.
            token: JWT access token; sent as ``Authorization: Bearer``.
            timeout: Per-request timeout in seconds.
            transport: Optional httpx transport (tests inject a mock or a
                transport that reaches the app in-process).
            http_client: Optional pre-built client (takes precedence).
        """
        if api_key is None and token is None:
            raise OpendataClientError("pass an api_key or a token")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._token = token
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers=self._auth_headers(),
        )

    def _auth_headers(self) -> dict[str, str]:
        """Build the authentication headers.

        Returns:
            The ``X-API-Key`` header when a key is configured, otherwise
            the bearer token (the API prefers the key when both appear).
        """
        if self._api_key is not None:
            return {"X-API-Key": self._api_key}
        return {"Authorization": f"Bearer {self._token}"}

    @property
    def headers(self) -> dict[str, str]:
        """Return the headers every request carries."""
        return dict(self._client.headers)

    def close(self) -> None:
        """Close the underlying HTTP client when this object owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpendataClient:
        """Enter a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Exit a context manager, closing the HTTP client."""
        self.close()

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate an HTTP failure into a client error.

        Args:
            response: The response to inspect.

        Raises:
            OpendataClientError: For 400/401/403/404/429/501 and any
                other non-2xx status, carrying the API's detail text.
        """
        if response.is_success:
            return
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("detail") or body.get("message") or "")
        except ValueError:
            detail = response.text[:200]
        message = f"HTTP {response.status_code}: {detail}"
        raise _ERRORS.get(response.status_code, OpendataClientError)(message)

    def _get(self, path: str, params: dict[str, Any]) -> Any:  # noqa: ANN401  # API JSON payload
        """Perform a GET and return the unwrapped ``data`` payload.

        Args:
            path: Path below ``/api/v1``.
            params: Query parameters; ``None`` values are dropped.

        Returns:
            The ``data`` field of the API envelope.

        Raises:
            OpendataClientError: On transport failure or a failure body.
        """
        clean = {key: value for key, value in params.items() if value is not None}
        try:
            response = self._client.get(f"/api/v1{path}", params=clean)
        except httpx.HTTPError as exc:  # unreachable host, timeout, ...
            raise OpendataClientError(f"request to {path} failed: {exc}") from exc
        self._raise_for_status(response)
        payload = response.json()
        if not payload.get("success", True):
            raise OpendataClientError(str(payload.get("message") or "API reported a failure"))
        return payload.get("data")

    def query(  # REST surface mirrors the API contract
        self,
        asset_class: str,
        domain: str,
        *,
        symbols: str | Sequence[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        source: str = "auto",
        layer: str = "dwd",
        adjust: str = "none",
        fields: str | Sequence[str] | None = None,
        period: str | None = None,
        report_date: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page:
        """Query one domain and return one page.

        Args:
            asset_class: Asset class segment of the path.
            domain: Domain identifier.
            symbols: One symbol or a sequence.
            start: Inclusive start date (``YYYY-MM-DD``).
            end: Inclusive end date.
            source: Source for the ods layer, or ``auto`` for dwd.
            layer: ``dwd`` (default) or ``ods``.
            adjust: ``none``, ``qfq`` or ``hfq``.
            fields: Field whitelist.
            period: Period filter for domains that need one.
            report_date: Report date filter for financial domains.
            page: One-based page number.
            page_size: Rows per page.

        Returns:
            The page with its rows and column order.
        """
        data = (
            self._get(
                f"/data/{asset_class}/{domain}",
                {
                    "symbols": _csv(symbols),
                    "start": start,
                    "end": end,
                    "source": source,
                    "layer": layer,
                    "adjust": adjust,
                    "fields": _csv(fields),
                    "period": period,
                    "report_date": report_date,
                    "page": page,
                    "page_size": page_size,
                },
            )
            or {}
        )
        rows = list(data.get("rows") or [])
        return Page(
            rows=rows,
            columns=list(data.get("columns") or []),
            page=int(data.get("page") or page),
            page_size=int(data.get("page_size") or page_size),
            count=int(data.get("count") or len(rows)),
        )

    def iter_rows(self, asset_class: str, domain: str, **kwargs: Any) -> Iterator[dict[str, Any]]:  # noqa: ANN401  # forwarded query args
        """Iterate every row of a query, following pagination.

        Args:
            asset_class: Asset class segment of the path.
            domain: Domain identifier.
            **kwargs: Any argument of :meth:`query` except ``page``.

        Yields:
            One row dict at a time.
        """
        page_number = int(kwargs.pop("page", 1))
        page_size = int(kwargs.pop("page_size", DEFAULT_PAGE_SIZE))
        while True:
            page = self.query(asset_class, domain, page=page_number, page_size=page_size, **kwargs)
            yield from page.rows
            if page.count < page.page_size or not page.rows:
                return
            page_number += 1

    def stock_daily(
        self,
        symbols: str | Sequence[str],
        start: str | None = None,
        end: str | None = None,
        *,
        adjust: str = "none",
        layer: str = "dwd",
        source: str = "auto",
        fields: str | Sequence[str] | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page:
        """Fetch A-share daily bars (the design's reference call).

        Args:
            symbols: One symbol or a sequence.
            start: Inclusive start date.
            end: Inclusive end date.
            adjust: ``none``, ``qfq`` or ``hfq``.
            layer: ``dwd`` (default) or ``ods``.
            source: Source for the ods layer.
            fields: Field whitelist.
            page: One-based page number.
            page_size: Rows per page.

        Returns:
            The page of bars.
        """
        return self.query(
            "equity",
            "stock_daily",
            symbols=symbols,
            start=start,
            end=end,
            source=source,
            layer=layer,
            adjust=adjust,
            fields=fields,
            page=page,
            page_size=page_size,
        )

    def stock_daily_all(self, symbols: str | Sequence[str], **kwargs: Any) -> list[dict[str, Any]]:  # noqa: ANN401  # forwarded query args
        """Fetch every A-share daily bar of a query, page by page.

        Args:
            symbols: One symbol or a sequence.
            **kwargs: Any argument of :meth:`stock_daily`.

        Returns:
            All rows across pages.
        """
        kwargs.pop("page", None)
        kwargs.pop("page_size", None)
        return list(self.iter_rows("equity", "stock_daily", symbols=symbols, **kwargs))

    def catalog(self) -> list[dict[str, Any]]:
        """List the domains this credential may read (FR-20).

        Returns:
            One entry per visible domain.
        """
        data = self._get("/data/catalog", {}) or {}
        return list(data.get("domains") or [])

    def freshness(self, domain: str, *, source: str | None = None) -> dict[str, Any]:
        """Report how current one domain is (design §9.4).

        Args:
            domain: Domain identifier.
            source: Source for the ods layer; defaults to dwd.

        Returns:
            The freshness payload.
        """
        return dict(self._get(f"/data/domains/{domain}/freshness", {"source": source}) or {})

    def diff_report(
        self, domain: str, *, batch_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Read the cross-check differences of one domain (design §8.2).

        Args:
            domain: Domain identifier.
            batch_id: Restrict to one cross-check batch.
            limit: Maximum rows.

        Returns:
            The difference rows.
        """
        data = (
            self._get(f"/data/domains/{domain}/diff-report", {"batch_id": batch_id, "limit": limit})
            or {}
        )
        return list(data.get("rows") or [])


_ERRORS: dict[int, type[OpendataClientError]] = {
    400: InvalidQueryError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    422: InvalidQueryError,
    429: OpendataClientError,
    501: UnsupportedQueryError,
}
