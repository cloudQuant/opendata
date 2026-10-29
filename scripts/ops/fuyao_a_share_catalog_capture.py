"""Capture at most two current Fuyao A-share catalog pages to private evidence.

The command is a deliberately small source canary. It uses the fixed
``/api/meta/tickers/list`` endpoint with ``asset_type=a-share``,
``limit=10000`` and offsets 0/10000. The second request is made only when the
first page contains exactly 10000 rows. No short page, reported row count, or
local digest establishes that the source page chain or A-share market is
complete. Fuyao's existing provider contract also describes this endpoint as a
current listing that excludes delisted A-shares.

Live CLI calls require a short-lived private authorization JSON file whose
exact fields and values are checked by :func:`_consume_authorization`. Its
nonce is reserved in a per-user private append-only state directory, so
copying the grant to another path does not make it reusable. This is a local
control, not independent user approval or cryptographically signed authority;
users who can modify that state directory can issue or clear local markers.
The call also requires a new private evidence directory. The response bodies
are first checked for reflected credentials, then stored under a
content-addressed private raw artifact root, then parsed offline into
anonymous page receipts. No row values are written to the summary. Importing
this module does not read credentials or perform I/O.

The body limit applies to each accumulated HTTP entity body, not wire bytes or
process RSS. The total task body limit is 16 MiB; parser allocations can exceed
that amount. The 30-second deadline bounds scoped request work, not a hard
wall-clock limit for local parsing or storage. Each raw HTTP exchange uses the
transport's one-send, no-retry method and a two-attempt local request budget.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import html
import json
import os
import pwd
import re
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote_from_bytes, unquote, unquote_plus

from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.endpoints import (
    TICKERS_LIST_ENDPOINT,
    build_tickers_list_request,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import (
    FuyaoHttpClient,
    FuyaoRawExchangeResponse,
)
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetError,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.pipeline.fuyao_instrument_census import (
    FuyaoInstrumentCensusCapture,
    FuyaoInstrumentCensusError,
    FuyaoInstrumentPageInput,
    build_fuyao_instrument_census,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

PAGE_LIMIT = 10_000
MAX_PAGES = 2
MAX_REQUESTS = 2
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = MAX_PAGES * MAX_RESPONSE_BYTES
MAX_TASK_ROWS = MAX_PAGES * PAGE_LIMIT
REQUEST_TIMEOUT_SECONDS = 12.0
MAX_TASK_TIMEOUT_SECONDS = 30.0
AUTHORIZATION_MAX_AGE = timedelta(minutes=15)
AUTHORIZATION_PURPOSE = "fuyao_a_share_catalog_capture"
AUTHORIZATION_SCHEMA_VERSION = 1
HOST = "fuyao.aicubes.cn"
_AUTHORIZATION_MAX_BYTES = 8 * 1024
_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "purpose",
        "endpoint_path",
        "asset_type",
        "page_limit",
        "max_pages",
        "max_requests",
        "max_response_bytes",
        "max_total_response_bytes",
        "max_task_rows",
        "timeout_seconds",
        "max_task_timeout_seconds",
        "issued_at_utc",
        "expires_at_utc",
        "nonce",
    }
)
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAGE_RECEIPT_FIELDS = (
    "page_sequence",
    "offset",
    "attempt_outcome",
    "failure_code",
    "http_status",
    "response_bytes",
    "response_sha256",
    "raw_manifest_sha256",
    "raw_response_persisted",
    "raw_persistence_state",
    "response_suppressed",
    "parsed_row_count",
    "request_summary_sha256",
    "credential_reflection",
    "source_authenticity",
    "page_completeness",
)
_PAGE_FAILURE_CODES = frozenset(
    {
        "credential_reflection_check_failed",
        "credential_reflection_detected",
        "http_status_not_200",
        "offline_parser_rejected",
        "raw_evidence_persistence_failed",
        "request_failed_or_outcome_unknown",
        "response_contract_invalid",
    }
)
_FAILURE_CODES = (
    frozenset(
        {
            "absolute_authorization_file_required",
            "absolute_evidence_directory_required",
            "absolute_nonce_state_directory_required",
            "absolute_private_directory_required",
            "authorization_already_in_use",
            "authorization_consume_failed",
            "authorization_expired_before_request",
            "authorization_expired_or_not_yet_valid",
            "authorization_fields_invalid",
            "authorization_file_changed",
            "authorization_file_permissions_invalid",
            "authorization_file_too_large",
            "authorization_file_unavailable",
            "authorization_json_invalid",
            "authorization_nonce_invalid",
            "authorization_nonce_state_permissions_invalid",
            "authorization_nonce_state_unavailable",
            "authorization_replay_detected",
            "authorization_root_invalid",
            "authorization_scope_mismatch",
            "authorization_timestamp_invalid",
            "catalog_capture_disabled_in_testing_environment",
            "client_contract_invalid",
            "credential_reflection_check_unavailable",
            "evidence_directory_create_failed",
            "evidence_directory_must_be_new",
            "evidence_directory_permissions_invalid",
            "evidence_file_permissions_invalid",
            "explicit_development_environment_required",
            "http_status_not_200",
            "live_nonce_state_override_not_allowed",
            "local_capture_operation_failed",
            "offline_dependency_pair_required",
            "offline_injection_requires_testing_environment",
            "offline_injection_requires_testing_flag",
            "offline_nonce_state_directory_required",
            "offline_parser_rejected",
            "page_capture_incomplete",
            "private_directory_permissions_required",
            "private_directory_unavailable",
            "provider_credentials_invalid",
            "provider_credentials_unavailable",
            "provider_origin_not_allowed",
            "raw_evidence_persistence_failed",
            "request_failed_or_outcome_unknown",
            "response_contract_invalid",
            "secure_local_file_primitives_unavailable",
            "task_response_byte_cap_exceeded",
            "task_row_cap_exceeded",
        }
    )
    | _PAGE_FAILURE_CODES
)
_JSON_SIMPLE_ESCAPE_RE = re.compile(r'\\(["\\/bfnrt])')
_JSON_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_JSON_SURROGATE_PAIR_RE = re.compile(
    r"\\u(d[89ab][0-9a-f]{2})\\u(d[cdef][0-9a-f]{2})", re.IGNORECASE
)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class CatalogCaptureError(RuntimeError):
    """A stable, sanitized rejection code for this capture command."""

    def __init__(self, code: str) -> None:
        """Create an error that contains no request, response, or credential values."""
        self.code = code
        super().__init__(code)


class _RawCatalogClient(Protocol):
    """The narrow raw exchange surface used by the capture runner."""

    def get_raw_exchange(
        self, endpoint: str, *, params: Mapping[str, object]
    ) -> FuyaoRawExchangeResponse:
        """Execute one bounded GET without parsing or caching its body."""

    def close(self) -> None:
        """Close the owned transport resources."""


@dataclass(frozen=True, slots=True)
class _Authorization:
    """Validated, one-use local admission record."""

    digest: str
    expires_at: datetime
    nonce: str


@dataclass(frozen=True, slots=True)
class _PageResult:
    """Anonymous operational receipt for one attempted source page."""

    page_input: FuyaoInstrumentPageInput
    receipt: Mapping[str, object]
    row_count: int


def run_catalog_capture(
    authorization_file: Path,
    evidence_dir: Path,
    *,
    credentials: FuyaoCredentials | None = None,
    client_factory: Callable[[FuyaoCredentials], _RawCatalogClient] | None = None,
    authorization_state_dir: Path | None = None,
) -> dict[str, object]:
    """Capture a current Fuyao A-share directory sample under an exact local grant.

    The CLI uses the live, lazily resolved project credentials. Supplying both
    ``credentials`` and ``client_factory`` is reserved for offline fakes; that
    seam is not exposed as CLI input and must never issue network requests.
    ``authorization_state_dir`` is available only with that test seam so tests
    can keep nonce markers inside their temporary directory.

    Args:
        authorization_file: Absolute, private, short-lived authorization JSON path.
        evidence_dir: Absolute path to a new private evidence directory.
        credentials: Optional credential object for an offline fake client.
        client_factory: Optional offline fake factory; must perform no I/O.
        authorization_state_dir: Optional temporary nonce state root for offline tests.

    Returns:
        A value-free summary containing page counts, statuses, and local hashes.
        A failed attempt is returned as a sanitized receipt when the evidence
        directory was created successfully.

    Raises:
        CatalogCaptureError: If the evidence directory cannot be safely created.
    """
    injected = credentials is not None and client_factory is not None
    if (credentials is None) != (client_factory is None):
        raise CatalogCaptureError("offline_dependency_pair_required")
    if injected:
        _require_offline_fake_environment()
        if authorization_state_dir is None:
            raise CatalogCaptureError("offline_nonce_state_directory_required")
        if (
            not isinstance(authorization_state_dir, Path)
            or not authorization_state_dir.is_absolute()
        ):
            raise CatalogCaptureError("absolute_nonce_state_directory_required")
    else:
        _require_live_environment()
        if authorization_state_dir is not None:
            raise CatalogCaptureError("live_nonce_state_override_not_allowed")

    directory_fd = _create_private_evidence_directory(evidence_dir)
    active_client: _RawCatalogClient | None = None
    page_results: list[_PageResult] = []
    page_capture_calls_started = 0
    auth: _Authorization | None = None
    budget: RequestBudget | None = None
    try:
        try:
            auth = _consume_authorization(
                authorization_file, state_directory=authorization_state_dir
            )
        except CatalogCaptureError as exc:
            receipt = _failure_receipt(
                page_results,
                failure_code=exc.code,
                authorization_state="rejected",
                page_capture_calls_started=page_capture_calls_started,
            )
            _write_json_exclusive(directory_fd, "receipt.json", receipt)
            return receipt

        reservation = _run_reservation(auth)
        _write_json_exclusive(directory_fd, "run-reservation.json", reservation)
        _require_authorization_live(auth)

        active_credentials = credentials or _load_project_credentials()
        _require_fixed_credentials(active_credentials)
        active_client = _make_client(active_credentials, client_factory)
        budget = _new_request_budget(auth)
        artifact_writer = RawResponseArtifactWriter(
            evidence_dir / "raw", max_response_bytes=MAX_RESPONSE_BYTES
        )
        started = time.monotonic()
        deadline = started + MAX_TASK_TIMEOUT_SECONDS

        with request_execution_scope(
            source=SOURCE,
            canonical_model="instrument",
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=deadline,
            inherited_scopes=(),
        ):
            for page_index in range(MAX_PAGES):
                offset = page_index * PAGE_LIMIT
                request_params = build_tickers_list_request(
                    limit=PAGE_LIMIT,
                    offset=offset,
                    asset_type="a-share",
                )
                request_summary = _request_summary(offset, request_params)
                request_digest = request_summary_sha256(
                    request_params,
                    allowed_fields=("asset_type", "limit", "offset"),
                )
                page_sequence = page_index + 1
                _write_json_exclusive(
                    directory_fd,
                    f"page-{page_sequence:02d}-reservation.json",
                    _page_reservation(page_sequence, offset, request_digest),
                )
                _require_authorization_live(auth)
                page_capture_calls_started += 1
                page_result = _capture_page(
                    active_client,
                    artifact_writer,
                    credentials=active_credentials,
                    page_sequence=page_sequence,
                    offset=offset,
                    request_params=request_params,
                    request_summary=request_summary,
                    request_digest=request_digest,
                    evidence_fd=directory_fd,
                )
                page_results.append(page_result)
                if sum(page.row_count for page in page_results) > MAX_TASK_ROWS:
                    raise CatalogCaptureError("task_row_cap_exceeded")
                if (
                    sum(len(page.page_input.raw_response_body or b"") for page in page_results)
                    > MAX_TOTAL_RESPONSE_BYTES
                ):
                    raise CatalogCaptureError("task_response_byte_cap_exceeded")
                if not _is_successful_page(page_result.page_input):
                    break
                if page_result.row_count < PAGE_LIMIT:
                    break

        aggregate = _aggregate_pages(page_results)
        if all(_is_successful_page(page.page_input) for page in page_results):
            receipt = _success_receipt(aggregate, page_results)
        else:
            failure_code = next(
                (
                    str(page.receipt["failure_code"])
                    for page in page_results
                    if type(page.receipt.get("failure_code")) is str
                ),
                "page_capture_incomplete",
            )
            receipt = _failure_receipt(
                page_results,
                failure_code=failure_code,
                authorization_state="accepted",
                page_capture_calls_started=page_capture_calls_started,
            )
        _write_json_exclusive(directory_fd, "receipt.json", receipt)
        return receipt
    except CatalogCaptureError as exc:
        receipt = _failure_receipt(
            page_results,
            failure_code=exc.code,
            authorization_state="accepted" if auth is not None else "not_accepted",
            page_capture_calls_started=page_capture_calls_started,
        )
        _write_if_missing(directory_fd, "receipt.json", receipt)
        return receipt
    except Exception:
        receipt = _failure_receipt(
            page_results,
            failure_code="local_capture_operation_failed",
            authorization_state="accepted" if auth is not None else "not_accepted",
            page_capture_calls_started=page_capture_calls_started,
        )
        _write_if_missing(directory_fd, "receipt.json", receipt)
        return receipt
    finally:
        if active_client is not None:
            with suppress(Exception):
                active_client.close()
        os.close(directory_fd)


def _capture_page(
    client: _RawCatalogClient,
    artifact_writer: RawResponseArtifactWriter,
    *,
    credentials: FuyaoCredentials,
    page_sequence: int,
    offset: int,
    request_params: Mapping[str, object],
    request_summary: Mapping[str, object],
    request_digest: str,
    evidence_fd: int,
) -> _PageResult:
    """Send one page, persist a checked raw response, then parse it offline."""
    try:
        response = client.get_raw_exchange(TICKERS_LIST_ENDPOINT, params=request_params)
    except (FuyaoError, RequestBudgetError):
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="request_failed_or_outcome_unknown",
        )
    except Exception:
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="request_failed_or_outcome_unknown",
        )

    if type(response) is not FuyaoRawExchangeResponse:
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="response_contract_invalid",
        )
    body = response.body
    status_code = response.status_code
    if (
        type(body) is not bytes
        or len(body) > MAX_RESPONSE_BYTES
        or type(status_code) is not int
        or not 100 <= status_code <= 599
    ):
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="response_contract_invalid",
            status_code=status_code if type(status_code) is int else None,
        )

    try:
        reflected = _contains_credential_reflection(body, credentials.api_key)
    except Exception:
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="credential_reflection_check_failed",
            status_code=status_code,
            response_suppressed=True,
        )
    if reflected:
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="credential_reflection_detected",
            status_code=status_code,
            response_suppressed=True,
        )

    try:
        captured = artifact_writer.capture(
            (body,),
            source=RawResponseSource(
                provider_id="fuyao",
                dataset_id="instrument_catalog",
                operation_id="list",
            ),
            request_summary_sha256=request_digest,
            http_status=status_code,
            retention_class="restricted",
            source_history_limit="unknown",
        )
        response_digest = captured.manifest.response_sha256
        manifest_digest = hashlib.sha256(captured.manifest_path.read_bytes()).hexdigest()
    except Exception:
        return _record_unknown_page(
            page_sequence,
            offset,
            request_digest,
            evidence_fd,
            failure_code="raw_evidence_persistence_failed",
            status_code=status_code,
            response_suppressed=False,
            persistence_state="unknown_after_writer_failure",
        )

    page_status = "failed"
    failure_code = "http_status_not_200"
    row_count = 0
    candidate: FuyaoInstrumentPageInput
    if status_code == 200:
        candidate = _page_input(
            page_sequence,
            offset,
            status_code,
            body,
            response_digest,
            request_summary,
            succeeded=True,
        )
        try:
            parsed = build_fuyao_instrument_census(
                (candidate,), observed_at=datetime.now(timezone.utc)
            )
            row_count = len(parsed.source_census.pages[0].rows)
            if row_count > PAGE_LIMIT:
                raise FuyaoInstrumentCensusError("page_row_limit_exceeded")
            page_status = "succeeded"
            failure_code = ""
        except FuyaoInstrumentCensusError:
            candidate = _page_input(
                page_sequence,
                offset,
                status_code,
                body,
                response_digest,
                request_summary,
                succeeded=False,
                failure_code="offline_parser_rejected",
            )
            page_status = "failed"
            failure_code = "offline_parser_rejected"
            row_count = 0
        except Exception:
            candidate = _page_input(
                page_sequence,
                offset,
                status_code,
                body,
                response_digest,
                request_summary,
                succeeded=False,
                failure_code="offline_parser_rejected",
            )
            page_status = "failed"
            failure_code = "offline_parser_rejected"
            row_count = 0
    else:
        candidate = _page_input(
            page_sequence,
            offset,
            status_code,
            body,
            response_digest,
            request_summary,
            succeeded=False,
            failure_code="http_status_not_200",
        )

    receipt: dict[str, object] = {
        "page_sequence": page_sequence,
        "offset": offset,
        "attempt_outcome": page_status,
        "failure_code": failure_code or None,
        "http_status": status_code,
        "response_bytes": len(body),
        "response_sha256": response_digest,
        "raw_manifest_sha256": manifest_digest,
        "raw_response_persisted": True,
        "parsed_row_count": row_count,
        "credential_reflection": "not_detected_in_entity_body",
        "source_authenticity": "not_established_by_local_hash",
        "page_completeness": "not_established",
    }
    safe_receipt = _sanitize_page_receipt(receipt)
    _write_json_exclusive(evidence_fd, f"page-{page_sequence:02d}-receipt.json", safe_receipt)
    return _PageResult(page_input=candidate, receipt=safe_receipt, row_count=row_count)


def _record_unknown_page(
    page_sequence: int,
    offset: int,
    request_digest: str,
    evidence_fd: int,
    *,
    failure_code: str,
    status_code: int | None = None,
    response_suppressed: bool = False,
    persistence_state: str = "not_persisted",
) -> _PageResult:
    """Persist a value-free receipt when transport or validation is inconclusive."""
    safe_status_code = (
        status_code if type(status_code) is int and 100 <= status_code <= 599 else None
    )
    page_input = FuyaoInstrumentPageInput(
        sequence_number=page_sequence,
        raw_response_body=None,
        declared_response_sha256=None,
        http_status_code=safe_status_code,
        attempt_status="unknown",
    )
    receipt: dict[str, object] = {
        "page_sequence": page_sequence,
        "offset": offset,
        "attempt_outcome": "unknown",
        "failure_code": failure_code,
        "response_bytes": None,
        "response_sha256": None,
        "raw_manifest_sha256": None,
        "raw_response_persisted": False,
        "raw_persistence_state": persistence_state,
        "response_suppressed": response_suppressed,
        "parsed_row_count": 0,
        "request_summary_sha256": request_digest,
        "source_authenticity": "not_established",
        "page_completeness": "not_established",
    }
    if safe_status_code is not None:
        receipt["http_status"] = safe_status_code
    safe_receipt = _sanitize_page_receipt(receipt)
    _write_json_exclusive(evidence_fd, f"page-{page_sequence:02d}-receipt.json", safe_receipt)
    return _PageResult(page_input=page_input, receipt=safe_receipt, row_count=0)


def _page_input(
    sequence: int,
    offset: int,
    status_code: int,
    body: bytes,
    response_digest: str,
    request_summary: Mapping[str, object],
    *,
    succeeded: bool,
    failure_code: str | None = None,
) -> FuyaoInstrumentPageInput:
    """Construct one page declaration for the offline census adapter."""
    return FuyaoInstrumentPageInput(
        sequence_number=sequence,
        raw_response_body=body,
        declared_response_sha256=response_digest,
        request_summary=request_summary,
        source_page_number=offset,
        http_status_code=status_code,
        attempt_status="succeeded" if succeeded else "failed",
        failure_code=failure_code,
    )


def _aggregate_pages(pages: Sequence[_PageResult]) -> FuyaoInstrumentCensusCapture:
    """Reparse the bounded page set and return the conservative census DTO."""
    return build_fuyao_instrument_census(
        (page.page_input for page in pages), observed_at=datetime.now(timezone.utc)
    )


def _success_receipt(
    aggregate: FuyaoInstrumentCensusCapture,
    pages: Sequence[_PageResult],
) -> dict[str, object]:
    """Return an anonymous summary that cannot promote a short capture to complete."""
    parsed_rows = sum(page.row_count for page in pages)
    return {
        "schema_version": 1,
        "outcome": "bounded_current_snapshot_sample_captured",
        "source": "fuyao.instrument",
        "endpoint_path": TICKERS_LIST_ENDPOINT,
        "declared_asset_scope": "a-share",
        "page_limit": PAGE_LIMIT,
        "max_pages": MAX_PAGES,
        "pages_attempted": len(pages),
        "pages_succeeded": sum(_is_successful_page(page.page_input) for page in pages),
        "pages_with_receipts": len(pages),
        "row_count": parsed_rows,
        "response_bytes_total": sum(
            len(page.page_input.raw_response_body or b"") for page in pages
        ),
        "response_sha256s": [
            page.receipt.get("response_sha256")
            for page in pages
            if _is_sha256_digest(page.receipt.get("response_sha256"))
        ],
        "raw_manifest_sha256s": [
            page.receipt.get("raw_manifest_sha256")
            for page in pages
            if _is_sha256_digest(page.receipt.get("raw_manifest_sha256"))
        ],
        "pagination_status": aggregate.pagination.chain_status.value,
        "pagination_reasons": [reason.value for reason in aggregate.pagination.reasons],
        "market_completeness": "not_established",
        "historical_coverage": "not_established",
        "retired_a_shares": "not_included_by_current_listing_source_contract",
        "source_authenticity": "not_established_by_local_hash",
        "raw_artifacts": "private_content_addressed_append_only",
        "row_values_in_receipt": False,
    }


def _failure_receipt(
    pages: Sequence[_PageResult],
    *,
    failure_code: str,
    authorization_state: str,
    page_capture_calls_started: int | None = None,
) -> dict[str, object]:
    """Build a sanitized partial-run summary while retaining page evidence."""
    started = len(pages) if page_capture_calls_started is None else page_capture_calls_started
    unreceipted = max(0, started - len(pages))
    return {
        "schema_version": 1,
        "outcome": "partial_or_failed_capture",
        "source": "fuyao.instrument",
        "endpoint_path": TICKERS_LIST_ENDPOINT,
        "declared_asset_scope": "a-share",
        "pages_attempted": started,
        "pages_with_receipts": len(pages),
        "unreceipted_page_capture_calls": unreceipted,
        "unreceipted_attempt_outcome": "unknown" if unreceipted else "not_applicable",
        "pages_succeeded": sum(_is_successful_page(page.page_input) for page in pages),
        "row_count": sum(page.row_count for page in pages),
        "failure_code": _safe_failure_code(failure_code),
        "authorization_state": (
            authorization_state
            if type(authorization_state) is str
            and authorization_state in {"accepted", "rejected", "not_accepted"}
            else "not_accepted"
        ),
        "market_completeness": "not_established",
        "historical_coverage": "not_established",
        "pagination_status": "unknown",
        "page_receipts": [_sanitize_page_receipt(page.receipt) for page in pages],
        "row_values_in_receipt": False,
    }


def _safe_failure_code(value: object) -> str:
    """Return a stable failure code without reflecting arbitrary exception text."""
    if type(value) is str and value in _FAILURE_CODES:
        return value
    return "local_capture_operation_failed"


def _is_sha256_digest(value: object) -> bool:
    """Accept only canonical lowercase SHA-256 text for anonymous summaries."""
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _sanitize_page_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """Project page metadata through a fixed field, type, and value allowlist."""
    if type(receipt) is not dict:
        return {}
    safe: dict[str, object] = {}
    for field in _PAGE_RECEIPT_FIELDS:
        if field not in receipt:
            continue
        value = receipt.get(field)
        if field == "page_sequence":
            valid = type(value) is int and 1 <= value <= MAX_PAGES
        elif field == "offset":
            valid = type(value) is int and value in {0, PAGE_LIMIT}
        elif field == "attempt_outcome":
            valid = type(value) is str and value in {"failed", "succeeded", "unknown"}
        elif field == "failure_code":
            valid = value is None or (type(value) is str and value in _FAILURE_CODES)
        elif field == "http_status":
            valid = value is None or (type(value) is int and 100 <= value <= 599)
        elif field == "response_bytes":
            valid = value is None or (type(value) is int and 0 <= value <= MAX_RESPONSE_BYTES)
        elif field in {"response_sha256", "raw_manifest_sha256", "request_summary_sha256"}:
            valid = value is None or _is_sha256_digest(value)
        elif field in {"raw_response_persisted", "response_suppressed"}:
            valid = type(value) is bool
        elif field == "raw_persistence_state":
            valid = type(value) is str and value in {
                "not_persisted",
                "unknown_after_writer_failure",
            }
        elif field == "parsed_row_count":
            valid = type(value) is int and 0 <= value <= PAGE_LIMIT
        elif field == "credential_reflection":
            valid = type(value) is str and value == "not_detected_in_entity_body"
        elif field == "source_authenticity":
            valid = type(value) is str and value in {
                "not_established",
                "not_established_by_local_hash",
            }
        elif field == "page_completeness":
            valid = type(value) is str and value == "not_established"
        else:
            valid = False
        if valid:
            safe[field] = value
    return safe


def _run_reservation(auth: _Authorization) -> dict[str, object]:
    """Describe the exact bounded request plan without including credentials."""
    return {
        "schema_version": 1,
        "authorization_state": "accepted",
        "authorization_sha256": auth.digest,
        "authorization_expires_at_utc": _format_utc(auth.expires_at),
        "source": "fuyao.instrument",
        "method": "GET",
        "host": HOST,
        "endpoint_path": TICKERS_LIST_ENDPOINT,
        "asset_type": "a-share",
        "page_offsets": [0, PAGE_LIMIT],
        "page_limit": PAGE_LIMIT,
        "max_pages": MAX_PAGES,
        "max_request_attempts": MAX_REQUESTS,
        "max_response_bytes_per_page": MAX_RESPONSE_BYTES,
        "max_total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
        "max_task_rows": MAX_TASK_ROWS,
        "timeout_seconds_per_request": REQUEST_TIMEOUT_SECONDS,
        "max_task_timeout_seconds": MAX_TASK_TIMEOUT_SECONDS,
        "retry_policy": "none",
        "transport_send_ceiling_per_page": 1,
        "redirects": "disabled",
        "response_cache": "disabled_for_raw_exchange",
        "raw_artifact_root": "raw/",
        "market_completeness": "not_established",
    }


def _page_reservation(sequence: int, offset: int, request_digest: str) -> dict[str, object]:
    """Describe a planned page before its transport call begins."""
    return {
        "page_sequence": sequence,
        "offset": offset,
        "limit": PAGE_LIMIT,
        "asset_type": "a-share",
        "request_summary_sha256": request_digest,
        "transport_send_ceiling": 1,
        "retry_policy": "none",
        "response_body_saved": False,
        "page_completeness": "not_established",
    }


def _request_summary(offset: int, params: Mapping[str, object]) -> dict[str, object]:
    """Describe the intended page request for parser context, not wire proof."""
    return {
        "method": "GET",
        "path": TICKERS_LIST_ENDPOINT,
        "params": dict(params),
        "page_sequence": offset // PAGE_LIMIT + 1,
    }


def _new_request_budget(auth: _Authorization) -> RequestBudget:
    """Create an exact two-attempt grant bounded to the fixed Fuyao origin."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    expires_at = min(_utcnow() + timedelta(minutes=1), auth.expires_at)
    grant = RequestGrant(
        source=SOURCE,
        canonical_model="instrument",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="local-user-admission-sha256:" + auth.digest,
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


def _make_client(
    credentials: FuyaoCredentials,
    factory: Callable[[FuyaoCredentials], _RawCatalogClient] | None,
) -> _RawCatalogClient:
    """Build a single-send transport or validate the offline fake seam."""
    if factory is not None:
        client = factory(credentials)
        if not callable(getattr(client, "get_raw_exchange", None)) or not callable(
            getattr(client, "close", None)
        ):
            raise CatalogCaptureError("client_contract_invalid")
        return client
    return FuyaoHttpClient(
        credentials=credentials,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        max_attempts=1,
        max_response_bytes=MAX_RESPONSE_BYTES,
        rate_limiter=FuyaoRateLimiter(rate_per_second=1.0, burst=1),
        raw_response_cache=None,
    )


def _load_project_credentials() -> FuyaoCredentials:
    """Resolve Fuyao credentials lazily through the existing project provider."""
    try:
        from opendata.data.providers.ths.models._client import credentials as provider_credentials

        resolved = provider_credentials()
    except Exception:
        raise CatalogCaptureError("provider_credentials_unavailable") from None
    if type(resolved) is not FuyaoCredentials:
        raise CatalogCaptureError("provider_credentials_invalid")
    return resolved


def _require_fixed_credentials(credentials: FuyaoCredentials) -> None:
    """Require the canonical Fuyao HTTPS origin before any transport is built."""
    if type(credentials) is not FuyaoCredentials:
        raise CatalogCaptureError("provider_credentials_invalid")
    if credentials.base_url != DEFAULT_FUYAO_API_BASE_URL:
        raise CatalogCaptureError("provider_origin_not_allowed")


def _contains_credential_reflection(body: bytes, credential_key: str) -> bool:
    """Detect reflected credentials before persistence or fail closed if undecodable."""
    if type(credential_key) is not str or not credential_key:
        raise CatalogCaptureError("credential_reflection_check_unavailable")
    if type(body) is not bytes:
        raise CatalogCaptureError("credential_reflection_check_unavailable")
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
    if any(marker and marker in body for marker in markers):
        return True
    text_markers = {marker.decode("utf-8") for marker in markers}

    def contains_marker(value: str) -> bool:
        return any(marker and marker in value for marker in text_markers)

    try:
        candidate = body.decode("utf-8", errors="strict")
        for _ in range(3):
            if contains_marker(candidate):
                return True
            decoded = _decode_json_string_escapes(candidate)
            if contains_marker(decoded):
                return True
            decoded = html.unescape(decoded)
            if contains_marker(decoded):
                return True
            decoded = unquote(decoded, errors="strict")
            if contains_marker(decoded):
                return True
            decoded = unquote_plus(decoded, errors="strict")
            if contains_marker(decoded):
                return True
            if decoded == candidate:
                return False
            candidate = decoded
    except (UnicodeDecodeError, ValueError):
        raise CatalogCaptureError("credential_reflection_check_unavailable") from None
    return contains_marker(candidate)


def _decode_json_string_escapes(value: str) -> str:
    """Decode JSON string escapes conservatively for reflection detection."""
    simple_escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def decode_surrogate_pair(match: re.Match[str]) -> str:
        high = int(match.group(1), 16)
        low = int(match.group(2), 16)
        code_point = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
        return chr(code_point)

    simple = _JSON_SIMPLE_ESCAPE_RE.sub(lambda match: simple_escapes[match.group(1)], value)
    paired = _JSON_SURROGATE_PAIR_RE.sub(decode_surrogate_pair, simple)
    return _JSON_UNICODE_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), paired)


