"""Capture one manually authorized Fuyao A-share daily-bar sample.

This script is an evidence recorder only. It does not register a provider,
start a scheduler, publish an API, or write to a production database. Importing
the module never reads credentials or performs I/O.

The one-shot request is fixed to ``600519.SH`` over 2026-10-19 through
2026-10-23, with Fuyao's ``adjust=none`` series. A private authorization file
and a new private evidence directory are required. The 64 KiB response limit
bounds application-level bytes accumulated by ``FuyaoHttpClient``; it is not a
wire-byte or process-RSS hard limit because HTTPX may buffer an individual
transport chunk before the client checks it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import quote_from_bytes

from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.endpoints import (
    PRICES_ENDPOINT,
    build_prices_request,
    millis_to_trading_date,
    normalize_bars,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.envelope import parse_envelope
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import (
    FuyaoHttpClient,
    FuyaoHttpResponse,
)
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetError,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    RequestRedirectError,
    request_execution_scope,
)

if TYPE_CHECKING:
    from typing import Any

    from opendata.data.models.market import Bar
    from opendata.data.raw_response_cache import RawResponseCache

PILOT_SYMBOL = "600519.SH"
PILOT_START_DATE = date(2026, 10, 19)
PILOT_END_DATE = date(2026, 10, 23)
PILOT_REQUEST_END_EXCLUSIVE = date(2026, 10, 24)
PILOT_ADJUSTMENT_BASIS = "unadjusted"
PILOT_PURPOSE = "fuyao_stock_daily_pilot"
PILOT_REQUEST_MODEL = "stock_daily"
PILOT_MAX_REQUESTS = 1
PILOT_MAX_RESPONSE_BYTES = 64 * 1024
PILOT_TIMEOUT_SECONDS = 12.0
PILOT_AUTHORIZATION_MAX_AGE = timedelta(minutes=15)
PILOT_HOST = "fuyao.aicubes.cn"
EVIDENCE_SCHEMA_VERSION = 1
_BAR_SOURCE_FIELDS = (
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)

_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HTTP_ERROR_CODE_PATTERN = re.compile(r"^FUYAO_HTTP_([1-5][0-9]{2})$")
_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "purpose",
        "symbol",
        "start_date",
        "end_date",
        "adjustment_basis",
        "max_requests",
        "max_response_bytes",
        "issued_at_utc",
        "expires_at_utc",
        "nonce",
    }
)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_REQUEST_PARAMS = build_prices_request(
    symbol=PILOT_SYMBOL,
    start=PILOT_START_DATE,
    end=PILOT_REQUEST_END_EXCLUSIVE,
    adjust=PILOT_ADJUSTMENT_BASIS,
)


class PilotError(RuntimeError):
    """A fixed, sanitized error code for a rejected pilot operation."""

    def __init__(self, code: str) -> None:
        """Create an error without preserving request or credential values."""
        super().__init__(code)
        self.code = code


class _PilotClient(Protocol):
    """The narrow client surface used by this pilot and its offline tests."""

    def get(self, endpoint: str, *, params: Mapping[str, object]) -> FuyaoHttpResponse:
        """Issue one already-authorized GET and return its bounded response."""

    def close(self) -> None:
        """Close the client resources owned by one pilot invocation."""


class _NoResponseCache:
    """Explicit no-op cache capability that prevents global cache discovery."""

    def get(
        self,
        *,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Always miss without reading or creating cache files."""
        return None

    def put(
        self,
        *,
        method: str,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        status_code: int,
        content: bytes,
        encoding: str | None = None,
    ) -> bool:
        """Decline every cache write."""
        return False

    def invalidate(
        self,
        *,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Do nothing because this cache never stores entries."""


def _no_response_cache() -> RawResponseCache:
    """Expose the no-op implementation through the client's concrete cache type."""
    return cast("RawResponseCache", _NoResponseCache())


@dataclass(frozen=True, slots=True)
class _Authorization:
    """Validated short-lived admission fields needed by the request path."""

    nonce: str
    digest: str
    issued_at: datetime
    expires_at: datetime


def run_pilot(
    authorization_file: Path,
    evidence_dir: Path,
    *,
    credentials: FuyaoCredentials | None = None,
    client_factory: Callable[[FuyaoCredentials], _PilotClient] | None = None,
) -> dict[str, object]:
    """Capture and validate one bounded response under a one-use local grant.

    Args:
        authorization_file: Absolute path to a private, one-use JSON admission file.
        evidence_dir: Absolute path to a new private output directory.
        credentials: Optional credentials seam for offline tests; production CLI
            resolves the project Fuyao configuration lazily after admission.
        client_factory: Optional offline-test factory. Production uses the
            pinned ``FuyaoHttpClient`` configuration below.

    Returns:
        Safe metadata for the CLI. Raw response values are never returned.

    Raises:
        PilotError: Admission, transport, validation, or evidence persistence failed.
    """
    _require_local_development()
    authorization = _consume_authorization(authorization_file)
    directory_fd = _create_evidence_directory(evidence_dir)
    budget = _new_pilot_budget(authorization)
    reservation = _reservation_manifest(authorization)
    active_client: _PilotClient | None = None
    capture_written = False
    try:
        _write_json_exclusive(directory_fd, "reservation.json", reservation)
        try:
            _require_authorization_live(authorization)
            active_credentials = credentials or _load_project_credentials()
            _require_fixed_credentials(active_credentials)
            active_client = _make_client(active_credentials, client_factory)
        except PilotError as exc:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state="failed_before_transport",
                failure_category=exc.code,
            )
            capture_written = True
            raise
        except Exception:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state="failed_before_transport",
                failure_category="local_configuration_rejected",
            )
            capture_written = True
            raise PilotError("pilot_configuration_rejected") from None

        started = time.monotonic()
        try:
            with request_execution_scope(
                source=SOURCE,
                canonical_model=PILOT_REQUEST_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                deadline=started + PILOT_TIMEOUT_SECONDS,
                inherited_scopes=(),
            ):
                response = active_client.get(PRICES_ENDPOINT, params=dict(_REQUEST_PARAMS))
        except FuyaoError as exc:
            _write_request_failure(
                directory_fd,
                budget=budget,
                reservation=reservation,
                code=exc.code,
                category=exc.category,
                request_id_present=exc.request_id is not None,
            )
            capture_written = True
            raise PilotError("pilot_source_request_failed") from None
        except RequestRedirectError:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state="response_rejected",
                failure_category="redirect_rejected",
            )
            capture_written = True
            raise PilotError("pilot_redirect_rejected") from None
        except RequestBudgetError as exc:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state=_state_from_budget(budget),
                failure_category=exc.__class__.__name__,
            )
            capture_written = True
            raise PilotError("pilot_request_not_authorized") from None
        except Exception:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state=_state_from_budget(budget),
                failure_category="transport_or_response_failure",
            )
            capture_written = True
            raise PilotError("pilot_request_failed") from None

        if budget.attempts_used != PILOT_MAX_REQUESTS:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state="failed_before_transport"
                if budget.attempts_used == 0
                else "transport_outcome_unknown",
                failure_category="request_budget_contract_invalid",
            )
            capture_written = True
            raise PilotError("pilot_request_budget_contract_invalid")

        manifest = _capture_response(
            directory_fd,
            reservation,
            response,
            budget,
            credential_key=active_credentials.api_key,
        )
        capture_written = True
        os.fsync(directory_fd)
        return manifest
    except PilotError:
        raise
    except Exception:
        if not capture_written:
            _write_failure_if_missing(
                directory_fd,
                budget=budget,
                reservation=reservation,
                capture_state=_state_from_budget(budget),
                failure_category="local_evidence_or_validation_failure",
            )
        raise PilotError("pilot_evidence_or_validation_failed") from None
    finally:
        if active_client is not None:
            with suppress(Exception):
                active_client.close()
        os.close(directory_fd)


