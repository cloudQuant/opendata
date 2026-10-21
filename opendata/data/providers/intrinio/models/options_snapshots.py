"""Intrinio options snapshots: metadata lookup, gzip CSV download, OCC contract parsing.

The documented route is two requests: ``GET /options/snapshots`` (an authenticated JSON envelope
whose ``snapshots[].files[].url`` names one bulk file) and a plain GET of that URL, which returns a
gzip CSV of upper-case labelled columns. This is a hand-written adapter rather than a ``ModelSpec``
because the engine declares neither the metadata preflight nor the columnar CSV shape; the governed
client still carries the credential and every refusal owns a stable code.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.http_client import get_shared_http_client
from opendata.data.models.intrinio_options_snapshots import IntrinioOptionsSnapshots
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.intrinio._source import SOURCE

BASE_URL = "https://api-v2.intrinio.com"
SNAPSHOTS_PATH = "/options/snapshots"
CREDENTIAL_ENV_VAR = "INTRINIO_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 30.0
MISSING_KEY_CODE = "INTRINIO_OPTIONS_API_KEY_MISSING"
HTTP_ERROR_CODE = "INTRINIO_OPTIONS_HTTP_ERROR"
SHAPE_CODE = "INTRINIO_OPTIONS_SNAPSHOTS_SHAPE_INVALID"
NO_FILE_CODE = "INTRINIO_OPTIONS_SNAPSHOTS_UNAVAILABLE"

_COLUMNS = {
    "CONTRACT ID": "contract",
    "OPEN INTEREST": "open_interest",
    "TRADE PRICE": "last_price",
    "TRADE SIZE": "last_size",
    "TOTAL TRADE VOLUME": "volume",
    "LAST TRADE TIMESTAMP": "last_timestamp",
    "TRADE HIGH PRICE": "high",
    "TRADE LOW PRICE": "low",
    "ASK PRICE": "ask",
    "ASK SIZE": "ask_size",
    "LAST ASK TIMESTAMP": "ask_timestamp",
    "BID PRICE": "bid",
    "BID SIZE": "bid_size",
    "LAST BID TIMESTAMP": "bid_timestamp",
    "TOTAL ASK VOLUME": "total_ask_volume",
    "ASK HIGH PRICE": "ask_high",
    "ASK LOW PRICE": "ask_low",
    "TOTAL BID VOLUME": "total_bid_volume",
    "BID HIGH PRICE": "bid_high",
    "BID LOW PRICE": "bid_low",
}


class IntrinioOptionsSnapshotsQuery(QueryParams):
    """The documented snapshot filters: an optional instant and the traded-only default."""

    source: Literal["auto", "intrinio"] = "auto"
    date: str | None = None
    only_traded: bool = True


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _float(value: object) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ProviderEngineError(SHAPE_CODE) from None


def _int(value: object) -> int | None:
    number = _float(value)
    return None if number is None else int(number)


def _moment(value: object) -> datetime | None:
    number = _float(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ProviderEngineError(SHAPE_CODE) from None


def _contract_parts(contract: str) -> tuple[str, date_type, Literal["call", "put"], float]:
    """Split ``UNDERLYING_YYMMDDC#########`` into the four published dimensions."""
    parts = contract.split("_")
    tail = parts[-1]
    if len(parts) < 2 or len(tail) < 8 or tail[6] not in ("C", "P") or not tail[7:].isdigit():
        raise ProviderEngineError(SHAPE_CODE)
    try:
        expiration = datetime.strptime(tail[:6], "%y%m%d").date()
    except ValueError:
        raise ProviderEngineError(SHAPE_CODE) from None
    underlying = parts[0]
    if not underlying:
        raise ProviderEngineError(SHAPE_CODE)
    option_type: Literal["call", "put"] = "call" if tail[6] == "C" else "put"
    return underlying, expiration, option_type, int(tail[7:]) / 1000.0


