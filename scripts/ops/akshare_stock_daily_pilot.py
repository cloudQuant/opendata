"""Capture one explicitly authorized, local AkShare/Eastmoney sample.

This is a manual evidence recorder, not a provider registration, scheduler,
API route, or production writer. Importing this module never performs I/O.

Run it with an absolute, previously nonexistent evidence directory and a short
private authorization file::

    python -m scripts.ops.akshare_stock_daily_pilot \
        --authorization-file /private/pilot/authorization.json \
        --evidence-dir /private/pilot/evidence-20261028

The authorization file must be owned by the current user, mode 0600, in a
current-user-owned mode-0700 directory with no symlinked path components. It
is operator-created local admission; the script does not authenticate its
issuer, verify a supplier agreement, or change the production source policy.
It is strict JSON with exactly these
keys and values for the fixed pilot: ``schema_version`` (1), ``purpose``
(``akshare_stock_daily_pilot``), ``symbol`` (``600519.SH``), ``start_date``
(``2026-10-12``), ``end_date`` (``2026-10-23``), ``adjustment_basis``
(``unadjusted``), ``max_requests`` (1), ``max_response_bytes`` (65536),
``issued_at_utc`` and ``expires_at_utc`` (canonical UTC timestamps with six
fractional digits and a trailing ``Z``, no more than 15 minutes apart), and
``nonce`` (32 lowercase hexadecimal chars).

The authorization is local operator admission for this exact manual sample;
it is not a vendor contract, a production source policy, or a global replay
ledger. A nonce-derived marker is created with exclusive creation in the
authorization directory before transport. This blocks concurrent or repeated
use of the same nonce in that directory, including after a failed or
interrupted attempt. A different nonce or a copy in another directory is a
new authorization and is outside that replay guarantee. The 15-second overall
timeout is a cooperative soft deadline checked around stream reads. The HTTP
library may perform multiple blocking socket reads while producing one iterator
chunk, so a slow-drip response can outlive this deadline substantially; this
pilot does not enforce a strict wall-clock upper bound. A real capture that
requires one must use a separately designed external hard watchdog, while
preserving the one-attempt reservation on timeout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import requests

from opendata.data.http_client import (
    BoundedHttpResponse,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.stock_daily_wire import (
    EASTMONEY_KLINE_HOST,
    EASTMONEY_KLINE_URL,
    parse_stock_daily_wire,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


PILOT_SYMBOL = "600519.SH"
PILOT_PROVIDER_SYMBOL = "600519"
PILOT_START_DATE = date(2026, 10, 12)
PILOT_END_DATE = date(2026, 10, 23)
PILOT_ADJUSTMENT_BASIS = "unadjusted"
PILOT_START_QUERY_DATE = "20261012"
PILOT_END_QUERY_DATE = "20261023"
PILOT_MAX_REQUESTS = 1
PILOT_MAX_RESPONSE_BYTES = 64 * 1024
PILOT_OVERALL_TIMEOUT_SECONDS = 15.0
PILOT_CONNECT_TIMEOUT_SECONDS = 3.0
PILOT_READ_TIMEOUT_SECONDS = 5.0
PILOT_AUTHORIZATION_MAX_AGE = timedelta(minutes=15)
PILOT_REQUEST_MODEL = "stock_daily"
PILOT_PURPOSE = "akshare_stock_daily_pilot"
EVIDENCE_SCHEMA_VERSION = 1

_KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
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
_REQUEST_PARAMS = MappingProxyType(
    {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": _KLINE_FIELDS,
        "klt": "101",
        "fqt": "0",
        "secid": "1.600519",
        "beg": PILOT_START_QUERY_DATE,
        "end": PILOT_END_QUERY_DATE,
    }
)


class PilotError(RuntimeError):
    """A fixed safe error code for a rejected pilot operation."""

    def __init__(self, code: str) -> None:
        """Initialize an error without carrying request or file contents."""
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _PilotHttpClient:
    """Pair the governed transport with the session this pilot owns."""

    client: GovernedHttpClient
    session: requests.Session

    def get_bounded(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        max_body_bytes: int,
        overall_timeout: float,
        timeout: tuple[float, float],
        source: str,
    ) -> BoundedHttpResponse:
        """Delegate one bounded request to the governed transport client."""
        return self.client.get_bounded(
            url,
            params=params,
            max_body_bytes=max_body_bytes,
            overall_timeout=overall_timeout,
            timeout=timeout,
            source=source,
        )

    def close(self) -> None:
        """Close the session owned exclusively by this pilot invocation."""
        self.session.close()


def run_pilot(authorization_file: Path, evidence_dir: Path) -> dict[str, object]:
    """Run the single fixed pilot request and write private raw evidence.

    The request is admitted only in an explicitly selected development
    environment, with a valid private, short-lived authorization file. The
    request budget and HTTP client each permit one physical attempt. Creating
    the evidence directory and consuming the authorization nonce happen before
    transport, so a failure or interruption cannot be retried with the same
    evidence path and authorization nonce in that authorization directory.
    The nonce marker is not a global replay ledger.

    Args:
        authorization_file: Absolute path to the one-use local authorization JSON.
        evidence_dir: Absolute path to a new private directory for this capture.

    Returns:
        Safe capture metadata suitable for printing; response bytes are never returned.

    Raises:
        PilotError: If admission, transport, response validation, or evidence writing fails.
    """
    _require_local_development()
    authorization = _consume_authorization(authorization_file)
    directory_fd = _create_evidence_directory(evidence_dir)
    reserved_at = _utcnow()
    request_fingerprint = _request_fingerprint()
    reservation = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "state": "attempt_reserved_before_transport",
        "reserved_at_utc": _format_utc(reserved_at),
        "source": SOURCE,
        "endpoint": EASTMONEY_KLINE_URL,
        "request": _request_description(),
        "request_sha256": request_fingerprint,
        "authorization_sha256": authorization.digest,
        "response_body_saved": False,
        "completeness_assessment": "not_evaluated",
    }
    try:
        _write_json_exclusive(directory_fd, "reservation.json", reservation)
        budget = _new_pilot_budget(authorization)
        client = _create_pilot_client()
        try:
            try:
                _require_authorization_live(authorization)
            except PilotError:
                _write_json_exclusive(
                    directory_fd,
                    "capture.json",
                    _failure_manifest(
                        reservation,
                        outcome="authorization_expired_before_transport",
                        attempts_used=budget.attempts_used,
                        http_status=None,
                        failure_category="authorization_expired_before_transport",
                        response_body_note="No request was sent; no response body exists.",
                    ),
                )
                raise
            try:
                with request_execution_scope(
                    source=SOURCE,
                    canonical_model=PILOT_REQUEST_MODEL,
                    operation=RequestOperation.QUERY,
                    budget=budget,
                    inherited_scopes=(),
                ):
                    response = client.get_bounded(
                        EASTMONEY_KLINE_URL,
                        params=dict(_REQUEST_PARAMS),
                        max_body_bytes=PILOT_MAX_RESPONSE_BYTES,
                        overall_timeout=PILOT_OVERALL_TIMEOUT_SECONDS,
                        timeout=(PILOT_CONNECT_TIMEOUT_SECONDS, PILOT_READ_TIMEOUT_SECONDS),
                        source=SOURCE,
                    )
            except HttpFetchError as exc:
                failure = _failure_manifest(
                    reservation,
                    outcome="http_request_failed",
                    attempts_used=budget.attempts_used,
                    http_status=exc.status,
                    failure_category=exc.category.value,
                    response_body_note=(
                        "The bounded client did not return an error response body; "
                        "no response body was saved."
                    ),
                )
                _write_json_exclusive(directory_fd, "capture.json", failure)
                raise PilotError("pilot_http_request_failed") from None
            except Exception:
                failure = _failure_manifest(
                    reservation,
                    outcome="request_failed",
                    attempts_used=budget.attempts_used,
                    http_status=None,
                    failure_category="governed_request_rejected",
                )
                _write_json_exclusive(directory_fd, "capture.json", failure)
                raise PilotError("pilot_request_failed") from None
        finally:
            _close_pilot_client(client)

        if type(response) is not BoundedHttpResponse:
            _write_json_exclusive(
                directory_fd,
                "capture.json",
                _failure_manifest(
                    reservation,
                    outcome="response_rejected",
                    attempts_used=budget.attempts_used,
                    http_status=None,
                    failure_category="bounded_response_type_invalid",
                ),
            )
            raise PilotError("pilot_response_rejected")
        if (
            type(response.status_code) is not int
            or response.status_code != 200
            or type(response.body) is not bytes
            or len(response.body) > PILOT_MAX_RESPONSE_BYTES
        ):
            _write_json_exclusive(
                directory_fd,
                "capture.json",
                _failure_manifest(
                    reservation,
                    outcome="response_rejected",
                    attempts_used=budget.attempts_used,
                    http_status=(
                        response.status_code if type(response.status_code) is int else None
                    ),
                    failure_category="bounded_response_contract_invalid",
                ),
            )
            raise PilotError("pilot_response_rejected")

        raw_digest = hashlib.sha256(response.body).hexdigest()
        _write_bytes_exclusive(directory_fd, "raw-response.bin", response.body)
        try:
            retrieved_at = _require_aware_utc(response.retrieved_at)
        except PilotError:
            invalid_time = _success_manifest(
                reservation,
                retrieved_at=None,
                raw_bytes=len(response.body),
                raw_sha256=raw_digest,
                attempts_used=budget.attempts_used,
                outcome="response_saved_but_rejected",
                row_count=None,
                observed_dates=(),
                failure_category="retrieval_timestamp_invalid",
            )
            _write_json_exclusive(directory_fd, "capture.json", invalid_time)
            raise PilotError("pilot_response_rejected") from None
        try:
            frame = parse_stock_daily_wire(
                response.body,
                symbol=PILOT_PROVIDER_SYMBOL,
                expected_market=1,
            )
            observed_dates = _validate_returned_dates(frame["日期"].tolist())
        except (TypeError, ValueError, KeyError):
            rejected = _success_manifest(
                reservation,
                retrieved_at=retrieved_at,
                raw_bytes=len(response.body),
                raw_sha256=raw_digest,
                attempts_used=budget.attempts_used,
                outcome="response_saved_but_rejected",
                row_count=None,
                observed_dates=(),
                failure_category="wire_or_window_validation_failed",
            )
            _write_json_exclusive(directory_fd, "capture.json", rejected)
            raise PilotError("pilot_response_rejected") from None

        if not observed_dates:
            rejected = _success_manifest(
                reservation,
                retrieved_at=retrieved_at,
                raw_bytes=len(response.body),
                raw_sha256=raw_digest,
                attempts_used=budget.attempts_used,
                outcome="response_saved_but_rejected",
                row_count=0,
                observed_dates=(),
                failure_category="empty_sample",
            )
            _write_json_exclusive(directory_fd, "capture.json", rejected)
            raise PilotError("pilot_response_rejected")

        manifest = _success_manifest(
            reservation,
            retrieved_at=retrieved_at,
            raw_bytes=len(response.body),
            raw_sha256=raw_digest,
            attempts_used=budget.attempts_used,
            outcome="bounded_sample_captured",
            row_count=len(observed_dates),
            observed_dates=observed_dates,
            failure_category=None,
        )
        _write_json_exclusive(directory_fd, "capture.json", manifest)
        os.fsync(directory_fd)
        return manifest
    except PilotError:
        raise
    except Exception:
        raise PilotError("pilot_evidence_write_failed") from None
    finally:
        os.close(directory_fd)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with explicit authorization and evidence paths."""
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
                "raw_sha256": metadata["raw_sha256"],
                "row_count": metadata["row_count"],
                "retrieved_at_utc": metadata["retrieved_at_utc"],
            },
            sort_keys=True,
        )
    )
    return 0


