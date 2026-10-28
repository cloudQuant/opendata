"""Source-native Eastmoney A-share daily responses and local evidence references.

This module preserves the response's kline text fields without numeric conversion.
It identifies the configured Eastmoney endpoint separately from the AkShare adapter,
and labels source identity, authorization, and field semantics as unverified.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal, NoReturn

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from opendata.data.request_summary import request_summary_sha256

CONTRACT_ID = "cn_equity.daily.eastmoney_push2his.native.v1"
ADAPTER_ID = "akshare"
UNDERLYING_PRODUCT_ID = "eastmoney.push2his.stock.kline"
ENDPOINT_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
OPERATION_ID = "stock-daily-wire"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_KLINE_ROWS = 10_000
MAX_REQUEST_WINDOW_DAYS = 3_660
KLINE_FIELD_IDS = (
    "f51",
    "f52",
    "f53",
    "f54",
    "f55",
    "f56",
    "f57",
    "f58",
    "f59",
    "f60",
    "f61",
)
KLINE_FIELD_NAMES = (
    "trade_date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "amplitude",
    "change_percent",
    "change",
    "turnover_rate",
)
_FIELD_INDEX = dict(zip(KLINE_FIELD_NAMES, range(len(KLINE_FIELD_NAMES)), strict=True))
_SYMBOL_PATTERN = re.compile(r"(?:(SH|SZ|BJ)([0-9]{6})|([0-9]{6})(?:\.(SH|SZ|BJ))?)", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8})")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UTC = timezone.utc


class StockDailyNativeRequest(BaseModel):
    """One explicit, inclusive SH/SZ daily-history request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    symbol: StrictStr
    exchange: Literal["SH", "SZ"]
    start_date: date
    end_date: date
    request_source: Literal["akshare", "auto"] = "akshare"

    @model_validator(mode="before")
    @classmethod
    def normalize_instrument(cls, value: object) -> object:
        """Canonicalize the code and bind it to the supported exchange route."""
        if not isinstance(value, dict):
            raise ValueError("stock-daily request must be an object")
        payload = dict(value)
        code, routed_exchange = _canonical_instrument(payload.get("symbol"))
        declared_exchange = payload.get("exchange")
        if declared_exchange is not None:
            if not isinstance(declared_exchange, str):
                raise ValueError("exchange must be SH or SZ")
            declared_exchange = declared_exchange.strip().upper()
            if declared_exchange not in {"SH", "SZ"}:
                raise ValueError("only SH and SZ routes are supported")
            if declared_exchange != routed_exchange:
                raise ValueError("symbol and declared exchange do not match")
        payload["symbol"] = code
        payload["exchange"] = routed_exchange
        return payload

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_business_date(cls, value: object) -> object:
        """Accept only a date or an exact ISO/compact business-date string."""
        if isinstance(value, datetime):
            raise ValueError("business dates must not include a time")
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("business dates must use YYYY-MM-DD or YYYYMMDD")
        candidate = value.strip()
        if _DATE_PATTERN.fullmatch(candidate) is None:
            raise ValueError("business dates must use YYYY-MM-DD or YYYYMMDD")
        try:
            if len(candidate) == 8:
                parsed = datetime.strptime(candidate, "%Y%m%d").date()
            else:
                parsed = date.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("business date is invalid") from exc
        if parsed.isoformat() != candidate and len(candidate) == 10:
            raise ValueError("business date must be canonical ISO format")
        return parsed

    @model_validator(mode="after")
    def validate_window(self) -> StockDailyNativeRequest:
        """Require ordered explicit dates within the current bounded wire window."""
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        if (self.end_date - self.start_date).days + 1 > MAX_REQUEST_WINDOW_DAYS:
            raise ValueError("request window exceeds the configured daily wire bound")
        return self

    @property
    def market_id(self) -> int:
        """Return the market ID configured by this repository's local route."""
        return 1 if self.exchange == "SH" else 0

    def request_parameters(self) -> dict[str, str]:
        """Build the fixed local Eastmoney request parameters for this scope."""
        return {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": ",".join(KLINE_FIELD_IDS),
            "klt": "101",
            "fqt": "0",
            "secid": f"{self.market_id}.{self.symbol}",
            "beg": self.start_date.strftime("%Y%m%d"),
            "end": self.end_date.strftime("%Y%m%d"),
        }


