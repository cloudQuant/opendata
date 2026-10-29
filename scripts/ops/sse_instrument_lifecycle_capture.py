"""Capture a small, private SSE instrument-list sample without lifecycle inference.

The command queries only four fixed SSE contexts: current Main Board A,
current STAR Market, and the corresponding ``COMPANY_STATUS=3`` candidate
lists. It requests at most two bounded pages per context. A short page and
SSE's reported totals are observations only; they do not establish source or
market completeness. Raw rows are retained as content-addressed private
evidence and never copied into receipts. ``COMPANY_CODE`` is not a security
identity, and ``DELIST_DATE`` is not interpreted as a termination date.

Live access requires both an explicit ``--live`` CLI flag and
``APP_ENV=development``. Offline fake clients are accepted only from an
explicit testing environment. Each physical request uses the governed
bounded transport with one attempt, a fixed host grant, redirects disabled by
the scoped HTTP client, and byte and cooperative timeout limits. The grant and
budgets are local controls; they do not prove source authorization or rights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import requests

from opendata.data.http_client import (
    BoundedHttpResponse,
    GovernedHttpClient,
    HttpClientConfig,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource
from opendata.pipeline.sse_instrument_census import (
    SSEInstrumentCensusError,
    SSEInstrumentPageInput,
    SSEInstrumentSelector,
    build_sse_instrument_census,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = ["SSECaptureError", "run_sse_instrument_capture"]

SOURCE = "sse"
CANONICAL_MODEL = "instrument_census"
HOST = "query.sse.com.cn"
ENDPOINT = f"https://{HOST}/sseQuery/commonQuery.do"
REFERER = "https://www.sse.com.cn/assortment/stock/list/share/"
PAGE_SIZE = 1_000
MAX_PAGES_PER_SELECTOR = 2
MAX_REQUESTS = 4 * MAX_PAGES_PER_SELECTOR
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 12.0
MAX_TASK_TIMEOUT_SECONDS = 60.0
TASK_GRANT_SECONDS = 90
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_SELECTORS: tuple[SSEInstrumentSelector, ...] = (
    SSEInstrumentSelector.CURRENT_MAIN_A,
    SSEInstrumentSelector.CURRENT_STAR,
    SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
    SSEInstrumentSelector.STATUS3_STAR_CANDIDATE,
)
_SELECTOR_PARAMS: dict[SSEInstrumentSelector, tuple[str, str]] = {
    SSEInstrumentSelector.CURRENT_MAIN_A: ("1", "2,4,5,7,8"),
    SSEInstrumentSelector.CURRENT_STAR: ("8", "2,4,5,7,8"),
    SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE: ("1", "3"),
    SSEInstrumentSelector.STATUS3_STAR_CANDIDATE: ("8", "3"),
}


class SSECaptureError(RuntimeError):
    """A stable, content-free error code for the capture command."""

    def __init__(self, code: str) -> None:
        """Create an error without embedding source values or transport details."""
        self.code = code
        super().__init__(code)


class _BoundedSSEClient(Protocol):
    """The narrow one-response transport surface used by the capture runner."""

    def get_bounded(
        self,
        url: str,
        *,
        max_body_bytes: int,
        overall_timeout: float,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Perform one governed bounded GET and return its body bytes."""

    def close(self) -> None:
        """Close resources owned by this client."""


@dataclass(frozen=True, slots=True, repr=False)
class _PageAttempt:
    """One selector-page attempt with values deliberately excluded from receipts."""

    selector: SSEInstrumentSelector
    sequence_number: int
    page_number: int
    request_summary_sha256: str
    outcome: str
    failure_code: str | None = None
    status_code: int | None = None
    response_bytes: int = 0
    response_sha256: str | None = None
    raw_manifest_sha256: str | None = None
    raw_persisted: bool | None = False
    row_count: int | None = None
    retrieved_at_utc: str | None = None
    page_input: SSEInstrumentPageInput | None = field(default=None, repr=False)