class IntrinioOptionsSnapshotsFetcher(Fetcher[IntrinioOptionsSnapshotsQuery, list[dict[str, Any]]]):
    """Fetch the bulk snapshot file Intrinio's metadata envelope points at."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "OptionsSnapshots"
    query_model: ClassVar[type[IntrinioOptionsSnapshotsQuery]] = IntrinioOptionsSnapshotsQuery
    row_model: ClassVar[type[IntrinioOptionsSnapshots]] = IntrinioOptionsSnapshots
    capability: ClassVar[Capability] = Capability(
        asset_class="derivative",
        domain="intrinio_options_snapshots",
        period="snapshot",
        market="us",
        source="intrinio",
        verified=False,
        notes=(
            "Bulk gzip CSV named by an authenticated metadata envelope; offline acceptance uses "
            "a two-response fixture."
        ),
    )

    def transform_query(self, **kwargs: object) -> IntrinioOptionsSnapshotsQuery:
        """Accept only the documented filters."""
        return IntrinioOptionsSnapshotsQuery.model_validate(kwargs)

    def _credential(self) -> str:
        key = os.environ.get(CREDENTIAL_ENV_VAR, "").strip()
        if not key:
            raise ProviderEngineError(MISSING_KEY_CODE)
        return key

    def _get(self, url: str, params: dict[str, str], ctx: FetchContext) -> Any:  # noqa: ANN401
        timeout = min(float(ctx.timeout or DEFAULT_TIMEOUT_SECONDS), DEFAULT_TIMEOUT_SECONDS)
        response = get_shared_http_client().get(
            url,
            params=params or None,
            timeout=timeout,
            source=SOURCE,
            cache_context={"canonical_model": self.canonical_model},
        )
        if not 200 <= int(response.status_code) < 300:
            raise ProviderEngineError(HTTP_ERROR_CODE, status=int(response.status_code), url=url)
        return response

    def extract_data(
        self,
        params: IntrinioOptionsSnapshotsQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read the metadata envelope, then decode the gzip (or plain) CSV it names."""
        query = {"api_key": self._credential()}
        if params.date:
            query["at_datetime"] = params.date
        envelope = self._get(f"{BASE_URL}{SNAPSHOTS_PATH}", query, ctx)
        try:
            payload: object = envelope.json()
        except Exception:
            raise ProviderEngineError(SHAPE_CODE) from None
        if not isinstance(payload, dict):
            raise ProviderEngineError(SHAPE_CODE)
        snapshots = payload.get("snapshots")
        if not isinstance(snapshots, list):
            raise ProviderEngineError(SHAPE_CODE)
        urls: list[str] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            files = snapshot.get("files")
            if not isinstance(files, list):
                continue
            urls.extend(
                entry["url"]
                for entry in files
                if isinstance(entry, dict) and isinstance(entry.get("url"), str)
            )
        if not urls:
            raise ProviderEngineError(NO_FILE_CODE)
        download = self._get(urls[0], {}, ctx)
        raw = download.content
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                raise ProviderEngineError(SHAPE_CODE) from None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderEngineError(SHAPE_CODE) from None
        reader = csv.DictReader(io.StringIO(text))
        header = tuple(reader.fieldnames or ())
        missing = [label for label in _COLUMNS if label not in header]
        if missing:
            raise ProviderEngineError(SHAPE_CODE)
        return [dict(record) for record in reader]

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: IntrinioOptionsSnapshotsQuery,
    ) -> FetchResult:
        """Map the CSV columns onto the contract, dropping untraded rows when asked."""
        rows: list[IntrinioOptionsSnapshots] = []
        for record in raw:
            contract = _text(record.get("CONTRACT ID"))
            if not contract:
                raise ProviderEngineError(SHAPE_CODE)
            underlying, expiration, option_type, strike = _contract_parts(contract)
            last_timestamp = _moment(record.get("LAST TRADE TIMESTAMP"))
            bid_timestamp = _moment(record.get("LAST BID TIMESTAMP"))
            ask_timestamp = _moment(record.get("LAST ASK TIMESTAMP"))
            if params.only_traded and last_timestamp is None:
                continue
            reference = last_timestamp or bid_timestamp or ask_timestamp
            dte = None if reference is None else (expiration - reference.date()).days
            mapped = {
                "underlying_symbol": underlying,
                "contract_symbol": contract.replace("_", ""),
                "expiration": expiration,
                "dte": dte,
                "strike": strike,
                "option_type": option_type,
                "volume": _int(record.get("TOTAL TRADE VOLUME")),
                "open_interest": _int(record.get("OPEN INTEREST")),
                "last_price": _float(record.get("TRADE PRICE")),
                "last_size": _int(record.get("TRADE SIZE")),
                "last_timestamp": last_timestamp,
                "bid": _float(record.get("BID PRICE")),
                "bid_size": _int(record.get("BID SIZE")),
                "bid_timestamp": bid_timestamp,
                "ask": _float(record.get("ASK PRICE")),
                "ask_size": _int(record.get("ASK SIZE")),
                "ask_timestamp": ask_timestamp,
                "total_bid_volume": _int(record.get("TOTAL BID VOLUME")),
                "bid_high": _float(record.get("BID HIGH PRICE")),
                "bid_low": _float(record.get("BID LOW PRICE")),
                "total_ask_volume": _int(record.get("TOTAL ASK VOLUME")),
                "ask_high": _float(record.get("ASK HIGH PRICE")),
                "ask_low": _float(record.get("ASK LOW PRICE")),
            }
            try:
                rows.append(IntrinioOptionsSnapshots.model_validate(mapped, strict=True))
            except ValidationError:
                raise ProviderEngineError(SHAPE_CODE) from None
        if not rows:
            raise ProviderEngineError(NO_FILE_CODE)
        return tuple(rows)