def _require_local_development() -> None:
    """Require an explicit development environment and refuse test/production use."""
    if os.environ.get("APP_ENV") != "development":
        raise PilotError("explicit_development_environment_required")
    if os.environ.get("TESTING", "").strip().casefold() in {"1", "true", "yes", "on"}:
        raise PilotError("pilot_disabled_in_testing_environment")


def _consume_authorization(path: Path) -> _Authorization:
    """Validate and consume one private authorization nonce before transport."""
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

        payload = _strict_json_object(raw)
        now = _utcnow()
        authorization = _validate_authorization(payload, raw=raw, now=now)
        marker_name = f".pilot-{hashlib.sha256(authorization.nonce.encode()).hexdigest()}.consumed"
        marker_fd = _open_exclusive_file(parent_fd, marker_name)
        try:
            marker = {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "state": "authorization_consumed_before_transport",
                "consumed_at_utc": _format_utc(now),
                "authorization_sha256": authorization.digest,
                "request_sha256": _request_fingerprint(),
            }
            _write_all(marker_fd, _json_bytes(marker))
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
    """Validate the exact short-lived manual authorization document."""
    if set(payload) != _AUTHORIZATION_KEYS:
        raise PilotError("authorization_scope_invalid")
    exact_values: dict[str, object] = {
        "schema_version": 1,
        "purpose": PILOT_PURPOSE,
        "symbol": PILOT_SYMBOL,
        "start_date": PILOT_START_DATE.isoformat(),
        "end_date": PILOT_END_DATE.isoformat(),
        "adjustment_basis": PILOT_ADJUSTMENT_BASIS,
        "max_requests": PILOT_MAX_REQUESTS,
        "max_response_bytes": PILOT_MAX_RESPONSE_BYTES,
    }
    for key, expected in exact_values.items():
        value = payload[key]
        if type(value) is not type(expected) or value != expected:
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