def _capture_response(
    directory_fd: int,
    reservation: Mapping[str, object],
    response: FuyaoHttpResponse,
    budget: RequestBudget,
    *,
    credential_key: str,
) -> dict[str, object]:
    """Validate one bounded response and persist only whitelisted market fields."""
    if type(response) is not FuyaoHttpResponse:
        _write_failure_if_missing(
            directory_fd,
            budget=budget,
            reservation=reservation,
            capture_state="response_rejected",
            failure_category="response_contract_invalid",
        )
        raise PilotError("pilot_response_contract_invalid")
    body = response.raw_content
    if type(body) is not bytes or len(body) > PILOT_MAX_RESPONSE_BYTES:
        _write_failure_if_missing(
            directory_fd,
            budget=budget,
            reservation=reservation,
            capture_state="response_rejected",
            failure_category="response_body_contract_invalid",
        )
        raise PilotError("pilot_response_body_rejected")

    try:
        reflected = _contains_credential_reflection(body, credential_key)
    except Exception:
        _write_suppressed_response_failure(
            directory_fd,
            budget=budget,
            reservation=reservation,
            status_code=response.status_code,
            failure_category="credential_reflection_check_failed",
        )
        raise PilotError("pilot_credential_reflection_check_failed") from None
    if reflected:
        _write_suppressed_response_failure(
            directory_fd,
            budget=budget,
            reservation=reservation,
            status_code=response.status_code,
            failure_category="credential_reflection_detected",
        )
        raise PilotError("pilot_credential_reflection_detected")

    retrieved_at = _format_utc(_utcnow())
    if type(response.status_code) is not int or response.status_code != 200:
        manifest = _response_manifest(
            reservation,
            status_code=response.status_code if type(response.status_code) is int else None,
            retrieved_at=retrieved_at,
            row_count=None,
            observed_dates=(),
            outcome="response_rejected",
            failure_category="expected_http_200",
        )
        _write_json_exclusive(directory_fd, "capture.json", manifest)
        raise PilotError("pilot_http_status_rejected")

    try:
        payload = _strict_json_object(body)
        envelope = parse_envelope(payload)
        if envelope.data.get("thscode") != PILOT_SYMBOL:
            raise PilotError("response_symbol_mismatch")
        source_rows = _extract_source_rows(envelope.items)
        bars = normalize_bars(envelope, symbol=PILOT_SYMBOL)
        observed_dates = _validate_bars(bars)
        if set(source_rows) != {bar.trade_date for bar in bars}:
            raise PilotError("response_source_rows_do_not_match_normalized_data")
    except PilotError as exc:
        manifest = _response_manifest(
            reservation,
            status_code=response.status_code,
            retrieved_at=retrieved_at,
            row_count=None,
            observed_dates=(),
            outcome="response_rejected",
            failure_category=exc.code,
        )
        _write_json_exclusive(directory_fd, "capture.json", manifest)
        raise
    except Exception:
        manifest = _response_manifest(
            reservation,
            status_code=response.status_code,
            retrieved_at=retrieved_at,
            row_count=None,
            observed_dates=(),
            outcome="response_rejected",
            failure_category="wire_or_window_validation_failed",
        )
        _write_json_exclusive(directory_fd, "capture.json", manifest)
        raise PilotError("pilot_response_rejected") from None

    if not observed_dates:
        manifest = _response_manifest(
            reservation,
            status_code=response.status_code,
            retrieved_at=retrieved_at,
            row_count=0,
            observed_dates=(),
            outcome="response_rejected",
            failure_category="empty_sample",
        )
        _write_json_exclusive(directory_fd, "capture.json", manifest)
        raise PilotError("pilot_empty_sample")

    manifest = _response_manifest(
        reservation,
        status_code=response.status_code,
        retrieved_at=retrieved_at,
        row_count=len(observed_dates),
        observed_dates=observed_dates,
        outcome="bounded_sample_captured",
        failure_category=None,
    )
    observations_persisted = False
    try:
        _write_json_exclusive(
            directory_fd,
            "observations.json",
            _observation_document(bars, source_rows),
        )
        observations_persisted = True
        _write_json_exclusive(directory_fd, "capture.json", manifest)
    except Exception:
        capture_state = (
            "validated_sample_unmanifested"
            if observations_persisted
            else "response_observed_not_persisted"
        )
        persistence_failure = _failure_manifest(
            reservation,
            capture_state=capture_state,
            failure_category="local_evidence_persistence_failure",
            budget=budget,
            http_status=response.status_code,
        )
        persistence_failure.update(
            {
                "outcome": "evidence_persistence_failed",
                "source_response_received": True,
                "source_response_received_semantics": (
                    "HTTP response observed locally; not an upstream processing receipt"
                ),
                "source_response_persisted": False,
                "source_response_trust": (
                    "whitelisted_fields_validated"
                    if observations_persisted
                    else "whitelisted_fields_validated_but_observation_write_failed"
                ),
                "response_body_saved": False,
                "sample_validation": "passed_fixed_scope_checks",
                "validated_observations_persisted": observations_persisted,
                "observations_file": "observations.json",
                "observations_artifact_state": (
                    "complete_unpublished_sample"
                    if observations_persisted
                    else "write_failed_may_be_partial"
                ),
                "row_count": len(observed_dates),
                "observed_date_min": min(observed_dates),
                "observed_date_max": max(observed_dates),
                "failure_note": (
                    "The sample is not published by a success manifest. Check the observations "
                    "artifact state before recovery. No raw provider response was persisted."
                ),
            }
        )
        _write_json_exclusive(directory_fd, "capture.json", persistence_failure)
        raise PilotError("pilot_evidence_persistence_failed") from None
    return manifest