class StockDailyNativeRow(BaseModel):
    """A response row with its original line and eleven untouched text tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    row_index: StrictInt = Field(ge=1)
    trade_date: date
    source_line: StrictStr
    raw_tokens: tuple[StrictStr, ...] = Field(min_length=11, max_length=11)

    @model_validator(mode="after")
    def validate_token_line(self) -> StockDailyNativeRow:
        """Ensure the row tokens are exactly those split from the retained line."""
        if tuple(self.source_line.split(",")) != self.raw_tokens:
            raise ValueError("source line and raw tokens differ")
        return self

    def token(self, field_name: str) -> str:
        """Return one source field's original text token without interpretation."""
        try:
            index = _FIELD_INDEX[field_name]
        except KeyError as exc:
            raise ValueError("field_name is not in the fixed Eastmoney daily row") from exc
        return self.raw_tokens[index]

    def tokens_by_name(self) -> dict[str, str]:
        """Return a new mapping of source field names to untouched raw tokens."""
        return dict(zip(KLINE_FIELD_NAMES, self.raw_tokens, strict=True))


class StockDailyNativeEvidence(BaseModel):
    """Raw artifact reference metadata without source authenticity claims."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capture_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    provider_id: StrictStr
    dataset_id: StrictStr
    operation_id: StrictStr
    request_summary_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    request_summary_provenance: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    http_status: StrictInt = Field(ge=100, le=599)
    http_status_provenance: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    response_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    response_size_bytes: StrictInt = Field(ge=0, le=MAX_RESPONSE_BYTES)
    manifest_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    captured_at_utc: datetime
    retention_class: Literal["unknown", "ephemeral", "restricted", "durable_candidate"]
    source_history_limit: Literal[
        "unknown",
        "provider_retention_window",
        "snapshot_only",
        "partial_history",
        "none_documented",
    ]
    source_identity_status: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    retention_status: Literal["caller_declared_unverified"] = "caller_declared_unverified"

    @field_validator("captured_at_utc")
    @classmethod
    def require_utc_capture_time(cls, value: datetime) -> datetime:
        """Require the evidence manifest's capture time to be aware UTC."""
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("captured_at_utc must be timezone-aware UTC")
        return value.astimezone(_UTC)