@dataclass(slots=True)
class _OwnedGovernedClient:
    """Pair the governed client with the Requests session it owns."""

    client: GovernedHttpClient
    session: requests.Session

    def get_bounded(
        self,
        url: str,
        *,
        max_body_bytes: int,
        overall_timeout: float,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Delegate to the configured one-attempt bounded client."""
        return self.client.get_bounded(
            url,
            max_body_bytes=max_body_bytes,
            overall_timeout=overall_timeout,
            params=params,
            headers=headers,
            timeout=timeout,
            source=source,
        )

    def close(self) -> None:
        """Close the owned Requests session."""
        self.session.close()


def run_sse_instrument_capture(
    evidence_dir: Path,
    *,
    live: bool = False,
    client: _BoundedSSEClient | None = None,
    client_factory: Callable[[], _BoundedSSEClient] | None = None,
) -> dict[str, object]:
    """Capture bounded SSE instrument pages and write anonymous private receipts.

    Args:
        evidence_dir: Absolute path to a new, private evidence directory.
        live: Explicit live-mode switch. This must be true only for a deliberate
            local CLI invocation in ``APP_ENV=development``.
        client: Optional offline-only fake transport used by tests.
        client_factory: Optional offline-only factory for an injected fake.

    Returns:
        A row-free summary of attempted pages, local hashes, and unknown source
        and market completeness. Raw response entities are stored separately.

    Raises:
        SSECaptureError: If the evidence path cannot be created safely or the
            caller selects an invalid live/offline execution mode.
    """
    if type(live) is not bool:
        raise SSECaptureError("live_flag_invalid")
    injected = client is not None or client_factory is not None
    if client is not None and client_factory is not None:
        raise SSECaptureError("offline_client_pair_invalid")
    if live and injected:
        raise SSECaptureError("live_and_offline_client_conflict")
    if live:
        _require_live_environment()
    elif injected:
        _require_offline_environment()
    else:
        raise SSECaptureError("explicit_live_mode_required")

    evidence_fd = _create_private_evidence_directory(evidence_dir)
    active_client: _BoundedSSEClient | None = None
    attempts: list[_PageAttempt] = []
    started = time.monotonic()
    deadline = started + MAX_TASK_TIMEOUT_SECONDS
    total_response_bytes = 0
    request_count = 0
    try:
        try:
            active_client = client if client is not None else _make_client(client_factory)
            budget = _new_request_budget()
            raw_writer = RawResponseArtifactWriter(
                evidence_dir / "raw", max_response_bytes=MAX_RESPONSE_BYTES
            )
            stop_code: str | None = None
            with request_execution_scope(
                source=SOURCE,
                canonical_model=CANONICAL_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                deadline=deadline,
                inherited_scopes=(),
            ):
                for selector in _SELECTORS:
                    for page_number in range(1, MAX_PAGES_PER_SELECTOR + 1):
                        if request_count >= MAX_REQUESTS:
                            stop_code = "request_cap_reached"
                            break
                        if time.monotonic() >= deadline:
                            stop_code = "task_deadline_reached"
                            break
                        sequence_number = request_count + 1
                        summary = _request_summary(selector, page_number)
                        request_hash = _summary_sha256(summary)
                        try:
                            _write_json_exclusive(
                                evidence_fd,
                                f"page-{sequence_number:02d}-reservation.json",
                                _page_reservation(
                                    selector,
                                    sequence_number,
                                    page_number,
                                    request_hash,
                                ),
                            )
                        except SSECaptureError:
                            stop_code = "page_reservation_persistence_failed"
                            break
                        request_count += 1
                        page_attempt, page_rows = _capture_page(
                            active_client,
                            raw_writer,
                            selector=selector,
                            sequence_number=sequence_number,
                            page_number=page_number,
                            request_summary=summary,
                            request_hash=request_hash,
                        )
                        attempts.append(page_attempt)
                        total_response_bytes += page_attempt.response_bytes
                        if total_response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                            page_attempt = replace(
                                page_attempt,
                                outcome="failed",
                                failure_code="task_response_byte_cap_exceeded",
                                row_count=None,
                                page_input=None,
                            )
                            attempts[-1] = page_attempt
                        try:
                            _write_json_exclusive(
                                evidence_fd,
                                f"page-{sequence_number:02d}-receipt.json",
                                _page_receipt(page_attempt),
                            )
                        except SSECaptureError:
                            attempts[-1] = replace(
                                page_attempt,
                                outcome="failed",
                                failure_code="page_receipt_persistence_failed",
                                row_count=None,
                                page_input=None,
                            )
                            stop_code = "page_receipt_persistence_failed"
                            break
                        if total_response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                            stop_code = "task_response_byte_cap_exceeded"
                            break
                        if page_attempt.outcome != "succeeded":
                            stop_code = page_attempt.failure_code or "page_capture_failed"
                            break
                        if page_rows < PAGE_SIZE:
                            break
                    if stop_code is not None:
                        break

            successful_inputs = tuple(
                attempt.page_input for attempt in attempts if attempt.page_input is not None
            )
            if successful_inputs:
                # The parser rechecks global sequence ordering and all response
                # bounds before a success summary is written.
                capture = build_sse_instrument_census(
                    successful_inputs, observed_at=datetime.now(timezone.utc)
                )
                if capture.source_authenticity != "UNKNOWN":
                    stop_code = "source_authenticity_contract_invalid"
            return _persist_terminal_receipt(evidence_fd, attempts, stop_code)
        except SSECaptureError as exc:
            return _persist_terminal_receipt(evidence_fd, attempts, exc.code)
        except Exception:
            return _persist_terminal_receipt(
                evidence_fd, attempts, "local_capture_operation_failed"
            )
    finally:
        if active_client is not None:
            with suppress(Exception):
                active_client.close()
        os.close(evidence_fd)


def _capture_page(
    client: _BoundedSSEClient,
    raw_writer: RawResponseArtifactWriter,
    *,
    selector: SSEInstrumentSelector,
    sequence_number: int,
    page_number: int,
    request_summary: Mapping[str, object],
    request_hash: str,
) -> tuple[_PageAttempt, int]:
    """Send one no-retry request, persist raw bytes, and validate the envelope."""
    params = _request_params(selector, page_number)
    try:
        response = client.get_bounded(
            ENDPOINT,
            max_body_bytes=MAX_RESPONSE_BYTES,
            overall_timeout=REQUEST_TIMEOUT_SECONDS,
            params=params,
            headers={
                "Accept": "application/json,*/*",
                "Referer": REFERER,
                "User-Agent": "OpenData-instrument-capture/1.0",
            },
            timeout=(5.0, 10.0),
            source=SOURCE,
        )
    except Exception:
        return (
            _PageAttempt(
                selector=selector,
                sequence_number=sequence_number,
                page_number=page_number,
                request_summary_sha256=request_hash,
                outcome="failed",
                failure_code="request_failed_or_outcome_unknown",
            ),
            0,
        )
    if type(response) is not BoundedHttpResponse:
        return (
            _PageAttempt(
                selector=selector,
                sequence_number=sequence_number,
                page_number=page_number,
                request_summary_sha256=request_hash,
                outcome="failed",
                failure_code="response_contract_invalid",
            ),
            0,
        )
    body = response.body
    if (
        type(body) is not bytes
        or not body
        or len(body) > MAX_RESPONSE_BYTES
        or type(response.status_code) is not int
        or response.status_code != 200
        or not _is_aware_datetime(response.retrieved_at)
    ):
        return (
            _PageAttempt(
                selector=selector,
                sequence_number=sequence_number,
                page_number=page_number,
                request_summary_sha256=request_hash,
                outcome="failed",
                failure_code="response_contract_invalid",
                status_code=response.status_code if type(response.status_code) is int else None,
                response_bytes=len(body) if type(body) is bytes else 0,
                response_sha256=hashlib.sha256(body).hexdigest() if type(body) is bytes else None,
                retrieved_at_utc=(
                    response.retrieved_at.astimezone(timezone.utc).isoformat()
                    if isinstance(response.retrieved_at, datetime)
                    and _is_aware_datetime(response.retrieved_at)
                    else None
                ),
            ),
            0,
        )

    response_hash = hashlib.sha256(body).hexdigest()
    manifest_hash: str | None = None
    try:
        captured = raw_writer.capture(
            (body,),
            source=RawResponseSource(
                provider_id="sse",
                dataset_id="instrument-census",
                operation_id="fixed-selector-page",
            ),
            request_summary_sha256=request_hash,
            http_status=response.status_code,
            retention_class="unknown",
            source_history_limit="unknown",
        )
        manifest_hash = captured.manifest_path.stem
    except Exception:
        return (
            _PageAttempt(
                selector=selector,
                sequence_number=sequence_number,
                page_number=page_number,
                request_summary_sha256=request_hash,
                raw_persisted=None,
                outcome="failed",
                failure_code="raw_evidence_persistence_failed",
                status_code=response.status_code,
                response_bytes=len(body),
                response_sha256=response_hash,
                retrieved_at_utc=response.retrieved_at.astimezone(timezone.utc).isoformat(),
            ),
            0,
        )

    try:
        page_input = SSEInstrumentPageInput(
            selector=selector,
            sequence_number=sequence_number,
            raw_response_body=body,
            declared_response_sha256=response_hash,
            request_summary=request_summary,
            http_status_code=response.status_code,
        )
        parsed = build_sse_instrument_census(
            (
                SSEInstrumentPageInput(
                    selector=selector,
                    sequence_number=1,
                    raw_response_body=body,
                    declared_response_sha256=response_hash,
                    request_summary=request_summary,
                    http_status_code=response.status_code,
                ),
            ),
            observed_at=response.retrieved_at,
        )
    except SSEInstrumentCensusError as exc:
        return (
            _PageAttempt(
                selector=selector,
                sequence_number=sequence_number,
                page_number=page_number,
                request_summary_sha256=request_hash,
                outcome="failed",
                failure_code=_safe_parser_code(exc.code),
                status_code=response.status_code,
                response_bytes=len(body),
                response_sha256=response_hash,
                raw_manifest_sha256=manifest_hash,
                raw_persisted=True,
                retrieved_at_utc=response.retrieved_at.astimezone(timezone.utc).isoformat(),
            ),
            0,
        )
    row_count = len(parsed.pages[0].source_rows)
    return (
        _PageAttempt(
            selector=selector,
            sequence_number=sequence_number,
            page_number=page_number,
            request_summary_sha256=request_hash,
            outcome="succeeded",
            status_code=response.status_code,
            response_bytes=len(body),
            response_sha256=response_hash,
            raw_manifest_sha256=manifest_hash,
            raw_persisted=True,
            row_count=row_count,
            retrieved_at_utc=response.retrieved_at.astimezone(timezone.utc).isoformat(),
            page_input=page_input,
        ),
        row_count,
    )


def _request_params(selector: SSEInstrumentSelector, page_number: int) -> dict[str, str]:
    """Build the fixed vendored SSE request with bounded page controls."""
    stock_type, company_status = _SELECTOR_PARAMS[selector]
    page = str(page_number)
    return {
        "STOCK_TYPE": stock_type,
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": company_status,
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": page,
        "pageHelp.pageSize": str(PAGE_SIZE),
        "pageHelp.pageNo": page,
        "pageHelp.endPage": page,
    }


def _request_summary(selector: SSEInstrumentSelector, page_number: int) -> dict[str, object]:
    """Describe the fixed request without credentials or response values."""
    return {
        "method": "GET",
        "endpoint": ENDPOINT,
        "params": _request_params(selector, page_number),
    }


def _page_reservation(
    selector: SSEInstrumentSelector,
    sequence_number: int,
    page_number: int,
    request_hash: str,
) -> dict[str, object]:
    """Record a durable pending marker before issuing a physical request."""
    return {
        "schema_version": 1,
        "page_sequence": sequence_number,
        "selector": selector.value,
        "page_number": page_number,
        "request_summary_sha256": request_hash,
        "state": "reserved_before_transport",
        "automatic_retry": False,
    }


def _new_request_budget() -> RequestBudget:
    """Create one short-lived exact-host grant with a finite eight-send ceiling."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TASK_GRANT_SECONDS)
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="user_authorized_sse_instrument_capture",
        task_attempts=MAX_REQUESTS,
        source_attempts=MAX_REQUESTS,
        allowed_hosts={HOST},
        expires_at=expires_at,
        profile=profile,
    )
    return RequestBudget(
        task_attempts=MAX_REQUESTS,
        source_attempts=MAX_REQUESTS,
        grants=(grant,),
        profile=profile,
    )