def _extract_source_rows(
    items: Sequence[object],
) -> dict[date, dict[str, int | float]]:
    """Select exact source fields and reject coercions before normalization."""
    result: dict[date, dict[str, int | float]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise PilotError("response_bar_not_object")
        timestamp = item.get("date_ms")
        if type(timestamp) is not int:
            raise PilotError("response_date_timestamp_invalid")
        try:
            trade_date = millis_to_trading_date(timestamp)
        except Exception:
            raise PilotError("response_date_timestamp_invalid") from None
        if not PILOT_START_DATE <= trade_date <= PILOT_END_DATE:
            raise PilotError("response_date_outside_window")
        if trade_date in result:
            raise PilotError("response_duplicate_business_date")
        source_values: dict[str, int | float] = {"date_ms": timestamp}
        for field in _BAR_SOURCE_FIELDS:
            value = item.get(field)
            if type(value) is int:
                source_value: int | float = value
            elif type(value) is float and math.isfinite(value):
                source_value = value
            else:
                raise PilotError("response_numeric_value_invalid")
            source_values[field] = source_value
        result[trade_date] = source_values
    return result


def _observation_document(
    bars: Sequence[Bar], source_rows: Mapping[date, Mapping[str, int | float]]
) -> dict[str, object]:
    """Build a private sample from only validated source fields and contract keys."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "source": SOURCE,
        "vendor": "Fuyao (THS) REST API",
        "endpoint_path": PRICES_ENDPOINT,
        "request": {
            "symbol": PILOT_SYMBOL,
            "start_date": PILOT_START_DATE.isoformat(),
            "end_date": PILOT_END_DATE.isoformat(),
            "adjustment_basis": PILOT_ADJUSTMENT_BASIS,
            "provider_adjust_parameter": "none",
        },
        "time_field": "date_ms",
        "time_semantics": "Unix milliseconds at Asia/Shanghai trading-date midnight",
        "field_units": {
            "open_price": "provider-native value; not independently calibrated",
            "high_price": "provider-native value; not independently calibrated",
            "low_price": "provider-native value; not independently calibrated",
            "close_price": "provider-native value; not independently calibrated",
            "volume": "provider contract label: shares; not independently calibrated",
            "turnover": "provider contract label: CNY; not independently calibrated",
        },
        "rows": [
            {
                "symbol": bar.symbol,
                "trade_date": bar.trade_date.isoformat(),
                "adjustment_basis": PILOT_ADJUSTMENT_BASIS,
                "source_fields": dict(source_rows[bar.trade_date]),
            }
            for bar in bars
        ],
    }


def _validate_bars(bars: Sequence[Bar]) -> tuple[str, ...]:
    """Validate fixed-window daily keys and basic finite OHLCV constraints."""
    dates: list[date] = []
    for bar in bars:
        trade_date = bar.trade_date
        if type(trade_date) is not date or not PILOT_START_DATE <= trade_date <= PILOT_END_DATE:
            raise PilotError("response_date_outside_window")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount)
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
            raise PilotError("response_numeric_value_invalid")
        open_price, high, low, close, volume, amount = values
        if min(open_price, high, low, close) <= 0:
            raise PilotError("response_price_not_positive")
        if (
            min(open_price, high, low, close, volume, amount) < 0
            or high < max(open_price, close)
            or low > min(open_price, close)
            or low > high
        ):
            raise PilotError("response_ohlcv_invariant_failed")
        dates.append(trade_date)
    if len(dates) != len(set(dates)):
        raise PilotError("response_duplicate_business_date")
    return tuple(sorted(value.isoformat() for value in dates))


def _contains_credential_reflection(body: bytes, credential_key: str) -> bool:
    """Detect the current API key in raw and common text encodings before persistence."""
    if type(credential_key) is not str or not credential_key:
        raise PilotError("credential_reflection_check_unavailable")
    secret = credential_key.encode("utf-8")
    percent_encoded = quote_from_bytes(secret, safe="")
    escaped_json = json.dumps(credential_key, ensure_ascii=True)[1:-1].encode("ascii")
    standard_b64 = base64.b64encode(secret)
    urlsafe_b64 = base64.urlsafe_b64encode(secret)
    markers = {
        secret,
        percent_encoded.encode("ascii"),
        re.sub(r"%[0-9A-F]{2}", lambda match: match.group().lower(), percent_encoded).encode(
            "ascii"
        ),
        percent_encoded.replace("%20", "+").encode("ascii"),
        escaped_json,
        html.escape(credential_key, quote=True).encode("utf-8"),
        standard_b64,
        standard_b64.rstrip(b"="),
        urlsafe_b64,
        urlsafe_b64.rstrip(b"="),
        secret.hex().encode("ascii"),
        secret.hex().upper().encode("ascii"),
    }
    return any(marker and marker in body for marker in markers)


def _new_pilot_budget(authorization: _Authorization) -> RequestBudget:
    """Create the exact, short-lived local grant for this one manual request."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    expiry = min(_utcnow() + timedelta(minutes=1), authorization.expires_at)
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=PILOT_REQUEST_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="local-user-admission-sha256:" + authorization.digest,
        task_attempts=PILOT_MAX_REQUESTS,
        source_attempts=PILOT_MAX_REQUESTS,
        allowed_hosts={PILOT_HOST},
        expires_at=expiry,
        profile=profile,
    )
    return RequestBudget(
        task_attempts=PILOT_MAX_REQUESTS,
        source_attempts=PILOT_MAX_REQUESTS,
        grants=(grant,),
        profile=profile,
    )