def _new_pilot_budget(authorization: _Authorization) -> RequestBudget:
    """Build an exact local-pilot grant bounded by the consumed admission expiry.

    This explicit ALLOWED grant is confined to this manual pilot entry point;
    it does not update or represent the production source-policy decision.
    """
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    now = _utcnow()
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=PILOT_REQUEST_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=("Local manual pilot authorization sha256=" + authorization.digest),
        task_attempts=PILOT_MAX_REQUESTS,
        source_attempts=PILOT_MAX_REQUESTS,
        allowed_hosts={EASTMONEY_KLINE_HOST},
        expires_at=min(now + timedelta(minutes=1), authorization.expires_at),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=PILOT_MAX_REQUESTS,
        source_attempts=PILOT_MAX_REQUESTS,
        grants=(grant,),
        profile=profile,
    )


def _create_pilot_client() -> _PilotHttpClient:
    """Create an isolated no-proxy, one-attempt bounded HTTP client."""
    session = requests.Session()
    session.trust_env = False
    config = HttpClientConfig(
        connect_timeout=PILOT_CONNECT_TIMEOUT_SECONDS,
        read_timeout=PILOT_READ_TIMEOUT_SECONDS,
        max_attempts=PILOT_MAX_REQUESTS,
        backoff_base=0,
        backoff_jitter=0,
        rate_limit_per_host=1,
        rate_burst=1,
        max_concurrency_per_host=1,
        rate_max_wait=0,
        default_headers={"User-Agent": "opendata-local-pilot/1"},
    )
    return _PilotHttpClient(
        client=GovernedHttpClient(config=config, session=session),
        session=session,
    )


