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
import keyword
import math
import os
from collections.abc import Mapping
from contextlib import suppress
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
_MAX_PROVIDER_QUERY_DEPTH = 256


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


def _validate_provider_model_identity(value: object, name: str) -> str:
    """Reject provider/model path components outside the server identity contract."""
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or keyword.iskeyword(value)
        or value.casefold() == "auto"
    ):
        raise ValueError(f"{name} must be a valid provider/model identifier")
    return value


def _query_has_only_json_values(value: dict[str, Any]) -> bool:
    """Check JSON-native values and ordinary string keys without recursion."""
    pending: list[tuple[Any, int, bool]] = [(value, 0, False)]
    active_containers: set[int] = set()

    while pending:
        current, depth, exiting = pending.pop()
        if exiting:
            active_containers.remove(id(current))
            continue
        if depth > _MAX_PROVIDER_QUERY_DEPTH:
            return False

        value_type = type(current)
        if current is None or value_type in (str, bool, int):
            continue
        if value_type is float:
            if not math.isfinite(current):
                return False
            continue

        if value_type is dict:
            container_id = id(current)
            if container_id in active_containers:
                return False
            active_containers.add(container_id)
            pending.append((current, depth, True))
            for key, child in current.items():
                if type(key) is not str:
                    return False
                pending.append((child, depth + 1, False))
            continue

        if value_type is list:
            container_id = id(current)
            if container_id in active_containers:
                return False
            active_containers.add(container_id)
            pending.append((current, depth, True))
            pending.extend((child, depth + 1, False) for child in current)
            continue

        return False

    return True