def _make_client(
    credentials: FuyaoCredentials,
    factory: Callable[[FuyaoCredentials], _PilotClient] | None,
) -> _PilotClient:
    """Build the pinned one-attempt client or an injected offline fake."""
    if factory is not None:
        client = factory(credentials)
        if not callable(getattr(client, "get", None)) or not callable(
            getattr(client, "close", None)
        ):
            raise PilotError("client_contract_invalid")
        return client
    return FuyaoHttpClient(
        credentials=credentials,
        timeout_seconds=PILOT_TIMEOUT_SECONDS,
        max_attempts=PILOT_MAX_REQUESTS,
        max_response_bytes=PILOT_MAX_RESPONSE_BYTES,
        rate_limiter=FuyaoRateLimiter(rate_per_second=1.0, burst=1),
        raw_response_cache=_no_response_cache(),
    )


def _load_project_credentials() -> FuyaoCredentials:
    """Resolve application credentials lazily so importing the pilot is offline."""
    try:
        from opendata.data.providers.ths.models._client import credentials

        resolved = credentials()
    except Exception:
        raise PilotError("provider_credentials_unavailable") from None
    if type(resolved) is not FuyaoCredentials:
        raise PilotError("provider_credentials_invalid")
    return resolved


def _require_fixed_credentials(credentials: FuyaoCredentials) -> None:
    """Reject any noncanonical origin before a request scope can send bytes."""
    if type(credentials) is not FuyaoCredentials:
        raise PilotError("provider_credentials_invalid")
    if credentials.base_url != DEFAULT_FUYAO_API_BASE_URL:
        raise PilotError("provider_origin_not_allowed")