def _make_client(factory: Callable[[], _BoundedSSEClient] | None) -> _BoundedSSEClient:
    """Create a live transport or validate the explicit offline fake seam."""
    if factory is not None:
        try:
            value = factory()
        except Exception:
            raise SSECaptureError("offline_client_factory_failed") from None
        if not callable(getattr(value, "get_bounded", None)) or not callable(
            getattr(value, "close", None)
        ):
            raise SSECaptureError("client_contract_invalid")
        return value
    session = requests.Session()
    session.trust_env = False
    client = GovernedHttpClient(
        HttpClientConfig(
            connect_timeout=5.0,
            read_timeout=10.0,
            max_attempts=1,
            backoff_base=0.0,
            backoff_jitter=0.0,
            rate_limit_per_host=1.0,
            rate_burst=1,
            max_concurrency_per_host=1,
            rate_max_wait=5.0,
            default_headers={"User-Agent": "OpenData-instrument-capture/1.0"},
            proxies={},
        ),
        session=session,
    )
    return _OwnedGovernedClient(client=client, session=session)


def _capture_receipt(
    attempts: Sequence[_PageAttempt], *, stop_code: str | None
) -> dict[str, object]:
    """Build a bounded receipt without source row values or exception details."""
    selector_receipts: list[dict[str, object]] = []
    for selector in _SELECTORS:
        selected = [attempt for attempt in attempts if attempt.selector is selector]
        selector_receipts.append(
            {
                "selector": selector.value,
                "pages_attempted": len(selected),
                "pages_succeeded": sum(item.outcome == "succeeded" for item in selected),
                "pages_failed": sum(item.outcome != "succeeded" for item in selected),
                "row_count_observed": sum(item.row_count or 0 for item in selected),
                "pagination_observation": _pagination_observation(selected),
            }
        )
    succeeded = sum(attempt.outcome == "succeeded" for attempt in attempts)
    failed = len(attempts) - succeeded
    outcome = (
        "bounded_unverified_sse_snapshot_captured"
        if failed == 0 and (stop_code is None or stop_code == "request_cap_reached")
        else "partial_or_failed_capture"
    )
    return {
        "schema_version": 1,
        "outcome": outcome,
        "source": SOURCE,
        "declared_scope": [
            "SSE",
            "current Main Board A",
            "current STAR Market",
            "COMPANY_STATUS=3 candidate contexts",
        ],
        "selectors": selector_receipts,
        "requests_attempted": len(attempts),
        "requests_succeeded": succeeded,
        "requests_failed": failed,
        "stop_code": stop_code,
        "request_cap": MAX_REQUESTS,
        "response_byte_cap_per_request": MAX_RESPONSE_BYTES,
        "response_byte_cap_total": MAX_TOTAL_RESPONSE_BYTES,
        "page_size_requested": PAGE_SIZE,
        "page_cap_per_selector": MAX_PAGES_PER_SELECTOR,
        "retry_policy": "one_transport_attempt_per_request",
        "redirect_policy": "disabled_by_scoped_bounded_transport",
        "pagination_status": "UNKNOWN",
        "source_authenticity": "UNKNOWN",
        "market_completeness": "UNKNOWN",
        "historical_coverage": "UNKNOWN",
        "identity_resolution": "not_performed",
        "lifecycle_inference": "not_performed",
        "short_pages_or_reported_totals_prove_completeness": False,
        "row_values_in_receipt": False,
        "page_receipts": [_page_receipt(attempt) for attempt in attempts],
    }