def _close_pilot_client(client: _PilotHttpClient) -> None:
    """Best-effort close of the one-shot transport session on all request paths."""
    with suppress(Exception):
        client.close()


def _request_description() -> dict[str, object]:
    """Return the fixed non-secret request dimensions recorded with evidence."""
    return {
        "method": "GET",
        "symbol": PILOT_SYMBOL,
        "provider_symbol": PILOT_PROVIDER_SYMBOL,
        "start_date": PILOT_START_DATE.isoformat(),
        "end_date": PILOT_END_DATE.isoformat(),
        "adjustment_basis": PILOT_ADJUSTMENT_BASIS,
        "fields1": _REQUEST_PARAMS["fields1"],
        "fields2": _REQUEST_PARAMS["fields2"],
        "klt": _REQUEST_PARAMS["klt"],
        "fqt": _REQUEST_PARAMS["fqt"],
        "secid": _REQUEST_PARAMS["secid"],
        "max_requests": PILOT_MAX_REQUESTS,
        "max_response_bytes": PILOT_MAX_RESPONSE_BYTES,
        "overall_timeout_seconds": PILOT_OVERALL_TIMEOUT_SECONDS,
        "overall_timeout_semantics": "cooperative_soft_deadline",
        "overall_timeout_wall_clock_limit": (
            "not_guaranteed; slow streaming reads may exceed the deadline substantially"
        ),
    }