def _reservation_manifest(authorization: _Authorization) -> dict[str, object]:
    """Describe the exact request without storing a full URL or secret value."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "reservation_state": "authorized_scope_recorded_before_transport",
        "scope_recorded_at_utc": _format_utc(_utcnow()),
        "source": SOURCE,
        "vendor": "Fuyao (THS) REST API",
        "endpoint_path": PRICES_ENDPOINT,
        "request": {
            "method": "GET",
            "symbol": PILOT_SYMBOL,
            "start_date": PILOT_START_DATE.isoformat(),
            "end_date": PILOT_END_DATE.isoformat(),
            "adjustment_basis": PILOT_ADJUSTMENT_BASIS,
            "provider_adjust_parameter": "none",
            "max_requests": PILOT_MAX_REQUESTS,
            "transport_send_attempt_ceiling": PILOT_MAX_REQUESTS,
            "max_response_bytes": PILOT_MAX_RESPONSE_BYTES,
            "response_limit_semantics": "application_cumulative_entity_bytes_not_wire_or_rss",
            "timeout_seconds": PILOT_TIMEOUT_SECONDS,
            "timeout_semantics": "cooperative_deadline_not_strict_wall_clock",
        },
        "request_sha256": _request_fingerprint(),
        "authorization_sha256": authorization.digest,
        "local_budget_attempts_reserved": 0,
        "physical_send_count": None,
        "physical_send_count_semantics": "not_observed_by_local_request_budget",
        "response_body_saved": False,
        "source_response_trust": "untrusted_until_selected_fields_are_validated",
        "source_response_persisted": False,
        "completeness_assessment": "not_evaluated",
    }


def _request_fingerprint() -> str:
    """Hash the fixed origin, endpoint path, and query values without credentials."""
    identity = {
        "method": "GET",
        "host": PILOT_HOST,
        "endpoint_path": PRICES_ENDPOINT,
        "params": _REQUEST_PARAMS,
    }
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _response_manifest(
    reservation: Mapping[str, object],
    *,
    status_code: int | None,
    retrieved_at: str,
    row_count: int | None,
    observed_dates: Sequence[str],
    outcome: str,
    failure_category: str | None,
) -> dict[str, object]:
    """Describe one response and the whitelist validation outcome without raw bytes."""
    return {
        **reservation,
        "capture_state": (
            "sample_captured" if outcome == "bounded_sample_captured" else "response_rejected"
        ),
        "outcome": outcome,
        "source_response_received": True,
        "source_response_persisted": False,
        "source_response_trust": (
            "only_whitelisted_fields_validated"
            if outcome == "bounded_sample_captured"
            else "untrusted_or_rejected"
        ),
        "response_body_saved": False,
        "validated_observations_persisted": outcome == "bounded_sample_captured",
        "observations_artifact_state": (
            "complete_published_sample" if outcome == "bounded_sample_captured" else None
        ),
        "observations_file": "observations.json" if outcome == "bounded_sample_captured" else None,
        "sample_validation": (
            "passed_fixed_scope_checks"
            if outcome == "bounded_sample_captured"
            else "not_run_for_http_error"
            if failure_category == "expected_http_200"
            else "rejected"
        ),
        "retrieved_at_utc": retrieved_at,
        "retrieval_time_semantics": "local_completion_time_after_response_return",
        "http_status": status_code,
        "local_budget_attempts_reserved": 1,
        "physical_send_count": None,
        "physical_send_count_semantics": "not_observed_by_local_request_budget",
        "row_count": row_count,
        "observed_date_min": min(observed_dates) if observed_dates else None,
        "observed_date_max": max(observed_dates) if observed_dates else None,
        "failure_category": failure_category,
        "unit_assessment": (
            "The THS Bar contract labels volume as shares and amount as CNY; this sample "
            "does not independently calibrate the provider's native units."
        ),
        "completeness_assessment": "not_established",
        "completeness_note": (
            "No independent trading-calendar, suspension, listing-boundary, source-retention, "
            "or head/interior/tail reconciliation was performed. This single short sample "
            "does not establish the window's completeness or since-inception history."
        ),
    }


def _write_request_failure(
    directory_fd: int,
    *,
    budget: RequestBudget,
    reservation: Mapping[str, object],
    code: str,
    category: str,
    request_id_present: bool,
) -> None:
    """Persist a sanitized failure classification without exception details."""
    status_match = _HTTP_ERROR_CODE_PATTERN.fullmatch(code)
    status = int(status_match.group(1)) if status_match is not None else None
    if status is None and code == "FUYAO_RATE_LIMITED_429":
        status = 429
    if status is not None:
        state = "http_error_response_received"
    elif (
        category
        in {
            "auth",
            "empty",
            "envelope_invalid",
            "not_ready",
            "permission",
            "rate_limited",
            "request",
            "response_too_large",
            "transient",
            "unsupported",
        }
        or request_id_present
    ):
        state = "response_rejected"
    else:
        state = _state_from_budget(budget)
    manifest = _failure_manifest(
        reservation,
        capture_state=state,
        failure_category=category,
        budget=budget,
        http_status=status,
    )
    _write_json_exclusive(directory_fd, "capture.json", manifest)


def _write_suppressed_response_failure(
    directory_fd: int,
    *,
    budget: RequestBudget,
    reservation: Mapping[str, object],
    status_code: object,
    failure_category: str,
) -> None:
    """Record a reflected-secret response without storing its body or digest."""
    safe_status = status_code if type(status_code) is int else None
    manifest = _failure_manifest(
        reservation,
        capture_state="response_rejected",
        failure_category=failure_category,
        budget=budget,
        http_status=safe_status,
    )
    manifest.update(
        {
            "source_response_received": True,
            "source_response_trust": "untrusted_source_bytes",
            "response_body_saved": False,
            "response_body_suppressed": failure_category,
            "sample_validation": "not_run_due_to_credential_reflection_check",
        }
    )
    _write_json_exclusive(directory_fd, "capture.json", manifest)


def _write_failure_if_missing(
    directory_fd: int,
    *,
    budget: RequestBudget,
    reservation: Mapping[str, object],
    capture_state: str,
    failure_category: str,
) -> None:
    """Create one failure manifest unless a more precise result already exists."""
    try:
        os.stat("capture.json", dir_fd=directory_fd, follow_symlinks=False)
        return
    except FileNotFoundError:
        pass
    _write_json_exclusive(
        directory_fd,
        "capture.json",
        _failure_manifest(
            reservation,
            capture_state=capture_state,
            failure_category=failure_category,
            budget=budget,
            http_status=None,
        ),
    )


def _failure_manifest(
    reservation: Mapping[str, object],
    *,
    capture_state: str,
    failure_category: str,
    budget: RequestBudget,
    http_status: int | None,
) -> dict[str, object]:
    """Describe a failed or uncertain attempt without values or exception text."""
    return {
        **reservation,
        "capture_state": capture_state,
        "outcome": (
            "response_rejected"
            if capture_state == "response_rejected"
            else "evidence_persistence_failed"
            if capture_state in {"response_observed_not_persisted", "validated_sample_unmanifested"}
            else "request_failed_or_rejected"
        ),
        "local_budget_attempts_reserved": budget.attempts_used,
        "physical_send_count": None,
        "physical_send_count_semantics": "not_observed_by_local_request_budget",
        "http_status": http_status,
        "response_body_saved": False,
        "source_response_received": capture_state
        in {
            "response_rejected",
            "http_error_response_received",
            "response_observed_not_persisted",
            "validated_sample_unmanifested",
        },
        "source_response_received_semantics": (
            "HTTP response observed locally; not an upstream processing receipt"
            if capture_state in {"response_observed_not_persisted", "validated_sample_unmanifested"}
            else None
        ),
        "source_response_persisted": False,
        "source_response_trust": (
            "whitelisted_fields_validated_but_not_persisted"
            if capture_state in {"response_observed_not_persisted", "validated_sample_unmanifested"}
            else "untrusted_or_invalid"
            if capture_state in {"response_rejected", "http_error_response_received"}
            else "not_available"
        ),
        "failure_category": failure_category,
        "completeness_assessment": "not_evaluated",
        "failure_note": (
            "An attempt reservation is local accounting and does not prove that the source "
            "received or processed the request. Unknown outcomes must not be retried with "
            "this authorization nonce."
        ),
    }


def _state_from_budget(budget: RequestBudget) -> str:
    """Choose a conservative no-send or unknown state from local reservations."""
    return "failed_before_transport" if budget.attempts_used == 0 else "transport_outcome_unknown"


def _strict_json_object(
    raw: bytes,
    *,
    failure_code: str = "response_json_invalid",
) -> dict[str, object]:
    """Decode bounded JSON while rejecting duplicate keys and non-finite constants."""
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PilotError(failure_code) from None
    if type(value) is not dict:
        raise PilotError(failure_code)
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Construct one JSON object only when all keys are unique."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    """Reject JSON extensions such as NaN and Infinity."""
    raise ValueError("non-finite JSON constant: " + value)


def _consume_authorization(path: Path) -> _Authorization:
    """Validate and atomically consume a private nonce before any transport."""
    parent_fd, filename = _open_private_parent(path)
    try:
        descriptor = _open_regular_file(parent_fd, filename)
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size <= 0
                or metadata.st_size > 4096
            ):
                raise PilotError("authorization_file_permissions_invalid")
            raw = _read_bounded_file(descriptor, limit=4096)
        finally:
            os.close(descriptor)
        payload = _strict_json_object(raw, failure_code="authorization_json_invalid")
        authorization = _validate_authorization(payload, raw=raw, now=_utcnow())
        marker_name = (
            ".fuyao-pilot-"
            + hashlib.sha256(authorization.nonce.encode("ascii")).hexdigest()
            + ".consumed"
        )
        marker_fd = _open_exclusive_file(parent_fd, marker_name)
        try:
            _write_all(
                marker_fd,
                _json_bytes(
                    {
                        "schema_version": EVIDENCE_SCHEMA_VERSION,
                        "state": "authorization_consumed_before_transport",
                        "consumed_at_utc": _format_utc(_utcnow()),
                        "authorization_sha256": authorization.digest,
                        "request_sha256": _request_fingerprint(),
                    }
                ),
            )
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
        os.fsync(parent_fd)
        return authorization
    except FileExistsError:
        raise PilotError("authorization_nonce_already_consumed") from None
    except PilotError:
        raise
    except Exception:
        raise PilotError("authorization_file_invalid") from None
    finally:
        os.close(parent_fd)


def _validate_authorization(
    payload: Mapping[str, object],
    *,
    raw: bytes,
    now: datetime,
) -> _Authorization:
    """Require an exact, unexpired local authorization for this one sample."""
    if set(payload) != _AUTHORIZATION_KEYS:
        raise PilotError("authorization_scope_invalid")
    expected_values: dict[str, object] = {
        "schema_version": 1,
        "purpose": PILOT_PURPOSE,
        "symbol": PILOT_SYMBOL,
        "start_date": PILOT_START_DATE.isoformat(),
        "end_date": PILOT_END_DATE.isoformat(),
        "adjustment_basis": PILOT_ADJUSTMENT_BASIS,
        "max_requests": PILOT_MAX_REQUESTS,
        "max_response_bytes": PILOT_MAX_RESPONSE_BYTES,
    }
    for key, expected in expected_values.items():
        if type(payload[key]) is not type(expected) or payload[key] != expected:
            raise PilotError("authorization_scope_invalid")
    nonce = payload["nonce"]
    if type(nonce) is not str or _NONCE_PATTERN.fullmatch(nonce) is None:
        raise PilotError("authorization_nonce_invalid")
    issued_at = _parse_utc_timestamp(payload["issued_at_utc"])
    expires_at = _parse_utc_timestamp(payload["expires_at_utc"])
    if (
        issued_at > now
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > PILOT_AUTHORIZATION_MAX_AGE
    ):
        raise PilotError("authorization_expired_or_not_yet_valid")
    return _Authorization(
        nonce=nonce,
        digest=hashlib.sha256(raw).hexdigest(),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _parse_utc_timestamp(value: object) -> datetime:
    """Parse a canonical UTC timestamp with microseconds and a trailing Z."""
    if type(value) is not str or not value.endswith("Z"):
        raise PilotError("authorization_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PilotError("authorization_timestamp_invalid") from None
    if _format_utc(parsed) != value:
        raise PilotError("authorization_timestamp_invalid")
    return parsed


def _require_local_development() -> None:
    """Require explicit local development mode and refuse test or production."""
    if os.environ.get("APP_ENV") != "development":
        raise PilotError("explicit_development_environment_required")
    if os.environ.get("TESTING", "").strip().casefold() in {"1", "true", "yes", "on"}:
        raise PilotError("pilot_disabled_in_testing_environment")


def _require_authorization_live(authorization: _Authorization) -> None:
    """Refuse to issue the request after local admission expiry."""
    if authorization.expires_at <= _utcnow():
        raise PilotError("authorization_expired_before_transport")


def _format_utc(value: datetime) -> str:
    """Render a stable UTC timestamp with microseconds and Z suffix."""
    rendered = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return rendered.replace("+00:00", "Z")


def _utcnow() -> datetime:
    """Return a timezone-aware current UTC time for authorization and evidence."""
    return datetime.now(timezone.utc)


def _open_path_directory(path: Path) -> int:
    """Open a directory chain without following symlinks; never chmod ancestors."""
    if not hasattr(os, "getuid") or not _O_DIRECTORY or not _O_NOFOLLOW or not _O_NONBLOCK:
        raise PilotError("secure_local_file_primitives_unavailable")
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise PilotError("absolute_private_directory_required")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.anchor,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
        )
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PilotError("private_directory_permissions_required")
        result = descriptor
        descriptor = None
        return result
    except PilotError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise PilotError("private_directory_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_private_parent(path: Path) -> tuple[int, str]:
    """Open the final private authorization parent and return its filename."""
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PilotError("absolute_authorization_file_required")
    return _open_path_directory(path.parent), path.name


def _open_regular_file(directory_fd: int, name: str) -> int:
    """Open a non-symlink regular file beneath an already-open directory."""
    descriptor = os.open(
        name,
        os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC,
        dir_fd=directory_fd,
    )
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise PilotError("authorization_file_not_regular")
    return descriptor


def _read_bounded_file(descriptor: int, *, limit: int) -> bytes:
    """Read only the exact file limit plus one sentinel byte."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > limit:
        raise PilotError("authorization_file_too_large")
    return raw


