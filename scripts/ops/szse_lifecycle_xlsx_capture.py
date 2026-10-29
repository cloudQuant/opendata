"""Capture three fixed SZSE lifecycle XLSX selectors as private raw evidence.

The fixed request parameters mirror the currently vendored AkShare
``stock_info.py`` implementation. They are operational observations, not a
stable SZSE contract. Each selector is requested at most once through the
governed bounded HTTP client. Response bytes are content-addressed before the
offline parser is called, so an actual workbook whose headers differ from the
synthetic #113 test contract remains available for safe review.

This command assigns no instrument identity, writes no database or API data,
and schedules no recurring work. Hashes establish local byte integrity only;
source authenticity, source terms, lifecycle semantics, market coverage, and
completeness remain unverified.
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
from collections.abc import Callable as Callable  # noqa: TC003 -- runtime public API annotation
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import requests
from requests.adapters import HTTPAdapter

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
from opendata.data.request_summary import request_summary_sha256
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource
from opendata.pipeline.szse_lifecycle_xlsx_adapter import (
    SzseLifecycleDatasetClass,
    SzseLifecycleXlsxError,
    parse_szse_lifecycle_xlsx,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["run_szse_lifecycle_capture"]

SOURCE = "szse"
CANONICAL_MODEL = "instrument_lifecycle_xlsx"
HOST = "www.szse.cn"
BASE_URL = f"https://{HOST}"
ENDPOINT_PATH = "/api/report/ShowReport"
ENDPOINT_URL = f"{BASE_URL}{ENDPOINT_PATH}"
MAX_REQUESTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = MAX_REQUESTS * MAX_RESPONSE_BYTES
MAX_ROWS_PER_SELECTOR = 50_000
MAX_TASK_ROWS = MAX_REQUESTS * MAX_ROWS_PER_SELECTOR
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_TASK_TIMEOUT_SECONDS = 45.0
_REQUEST_PARAMS: tuple[tuple[SzseLifecycleDatasetClass, str, str], ...] = (
    ("current_a", "1110", "tab1"),
    ("suspended", "1793_ssgs", "tab1"),
    ("terminated", "1793_ssgs", "tab2"),
)
_FIXED_RANDOM = "0.6935816432433362"
_PARAMETER_FIELDS = ("showtype", "catalogid", "tabkey", "random")
_SHA256_LENGTH = 64
_PATH_TYPE = type(Path())
_SAFE_FAILURE_CODES = frozenset(
    {
        "client_contract_invalid",
        "evidence_directory_invalid",
        "evidence_directory_must_be_new",
        "evidence_directory_unavailable",
        "header_contract_mismatch",
        "local_capture_operation_failed",
        "raw_evidence_persistence_failed",
        "response_body_too_large",
        "response_contract_invalid",
        "request_failed_or_outcome_unknown",
        "synthetic_parser_rejected",
        "task_response_byte_cap_exceeded",
        "task_row_cap_exceeded",
    }
)
_SAFE_PARSER_FAILURE_CODES = frozenset(
    {
        "body_type_invalid",
        "body_too_large",
        "cell_limit_exceeded",
        "cell_value_invalid",
        "column_limit_exceeded",
        "dataset_class_invalid",
        "header_contract_mismatch",
        "macro_or_external_content_rejected",
        "package_member_unsupported",
        "package_structure_invalid",
        "relationship_rejected",
        "row_limit_exceeded",
        "sheet_unsupported",
        "workbook_unsupported",
        "xlsx_parse_failed",
        "xml_unsafe_or_invalid",
        "zip_expanded_too_large",
        "zip_invalid",
        "zip_member_count_invalid",
        "zip_member_duplicate",
        "zip_member_encrypted",
        "zip_member_path_invalid",
        "zip_member_too_large",
        "zip_member_unsupported",
        "zip_ratio_too_large",
    }
)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class SzseCaptureError(RuntimeError):
    """A fixed, value-free failure code for local capture operations."""

    def __init__(self, code: str) -> None:
        """Create a sanitized failure without a path or source value."""
        self.code = code if code in _SAFE_FAILURE_CODES else "local_capture_operation_failed"
        super().__init__(self.code)


class _BoundedClient(Protocol):
    """The bounded GET method needed by the capture loop."""

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
        """Send one bounded request through an already-governed client."""


@dataclass(frozen=True, slots=True)
class _Selector:
    """One fixed dataset label and its audited AkShare request parameters."""

    dataset_class: SzseLifecycleDatasetClass
    catalog_id: str
    tab_key: str

    @property
    def request_params(self) -> dict[str, str]:
        """Return a fresh copy of the fixed XLSX query parameters."""
        return {
            "SHOWTYPE": "xlsx",
            "CATALOGID": self.catalog_id,
            "TABKEY": self.tab_key,
            "random": _FIXED_RANDOM,
        }


@dataclass(frozen=True, slots=True)
class _LiveClient:
    """Owned governed client and its Requests session for deterministic cleanup."""

    client: GovernedHttpClient
    session: requests.Session


def run_szse_lifecycle_capture(
    evidence_dir: Path,
    *,
    client_factory: Callable[[], _BoundedClient] | None = None,
) -> dict[str, object]:
    """Capture fixed current, suspended, and terminated workbook responses.

    With no injected client, this function requires an explicit development
    environment and sends at most three fixed HTTPS GETs. The fake-client seam
    is restricted to ``APP_ENV=testing`` and ``TESTING=true``. No environment
    file is loaded. Every accepted response is written to a new private,
    content-addressed evidence directory before parser validation.

    Args:
        evidence_dir: Absolute path to a new, owner-private evidence directory.
        client_factory: Test-only client factory; it must not perform I/O.

    Returns:
        A value-free receipt with fixed selectors, outcomes, sizes, and hashes.

    Raises:
        SzseCaptureError: If the evidence path cannot be safely created.
    """
    if client_factory is None:
        _require_live_environment()
    else:
        _require_offline_fake_environment()
    directory_fd = _create_private_evidence_directory(evidence_dir)
    receipts: list[dict[str, object]] = []
    active_client: _BoundedClient | None = None
    live_client: _LiveClient | None = None
    request_calls_started = 0
    total_response_bytes = 0
    total_rows = 0
    stop_code: str | None = None
    budget: RequestBudget | None = None
    started = time.monotonic()
    deadline = started + MAX_TASK_TIMEOUT_SECONDS

    try:
        budget = _new_request_budget()
        _write_json_exclusive(
            directory_fd,
            "run-reservation.json",
            _run_reservation(),
        )
        artifact_writer = RawResponseArtifactWriter(
            evidence_dir / "raw",
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        if client_factory is None:
            live_client = _new_live_client()
            active_client = live_client.client
        else:
            active_client = client_factory()
            if not callable(getattr(active_client, "get_bounded", None)):
                raise SzseCaptureError("client_contract_invalid")
        if active_client is None:
            raise SzseCaptureError("client_contract_invalid")

        with request_execution_scope(
            source=SOURCE,
            canonical_model=CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=deadline,
            inherited_scopes=(),
        ):
            for sequence, (dataset_class, catalog_id, tab_key) in enumerate(
                _REQUEST_PARAMS,
                start=1,
            ):
                selector = _Selector(dataset_class, catalog_id, tab_key)
                request_digest = _selector_request_digest(selector)
                _write_json_exclusive(
                    directory_fd,
                    f"selector-{sequence:02d}-reservation.json",
                    _selector_reservation(sequence, selector, request_digest),
                )
                if time.monotonic() >= deadline:
                    stop_code = "request_failed_or_outcome_unknown"
                    receipts.append(
                        _failure_selector_receipt(
                            sequence,
                            selector,
                            request_digest,
                            failure_code=stop_code,
                            request_invoked=False,
                        )
                    )
                    _write_json_exclusive(
                        directory_fd,
                        f"selector-{sequence:02d}-receipt.json",
                        receipts[-1],
                    )
                    continue

                request_calls_started += 1
                one_receipt = _capture_selector(
                    active_client,
                    artifact_writer,
                    sequence=sequence,
                    selector=selector,
                    request_digest=request_digest,
                )
                receipts.append(one_receipt)
                _write_json_exclusive(
                    directory_fd,
                    f"selector-{sequence:02d}-receipt.json",
                    one_receipt,
                )
                response_size = one_receipt.get("response_bytes")
                row_count = one_receipt.get("parsed_row_count")
                if type(response_size) is int:
                    total_response_bytes += response_size
                if type(row_count) is int:
                    total_rows += row_count
                if total_response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    stop_code = "task_response_byte_cap_exceeded"
                    break
                if total_rows > MAX_TASK_ROWS:
                    stop_code = "task_row_cap_exceeded"
                    break

        parser_contracts_passed = _all_parser_contracts_passed(receipts)
        outcome = (
            "bounded_raw_capture_completed"
            if parser_contracts_passed
            else ("partial_or_failed_capture")
        )
        if stop_code is not None:
            outcome = "partial_or_failed_capture"
        summary = _summary_receipt(
            receipts,
            outcome=outcome,
            request_calls_started=request_calls_started,
            total_response_bytes=total_response_bytes,
            total_rows=total_rows,
            failure_code=stop_code or _first_failure_code(receipts),
            transport_attempts=_transport_attempts(budget),
            parser_status=(
                "all_synthetic_contracts_passed"
                if parser_contracts_passed
                else "not_all_synthetic_contracts_passed"
            ),
        )
        _write_json_exclusive(directory_fd, "receipt.json", summary)
        return summary
    except SzseCaptureError as exc:
        summary = _summary_receipt(
            receipts,
            outcome="partial_or_failed_capture",
            request_calls_started=request_calls_started,
            total_response_bytes=total_response_bytes,
            total_rows=total_rows,
            failure_code=exc.code,
            transport_attempts=_transport_attempts(budget),
            parser_status="not_all_synthetic_contracts_passed",
        )
        _write_json_if_missing(directory_fd, "receipt.json", summary)
        return summary
    except Exception:
        summary = _summary_receipt(
            receipts,
            outcome="partial_or_failed_capture",
            request_calls_started=request_calls_started,
            total_response_bytes=total_response_bytes,
            total_rows=total_rows,
            failure_code="local_capture_operation_failed",
            transport_attempts=_transport_attempts(budget),
            parser_status="not_all_synthetic_contracts_passed",
        )
        _write_json_if_missing(directory_fd, "receipt.json", summary)
        return summary
    finally:
        if live_client is not None:
            live_client.session.close()
        os.close(directory_fd)


def _capture_selector(
    client: _BoundedClient,
    artifact_writer: RawResponseArtifactWriter,
    *,
    sequence: int,
    selector: _Selector,
    request_digest: str,
) -> dict[str, object]:
    """Fetch one fixed selector, persist bytes, then apply the synthetic parser."""
    try:
        response = client.get_bounded(
            ENDPOINT_URL,
            max_body_bytes=MAX_RESPONSE_BYTES,
            overall_timeout=REQUEST_TIMEOUT_SECONDS,
            params=selector.request_params,
            headers={
                "Accept": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, "
                    "application/octet-stream;q=0.9"
                ),
            },
            timeout=(4.0, 8.0),
            source=SOURCE,
        )
    except Exception:
        return _failure_selector_receipt(
            sequence,
            selector,
            request_digest,
            failure_code="request_failed_or_outcome_unknown",
            request_invoked=True,
        )

    if type(response) is not BoundedHttpResponse:
        return _failure_selector_receipt(
            sequence,
            selector,
            request_digest,
            failure_code="response_contract_invalid",
            request_invoked=True,
            attempt_outcome="failed",
        )
    body = response.body
    status_code = response.status_code
    if (
        type(body) is not bytes
        or len(body) > MAX_RESPONSE_BYTES
        or type(status_code) is not int
        or not 200 <= status_code <= 299
    ):
        return _failure_selector_receipt(
            sequence,
            selector,
            request_digest,
            failure_code=(
                "response_body_too_large"
                if type(body) is bytes and len(body) > MAX_RESPONSE_BYTES
                else "response_contract_invalid"
            ),
            request_invoked=True,
            attempt_outcome="failed",
            http_status=status_code if type(status_code) is int else None,
            response_suppressed=True,
        )

    try:
        captured = artifact_writer.capture(
            (body,),
            source=RawResponseSource(
                provider_id="szse",
                dataset_id="instrument_lifecycle_xlsx",
                operation_id=selector.dataset_class,
            ),
            request_summary_sha256=request_digest,
            http_status=status_code,
            retention_class="restricted",
            source_history_limit="unknown",
        )
        manifest_bytes = captured.manifest_path.read_bytes()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    except Exception:
        return _failure_selector_receipt(
            sequence,
            selector,
            request_digest,
            failure_code="raw_evidence_persistence_failed",
            request_invoked=True,
            attempt_outcome="failed",
            http_status=status_code,
            response_bytes=len(body),
            response_sha256=hashlib.sha256(body).hexdigest(),
            persistence_state="unknown_after_writer_failure",
        )

    base = {
        "sequence": sequence,
        "dataset_class": selector.dataset_class,
        "request_invoked": True,
        "attempt_outcome": "failed",
        "failure_code": None,
        "http_status": status_code,
        "response_bytes": len(body),
        "response_sha256": captured.manifest.response_sha256,
        "raw_manifest_sha256": manifest_digest,
        "raw_response_persisted": True,
        "raw_persistence_state": "content_addressed_private",
        "request_summary_sha256": request_digest,
        "parsed_row_count": 0,
        "header_contract_status": "not_evaluated",
        "source_id_status": "unknown",
        "lifecycle_semantics_status": "unknown",
        "market_scope_status": "unknown",
        "source_completeness_status": "unknown",
        "dataset_class_binding_status": "selector_declared_unverified",
        "source_authenticity": "not_established_by_local_hash",
        "row_values_in_receipt": False,
    }
    try:
        parsed = parse_szse_lifecycle_xlsx(body, dataset_class=selector.dataset_class)
    except SzseLifecycleXlsxError as exc:
        code = exc.code if exc.code in _SAFE_PARSER_FAILURE_CODES else "synthetic_parser_rejected"
        base["failure_code"] = code
        base["header_contract_status"] = (
            "synthetic_contract_mismatch"
            if code == "header_contract_mismatch"
            else "synthetic_contract_rejected_or_unknown"
        )
        return base
    except Exception:
        base["failure_code"] = "synthetic_parser_rejected"
        base["header_contract_status"] = "synthetic_contract_rejected_or_unknown"
        return base

    row_count = len(parsed.rows)
    if row_count > MAX_ROWS_PER_SELECTOR:
        base["failure_code"] = "task_row_cap_exceeded"
        base["header_contract_status"] = "synthetic_contract_rejected_or_unknown"
        return base
    base["attempt_outcome"] = "succeeded"
    base["parsed_row_count"] = row_count
    base["header_contract_status"] = parsed.header_contract_status
    return base


def _failure_selector_receipt(
    sequence: int,
    selector: _Selector,
    request_digest: str,
    *,
    failure_code: str,
    request_invoked: bool,
    http_status: int | None = None,
    response_bytes: int | None = None,
    response_sha256: str | None = None,
    response_suppressed: bool = False,
    persistence_state: str = "not_persisted",
    attempt_outcome: str | None = None,
) -> dict[str, object]:
    """Return an anonymous fixed-field receipt for one incomplete attempt."""
    safe_code = (
        failure_code
        if failure_code in _SAFE_FAILURE_CODES
        else (
            failure_code
            if failure_code in _SAFE_PARSER_FAILURE_CODES
            else "local_capture_operation_failed"
        )
    )
    receipt: dict[str, object] = {
        "sequence": sequence,
        "dataset_class": selector.dataset_class,
        "request_invoked": request_invoked,
        "attempt_outcome": (
            attempt_outcome
            if attempt_outcome in {"failed", "not_attempted", "unknown"}
            else ("unknown" if request_invoked else "not_attempted")
        ),
        "failure_code": safe_code,
        "http_status": http_status
        if type(http_status) is int and 100 <= http_status <= 599
        else None,
        "response_bytes": (
            response_bytes
            if type(response_bytes) is int and 0 <= response_bytes <= MAX_RESPONSE_BYTES
            else None
        ),
        "response_sha256": response_sha256 if _is_sha256(response_sha256) else None,
        "raw_manifest_sha256": None,
        "raw_response_persisted": False,
        "raw_persistence_state": persistence_state,
        "response_suppressed": response_suppressed,
        "request_summary_sha256": request_digest,
        "parsed_row_count": 0,
        "header_contract_status": "not_evaluated",
        "source_id_status": "unknown",
        "lifecycle_semantics_status": "unknown",
        "market_scope_status": "unknown",
        "source_completeness_status": "unknown",
        "dataset_class_binding_status": "selector_declared_unverified",
        "source_authenticity": "not_established",
        "row_values_in_receipt": False,
    }
    return receipt


def _summary_receipt(
    receipts: Sequence[Mapping[str, object]],
    *,
    outcome: str,
    request_calls_started: int,
    total_response_bytes: int,
    total_rows: int,
    failure_code: str | None,
    transport_attempts: int,
    parser_status: str,
) -> dict[str, object]:
    """Project selector outcomes into a value-free top-level receipt."""
    safe_receipts = [_sanitize_selector_receipt(item) for item in receipts]
    safe_failure = (
        failure_code
        if type(failure_code) is str
        and (failure_code in _SAFE_FAILURE_CODES or failure_code in _SAFE_PARSER_FAILURE_CODES)
        else None
    )
    return {
        "schema_version": 1,
        "outcome": outcome
        if outcome in {"bounded_raw_capture_completed", "partial_or_failed_capture"}
        else "partial_or_failed_capture",
        "synthetic_parser_contract_status": parser_status
        if parser_status
        in {
            "all_synthetic_contracts_passed",
            "not_all_synthetic_contracts_passed",
        }
        else "not_all_synthetic_contracts_passed",
        "source": "szse.public",
        "method": "GET",
        "host": HOST,
        "endpoint_path": ENDPOINT_PATH,
        "selectors_expected": MAX_REQUESTS,
        "selectors_attempted": request_calls_started,
        "selectors_with_receipts": len(safe_receipts),
        "transport_attempts": min(max(transport_attempts, 0), MAX_REQUESTS),
        "response_bytes_total": min(max(total_response_bytes, 0), MAX_TOTAL_RESPONSE_BYTES),
        "parsed_row_count_total": min(max(total_rows, 0), MAX_TASK_ROWS),
        "failure_code": safe_failure,
        "retry_policy": "none",
        "transport_send_ceiling_per_selector": 1,
        "redirects": "disabled",
        "raw_artifacts": "private_content_addressed_append_only",
        "source_authorization": "user_authorization_declared; source_terms_unverified",
        "source_authenticity": "not_established_by_local_hash",
        "source_identity": "unknown",
        "lifecycle_semantics": "unknown",
        "market_completeness": "not_established",
        "historical_coverage": "not_established",
        "publication": "not_performed",
        "scheduled_collection": "not_configured",
        "row_values_in_receipt": False,
        "selector_receipts": safe_receipts,
    }


def _sanitize_selector_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """Keep only exact, bounded receipt keys and fixed statuses."""
    if type(receipt) is not dict:
        return {}
    sequence = receipt.get("sequence")
    dataset_class = receipt.get("dataset_class")
    outcome = receipt.get("attempt_outcome")
    failure = receipt.get("failure_code")
    header_status = receipt.get("header_contract_status")
    parsed_count = receipt.get("parsed_row_count")
    result: dict[str, object] = {
        "sequence": sequence if type(sequence) is int and 1 <= sequence <= MAX_REQUESTS else None,
        "dataset_class": dataset_class
        if dataset_class in {item[0] for item in _REQUEST_PARAMS}
        else None,
        "request_invoked": receipt.get("request_invoked") is True,
        "attempt_outcome": outcome
        if outcome in {"failed", "succeeded", "unknown", "not_attempted"}
        else "unknown",
        "failure_code": failure
        if failure is None or failure in _SAFE_FAILURE_CODES | _SAFE_PARSER_FAILURE_CODES
        else "local_capture_operation_failed",
        "http_status": receipt.get("http_status")
        if type(receipt.get("http_status")) is int and 100 <= receipt["http_status"] <= 599
        else None,
        "response_bytes": receipt.get("response_bytes")
        if type(receipt.get("response_bytes")) is int
        and 0 <= receipt["response_bytes"] <= MAX_RESPONSE_BYTES
        else None,
        "response_sha256": receipt.get("response_sha256")
        if _is_sha256(receipt.get("response_sha256"))
        else None,
        "raw_manifest_sha256": receipt.get("raw_manifest_sha256")
        if _is_sha256(receipt.get("raw_manifest_sha256"))
        else None,
        "raw_response_persisted": receipt.get("raw_response_persisted") is True,
        "raw_persistence_state": receipt.get("raw_persistence_state")
        if receipt.get("raw_persistence_state")
        in {"not_persisted", "content_addressed_private", "unknown_after_writer_failure"}
        else "not_persisted",
        "request_summary_sha256": receipt.get("request_summary_sha256")
        if _is_sha256(receipt.get("request_summary_sha256"))
        else None,
        "parsed_row_count": parsed_count
        if type(parsed_count) is int and 0 <= parsed_count <= MAX_ROWS_PER_SELECTOR
        else 0,
        "header_contract_status": header_status
        if header_status
        in {
            "not_evaluated",
            "synthetic_contract_only",
            "synthetic_contract_mismatch",
            "synthetic_contract_rejected_or_unknown",
        }
        else "not_evaluated",
        "source_id_status": "unknown",
        "lifecycle_semantics_status": "unknown",
        "market_scope_status": "unknown",
        "source_completeness_status": "unknown",
        "dataset_class_binding_status": "selector_declared_unverified",
        "source_authenticity": receipt.get("source_authenticity")
        if receipt.get("source_authenticity")
        in {"not_established", "not_established_by_local_hash"}
        else "not_established",
        "row_values_in_receipt": False,
    }
    if receipt.get("response_suppressed") is True:
        result["response_suppressed"] = True
    return result


def _selector_request_digest(selector: _Selector) -> str:
    """Hash only the selector's allowlisted, fixed query parameters."""
    digest = request_summary_sha256(
        {
            "showtype": selector.request_params["SHOWTYPE"],
            "catalogid": selector.request_params["CATALOGID"],
            "tabkey": selector.request_params["TABKEY"],
            "random": selector.request_params["random"],
        },
        allowed_fields=_PARAMETER_FIELDS,
    )
    if type(digest) is not str:
        raise SzseCaptureError("local_capture_operation_failed")
    return digest