def _request_fingerprint() -> str:
    """Hash the fixed endpoint and query parameters without adding credentials."""
    identity = {
        "method": "GET",
        "endpoint": EASTMONEY_KLINE_URL,
        "params": dict(_REQUEST_PARAMS),
    }
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _validate_returned_dates(values: list[object]) -> tuple[str, ...]:
    """Require unique ISO business dates inside the explicitly requested window."""
    parsed: list[date] = []
    for value in values:
        if type(value) is not str:
            raise ValueError("returned date is not text")
        try:
            parsed_value = date.fromisoformat(value)
        except ValueError:
            raise ValueError("returned date is invalid") from None
        if parsed_value.isoformat() != value:
            raise ValueError("returned date is not canonical ISO format")
        if not PILOT_START_DATE <= parsed_value <= PILOT_END_DATE:
            raise ValueError("returned date is outside the requested window")
        parsed.append(parsed_value)
    if len(parsed) != len(set(parsed)):
        raise ValueError("returned dates contain duplicate business keys")
    return tuple(value.isoformat() for value in parsed)


def _success_manifest(
    reservation: Mapping[str, object],
    *,
    retrieved_at: datetime | None,
    raw_bytes: int,
    raw_sha256: str,
    attempts_used: int,
    outcome: str,
    row_count: int | None,
    observed_dates: tuple[str, ...],
    failure_category: str | None,
) -> dict[str, object]:
    """Bind the captured bytes, request identity, and bounded sample summary."""
    return {
        **reservation,
        "outcome": outcome,
        "retrieved_at_utc": None if retrieved_at is None else _format_utc(retrieved_at),
        "http_status": 200,
        "attempts_used": attempts_used,
        "response_body_saved": True,
        "raw_file": "raw-response.bin",
        "raw_bytes": raw_bytes,
        "raw_sha256": raw_sha256,
        "row_count": row_count,
        "observed_date_min": min(observed_dates) if observed_dates else None,
        "observed_date_max": max(observed_dates) if observed_dates else None,
        "failure_category": failure_category,
        "completeness_assessment": "not_evaluated",
        "completeness_note": (
            "Trading calendar, suspension dates, listing boundary, source retention, "
            "and head/interior/tail coverage were not reconciled; this bounded sample "
            "does not establish completeness."
        ),
    }


def _failure_manifest(
    reservation: Mapping[str, object],
    *,
    outcome: str,
    attempts_used: int,
    http_status: int | None,
    failure_category: str,
    response_body_note: str = "No response body was saved.",
) -> dict[str, object]:
    """Record a safe failed attempt without claiming an unavailable response body."""
    return {
        **reservation,
        "outcome": outcome,
        "attempts_used": attempts_used,
        "http_status": http_status,
        "response_body_saved": False,
        "response_body_note": response_body_note,
        "failure_category": failure_category,
        "completeness_assessment": "not_evaluated",
    }