def _page_receipt(attempt: _PageAttempt) -> dict[str, object]:
    """Render safe metadata for one attempted page."""
    return {
        "page_sequence": attempt.sequence_number,
        "selector": attempt.selector.value,
        "page_number": attempt.page_number,
        "request_attempted": True,
        "outcome": attempt.outcome,
        "failure_code": attempt.failure_code,
        "http_status": attempt.status_code,
        "response_bytes": attempt.response_bytes,
        "response_sha256": attempt.response_sha256,
        "raw_manifest_sha256": attempt.raw_manifest_sha256,
        "raw_response_persisted": attempt.raw_persisted,
        "parsed_row_count": attempt.row_count,
        "request_summary_sha256": attempt.request_summary_sha256,
        "retrieved_at_utc": attempt.retrieved_at_utc,
    }


def _pagination_observation(attempts: Sequence[_PageAttempt]) -> str:
    """Describe bounded local observations without a completeness conclusion."""
    if not attempts:
        return "not_attempted"
    last = attempts[-1]
    if last.outcome != "succeeded":
        return "unknown_after_failed_request"
    if last.row_count is not None and last.row_count < PAGE_SIZE:
        return "short_page_observed"
    if len(attempts) >= MAX_PAGES_PER_SELECTOR:
        return "page_cap_reached"
    return "bounded_sample_only"