def _consume_authorization(path: Path, *, state_directory: Path | None = None) -> _Authorization:
    """Validate a grant, reserve its nonce durably, and unlink its private file."""
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CatalogCaptureError("absolute_authorization_file_required")
    parent_fd = _open_private_directory_chain(path.parent)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError:
            raise CatalogCaptureError("authorization_file_unavailable") from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CatalogCaptureError("authorization_already_in_use") from None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
        ):
            raise CatalogCaptureError("authorization_file_permissions_invalid")
        raw = _read_bounded(descriptor, _AUTHORIZATION_MAX_BYTES)
        payload = _strict_json_object(raw)
        authorization = _validate_authorization(payload, raw)
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise CatalogCaptureError("authorization_consume_failed") from None
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise CatalogCaptureError("authorization_file_changed")
        state_root = state_directory or _default_authorization_state_directory()
        _reserve_authorization_nonce(authorization, state_root)
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return authorization
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _validate_authorization(payload: Mapping[str, object], raw: bytes) -> _Authorization:
    """Validate every fixed request bound and short-lived nonce in the grant."""
    if set(payload) != _AUTHORIZATION_KEYS:
        raise CatalogCaptureError("authorization_fields_invalid")
    expected: Mapping[str, object] = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "purpose": AUTHORIZATION_PURPOSE,
        "endpoint_path": TICKERS_LIST_ENDPOINT,
        "asset_type": "a-share",
        "page_limit": PAGE_LIMIT,
        "max_pages": MAX_PAGES,
        "max_requests": MAX_REQUESTS,
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "max_total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
        "max_task_rows": MAX_TASK_ROWS,
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_task_timeout_seconds": MAX_TASK_TIMEOUT_SECONDS,
    }
    for key, value in expected.items():
        if type(payload.get(key)) is not type(value) or payload.get(key) != value:
            raise CatalogCaptureError("authorization_scope_mismatch")
    nonce = payload.get("nonce")
    if type(nonce) is not str or _NONCE_RE.fullmatch(nonce) is None:
        raise CatalogCaptureError("authorization_nonce_invalid")
    issued_at = _parse_utc_timestamp(payload.get("issued_at_utc"))
    expires_at = _parse_utc_timestamp(payload.get("expires_at_utc"))
    now = _utcnow()
    if (
        issued_at > now
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > AUTHORIZATION_MAX_AGE
    ):
        raise CatalogCaptureError("authorization_expired_or_not_yet_valid")
    return _Authorization(
        digest=hashlib.sha256(raw).hexdigest(), expires_at=expires_at, nonce=nonce
    )