def _strict_json_object(raw: bytes) -> dict[str, object]:
    """Decode bounded strict JSON and reject duplicate keys and non-finite values."""
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PilotError("authorization_json_invalid") from None
    if type(value) is not dict:
        raise PilotError("authorization_json_invalid")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON mapping only when each key appears once."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    """Reject JavaScript-style NaN and Infinity tokens accepted by Python JSON."""
    raise ValueError("non-finite JSON constant: " + value)


def _parse_utc_timestamp(value: object) -> datetime:
    """Parse one exact UTC timestamp ending in Z."""
    if type(value) is not str or not value.endswith("Z"):
        raise PilotError("authorization_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PilotError("authorization_timestamp_invalid") from None
    if _format_utc(parsed) != value:
        raise PilotError("authorization_timestamp_invalid")
    return parsed


def _require_aware_utc(value: object) -> datetime:
    """Validate the bounded response's observation time and normalize it to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PilotError("response_retrieval_time_invalid")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        raise PilotError("response_retrieval_time_invalid") from None


def _utcnow() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(timezone.utc)


def _require_authorization_live(authorization: _Authorization) -> None:
    """Refuse to enter the request scope after the local admission expires."""
    if authorization.expires_at <= _utcnow():
        raise PilotError("authorization_expired_before_transport")


def _format_utc(value: datetime) -> str:
    """Format an aware timestamp with stable microsecond precision and Z suffix."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _open_private_directory(
    path: Path,
    *,
    unavailable_code: str,
    permissions_code: str,
) -> int:
    """Open a 0700 current-user directory without following path symlinks."""
    if not hasattr(os, "getuid") or not _O_DIRECTORY or not _O_NOFOLLOW or not _O_NONBLOCK:
        raise PilotError("secure_local_file_primitives_unavailable")
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise PilotError(unavailable_code)

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
            raise PilotError(permissions_code)
        result = descriptor
        descriptor = None
        return result
    except PilotError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise PilotError(unavailable_code) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_private_parent(path: Path) -> tuple[int, str]:
    """Open a current-user-owned private directory and return its descriptor/name."""
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PilotError("absolute_file_path_required")
    parent_fd = _open_private_directory(
        path.parent,
        unavailable_code="private_parent_directory_unavailable",
        permissions_code="private_parent_directory_required",
    )
    return parent_fd, path.name


def _open_regular_file(directory_fd: int, name: str) -> int:
    """Open one non-symlink regular file beneath an already opened directory."""
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
    """Read at most the exact authorization-file limit plus one sentinel byte."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    result = b"".join(chunks)
    if len(result) > limit:
        raise PilotError("authorization_file_too_large")
    return result


def _create_evidence_directory(path: Path) -> int:
    """Atomically create and open one new, private evidence directory."""
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PilotError("absolute_evidence_directory_required")
    parent_fd = _open_private_directory(
        path.parent,
        unavailable_code="evidence_parent_directory_unavailable",
        permissions_code="evidence_parent_directory_required",
    )
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
    if (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700
    ):
        os.close(directory_fd)
        raise PilotError("evidence_directory_permissions_invalid")
    return directory_fd


def _open_exclusive_file(directory_fd: int, name: str) -> int:
    """Create a mode-0600 regular output file without following or replacing names."""
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
    """Create and durably write one evidence file without overwriting prior bytes."""
    descriptor = _open_exclusive_file(directory_fd, name)
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory_fd)


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Create and durably write one compact JSON evidence document."""
    _write_bytes_exclusive(directory_fd, name, _json_bytes(value))


def _write_all(descriptor: int, content: bytes) -> None:
    """Write every byte or raise without exposing any body in an error message."""
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short evidence write")
        remaining = remaining[written:]


def _json_bytes(value: Mapping[str, object]) -> bytes:
    """Encode metadata as deterministic, finite UTF-8 JSON."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Authorization:
    """Validated fields needed after a private nonce has been consumed."""

    nonce: str
    digest: str
    issued_at: datetime
    expires_at: datetime


if __name__ == "__main__":
    sys.exit(main())
