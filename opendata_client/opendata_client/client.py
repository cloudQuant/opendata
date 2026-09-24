"""Minimal Python client for the opendata REST API (design §10.4).

The consumer (``backtrader_web``) installs this directory as a git
dependency, hands over an API key and asks for bars:

    client = OpendataClient("http://127.0.0.1:8000", api_key=key)
    page = client.stock_daily(["600519"], start="2024-01-01", adjust="qfq")

Synchronous on purpose: the consumer prepares backtest data in the same
process that runs the backtest. The WebSocket helper (design §10.4
"notify then pull") keeps that shape - :meth:`OpendataClient.subscribe`
is a blocking generator, so a consumer loop is a plain ``for``.

The socket half needs the ``ws`` extra (``websockets``); the REST half
does not, so a consumer that only pulls history installs nothing extra.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT = 30.0
#: Seconds to wait for the server's first answer after subscribing.
DEFAULT_WS_TIMEOUT = 30.0


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


class SubscriptionError(OpendataClientError):
    """The data subscription socket failed or reported an error frame."""


@dataclass(frozen=True)
class DataUpdate:
    """One ``data.update`` event of a subscription (design §10.2).

    Attributes:
        domain: Domain the batch landed in.
        source: Source that produced the batch.
        layer: Layer the batch landed in (``ods`` / ``dwd``).
        batch_id: Batch identifier - the watermark key a reconnect can
            replay from.
        window: ``{"start": ..., "end": ...}`` of the batch.
        rows: Rows the batch wrote.
        created_at: When the batch finished (ISO-8601, UTC).
        replayed: True when this event is catch-up traffic rather than a
            live push.
        payload: ``meta`` (notification only) or ``full`` (rows inline).
        data: Batch rows; empty for ``meta`` unless the caller asked the
            client to pull them.
    """

    domain: str
    source: str
    layer: str
    batch_id: str
    window: dict[str, str | None]
    rows: int
    created_at: str
    replayed: bool = False
    payload: str = "meta"
    data: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> DataUpdate:
        """Build an update from a ``data.update`` frame.

        Args:
            message: The decoded frame.

        Returns:
            The parsed update.
        """
        return cls(
            domain=str(message.get("domain", "")),
            source=str(message.get("source", "")),
            layer=str(message.get("layer", "")),
            batch_id=str(message.get("batch_id", "")),
            window=dict(message.get("window") or {}),
            rows=int(message.get("rows") or 0),
            created_at=str(message.get("created_at", "")),
            replayed=bool(message.get("replayed", False)),
            payload=str(message.get("payload", "meta")),
            data=tuple(message.get("data") or ()),
        )


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


def _symbol_list(values: str | Sequence[str] | None) -> list[str]:
    """Normalize a symbol argument into the list the socket expects.

    Args:
        values: One symbol or a sequence.

    Returns:
        The symbols as a list (empty when none were given).
    """
    if values is None:
        return []
    if isinstance(values, str):
        return [item.strip() for item in values.split(",") if item.strip()]
    return [str(item) for item in values]


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
        self._asset_classes: dict[str, str] | None = None
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
            The ``data`` field of the API envelope; a body that is not an
            envelope at all (the bare list of ``/data/capabilities``) is
            returned as it came.

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
        if not isinstance(payload, dict):
            return payload
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

    def subscribe(
        self,
        domain: str,
        *,
        layer: str = "dwd",
        payload: str = "meta",
        symbols: str | Sequence[str] | None = None,
        since_batch_id: str | None = None,
        asset_class: str | None = None,
        pull: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float | None = DEFAULT_WS_TIMEOUT,
    ) -> SubscriptionSession:
        """Open a subscription to one domain (design §10.2/§10.4).

        The returned session is a context manager and an iterator, so a
        consumer loop is a plain ``for``::

            with client.subscribe("stock_daily", pull=True) as batches:
                for batch in batches:
                    backtest.add(batch.data)

        The socket is connected, authenticated with the first frame and
        subscribed to when the session opens; iterating yields one
        :class:`DataUpdate` per ``data.update``.

        Args:
            domain: Domain identifier to watch.
            layer: Layer to watch (``dwd`` by default).
            payload: ``meta`` (notifications) or ``full`` (rows inline).
            symbols: Optional symbol filter for live events.
            since_batch_id: Last batch already processed, for catch-up.
            asset_class: Asset class used when ``pull`` reads the rows
                back; defaults to the domain's registered one.
            pull: Fetch each batch's rows over REST after the notice.
            page_size: Page size of those REST reads.
            timeout: Seconds to wait for the server's first answer.

        Returns:
            The open session (nothing has been sent until it is entered
            or iterated).

        Raises:
            OpendataClientError: ``websockets`` is not installed.
        """
        return SubscriptionSession(
            self,
            domain,
            layer=layer,
            payload=payload,
            symbols=_symbol_list(symbols),
            since_batch_id=since_batch_id,
            asset_class=asset_class,
            pull=pull,
            page_size=page_size,
            timeout=timeout,
        )

    def _with_rows(
        self,
        update: DataUpdate,
        domain: str,
        asset_class: str | None,
        page_size: int,
    ) -> DataUpdate:
        """Attach the batch rows read back over REST."""
        if update.payload == "full":
            return update
        rows = list(
            self.iter_rows(
                asset_class or self._asset_class_for(domain),
                domain,
                layer=update.layer,
                source=update.source,
                start=update.window.get("start"),
                end=update.window.get("end"),
                page_size=page_size,
            )
        )
        return replace(update, data=tuple(rows))

    def _expect(self, socket: Any, kind: str) -> dict[str, Any]:  # noqa: ANN401  # websockets conn
        """Read frames until the expected type arrives.

        Args:
            socket: The connected socket.
            kind: Expected ``type`` value.

        Returns:
            The frame.

        Raises:
            SubscriptionError: The server answered with an error frame
                or closed the socket first.
        """
        for raw in socket:
            frame: dict[str, Any] = json.loads(raw)
            if frame.get("type") == kind:
                return frame
            if frame.get("type") == "error":
                raise SubscriptionError(
                    f"{frame.get('code')}: {frame.get('message')} ({frame.get('suggestion')})"
                )
        raise SubscriptionError(f"connection closed before {kind}")

    def _asset_class_for(self, domain: str) -> str:
        """Look up the asset class a domain is registered under.

        The client does not carry the server's domain registry, so it
        asks the capabilities endpoint - the same source the server
        routes by - and remembers the answer.

        Args:
            domain: Domain identifier.

        Returns:
            The asset class segment of the REST path.

        Raises:
            NotFoundError: The server does not register the domain.
        """
        if self._asset_classes is None:
            payload = self._get("/data/capabilities", {}) or []
            self._asset_classes = {
                str(entry.get("domain")): str(entry.get("asset_class"))
                for entry in payload
                if isinstance(entry, dict)
            }
        try:
            return self._asset_classes[domain]
        except KeyError as exc:
            raise NotFoundError(f"domain {domain!r} is not registered") from exc

    def _credential(self) -> str:
        """Return the credential to present in the auth frame."""
        return self._api_key if self._api_key is not None else str(self._token)

    def _ws_url(self) -> str:
        """Return the WebSocket URL of the subscription endpoint."""
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}/ws/data/subscribe"


class SubscriptionSession:
    """One open ``/ws/data/subscribe`` connection (design §10.2).

    Usable as a context manager and as an iterator; entering it performs
    the documented handshake (first-frame auth, then subscribe) and
    leaves the socket ready to yield updates.

    Attributes:
        acknowledged: True once the server accepted the subscription.
        replayed: Batches the server replayed for ``since_batch_id``.
    """

    def __init__(
        self,
        client: OpendataClient,
        domain: str,
        *,
        layer: str = "dwd",
        payload: str = "meta",
        symbols: Sequence[str] = (),
        since_batch_id: str | None = None,
        asset_class: str | None = None,
        pull: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float | None = DEFAULT_WS_TIMEOUT,
    ) -> None:
        """Bind a session to its client and subscription parameters."""
        self._client = client
        self._domain = domain
        self._layer = layer
        self._payload = payload
        self._symbols = list(symbols)
        self._since_batch_id = since_batch_id
        self._asset_class = asset_class
        self._pull = pull
        self._page_size = page_size
        self._timeout = timeout
        self._socket: Any = None  # websockets' ClientConnection
        self.acknowledged = False
        self.replayed = 0

    def __enter__(self) -> SubscriptionSession:
        """Connect, authenticate and subscribe.

        Returns:
            This session, ready to iterate.

        Raises:
            SubscriptionError: The socket could not be opened, the
                credential was rejected, or the server refused the
                subscription.
            OpendataClientError: ``websockets`` is not installed.
        """
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # optional extra
            raise OpendataClientError(
                "WebSocket subscription needs the 'websockets' package: "
                "pip install 'opendata-client[ws]'"
            ) from exc

        url = self._client._ws_url()
        try:
            self._socket = connect(url, open_timeout=self._timeout).__enter__()
        except OSError as exc:  # unreachable host, handshake refused
            raise SubscriptionError(f"subscription to {url} failed: {exc}") from exc
        try:
            self._socket.send(json.dumps({"action": "auth", "token": self._client._credential()}))
            self._client._expect(self._socket, "auth.ok")
            self._socket.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "domain": self._domain,
                        "layer": self._layer,
                        "payload": self._payload,
                        "symbols": self._symbols,
                        "since_batch_id": self._since_batch_id,
                    }
                )
            )
            ack = self._client._expect(self._socket, "subscribe.ok")
        except SubscriptionError:
            self.close()
            raise
        self.acknowledged = True
        self.replayed = int(ack.get("replayed") or 0)
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the socket."""
        self.close()

    def close(self) -> None:
        """Close the socket if it is open."""
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError as exc:  # already gone
                logger.debug(f"subscription close failed: {exc}")
            self._socket = None

    def __iter__(self) -> Iterator[DataUpdate]:
        """Yield one update per batch that lands.

        Yields:
            The updates, with rows attached when ``pull`` is set.

        Raises:
            SubscriptionError: The server sent an error frame, or the
                session was never entered.
        """
        if self._socket is None:
            self.__enter__()
        for raw in self._socket:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "data.update":
                update = DataUpdate.from_message(message)
                if self._pull:
                    update = self._client._with_rows(
                        update, self._domain, self._asset_class, self._page_size
                    )
                yield update
            elif kind == "error":
                raise SubscriptionError(
                    f"{message.get('code')}: {message.get('message')} ({message.get('suggestion')})"
                )


_ERRORS: dict[int, type[OpendataClientError]] = {
    400: InvalidQueryError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    422: InvalidQueryError,
    429: OpendataClientError,
    501: UnsupportedQueryError,
}