def _default_authorization_state_directory() -> Path:
    """Return the stable per-user private state root for consumed grant nonces."""
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if not account_home.is_absolute():
            raise ValueError("account home is not absolute")
        return account_home / ".local/state/opendata/fuyao-a-share-catalog/used-nonces"
    except Exception:
        raise CatalogCaptureError("authorization_nonce_state_unavailable") from None


def _reserve_authorization_nonce(authorization: _Authorization, path: Path) -> None:
    """Append one durable nonce marker, rejecting copies of an already-used grant."""
    directory_fd = _open_or_create_private_directory_chain(path)
    nonce_digest = hashlib.sha256(authorization.nonce.encode("ascii")).hexdigest()
    marker_name = f"nonce-{nonce_digest}.json"
    marker = json.dumps(
        {
            "schema_version": 1,
            "authorization_sha256": authorization.digest,
            "expires_at_utc": _format_utc(authorization.expires_at),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                marker_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            raise CatalogCaptureError("authorization_replay_detected") from None
        except OSError:
            raise CatalogCaptureError("authorization_nonce_state_unavailable") from None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise CatalogCaptureError("authorization_nonce_state_unavailable")
        view = memoryview(marker)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CatalogCaptureError("authorization_nonce_state_unavailable")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(directory_fd)
    except OSError:
        raise CatalogCaptureError("authorization_nonce_state_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _strict_json_object(raw: bytes) -> dict[str, object]:
    """Decode one bounded JSON object while rejecting duplicate keys and NaN."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid constant")),
        )
    except Exception:
        raise CatalogCaptureError("authorization_json_invalid") from None
    if type(value) is not dict:
        raise CatalogCaptureError("authorization_root_invalid")
    return value


def _parse_utc_timestamp(value: object) -> datetime:
    """Parse the authorization's canonical UTC timestamp."""
    if type(value) is not str or not value.endswith("Z"):
        raise CatalogCaptureError("authorization_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CatalogCaptureError("authorization_timestamp_invalid") from None
    if _format_utc(parsed) != value:
        raise CatalogCaptureError("authorization_timestamp_invalid")
    return parsed


def _read_bounded(descriptor: int, limit: int) -> bytes:
    """Read no more than the exact file cap plus one sentinel byte."""
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
        raise CatalogCaptureError("authorization_file_too_large")
    return raw


def _open_private_directory_chain(path: Path) -> int:
    """Open a no-follow directory chain and require its final directory be private."""
    if not hasattr(os, "getuid") or not _O_DIRECTORY or not _O_NOFOLLOW:
        raise CatalogCaptureError("secure_local_file_primitives_unavailable")
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise CatalogCaptureError("absolute_private_directory_required")
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
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700
        ):
            raise CatalogCaptureError("private_directory_permissions_required")
        result = descriptor
        descriptor = -1
        return result
    except CatalogCaptureError:
        raise
    except OSError:
        raise CatalogCaptureError("private_directory_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_or_create_private_directory_chain(path: Path) -> int:
    """Create a private state path without following symlinks or changing modes."""
    if not hasattr(os, "getuid") or not _O_DIRECTORY or not _O_NOFOLLOW:
        raise CatalogCaptureError("secure_local_file_primitives_unavailable")
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise CatalogCaptureError("absolute_private_directory_required")
    descriptor = os.open(path.anchor, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                except OSError:
                    raise CatalogCaptureError("authorization_nonce_state_unavailable") from None
                try:
                    next_descriptor = os.open(
                        component,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                        dir_fd=descriptor,
                    )
                except OSError:
                    raise CatalogCaptureError("authorization_nonce_state_unavailable") from None
            except OSError:
                raise CatalogCaptureError("authorization_nonce_state_unavailable") from None
            component_metadata = os.fstat(next_descriptor)
            component_mode = stat.S_IMODE(component_metadata.st_mode)
            is_ancestor = index < len(components) - 1
            # Permit only root-owned sticky shared ancestors such as /tmp;
            # the final state directory is still owner-checked and private.
            is_root_owned_sticky_ancestor = (
                is_ancestor
                and component_metadata.st_uid == 0
                and bool(component_metadata.st_mode & stat.S_ISVTX)
            )
            if (
                not stat.S_ISDIR(component_metadata.st_mode)
                or component_metadata.st_uid not in {0, os.getuid()}
                or (component_mode & 0o022 and not is_root_owned_sticky_ancestor)
            ):
                os.close(next_descriptor)
                raise CatalogCaptureError("authorization_nonce_state_permissions_invalid")
            previous_descriptor = descriptor
            descriptor = next_descriptor
            os.close(previous_descriptor)
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or mode & 0o077
            or mode & 0o700 != 0o700
        ):
            raise CatalogCaptureError("authorization_nonce_state_permissions_invalid")
        result = descriptor
        descriptor = -1
        return result
    except CatalogCaptureError:
        raise
    except OSError:
        raise CatalogCaptureError("authorization_nonce_state_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_private_evidence_directory(path: Path) -> int:
    """Create a fresh 0700 evidence directory without changing ancestor modes."""
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CatalogCaptureError("absolute_evidence_directory_required")
    parent_fd = _open_private_directory_chain(path.parent)
    try:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            raise CatalogCaptureError("evidence_directory_must_be_new") from None
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
            raise CatalogCaptureError("evidence_directory_permissions_invalid")
        return descriptor
    except CatalogCaptureError:
        raise
    except OSError:
        raise CatalogCaptureError("evidence_directory_create_failed") from None
    finally:
        os.close(parent_fd)


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Write one durable, exclusive, value-safe JSON evidence document."""
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise CatalogCaptureError("evidence_file_permissions_invalid")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    else:
        os.close(descriptor)
    os.fsync(directory_fd)


def _write_if_missing(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Append a sanitized receipt only when no earlier final receipt exists."""
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            _write_json_exclusive(directory_fd, name, value)
        except OSError:
            return
        except CatalogCaptureError:
            return


def _is_successful_page(page: FuyaoInstrumentPageInput) -> bool:
    """Return whether the conservative parser accepted this source page."""
    return page.attempt_status == "succeeded"


def _require_live_environment() -> None:
    """Refuse live CLI access outside an explicit local development session."""
    if os.environ.get("APP_ENV") != "development":
        raise CatalogCaptureError("explicit_development_environment_required")
    if os.environ.get("TESTING", "").strip().casefold() in {"1", "true", "yes", "on"}:
        raise CatalogCaptureError("catalog_capture_disabled_in_testing_environment")


def _require_offline_fake_environment() -> None:
    """Allow injected clients only inside an explicitly isolated test process."""
    if os.environ.get("APP_ENV") != "testing":
        raise CatalogCaptureError("offline_injection_requires_testing_environment")
    if os.environ.get("TESTING", "").strip().casefold() not in {"1", "true", "yes", "on"}:
        raise CatalogCaptureError("offline_injection_requires_testing_flag")


def _require_authorization_live(auth: _Authorization) -> None:
    """Stop before a page send if the local admission has expired."""
    if auth.expires_at <= _utcnow():
        raise CatalogCaptureError("authorization_expired_before_request")


def _format_utc(value: datetime) -> str:
    """Render a canonical UTC timestamp with microseconds."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utcnow() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the live CLI and print only an anonymous receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization-file", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = run_catalog_capture(args.authorization_file, args.evidence_dir)
    except CatalogCaptureError as exc:
        print(
            json.dumps(
                {"outcome": "rejected", "code": _safe_failure_code(exc.code)}, sort_keys=True
            )
        )
        return 1
    print(json.dumps(receipt, sort_keys=True, ensure_ascii=False))
    return 0 if receipt.get("outcome") == "bounded_current_snapshot_sample_captured" else 1


if __name__ == "__main__":
    sys.exit(main())