def _run_reservation() -> dict[str, object]:
    """Describe fixed scope and ceilings without including data or raw URLs."""
    return {
        "schema_version": 1,
        "source": "szse.public",
        "method": "GET",
        "host": HOST,
        "endpoint_path": ENDPOINT_PATH,
        "selectors": [
            {
                "dataset_class": dataset_class,
                "catalog_id": catalog_id,
                "tab_key": tab_key,
            }
            for dataset_class, catalog_id, tab_key in _REQUEST_PARAMS
        ],
        "max_requests": MAX_REQUESTS,
        "max_response_bytes_per_request": MAX_RESPONSE_BYTES,
        "max_total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
        "max_rows_per_selector": MAX_ROWS_PER_SELECTOR,
        "max_task_rows": MAX_TASK_ROWS,
        "timeout_seconds_per_request": REQUEST_TIMEOUT_SECONDS,
        "max_task_timeout_seconds": MAX_TASK_TIMEOUT_SECONDS,
        "max_transport_attempts_per_selector": 1,
        "max_transport_attempts_task": MAX_REQUESTS,
        "retry_policy": "none",
        "redirects": "disabled",
        "cache": "disabled_for_bounded_get",
        "raw_artifact_root": "raw/",
        "header_contract": "synthetic_fixture_only",
        "source_terms": "unverified",
        "market_completeness": "not_established",
        "publication": "not_performed",
    }