def _to_dataframe(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    columns: list[str] | None = None,
) -> Any:  # noqa: ANN401  # pandas is an optional runtime dependency
    """Convert rows to a pandas DataFrame when the frames extra is installed."""
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "DataFrame conversion requires pandas; install it with "
            "`pip install 'opendata-client[frames]'`."
        ) from exc
    return pd.DataFrame(rows, columns=columns)


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

    def to_dataframe(self) -> Any:  # noqa: ANN401  # pandas is optional
        """Convert this update's rows to a pandas DataFrame.

        Column order follows first occurrence across the rows because
        notification frames do not include REST column metadata.

        Returns:
            The update data as a DataFrame.

        Raises:
            ImportError: If the ``frames`` extra is not installed.
        """
        return _to_dataframe(self.data)


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

    def to_dataframe(self) -> Any:  # noqa: ANN401  # pandas is optional
        """Convert the page rows to a pandas DataFrame.

        Column order follows the service's ``columns`` metadata. Empty
        pages retain those columns when the service supplied them.

        Returns:
            The page rows as a DataFrame.

        Raises:
            ImportError: If the ``frames`` extra is not installed.
        """
        return _to_dataframe(self.rows, self.columns or None)


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
        """List the domains this credential may read (FR-20 / AC-18|02).

        Returns:
            One entry per visible domain: the merged ``table`` and the
            ``freshness_field`` it is measured on, ``coverage`` (rows, 标的数,
            时间范围), the domain's own ``latest``/``lag_days``/``status``, a
            ``quality`` flag and one ``sources`` leg per registered source
            (``unmapped`` when that leg has no field mapping to measure with).
            A lag is relative to ``expected_data_date``, which
            :meth:`freshness` returns per domain.
        """
        data = self._get("/data/catalog", {}) or {}
        return list(data.get("domains") or [])

    def provider_models(self, *, source: str | None = None) -> list[dict[str, Any]]:
        """List registered provider model descriptors visible to this credential.

        Args:
            source: Optional exact provider identity filter.

        Returns:
            The registered provider model descriptors, or an empty list.

        Raises:
            ValueError: If an explicit source is not a valid exact identity.
            OpendataClientError: If the API returns a malformed payload.
        """
        if source is not None:
            source = _validate_provider_model_identity(source, "source")
        data = self._get("/providers/models", {"source": source})
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise OpendataClientError("provider models response has an invalid shape")
        return data

    def provider_model_schema(self, source: str, model: str) -> dict[str, Any]:
        """Read the complete query validation schema for one registered model.

        Args:
            source: Exact provider identity.
            model: Exact provider model identity.

        Returns:
            The metadata and complete JSON Schema returned by the API.

        Raises:
            ValueError: If either identity is not a valid exact identity.
            OpendataClientError: If the API returns malformed or mismatched metadata.
        """
        source = _validate_provider_model_identity(source, "source")
        model = _validate_provider_model_identity(model, "model")
        data = self._get(f"/providers/{source}/models/{model}/schema", {})
        if (
            not isinstance(data, Mapping)
            or not isinstance(data.get("schema"), Mapping)
            or data.get("source") != source
            or data.get("model") != model
        ):
            raise OpendataClientError("provider model schema response has an invalid shape")
        return dict(data)

    def query_provider_model(
        self, source: str, model: str, query: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute one query through an exact registered provider model.

        The server applies authorization and a bounded execution context. This
        method sends only the caller's complete query object and never creates
        or serializes a trusted execution context.

        Args:
            source: Exact provider identity.
            model: Exact provider model identity.
            query: Complete model query object.

        Returns:
            The complete result metadata and rows returned by the API.

        Raises:
            ValueError: If an identity is invalid or the query is not JSON-compatible.
            OpendataClientError: If the request fails or the API response is malformed.
        """
        source = _validate_provider_model_identity(source, "source")
        model = _validate_provider_model_identity(model, "model")
        if not isinstance(query, Mapping):
            raise ValueError("query must be a JSON-compatible object")

        body_content: bytes | None = None
        with suppress(Exception):  # caller mappings may fail while being copied or encoded
            query_copy = dict(query)
            if _query_has_only_json_values(query_copy):
                body_content = json.dumps(
                    {"query": query_copy},
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        if body_content is None:
            raise ValueError("query must use JSON values, finite numbers, and string object keys")

        response: httpx.Response | None = None
        with suppress(httpx.HTTPError):
            response = self._client.post(
                f"/api/v1/providers/{source}/models/{model}/query",
                content=body_content,
                headers={"Content-Type": "application/json"},
            )
        if response is None:
            raise OpendataClientError("provider model query request failed")

        self._raise_for_status(response)
        envelope: Any = None
        with suppress(ValueError):
            envelope = response.json()
        if not isinstance(envelope, Mapping) or envelope.get("success") is not True:
            raise OpendataClientError("provider model query returned an invalid response envelope")

        data = envelope.get("data")
        if (
            not isinstance(data, Mapping)
            or data.get("source") != source
            or data.get("model") != model
            or not isinstance(data.get("results"), list)
            or any(not isinstance(row, dict) for row in data["results"])
            or "pagination" not in data
        ):
            raise OpendataClientError("provider model query returned an invalid response shape")

        pagination = data["pagination"]
        if pagination is not None:
            if not isinstance(pagination, Mapping):
                raise OpendataClientError("provider model query returned invalid pagination")
            total = pagination.get("total")
            offset = pagination.get("offset")
            limit = pagination.get("limit")
            results = data["results"]
            if (
                type(total) is not int
                or type(offset) is not int
                or type(limit) is not int
                or total < 0
                or offset < 0
                or limit < 1
                or len(results) > limit
                or (results and offset + len(results) > total)
            ):
                raise OpendataClientError("provider model query returned invalid pagination")

        return dict(data)

    def query_provider_model_warehouse(
        self, source: str, model: str, query: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Read one bounded page from a provider model's native warehouse table.

        This method requests one bounded warehouse page. It sends the caller's
        query unchanged and does not follow pagination or convert
        provider-specific date and period fields.

        Args:
            source: Exact provider identity.
            model: Exact provider model identity.
            query: Complete JSON-compatible warehouse query object.

        Returns:
            The complete native warehouse result metadata and rows.

        Raises:
            ValueError: If an identity is invalid or the query is not JSON-compatible.
            OpendataClientError: If the request fails or the response is malformed.
        """
        source = _validate_provider_model_identity(source, "source")
        model = _validate_provider_model_identity(model, "model")
        if not isinstance(query, Mapping):
            raise ValueError("query must be a JSON-compatible object")

        body_content: bytes | None = None
        with suppress(Exception):  # caller mappings may fail while being copied or encoded
            query_copy = dict(query)
            if _query_has_only_json_values(query_copy):
                body_content = json.dumps(
                    {"query": query_copy},
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        if body_content is None:
            raise ValueError("query must use JSON values, finite numbers, and string object keys")

        response: httpx.Response | None = None
        with suppress(httpx.HTTPError):
            response = self._client.post(
                f"/api/v1/providers/{source}/models/{model}/warehouse/query",
                content=body_content,
                headers={"Content-Type": "application/json"},
            )
        if response is None:
            raise OpendataClientError("provider model warehouse query request failed")

        if not response.is_success:
            error_type = _ERRORS.get(response.status_code, OpendataClientError)
            raise error_type(f"HTTP {response.status_code}: provider model warehouse query failed")

        envelope: Any = None
        with suppress(ValueError):
            envelope = response.json()
        if not isinstance(envelope, Mapping) or envelope.get("success") is not True:
            raise OpendataClientError(
                "provider model warehouse query returned an invalid response envelope"
            )

        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise OpendataClientError(
                "provider model warehouse query returned an invalid response shape"
            )
        data_has_json_values = False
        with suppress(Exception):  # decoded response mappings are untrusted input
            data_has_json_values = _query_has_only_json_values(dict(data))
        if not data_has_json_values:
            raise OpendataClientError("provider model warehouse query returned invalid JSON values")

        required_fields = {
            "source",
            "model",
            "domain",
            "verified",
            "read_at",
            "completeness",
            "snapshot_scope",
            "results",
            "pagination",
        }
        if (
            not required_fields.issubset(data)
            or data.get("source") != source
            or data.get("model") != model
            or not isinstance(data.get("domain"), str)
            or not data["domain"]
            or type(data.get("verified")) is not bool
            or not isinstance(data.get("read_at"), str)
            or not data["read_at"]
            or data.get("completeness") != "NOT_ASSESSED"
            or data.get("snapshot_scope") != "single_read_transaction"
            or not isinstance(data.get("results"), list)
            or any(not isinstance(row, dict) for row in data["results"])
        ):
            raise OpendataClientError(
                "provider model warehouse query returned an invalid response shape"
            )

        pagination = data["pagination"]
        page_fields = {"limit", "offset", "returned", "total"}
        if not isinstance(pagination, Mapping) or not page_fields.issubset(pagination):
            raise OpendataClientError("provider model warehouse query returned invalid pagination")
        limit = pagination["limit"]
        offset = pagination["offset"]
        returned = pagination["returned"]
        results = data["results"]
        if (
            type(limit) is not int
            or type(offset) is not int
            or type(returned) is not int
            or pagination["total"] is not None
            or not 1 <= limit <= 1000
            or offset < 0
            or returned != len(results)
            or returned > limit
        ):
            raise OpendataClientError("provider model warehouse query returned invalid pagination")

        return dict(data)

    def ingest_provider_model(
        self, source: str, model: str, query: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Submit one source-native query to the provider-model ingest API.

        The query is sent unchanged inside the API request envelope. The
        returned receipt reports the API's capture and storage metadata.

        Args:
            source: Exact provider identity.
            model: Exact provider model identity.
            query: Complete JSON-compatible source query object.

        Returns:
            The complete ingest receipt returned by the API.

        Raises:
            ValueError: If an identity is invalid or the query is not JSON-compatible.
            OpendataClientError: If the request fails or the response is malformed.
        """
        from datetime import datetime, timedelta
        from uuid import RFC_4122, UUID

        source = _validate_provider_model_identity(source, "source")
        model = _validate_provider_model_identity(model, "model")
        if not isinstance(query, Mapping):
            raise ValueError("query must be a JSON-compatible object")

        body_content: bytes | None = None
        with suppress(Exception):  # caller mappings may fail while being copied or encoded
            query_copy = dict(query)
            if _query_has_only_json_values(query_copy):
                body_content = json.dumps(
                    {"query": query_copy},
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        if body_content is None:
            raise ValueError("query must use JSON values, finite numbers, and string object keys")

        response: httpx.Response | None = None
        with suppress(httpx.HTTPError):
            response = self._client.post(
                f"/api/v1/providers/{source}/models/{model}/ingest",
                content=body_content,
                headers={"Content-Type": "application/json"},
            )
        if response is None:
            raise OpendataClientError("provider model ingest request failed")

        if not response.is_success:
            error_type = _ERRORS.get(response.status_code, OpendataClientError)
            raise error_type(f"HTTP {response.status_code}: provider model ingest failed")

        envelope: Any = None
        with suppress(Exception):  # do not reflect untrusted response parser errors
            envelope = response.json()
        if not isinstance(envelope, Mapping) or envelope.get("success") is not True:
            raise OpendataClientError("provider model ingest returned an invalid response envelope")

        data = envelope.get("data")
        if not isinstance(data, Mapping):
            raise OpendataClientError("provider model ingest returned an invalid response shape")
        data_has_json_values = False
        with suppress(Exception):  # decoded response mappings are untrusted input
            data_has_json_values = _query_has_only_json_values(dict(data))
        if not data_has_json_values:
            raise OpendataClientError("provider model ingest returned invalid JSON values")

        required_fields = {
            "source",
            "model",
            "domain",
            "verified",
            "batch_id",
            "observed_at",
            "raw_rows",
            "stored_rows",
            "raw_scope",
            "completeness",
            "transaction_scope",
        }
        if (
            not required_fields.issubset(data)
            or data.get("source") != source
            or data.get("model") != model
            or not isinstance(data.get("domain"), str)
            or not data["domain"]
            or type(data.get("verified")) is not bool
            or not isinstance(data.get("batch_id"), str)
            or not isinstance(data.get("observed_at"), str)
            or type(data.get("raw_rows")) is not int
            or type(data.get("stored_rows")) is not int
            or not 0 <= data["raw_rows"] <= 10_000
            or not 0 <= data["stored_rows"] <= 10_000
            or data.get("raw_scope") != "extract_data_output"
            or data.get("completeness") != "NOT_ASSESSED"
            or data.get("transaction_scope") != "ods_and_dwd_single_transaction"
        ):
            raise OpendataClientError("provider model ingest returned an invalid response shape")

        batch_id = data["batch_id"]
        parsed_batch_id: UUID | None = None
        with suppress(ValueError, AttributeError, TypeError):
            parsed_batch_id = UUID(batch_id)
        if (
            parsed_batch_id is None
            or str(parsed_batch_id) != batch_id
            or parsed_batch_id.version != 4
            or parsed_batch_id.variant != RFC_4122
        ):
            raise OpendataClientError("provider model ingest returned an invalid response shape")

        observed_at = data["observed_at"]
        parsed_observed_at: datetime | None = None
        with suppress(ValueError, OverflowError):
            parsed_observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if (
            parsed_observed_at is None
            or parsed_observed_at.tzinfo is None
            or parsed_observed_at.utcoffset() != timedelta(0)
            or not (observed_at.endswith("+00:00") or observed_at.endswith("Z"))
        ):
            raise OpendataClientError("provider model ingest returned an invalid response shape")

        return dict(data)

    def export_provider_model(
        self,
        *,
        source: str,
        model: str,
        query: Mapping[str, Any],
        destination: str | os.PathLike[str],
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Write one integrity-checked native warehouse export to a file.

        The API returns one bounded NDJSON snapshot. Bytes are copied to a
        private temporary file and published only after the complete stream,
        metadata, row count, length, and digest have been verified.

        Args:
            source: Exact provider identity.
            model: Exact provider model identity.
            query: JSON-compatible export options and filters.
            destination: File path where the verified NDJSON should be written.
            overwrite: Whether to atomically replace an existing regular file.

        Returns:
            The export metadata, verified row and byte counts, digest, and
            caller-supplied destination.

        Raises:
            ValueError: If an identity, query, overwrite flag, or destination is invalid.
            OpendataClientError: If the request or export validation fails.
        """
        import hashlib
        import stat
        import tempfile
        from pathlib import Path

        source = _validate_provider_model_identity(source, "source")
        model = _validate_provider_model_identity(model, "model")
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a bool")
        if not isinstance(query, Mapping):
            raise ValueError("query must be a JSON-compatible object")

        query_copy: dict[str, Any] = {}
        try:
            for key, value in query.items():
                if type(key) is not str or key in query_copy:
                    raise ValueError("query must use unique string keys")
                query_copy[key] = value
        except Exception:
            raise ValueError("query must be a JSON-compatible object") from None

        allowed_query_fields = {"filters", "start", "end", "max_records", "max_bytes"}
        if set(query_copy) - allowed_query_fields:
            raise ValueError("query contains unsupported export fields")

        max_records = query_copy.get("max_records", _EXPORT_MAX_RECORDS)
        max_bytes = query_copy.get("max_bytes", _EXPORT_MAX_BYTES)
        if type(max_records) is not int or not 1 <= max_records <= _EXPORT_MAX_RECORDS:
            raise ValueError("max_records must be an integer from 1 through 200000")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _EXPORT_MAX_BYTES:
            raise ValueError("max_bytes must be an integer from 1 through 67108864")

        body_content: bytes | None = None
        with suppress(Exception):
            if _query_has_only_json_values(query_copy):
                body_content = json.dumps(
                    {"query": query_copy},
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
        if body_content is None:
            raise ValueError("query must use JSON values, finite numbers, and string object keys")

        try:
            destination_text = os.fspath(destination)
        except Exception:
            raise ValueError("destination must be a filesystem path") from None
        if not isinstance(destination_text, str) or not destination_text:
            raise ValueError("destination must be a non-empty text path")
        try:
            destination_path = Path(destination_text)
            if not destination_path.parent.is_dir():
                raise ValueError("destination parent must be an existing directory")
            try:
                destination_stat = os.lstat(destination_path)
            except FileNotFoundError:
                destination_stat = None
            if destination_stat is not None:
                if stat.S_ISLNK(destination_stat.st_mode) or not stat.S_ISREG(
                    destination_stat.st_mode
                ):
                    raise ValueError("destination must be a regular file path")
                if not overwrite:
                    raise ValueError("destination already exists")
        except ValueError:
            raise
        except Exception:
            raise ValueError("destination is not available") from None

        temporary_path: Path | None = None
        temporary_fd: int | None = None
        try:
            with self._client.stream(
                "POST",
                f"/api/v1/providers/{source}/models/{model}/warehouse/export",
                content=body_content,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    error_type = _ERRORS.get(response.status_code, OpendataClientError)
                    raise error_type(
                        f"HTTP {response.status_code}: provider model warehouse export failed"
                    )

                content_type = response.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip().casefold() != "application/x-ndjson":
                    raise OpendataClientError("provider model warehouse export has invalid headers")
                content_encoding = response.headers.get("content-encoding")
                if (
                    content_encoding is not None
                    and content_encoding.strip().casefold() != "identity"
                ):
                    raise OpendataClientError("provider model warehouse export has invalid headers")

                content_length = _provider_export_uint_header(response.headers, "content-length")
                header_row_count = _provider_export_uint_header(response.headers, "x-row-count")
                expected_digest = response.headers.get("x-content-sha256", "")
                header_created_at = response.headers.get("x-export-created-at")
                header_created_timestamp = _provider_export_parse_utc_timestamp(header_created_at)
                if (
                    content_length > max_bytes
                    or header_row_count > max_records
                    or len(expected_digest) != 64
                    or any(char not in "0123456789abcdef" for char in expected_digest)
                    or response.headers.get("x-ndjson-schema-version") != "1"
                    or response.headers.get("x-snapshot-complete") != "true"
                    or response.headers.get("x-snapshot-consistency") != _EXPORT_CONSISTENCY
                    or response.headers.get("x-completeness") != _EXPORT_COMPLETENESS
                    or header_created_timestamp is None
                ):
                    raise OpendataClientError("provider model warehouse export has invalid headers")

                temporary_fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.",
                    suffix=".opendata-export.tmp",
                    dir=destination_path.parent,
                )
                temporary_path = Path(temporary_name)
                os.fchmod(temporary_fd, 0o600)
                output = os.fdopen(temporary_fd, "wb")
                temporary_fd = None
                digest = hashlib.sha256()
                total_bytes = 0
                row_count = 0
                line_buffer = bytearray()
                metadata: dict[str, Any] | None = None
                summary_seen = False

                with output:
                    for chunk in response.iter_bytes(chunk_size=_EXPORT_CHUNK_SIZE):
                        if not isinstance(chunk, bytes):
                            raise OpendataClientError(
                                "provider model warehouse export stream is invalid"
                            )
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes or total_bytes > content_length:
                            raise OpendataClientError(
                                "provider model warehouse export exceeds its byte limit"
                            )
                        if output.write(chunk) != len(chunk):
                            raise OSError("short export write")
                        digest.update(chunk)
                        line_buffer.extend(chunk)

                        while True:
                            newline_at = line_buffer.find(b"\n")
                            if newline_at < 0:
                                if len(line_buffer) > max_bytes:
                                    raise OpendataClientError(
                                        "provider model export line exceeds byte limit"
                                    )
                                break
                            raw_line = bytes(line_buffer[:newline_at])
                            del line_buffer[: newline_at + 1]
                            if raw_line.endswith(b"\r"):
                                raw_line = raw_line[:-1]
                            if not raw_line:
                                raise OpendataClientError(
                                    "provider model warehouse export contains an empty line"
                                )
                            record = _decode_provider_export_line(raw_line)

                            if metadata is None:
                                metadata_created_timestamp = _provider_export_parse_utc_timestamp(
                                    record.get("created_at")
                                )
                                if (
                                    set(record) != _EXPORT_METADATA_FIELDS
                                    or record.get("kind") != "metadata"
                                    or type(record.get("schema_version")) is not int
                                    or record.get("schema_version") != 1
                                    or record.get("source") != source
                                    or record.get("model") != model
                                    or not isinstance(record.get("domain"), str)
                                    or not record["domain"]
                                    or type(record.get("verified")) is not bool
                                    or metadata_created_timestamp is None
                                    or metadata_created_timestamp != header_created_timestamp
                                    or record.get("consistency") != _EXPORT_CONSISTENCY
                                    or record.get("completeness") != _EXPORT_COMPLETENESS
                                ):
                                    raise OpendataClientError(
                                        "provider model warehouse export metadata is invalid"
                                    )
                                metadata = record
                                continue

                            if summary_seen:
                                raise OpendataClientError(
                                    "provider model warehouse export has trailing records"
                                )
                            kind = record.get("kind")
                            if kind == "row":
                                if set(record) != _EXPORT_ROW_FIELDS or not isinstance(
                                    record.get("data"), dict
                                ):
                                    raise OpendataClientError(
                                        "provider model warehouse export row is invalid"
                                    )
                                row_count += 1
                                if row_count > max_records or row_count > header_row_count:
                                    raise OpendataClientError(
                                        "provider model warehouse export row count is invalid"
                                    )
                            elif kind == "summary":
                                if (
                                    set(record) != _EXPORT_SUMMARY_FIELDS
                                    or type(record.get("rows")) is not int
                                    or record.get("rows") != row_count
                                    or record.get("rows") != header_row_count
                                    or record.get("snapshot_complete") is not True
                                ):
                                    raise OpendataClientError(
                                        "provider model warehouse export summary is invalid"
                                    )
                                summary_seen = True
                            else:
                                raise OpendataClientError(
                                    "provider model warehouse export record is invalid"
                                )

                    if line_buffer:
                        raise OpendataClientError(
                            "provider model warehouse export is missing its final newline"
                        )
                    if (
                        metadata is None
                        or not summary_seen
                        or row_count != header_row_count
                        or total_bytes != content_length
                        or digest.hexdigest() != expected_digest
                    ):
                        raise OpendataClientError(
                            "provider model warehouse export failed integrity checks"
                        )
                    output.flush()
                    os.fsync(output.fileno())

            if temporary_path is None:
                raise OpendataClientError("provider model warehouse export was not staged")
            if overwrite:
                try:
                    final_stat = os.lstat(destination_path)
                except FileNotFoundError:
                    final_stat = None
                if final_stat is not None and (
                    stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISREG(final_stat.st_mode)
                ):
                    raise OpendataClientError(
                        "provider model warehouse export destination is not a regular file"
                    )
                os.replace(temporary_path, destination_path)
            else:
                os.link(temporary_path, destination_path, follow_symlinks=False)
                os.unlink(temporary_path)
            temporary_path = None

            result = dict(metadata)
            result.update(
                {
                    "row_count": row_count,
                    "byte_count": total_bytes,
                    "sha256": expected_digest,
                    "snapshot_complete": True,
                    "destination": destination_text,
                }
            )
            return result
        except OpendataClientError:
            raise
        except Exception:
            raise OpendataClientError(
                "provider model warehouse export failed validation or transfer"
            ) from None
        finally:
            if temporary_fd is not None:
                with suppress(OSError):
                    os.close(temporary_fd)
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink()

    def freshness(self, domain: str, *, source: str | None = None) -> dict[str, Any]:
        """Report how current one domain is (design §9.4).

        Args:
            domain: Domain identifier.
            source: Source for the ods layer; defaults to dwd.

        Returns:
            The freshness payload, including the ``expected_data_date``
            ``lag_days`` was measured against.
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

_EXPORT_MAX_RECORDS = 200_000
_EXPORT_MAX_BYTES = 64 * 1024 * 1024
_EXPORT_CHUNK_SIZE = 64 * 1024
_EXPORT_CONSISTENCY = "single_repeatable_read_transaction"
_EXPORT_COMPLETENESS = "NOT_ASSESSED"
_EXPORT_METADATA_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "source",
        "model",
        "domain",
        "verified",
        "created_at",
        "consistency",
        "completeness",
    }
)
_EXPORT_ROW_FIELDS = frozenset({"kind", "data"})
_EXPORT_SUMMARY_FIELDS = frozenset({"kind", "rows", "snapshot_complete"})


def _provider_export_uint_header(headers: httpx.Headers, name: str) -> int:
    """Read one canonical, nonnegative decimal export header."""
    value = headers.get(name)
    if (
        value is None
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise OpendataClientError("provider model warehouse export has invalid headers")
    try:
        return int(value)
    except ValueError:
        raise OpendataClientError("provider model warehouse export has invalid headers") from None


def _provider_export_parse_utc_timestamp(value: object) -> object | None:
    """Parse an ISO timestamp with an explicit UTC offset."""
    import re
    from datetime import datetime, timedelta

    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)",
            value,
        )
        is None
    ):
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except (OverflowError, ValueError):
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        return None
    return timestamp


def _decode_provider_export_line(line: bytes) -> dict[str, Any]:
    """Decode a strict JSON object line with duplicate and depth checks."""

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        text = line.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError):
        raise ValueError("invalid export JSON line") from None
    if not isinstance(value, dict) or not _query_has_only_json_values(value):
        raise ValueError("invalid export JSON values")
    return value