def _create_evidence_directory(path: Path) -> int:
    """Create and open a new 0700 evidence directory beneath a private parent."""
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PilotError("absolute_evidence_directory_required")
    parent_fd = _open_path_directory(path.parent)
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        directory_fd = os.open(
            path.name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except FileExistsError:
        raise PilotError("evidence_directory_must_be_new") from None
    except OSError:
        raise PilotError("evidence_directory_create_failed") from None
    finally:
        os.close(parent_fd)
    metadata = os.fstat(directory_fd)
    permissions = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != os.getuid() or permissions & 0o077 or permissions & 0o700 != 0o700:
        os.close(directory_fd)
        raise PilotError("evidence_directory_permissions_invalid")
    return directory_fd


def _open_exclusive_file(directory_fd: int, name: str) -> int:
    """Create one private regular output file with no overwrite or symlink use."""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise PilotError("evidence_file_permissions_invalid")
    return descriptor


def _write_bytes_exclusive(directory_fd: int, name: str, content: bytes) -> None:
    """Durably write one exclusive evidence file beneath its open directory."""
    descriptor = _open_exclusive_file(directory_fd, name)
    try:
        created_metadata = os.fstat(descriptor)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        _unlink_if_same_file(directory_fd, name, created_metadata)
        raise
    try:
        os.close(descriptor)
    except Exception:
        _unlink_if_same_file(directory_fd, name, created_metadata)
        raise
    try:
        os.fsync(directory_fd)
    except Exception:
        _unlink_if_same_file(directory_fd, name, created_metadata)
        raise


def _unlink_if_same_file(directory_fd: int, name: str, expected: os.stat_result) -> None:
    """Best-effort inode-checked cleanup, not atomic against same-user races."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Write one deterministic compact JSON evidence file."""
    _write_bytes_exclusive(directory_fd, name, _json_bytes(value))


def _write_all(descriptor: int, content: bytes) -> None:
    """Write all bytes while keeping the exception free of body data."""
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short evidence write")
        remaining = remaining[written:]


def _json_bytes(value: Mapping[str, object]) -> bytes:
    """Serialize deterministic finite JSON as UTF-8."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local CLI and print only a sanitized summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization-file", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        metadata = run_pilot(arguments.authorization_file, arguments.evidence_dir)
    except PilotError as exc:
        print(json.dumps({"outcome": "failed", "code": exc.code}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "outcome": metadata["outcome"],
                "request_sha256": metadata["request_sha256"],
                "row_count": metadata["row_count"],
                "retrieved_at_utc": metadata["retrieved_at_utc"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