def _selector_reservation(
    sequence: int,
    selector: _Selector,
    request_digest: str,
) -> dict[str, object]:
    """Describe one exact query choice before sending its only request."""
    return {
        "sequence": sequence,
        "dataset_class": selector.dataset_class,
        "catalog_id": selector.catalog_id,
        "tab_key": selector.tab_key,
        "request_summary_sha256": request_digest,
        "transport_send_ceiling": 1,
        "retry_policy": "none",
        "response_body_saved": False,
        "source_semantics": "unverified",
    }


def _new_request_budget() -> RequestBudget:
    """Create a short-lived three-attempt grant pinned to the SZSE host."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="user_authorized_akshare_szse_canary_terms_unverified",
        task_attempts=MAX_REQUESTS,
        source_attempts=MAX_REQUESTS,
        allowed_hosts={HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=MAX_REQUESTS,
        source_attempts=MAX_REQUESTS,
        grants=(grant,),
        profile=profile,
    )


def _transport_attempts(budget: RequestBudget | None) -> int:
    """Return attempts reserved so far, including a pre-client failure safely."""
    return 0 if budget is None else budget.attempts_used


def _new_live_client() -> _LiveClient:
    """Build a one-send, no-cache, no-environment-proxy bounded client."""
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", HTTPAdapter(max_retries=0))
    session.mount("http://", HTTPAdapter(max_retries=0))
    config = HttpClientConfig(
        connect_timeout=4.0,
        read_timeout=8.0,
        max_attempts=1,
        backoff_base=0.0,
        backoff_jitter=0.0,
        rate_limit_per_host=1.0,
        rate_burst=1,
        max_concurrency_per_host=1,
        rate_max_wait=0.0,
        default_headers={
            "User-Agent": "opendata/afi204-szse-lifecycle-capture",
            "Accept-Encoding": "identity",
        },
        proxies=None,
    )
    client = GovernedHttpClient(config, session=session, raw_response_cache=None)
    return _LiveClient(client=client, session=session)


def _require_live_environment() -> None:
    """Require an explicit development runtime and forbid test environments."""
    if (
        os.environ.get("APP_ENV") != "development"
        or os.environ.get("TESTING", "").lower() == "true"
    ):
        raise SzseCaptureError("local_capture_operation_failed")


def _require_offline_fake_environment() -> None:
    """Keep the injected fake seam inside an explicitly isolated test process."""
    if os.environ.get("APP_ENV") != "testing" or os.environ.get("TESTING", "").lower() != "true":
        raise SzseCaptureError("local_capture_operation_failed")


def _create_private_evidence_directory(path: Path) -> int:
    """Create only a new private leaf directory without chmodding ancestors."""
    if (
        type(path) is not _PATH_TYPE
        or not path.is_absolute()
        or ".." in path.parts
        or path.name in {"", ".", ".."}
        or not _O_DIRECTORY
        or not _O_NOFOLLOW
        or not hasattr(os, "getuid")
    ):
        raise SzseCaptureError("evidence_directory_invalid")
    descriptor = os.open(
        path.anchor,
        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
    )
    child_descriptor: int | None = None
    try:
        for component in path.parent.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
        except FileExistsError:
            raise SzseCaptureError("evidence_directory_must_be_new") from None
        child_descriptor = os.open(
            path.name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=descriptor,
        )
        info = os.fstat(child_descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SzseCaptureError("evidence_directory_unavailable")
        os.fchmod(child_descriptor, 0o700)
        os.fsync(child_descriptor)
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(child_descriptor).st_mode) != 0o700:
            raise SzseCaptureError("evidence_directory_unavailable")
        os.close(descriptor)
        descriptor = -1
        result = child_descriptor
        if result is None:
            raise SzseCaptureError("evidence_directory_unavailable")
        child_descriptor = None
        return result
    except SzseCaptureError:
        raise
    except OSError:
        raise SzseCaptureError("evidence_directory_unavailable") from None
    finally:
        if child_descriptor is not None:
            with suppress(OSError):
                os.close(child_descriptor)
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _write_json_exclusive(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Write one bounded, private JSON receipt without overwriting evidence."""
    if name not in {
        "run-reservation.json",
        "receipt.json",
        "selector-01-reservation.json",
        "selector-02-reservation.json",
        "selector-03-reservation.json",
        "selector-01-receipt.json",
        "selector-02-receipt.json",
        "selector-03-receipt.json",
    }:
        raise SzseCaptureError("local_capture_operation_failed")
    _write_json(directory_fd, name, value)