class StockDailyNativeEnvelope(BaseModel):
    """A bounded Eastmoney-shaped response with conservative source semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: Literal["cn_equity.daily.eastmoney_push2his.native.v1"] = CONTRACT_ID
    adapter_id: Literal["akshare"] = ADAPTER_ID
    underlying_product_id: Literal["eastmoney.push2his.stock.kline"] = UNDERLYING_PRODUCT_ID
    endpoint_url: Literal["https://push2his.eastmoney.com/api/qt/stock/kline/get"] = ENDPOINT_URL
    request: StockDailyNativeRequest
    response_symbol: StrictStr
    response_market_id: StrictInt
    rows: tuple[StockDailyNativeRow, ...]
    observed_start_date: date | None
    observed_end_date: date | None
    duplicate_trade_dates: tuple[date, ...]
    observed_at_utc: datetime | None
    observed_at_status: Literal["unavailable", "caller_declared_unverified"]
    response_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    response_size_bytes: StrictInt = Field(ge=0, le=MAX_RESPONSE_BYTES)
    evidence: StockDailyNativeEvidence | None = None
    requested_adjustment_parameter: Literal["fqt=0"] = "fqt=0"
    adjustment_semantics: Literal["unverified"] = "unverified"
    price_unit_semantics: Literal["unknown"] = "unknown"
    volume_unit_semantics: Literal["unknown"] = "unknown"
    amount_unit_semantics: Literal["unknown"] = "unknown"
    source_identity_status: Literal["unverified"] = "unverified"
    source_authorization_status: Literal["unknown"] = "unknown"
    history_coverage_status: Literal["unknown"] = "unknown"
    missing_date_interpretation: Literal["unknown"] = "unknown"

    @field_validator("observed_at_utc", mode="before")
    @classmethod
    def require_utc_observation_time(cls, value: object) -> object:
        """Accept only a caller-supplied timezone-aware UTC retrieval timestamp."""
        if value is None:
            return None
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("observed_at_utc must be a timezone-aware UTC datetime")
        return value.astimezone(_UTC)

    @model_validator(mode="after")
    def validate_response_contract(self) -> StockDailyNativeEnvelope:
        """Check parsed identity, observed bounds, evidence links, and time ordering."""
        if self.response_symbol != self.request.symbol:
            raise ValueError("response symbol does not match the explicit request")
        if self.response_market_id != self.request.market_id:
            raise ValueError("response market ID does not match the configured route")
        dates = tuple(row.trade_date for row in self.rows)
        expected_start = min(dates) if dates else None
        expected_end = max(dates) if dates else None
        if self.observed_start_date != expected_start or self.observed_end_date != expected_end:
            raise ValueError("observed bounds do not match the retained rows")
        expected_duplicates = _duplicate_dates(dates)
        if self.duplicate_trade_dates != expected_duplicates:
            raise ValueError("duplicate-date summary does not match the retained rows")
        expected_observed_status = (
            "caller_declared_unverified" if self.observed_at_utc is not None else "unavailable"
        )
        if self.observed_at_status != expected_observed_status:
            raise ValueError("observation timestamp status does not match its value")
        if self.evidence is not None:
            if (
                self.evidence.provider_id != ADAPTER_ID
                or self.evidence.operation_id != OPERATION_ID
                or self.evidence.http_status != 200
            ):
                raise ValueError(
                    "raw evidence identity or HTTP status does not match this contract"
                )
            if self.evidence.request_summary_sha256 != _request_summary_sha256(self.request):
                raise ValueError("raw evidence request summary does not match the request")
            if (
                self.evidence.response_sha256 != self.response_sha256
                or self.evidence.response_size_bytes != self.response_size_bytes
            ):
                raise ValueError("raw evidence response hash or size does not match the body")
            if self.observed_at_utc is not None and (
                self.evidence.captured_at_utc < self.observed_at_utc
            ):
                raise ValueError("capture timestamp precedes the declared observation time")
        if any(
            not self.request.start_date <= trade_date <= self.request.end_date
            for trade_date in dates
        ):
            raise ValueError("response contains a business date outside the request window")
        return self


def parse_eastmoney_stock_daily_response(
    body: bytes,
    *,
    request: StockDailyNativeRequest,
    observed_at_utc: datetime | None = None,
) -> StockDailyNativeEnvelope:
    """Parse a caller-provided body while preserving every original field token.

    The body hash is computed locally. Without a verified artifact reference, the
    result does not claim that the bytes came from Eastmoney or were persisted.
    Any supplied observation timestamp is labelled caller-declared and unverified.

    Args:
        body: Bounded response bytes supplied by the caller.
        request: Explicit SH/SZ symbol and inclusive date window.
        observed_at_utc: Optional caller-declared UTC response-retrieval time.

    Returns:
        A source-native envelope with unchanged kline text tokens.

    Raises:
        ValueError: If the body, wire identity, field count, or date range is invalid.
    """
    if type(body) is not bytes:
        raise ValueError("Eastmoney response body must be bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Eastmoney response exceeds the local raw-body size bound")
    if not isinstance(request, StockDailyNativeRequest):
        raise TypeError("request must be a validated StockDailyNativeRequest")
    observed_at = _normalize_utc_timestamp(observed_at_utc, "observed_at_utc")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("Eastmoney response is not strict UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Eastmoney response must be a JSON object")
    if type(payload.get("rc")) is not int or payload["rc"] != 0:
        raise ValueError("Eastmoney response has a missing or non-success rc")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Eastmoney response is missing its data object")
    response_symbol = data.get("code")
    if not isinstance(response_symbol, str) or response_symbol != request.symbol:
        raise ValueError("Eastmoney response symbol does not match the request")
    response_market_id = data.get("market")
    if type(response_market_id) is not int or response_market_id != request.market_id:
        raise ValueError("Eastmoney response market does not match the configured route")
    klines = data.get("klines")
    if not isinstance(klines, list):
        raise ValueError("Eastmoney response is missing its klines list")
    if len(klines) > MAX_KLINE_ROWS:
        raise ValueError("Eastmoney response exceeds the local row-count bound")

    rows: list[StockDailyNativeRow] = []
    for row_index, source_line in enumerate(klines, start=1):
        if not isinstance(source_line, str):
            raise ValueError(f"Eastmoney row {row_index} must be a text line")
        raw_tokens = tuple(source_line.split(","))
        if len(raw_tokens) != len(KLINE_FIELD_NAMES):
            raise ValueError(f"Eastmoney row {row_index} must contain eleven fields")
        trade_date = _parse_trade_date(raw_tokens[0], row_index=row_index)
        if not request.start_date <= trade_date <= request.end_date:
            raise ValueError(f"Eastmoney row {row_index} is outside the requested date window")
        rows.append(
            StockDailyNativeRow(
                row_index=row_index,
                trade_date=trade_date,
                source_line=source_line,
                raw_tokens=raw_tokens,
            )
        )

    dates = tuple(row.trade_date for row in rows)
    return StockDailyNativeEnvelope(
        request=request,
        response_symbol=response_symbol,
        response_market_id=response_market_id,
        rows=tuple(rows),
        observed_start_date=min(dates) if dates else None,
        observed_end_date=max(dates) if dates else None,
        duplicate_trade_dates=_duplicate_dates(dates),
        observed_at_utc=observed_at,
        observed_at_status=(
            "caller_declared_unverified" if observed_at is not None else "unavailable"
        ),
        response_sha256=hashlib.sha256(body).hexdigest(),
        response_size_bytes=len(body),
    )


def parse_verified_stock_daily_capture(
    capture: object,
    *,
    artifact_writer: object,
    request: StockDailyNativeRequest,
    observed_at_utc: datetime,
) -> StockDailyNativeEnvelope:
    """Read a captured response through the existing hash-verifying stock reader.

    Local body and manifest integrity are checked against the content-addressed
    files. The manifest's provider, dataset, operation, retention, and history
    values are caller declarations; these checks do not authenticate Eastmoney,
    confirm source semantics, or grant permission to collect or retain data.

    Args:
        capture: Existing ``CapturedRawResponse`` returned by the evidence writer.
        artifact_writer: The writer whose private artifact root owns the capture.
        request: Explicit request used to recompute the expected request summary.
        observed_at_utc: Caller-declared UTC response-retrieval time.

    Returns:
        A parsed envelope linked to a locally verified body and manifest.

    Raises:
        TypeError: If the capture or writer has an unsupported type.
        ValueError: If declared provenance, request scope, time, or artifact fails.
    """
    from opendata.pipeline.raw_evidence import (
        CapturedRawResponse,
        RawResponseArtifactWriter,
        RawResponseManifest,
        RawResponseSource,
    )
    from opendata.pipeline.stock_daily_job import _verified_capture

    if not isinstance(capture, CapturedRawResponse):
        raise TypeError("capture must be a CapturedRawResponse")
    if not isinstance(artifact_writer, RawResponseArtifactWriter):
        raise TypeError("artifact_writer must be a RawResponseArtifactWriter")
    if not isinstance(request, StockDailyNativeRequest):
        raise TypeError("request must be a validated StockDailyNativeRequest")
    if not isinstance(capture.manifest, RawResponseManifest):
        raise ValueError("capture manifest has an unsupported type")
    source = capture.manifest.source
    if not isinstance(source, RawResponseSource):
        raise ValueError("capture source identity has an unsupported type")
    if source.provider_id != ADAPTER_ID or source.operation_id != OPERATION_ID:
        raise ValueError(
            "capture source identity is not the declared AkShare stock-daily operation"
        )
    expected_request_hash = _request_summary_sha256(request)
    body, manifest, manifest_sha256 = _verified_capture(
        capture,
        artifact_writer=artifact_writer,
        expected_source=source,
        expected_request_summary_sha256=expected_request_hash,
    )
    envelope = parse_eastmoney_stock_daily_response(
        body,
        request=request,
        observed_at_utc=observed_at_utc,
    )
    evidence = StockDailyNativeEvidence(
        capture_id=manifest.capture_id,
        provider_id=manifest.source.provider_id,
        dataset_id=manifest.source.dataset_id,
        operation_id=manifest.source.operation_id,
        request_summary_sha256=manifest.request_summary_sha256,
        http_status=manifest.http_status,
        response_sha256=manifest.response_sha256,
        response_size_bytes=manifest.response_size_bytes,
        manifest_sha256=manifest_sha256,
        captured_at_utc=manifest.captured_at_utc,
        retention_class=manifest.retention_class,
        source_history_limit=manifest.source_history_limit,
    )
    return StockDailyNativeEnvelope(
        contract_id=envelope.contract_id,
        adapter_id=envelope.adapter_id,
        underlying_product_id=envelope.underlying_product_id,
        endpoint_url=envelope.endpoint_url,
        request=envelope.request,
        response_symbol=envelope.response_symbol,
        response_market_id=envelope.response_market_id,
        rows=envelope.rows,
        observed_start_date=envelope.observed_start_date,
        observed_end_date=envelope.observed_end_date,
        duplicate_trade_dates=envelope.duplicate_trade_dates,
        observed_at_utc=envelope.observed_at_utc,
        observed_at_status=envelope.observed_at_status,
        response_sha256=envelope.response_sha256,
        response_size_bytes=envelope.response_size_bytes,
        evidence=evidence,
    )


def _canonical_instrument(value: object) -> tuple[str, Literal["SH", "SZ"]]:
    """Return a supported canonical code and its code-prefix route."""
    if not isinstance(value, str):
        raise ValueError("symbol must be a six-digit SH/SZ A-share code")
    candidate = value.strip()
    match = _SYMBOL_PATTERN.fullmatch(candidate)
    if match is None:
        raise ValueError("symbol must be a six-digit code with an optional SH/SZ suffix")
    prefixed_exchange, prefixed_code, suffixed_code, suffixed_exchange = match.groups()
    code = (prefixed_code or suffixed_code or "").upper()
    declared_exchange = (prefixed_exchange or suffixed_exchange or "").upper()
    if declared_exchange == "BJ" or code.startswith(("4", "8", "920")):
        raise ValueError("BSE stock-daily routing is unsupported")
    if code.startswith("6"):
        expected_exchange: Literal["SH", "SZ"] = "SH"
    elif code.startswith(("0", "3")):
        expected_exchange = "SZ"
    else:
        raise ValueError("symbol does not match the configured SH/SZ A-share code routes")
    if declared_exchange and declared_exchange != expected_exchange:
        raise ValueError("symbol suffix does not match the configured code-prefix route")
    return code, expected_exchange


def _parse_trade_date(value: str, *, row_index: int) -> date:
    """Parse the source date token while retaining the original token separately."""
    if _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Eastmoney row {row_index} has an invalid trade-date token")
    try:
        parsed = (
            datetime.strptime(value, "%Y%m%d").date()
            if len(value) == 8
            else date.fromisoformat(value)
        )
    except ValueError as exc:
        raise ValueError(f"Eastmoney row {row_index} has an invalid trade-date token") from exc
    if len(value) == 10 and parsed.isoformat() != value:
        raise ValueError(f"Eastmoney row {row_index} has a noncanonical trade-date token")
    return parsed


def _normalize_utc_timestamp(value: datetime | None, label: str) -> datetime | None:
    """Validate and normalize one optional caller-declared UTC timestamp."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a timezone-aware UTC datetime")
    try:
        return value.astimezone(_UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is outside the supported UTC range") from exc


def _request_summary_sha256(request: StockDailyNativeRequest) -> str:
    """Recompute the same allowlisted query summary used by the stock-daily job."""
    return request_summary_sha256(
        {
            "adjust": "",
            "end_date": request.end_date,
            "market": "cn",
            "source": request.request_source,
            "start_date": request.start_date,
            "symbol": request.symbol,
        },
        allowed_fields=("adjust", "end_date", "market", "source", "start_date", "symbol"),
    )


def _duplicate_dates(values: tuple[date, ...]) -> tuple[date, ...]:
    """Return duplicate business dates in stable ascending order without dropping rows."""
    seen: set[date] = set()
    duplicates: set[date] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Decode one JSON object while refusing ambiguous duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    """Reject JavaScript-style non-finite numeric constants in the response."""
    raise ValueError(f"JSON contains unsupported constant {value}")


__all__ = [
    "ADAPTER_ID",
    "CONTRACT_ID",
    "ENDPOINT_URL",
    "KLINE_FIELD_IDS",
    "KLINE_FIELD_NAMES",
    "MAX_KLINE_ROWS",
    "MAX_REQUEST_WINDOW_DAYS",
    "MAX_RESPONSE_BYTES",
    "OPERATION_ID",
    "StockDailyNativeEnvelope",
    "StockDailyNativeEvidence",
    "StockDailyNativeRequest",
    "StockDailyNativeRow",
    "UNDERLYING_PRODUCT_ID",
    "parse_eastmoney_stock_daily_response",
    "parse_verified_stock_daily_capture",
]