def _safe_parser_code(code: str) -> str:
    """Allow only bounded parser rejection codes in operational receipts."""
    if (
        code
        and len(code) <= 96
        and code.isascii()
        and all(char.islower() or char.isdigit() or char == "_" for char in code)
    ):
        return code
    return "offline_parser_rejected"


def _summary_sha256(summary: Mapping[str, object]) -> str:
    """Hash a deterministic request summary without storing values in receipts."""
    raw = json.dumps(
        dict(summary),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_aware_datetime(value: object) -> bool:
    """Return whether a datetime has a usable explicit offset."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False


def _create_private_evidence_directory(path: Path) -> int:
    """Create a fresh private directory without following symlinks or chmodding parents."""
    if not hasattr(os, "getuid") or not _O_DIRECTORY or not _O_NOFOLLOW:
        raise SSECaptureError("secure_local_file_primitives_unavailable")
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise SSECaptureError("absolute_evidence_directory_required")
    if path.name in {"", ".", ".."}:
        raise SSECaptureError("absolute_evidence_directory_required")
    parent_fd = _open_private_directory_chain(path.parent)
    try:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            raise SSECaptureError("evidence_directory_must_be_new") from None
        os.fsync(parent_fd)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or mode & 0o077
            or mode & 0o700 != 0o700
        ):
            os.close(descriptor)
            raise SSECaptureError("evidence_directory_permissions_invalid")
        return descriptor
    except SSECaptureError:
        raise
    except OSError:
        raise SSECaptureError("evidence_directory_create_failed") from None
    finally:
        os.close(parent_fd)


def _open_private_directory_chain(path: Path) -> int:
    """Open the parent chain without symlinks and require only the final parent private."""
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise SSECaptureError("absolute_private_directory_required")
    descriptor = os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or mode & 0o077
            or mode & 0o700 != 0o700
        ):
            raise SSECaptureError("private_directory_permissions_required")
        result = descriptor
        descriptor = -1
        return result
    except SSECaptureError:
        raise
    except OSError:
        raise SSECaptureError("private_directory_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Atomically publish bounded private JSON through an exclusive temp file."""
    if not name.endswith(".json") or "/" in name or name in {".json", "..json"}:
        raise SSECaptureError("receipt_name_invalid")
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > 256 * 1024:
        raise SSECaptureError("receipt_too_large")
    temporary_name = f".tmp-{secrets.token_hex(16)}"
    descriptor = -1
    temporary_created = False
    final_published = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("receipt write incomplete")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        final_published = True
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    except Exception:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary_created:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        if final_published:
            # The filename was created by this call in a new evidence directory.
            with suppress(OSError):
                os.unlink(name, dir_fd=directory_fd)
        with suppress(OSError):
            os.fsync(directory_fd)
        raise SSECaptureError("receipt_persistence_failed") from None


def _persist_terminal_receipt(
    directory_fd: int,
    attempts: Sequence[_PageAttempt],
    stop_code: str | None,
) -> dict[str, object]:
    """Persist the final summary or refuse to return a success-shaped result."""
    receipt = _capture_receipt(attempts, stop_code=stop_code)
    _write_json_exclusive(directory_fd, "receipt.json", receipt)
    return receipt


def _new_task_receipt(code: str) -> dict[str, object]:
    """Return a sanitized receipt for a refusal before page attempts begin."""
    return _capture_receipt((), stop_code=code)


def _require_live_environment() -> None:
    """Require an explicit development runtime for the live CLI path."""
    if os.environ.get("APP_ENV") != "development":
        raise SSECaptureError("explicit_development_environment_required")
    if os.environ.get("TESTING", "").strip().casefold() in {"1", "true", "yes", "on"}:
        raise SSECaptureError("live_capture_disabled_in_testing_environment")


def _require_offline_environment() -> None:
    """Allow fake clients only inside an explicit testing runtime."""
    if os.environ.get("APP_ENV") != "testing":
        raise SSECaptureError("offline_injection_requires_testing_environment")
    if os.environ.get("TESTING", "").strip().casefold() not in {"1", "true", "yes", "on"}:
        raise SSECaptureError("offline_injection_requires_testing_flag")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a single explicit live capture and print only its sanitized receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument(
        "--live", action="store_true", help="explicitly permit one bounded live capture"
    )
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps(_new_task_receipt("explicit_live_mode_required"), sort_keys=True))
        return 2
    try:
        receipt = run_sse_instrument_capture(args.evidence_dir, live=True)
    except SSECaptureError as exc:
        print(json.dumps(_new_task_receipt(exc.code), sort_keys=True))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return (
        0
        if receipt.get("outcome") == "bounded_unverified_sse_snapshot_captured"
        and receipt.get("requests_failed") == 0
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