def _write_json_if_missing(directory_fd: int, name: str, value: Mapping[str, object]) -> None:
    """Write a final failure receipt only when no receipt already exists."""
    try:
        _write_json(directory_fd, name, value)
    except FileExistsError:
        return
    except OSError:
        return


def _write_json(
    directory_fd: int,
    name: str,
    value: Mapping[str, object],
) -> None:
    """Atomically publish a small no-clobber JSON receipt with private mode."""
    temporary_name = f".pending-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > 128 * 1024:
            raise SzseCaptureError("local_capture_operation_failed")
        view = memoryview(encoded)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short receipt write")
            view = view[count:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise
    try:
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise


def _all_parser_contracts_passed(receipts: Sequence[Mapping[str, object]]) -> bool:
    """Return true only when all three local synthetic parser contracts passed."""
    return len(receipts) == MAX_REQUESTS and all(
        receipt.get("attempt_outcome") == "succeeded"
        and receipt.get("header_contract_status") == "synthetic_contract_only"
        for receipt in receipts
    )


def _first_failure_code(receipts: Sequence[Mapping[str, object]]) -> str | None:
    """Return the first fixed sanitized failure code, if any."""
    for receipt in receipts:
        failure = receipt.get("failure_code")
        if type(failure) is str and failure in _SAFE_FAILURE_CODES | _SAFE_PARSER_FAILURE_CODES:
            return failure
    return None


def _is_sha256(value: object) -> bool:
    """Accept only lowercase 64-character SHA-256 text."""
    return (
        type(value) is str
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    """Create the explicit live-capture command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--live",
        required=True,
        action="store_true",
        help="explicitly authorize this one bounded public-source capture run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit CLI and print only the anonymous receipt."""
    arguments = _build_cli_parser().parse_args(argv)
    if not arguments.live:
        return 2
    try:
        receipt = run_szse_lifecycle_capture(arguments.evidence_dir)
    except SzseCaptureError as exc:
        print(json.dumps({"outcome": "rejected", "failure_code": exc.code}, sort_keys=True))
        return 2
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt.get("outcome") == "bounded_raw_capture_completed" else 1


if __name__ == "__main__":
    sys.exit(main())
